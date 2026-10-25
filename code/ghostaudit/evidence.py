"""Deterministic evidence readers.

Only participant-visible evidence is read here.  Every parser emits
``Observation`` objects with a canonical resource category, state and evidence
identifier.  Linking to an identity is attempted from explicit user/email/
phone/hash fields; unresolved observations are left unlinked rather than being
guessed.
"""

from __future__ import annotations

import csv
import json
import re
import sqlite3
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Tuple

from . import model
from .identity import IdentityIndex, norm_email, norm_hash, norm_phone


# ---------------------------------------------------------------------------
# Category / state inference
# ---------------------------------------------------------------------------

_PREFIX_CATEGORIES = [
    ("account_", model.ACCOUNT),
    ("search_", model.SEARCH),
    ("cache_", model.CACHE),
    ("support_", model.SUPPORT_MESSAGE),
    ("analytics_", model.ANALYTICS),
    ("export_", model.EXPORT),
    ("marketing_", model.MARKETING),
    ("email_delivery_", model.EMAIL_DELIVERY),
    ("data_flow_", model.FLOW),
    ("backup_", model.BACKUP),
    ("audit_record_", model.SECURITY),
    ("tombstone_", model.TOMBSTONE),
    ("legal_", model.LEGAL),
    ("object_", model.OBJECT),
    ("secondary_", model.SECONDARY),
]


def category_for(resource_id: str, system: str = "", collection: str = "", payload: Optional[Dict[str, Any]] = None) -> str:
    rid = (resource_id or "").lower()
    for prefix, cat in _PREFIX_CATEGORIES:
        if rid.startswith(prefix):
            # Support attachments use a support_ prefix too; collection and
            # payload disambiguate them below.
            if cat == model.SUPPORT_MESSAGE:
                hay = f"{collection} {system} {payload or {}}".lower()
                if "attachment" in hay or "object_key" in hay or "object_storage" in hay:
                    return model.SUPPORT_ATTACHMENT
            return cat
    coll = (collection or "").lower()
    sys = (system or "").lower()
    if "tombstone" in coll or "deleted_customer_reference" in coll:
        return model.TOMBSTONE
    if "search" in coll or "search" in sys:
        return model.SEARCH
    if "analytics" in coll or "analytics" in sys:
        return model.ANALYTICS
    if "attachment" in coll:
        return model.SUPPORT_ATTACHMENT
    if "support" in coll or "support" in sys:
        return model.SUPPORT_MESSAGE
    if "export" in coll or "export" in sys:
        return model.EXPORT
    if "connector" in coll or "integration" in sys:
        return model.FLOW
    if "backup" in coll or "backup" in sys:
        return model.BACKUP
    if "security" in coll or "audit" in coll or "security" in sys:
        return model.SECURITY
    if "legal" in coll or "legal" in sys:
        return model.LEGAL
    if "profile" in coll or "account" in sys or "customer_profile" in coll:
        return model.ACCOUNT
    if "marketing" in coll or "marketing" in sys:
        return model.MARKETING
    if "delivery" in coll or "notification" in sys:
        return model.EMAIL_DELIVERY
    if "cache" in coll or "cache" in sys:
        return model.CACHE
    return model.UNKNOWN


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    if isinstance(value, (int, float)):
        return value != 0
    text = str(value).strip().lower()
    return text in {"1", "true", "yes", "y", "on", "active", "enabled", "found", "identifiable", "present", "hit"}


def _falsey(value: Any) -> bool:
    if isinstance(value, bool):
        return not value
    if value is None:
        return True
    if isinstance(value, (int, float)):
        return value == 0
    text = str(value).strip().lower()
    return text in {"", "0", "false", "no", "n", "off", "disabled", "not_found", "absent", "miss", "none", "null"}


def _has_identifier(payload: Dict[str, Any]) -> bool:
    for key in (
        "customer_reference",
        "user_id",
        "subject_reference",
        "email",
        "phone",
        "email_hash",
        "owner_email",
        "owner_phone",
        "customer_email",
        "requester_email",
        "requester_phone",
        "requester_name",
        "matched_value",
    ):
        val = payload.get(key)
        if val not in (None, "", "null", "None"):
            return True
    if payload.get("direct_identifiers") not in (None, "", 0, "0"):
        return True
    return False


def _has_direct_pii(payload: Dict[str, Any], *, include_reference: bool = False) -> bool:
    """Whether a record exposes actual direct identifiers.

    A stable ``customer_reference``/``subject_reference`` is deliberately not
    PII: it is the permitted tombstone link.  For support messages only
    requester fields and the message body matter.
    """
    keys = [
        "name",
        "full_name",
        "email",
        "phone",
        "email_hash",
        "owner_email",
        "owner_phone",
        "customer_email",
        "requester_email",
        "requester_phone",
        "requester_name",
        "recipient_email",
        "matched_value",
    ]
    if include_reference:
        keys += ["customer_reference", "subject_reference", "user_id"]
    for key in keys:
        val = payload.get(key)
        if isinstance(val, str):
            if val.strip().lower() not in {"", "null", "none", "[redacted]"}:
                return True
        elif val not in (None, ""):
            return True
    excerpt = payload.get("message_excerpt") or payload.get("message_text") or payload.get("body")
    if isinstance(excerpt, str):
        if "[REDACTED]" in excerpt.upper():
            return False
        if re.search(r"[\w.+-]+@[\w.-]+\.\w+", excerpt):
            return True
        if re.search(r"\+?\d[\d ()-]{7,}\d", excerpt):
            return True
    if payload.get("direct_identifiers") not in (None, "", 0, "0"):
        return True
    return False



def infer_state(category: str, payload: Dict[str, Any]) -> str:
    """Infer the canonical state for a resource payload."""
    status = str(payload.get("status_code", "")).strip()
    result = str(payload.get("result", payload.get("status", ""))).strip().lower()
    http = str(payload.get("http_status", "")).strip()

    if category == model.ACCOUNT:
        if payload.get("profile") is None and "profile" in payload:
            # profile_lookups 404 shape
            if status in {"404", "not_found", "absent"} or ("rows" in payload and _falsey(payload.get("rows"))):
                return model.ABSENT
        if status == "404" or http == "404":
            return model.ABSENT
        if result in {"not_found", "absent"}:
            return model.ABSENT
        if "rows" in payload and _falsey(payload.get("rows")):
            return model.ABSENT
        if str(payload.get("account_status", "")).upper() == "DELETED":
            return model.ABSENT
        return model.PRESENT

    if category in {model.SEARCH, model.CACHE, model.EXPORT, model.MARKETING, model.SECONDARY, model.LEGAL}:
        if result in {"not_found", "absent", "miss"}:
            return model.ABSENT
        if payload.get("available") is False or payload.get("found") is False:
            return model.ABSENT
        if payload.get("total") == 0 or payload.get("hits", {}).get("total") == 0:
            return model.ABSENT
        return model.PRESENT

    if category in {model.SUPPORT_MESSAGE, model.EMAIL_DELIVERY, model.TOMBSTONE}:
        if result in {"redacted", "anonymized"}:
            return model.REDACTED
        if payload.get("redacted") is True or payload.get("pii_removed") is True:
            return model.REDACTED
        if category == model.TOMBSTONE:
            return model.PRESENT if _has_direct_pii(payload) else model.REDACTED
        if category == model.EMAIL_DELIVERY:
            return model.REDACTED if not _has_direct_pii(payload) else model.PRESENT
        return model.PRESENT if _has_direct_pii(payload) or result in {"identifiable", "found", "present"} else model.REDACTED

    if category == model.SUPPORT_ATTACHMENT or category == model.OBJECT:
        if result in {"not_found", "absent", "deleted"}:
            return model.ABSENT
        return model.PRESENT

    if category == model.ANALYTICS:
        if result in {"anonymized"}:
            return model.ANONYMIZED
        if payload.get("direct_identifiers") not in (None, ""):
            return model.ANONYMIZED if not _truthy(payload.get("direct_identifiers")) else model.PRESENT
        if result in {"identifiable", "found", "present"}:
            return model.PRESENT
        if not _has_identifier(payload):
            return model.ANONYMIZED
        return model.PRESENT

    if category == model.FLOW:
        if result in {"disabled", "blocked", "inactive"}:
            return model.DISABLED
        if "enabled" in payload:
            return model.ACTIVE if _truthy(payload.get("enabled")) else model.DISABLED
        if result in {"active", "enabled"}:
            return model.ACTIVE
        return model.DISABLED

    if category == model.BACKUP:
        if result in {"blocked", "disabled"} or payload.get("restore_allowed") is False:
            return model.BLOCKED
        if "restore_allowed" in payload:
            return model.ACTIVE if _truthy(payload.get("restore_allowed")) else model.BLOCKED
        if result in {"active", "eligible", "allowed"}:
            return model.ACTIVE
        if payload.get("eligible") is False:
            return model.BLOCKED
        return model.ACTIVE

    if category == model.SECURITY:
        if result in {"not_found", "absent"} or ("rows" in payload and _falsey(payload.get("rows"))):
            return model.ABSENT
        return model.PRESENT

    if category == model.LEGAL:
        if result in {"not_found", "absent"}:
            return model.ABSENT
        return model.PRESENT

    if category == model.UNKNOWN:
        if result in {"not_found", "absent"}:
            return model.ABSENT
        return model.PRESENT

    return model.PRESENT


# ---------------------------------------------------------------------------
# Identity linking
# ---------------------------------------------------------------------------

_LINK_FIELDS = (
    "user_id",
    "customer_reference",
    "subject_reference",
    "subject_ref",
    "subject_selector",
    "owner_user_id",
    "email",
    "customer_email",
    "requester_email",
    "owner_email",
    "recipient_email",
    "subject_email",
    "lookup_key",
    "lookup_value",
    "correlation_key",
    "match_key",
    "subject_key",
    "scan_key",
    "query",
    "email_hash",
    "phone",
    "owner_phone",
    "requester_phone",
    "customer_phone",
    "matched_value",
    "uploaded_by",
    "created_by",
    "submitted_by",
)


def link_payload(payload: Dict[str, Any], identities: IdentityIndex) -> Tuple[Optional[str], str, str]:
    """Return (user_id, link_method, link_value) from explicit fields."""
    # Stable user references always win.
    for key in ("user_id", "customer_reference", "subject_reference", "subject_ref", "subject_selector", "owner_user_id"):
        val = payload.get(key)
        if val:
            user = identities.resolve_text(val)
            if user:
                return user, "explicit_user_reference", str(val)
    for key in _LINK_FIELDS:
        val = payload.get(key)
        if val:
            user = identities.resolve_text(val)
            if user:
                if "@" in str(val):
                    return user, "exact_email", str(val)
                if re.fullmatch(r"[0-9a-fA-F]{64}", str(val).strip()):
                    return user, "exact_email_hash", str(val)
                return user, "exact_phone", str(val)
    # Nested profile/document fields.
    for nested_key in ("profile", "document", "customer", "owner", "requester"):
        nested = payload.get(nested_key)
        if isinstance(nested, dict):
            user, method, value = link_payload(nested, identities)
            if user:
                return user, method, value
    return None, "unlinked", ""


# ---------------------------------------------------------------------------
# SQL reader
# ---------------------------------------------------------------------------

_STATE_COLUMNS = {
    "account_status",
    "restore_allowed",
    "enabled",
    "result",
    "status",
}


def parse_sql(path: Path, evidence_id: str, identities: IdentityIndex) -> List[model.Observation]:
    observations: List[model.Observation] = []
    try:
        sql = path.read_text(encoding="utf-8", errors="replace")
        con = sqlite3.connect(":memory:")
        con.executescript(sql)
    except Exception:
        return observations

    tables = [r[0] for r in con.execute("select name from sqlite_master where type='table'")]
    scope_times: Dict[str, str] = {}
    for table in tables:
        if table == "snapshot_scope":
            cols = [r[1] for r in con.execute(f'PRAGMA table_info("{table}")')]
            for row in con.execute(f'select * from "{table}"'):
                d = dict(zip(cols, row))
                if d.get("origin_system"):
                    scope_times[str(d["origin_system"])] = str(d.get("captured_at") or "")
    for table in tables:
        if table == "snapshot_scope":
            continue
        cols = [r[1] for r in con.execute(f'PRAGMA table_info("{table}")')]
        try:
            rows = con.execute(f'select * from "{table}"').fetchall()
        except Exception:
            continue
        for row in rows:
            d = dict(zip(cols, row))
            record: Dict[str, Any] = {}
            system = str(d.get("origin_system") or "")
            collection = str(d.get("collection_name") or "")
            if d.get("record_json"):
                try:
                    record = json.loads(d["record_json"])
                except Exception:
                    record = {}
                if not isinstance(record, dict):
                    record = {"value": record}
            else:
                record = {k: v for k, v in d.items() if v not in (None, "")}
                # The flat table name is the collection.
                if not collection:
                    collection = table
            rid = str(record.get("resource_id") or d.get("resource_id") or "")
            if not rid:
                continue
            if not system:
                system = _system_for_collection(collection, rid)
            observed_at = str(
                record.get("observed_at")
                or record.get("updated_at")
                or d.get("updated_at")
                or scope_times.get(system)
                or ""
            )
            category = category_for(rid, system, collection, record)
            state = infer_state(category, record)
            user, method, value = link_payload(record, identities)
            observations.append(
                model.Observation(
                    resource_id=rid,
                    system_id=system or _system_for_category(category),
                    category=category,
                    state=state,
                    evidence_id=evidence_id,
                    observed_at=observed_at,
                    user_id=user,
                    link_method=method,
                    link_value=value,
                    payload=record,
                    location=f"{path.name}:{table}",
                )
            )
    con.close()
    return observations


def _system_for_collection(collection: str, rid: str) -> str:
    coll = (collection or "").lower()
    rid = (rid or "").lower()
    if rid.startswith("account_") or "customer_profile" in coll:
        return "account_service"
    if rid.startswith("search_") or "search" in coll:
        return "search_service"
    if rid.startswith("analytics_") or "analytics" in coll:
        return "analytics_platform"
    if rid.startswith("support_") or "support" in coll or "ticket" in coll:
        return "support_platform"
    if rid.startswith("export_") or "export" in coll:
        return "export_service"
    if rid.startswith("data_flow_") or "connector" in coll:
        return "integration_service"
    if rid.startswith("backup_") or "backup" in coll:
        return "backup_service"
    if rid.startswith("audit_record_") or "security" in coll:
        return "security_service"
    if rid.startswith("tombstone_") or "identity" in coll:
        return "identity_service"
    if rid.startswith("legal_") or "legal" in coll:
        return "legal_service"
    if rid.startswith("marketing_") or "marketing" in coll:
        return "marketing_platform"
    if rid.startswith("email_delivery_") or "delivery" in coll:
        return "notification_service"
    if rid.startswith("cache_") or "cache" in coll:
        return "cache_service"
    if rid.startswith("object_"):
        return "object_storage"
    return ""


def _system_for_category(category: str) -> str:
    return {
        model.ACCOUNT: "account_service",
        model.SEARCH: "search_service",
        model.ANALYTICS: "analytics_platform",
        model.SUPPORT_MESSAGE: "support_platform",
        model.SUPPORT_ATTACHMENT: "support_platform",
        model.EXPORT: "export_service",
        model.FLOW: "integration_service",
        model.BACKUP: "backup_service",
        model.SECURITY: "security_service",
        model.TOMBSTONE: "identity_service",
        model.LEGAL: "legal_service",
        model.MARKETING: "marketing_platform",
        model.EMAIL_DELIVERY: "notification_service",
        model.CACHE: "cache_service",
        model.OBJECT: "object_storage",
        model.SECONDARY: "account_service",
    }.get(category, "")


# ---------------------------------------------------------------------------
# JSON reader
# ---------------------------------------------------------------------------

_SERVICE_SYSTEMS = {
    "account_service": "account_service",
    "analytics_platform": "analytics_platform",
    "backup_service": "backup_service",
    "export_service": "export_service",
    "integration_service": "integration_service",
    "search_service": "search_service",
    "security_service": "security_service",
    "support_platform": "support_platform",
    "identity_service": "identity_service",
    "legal_service": "legal_service",
    "marketing_platform": "marketing_platform",
    "cache_service": "cache_service",
    "notification_service": "notification_service",
    "object_storage": "object_storage",
}


def _obs_from_record(
    record: Dict[str, Any],
    *,
    system: str,
    collection: str,
    evidence_id: str,
    captured_at: str,
    identities: IdentityIndex,
    location: str,
) -> Optional[model.Observation]:
    rid = str(record.get("resource_id") or "")
    if not rid:
        return None
    # Retrieve observed_at before nested profile records lose it.
    observed_at = str(
        record.get("observed_at")
        or record.get("updated_at")
        or record.get("captured_at")
        or captured_at
        or ""
    )
    payload = dict(record)
    category = category_for(rid, system, collection, payload)
    # Some analytics-platform records are marketing contact preferences rather
    # than behavioural profiles.  Policy deletes those records outright.
    if category == model.ANALYTICS and (
        "email_opt_in" in payload or "contact_lookup" in str(payload.get("event", ""))
    ):
        category = model.MARKETING
    # Empty profile lookups need to stay linked through their containing profile
    # only when a profile is actually present.
    state = infer_state(category, payload)
    user, method, value = link_payload(payload, identities)
    return model.Observation(
        resource_id=rid,
        system_id=system or _system_for_category(category),
        category=category,
        state=state,
        evidence_id=evidence_id,
        observed_at=observed_at,
        user_id=user,
        link_method=method,
        link_value=value,
        payload=payload,
        location=location,
    )


def parse_json(path: Path, evidence_id: str, captured_at: str, identities: IdentityIndex) -> List[model.Observation]:
    try:
        doc = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return []
    observations: List[model.Observation] = []
    if not isinstance(doc, (dict, list)):
        return observations

    def add(record: Dict[str, Any], system: str, collection: str, loc: str) -> None:
        obs = _obs_from_record(
            record,
            system=system,
            collection=collection,
            evidence_id=evidence_id,
            captured_at=captured_at,
            identities=identities,
            location=loc,
        )
        if obs:
            observations.append(obs)

    def walk_service_export(service: str, body: Any) -> None:
        system = _SERVICE_SYSTEMS.get(service, service)
        if not isinstance(body, dict):
            return
        for key, items in body.items():
            if isinstance(items, list):
                for idx, item in enumerate(items):
                    if not isinstance(item, dict):
                        continue
                    item = dict(item)
                    # Flatten nested profiles/documents for state inference and
                    # linking while preserving the wrapper's observation time.
                    if key == "profile_lookups":
                        profile = item.get("profile")
                        if isinstance(profile, dict):
                            merged = dict(profile)
                            merged.update({k: v for k, v in item.items() if k != "profile"})
                            merged["profile"] = profile
                            item = merged
                        add(item, system, "customer_profiles", f"{path.name}:{service}.{key}[{idx}]")
                    elif key == "index_queries":
                        hits = item.get("hits") or {}
                        docs = hits.get("documents") if isinstance(hits, dict) else None
                        if isinstance(docs, list):
                            for didx, doc in enumerate(docs):
                                if isinstance(doc, dict):
                                    merged = dict(doc)
                                    merged.update(
                                        {
                                            "query": item.get("query"),
                                            "observed_at": item.get("observed_at"),
                                            "total": hits.get("total", len(docs)),
                                        }
                                    )
                                    add(merged, system, "customer_search_documents", f"{path.name}:{service}.{key}[{idx}].hits[{didx}]")
                        else:
                            add(item, system, "customer_search_documents", f"{path.name}:{service}.{key}[{idx}]")
                    elif key == "tickets":
                        add(item, system, "support_conversations", f"{path.name}:{service}.{key}[{idx}]")
                        if item.get("attachment") or item.get("object_key"):
                            att = dict(item)
                            if isinstance(item.get("attachment"), dict):
                                att = dict(item["attachment"])
                                att.update({k: v for k, v in item.items() if k != "attachment"})
                            add(att, system, "ticket_attachments", f"{path.name}:{service}.{key}[{idx}].attachment")
                    elif key == "restore_entries":
                        add(item, system, "backup_restore_entries", f"{path.name}:{service}.{key}[{idx}]")
                    elif key == "connectors":
                        add(item, system, "connector_subscriptions", f"{path.name}:{service}.{key}[{idx}]")
                    elif key == "profiles":
                        add(item, system, "customer_analytics_profiles", f"{path.name}:{service}.{key}[{idx}]")
                    elif key == "exports":
                        add(item, system, "customer_exports", f"{path.name}:{service}.{key}[{idx}]")
                    elif key == "audit_records":
                        add(item, system, "security_audit_events", f"{path.name}:{service}.{key}[{idx}]")
                    else:
                        add(item, system, key, f"{path.name}:{service}.{key}[{idx}]")
            elif isinstance(items, dict):
                walk_service_export(service, items)

    if isinstance(doc, dict) and isinstance(doc.get("service_exports"), dict):
        for service, body in doc["service_exports"].items():
            walk_service_export(str(service), body)
    elif isinstance(doc, dict) and isinstance(doc.get("snapshots"), list):
        for s_idx, snapshot in enumerate(doc["snapshots"]):
            if not isinstance(snapshot, dict):
                continue
            store = str(snapshot.get("data_store") or "")
            collection = str(snapshot.get("collection") or "")
            captured = str(snapshot.get("captured_at") or captured_at)
            for r_idx, record in enumerate(snapshot.get("records") or []):
                if isinstance(record, dict):
                    add(record, store, collection, f"{path.name}:snapshots[{s_idx}].records[{r_idx}]")

    # Also walk top-level arrays/objects that are direct manifests (exports,
    # connectors, ledgers, reports).
    if not observations:
        def walk(node: Any, system: str, collection: str, loc: str) -> None:
            if isinstance(node, dict):
                if node.get("resource_id"):
                    add(node, system, collection, loc)
                for key, val in node.items():
                    if key in {"resource_id", "document", "profile", "metrics", "recent_activity"}:
                        continue
                    if isinstance(val, list):
                        for idx, item in enumerate(val):
                            if isinstance(item, dict):
                                walk(item, system or _SERVICE_SYSTEMS.get(key, ""), key, f"{loc}/{key}[{idx}]")
                    elif isinstance(val, dict):
                        walk(val, system or _SERVICE_SYSTEMS.get(key, ""), key, f"{loc}/{key}")
            elif isinstance(node, list):
                for idx, item in enumerate(node):
                    walk(item, system, collection, f"{loc}[{idx}]")

        walk(doc, "", "", path.name)

    # A manifest may include both service exports and other top-level records;
    # if the structured walk produced nothing, the fallback above handles it.
    if not observations and isinstance(doc, dict):
        # Common manifests put records under keys like "exports", "connectors".
        for key, val in doc.items():
            if isinstance(val, list):
                for idx, item in enumerate(val):
                    if isinstance(item, dict) and item.get("resource_id"):
                        add(item, _SERVICE_SYSTEMS.get(key, ""), key, f"{path.name}:{key}[{idx}]")
    return observations


# ---------------------------------------------------------------------------
# Log reader
# ---------------------------------------------------------------------------

_KV_RE = re.compile(r'([A-Za-z_][A-Za-z0-9_]*)=(?:"([^"]*)"|\'([^\']*)\'|(\S+))')


def _parse_kv(text: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for m in _KV_RE.finditer(text):
        out[m.group(1)] = m.group(2) if m.group(2) is not None else (m.group(3) if m.group(3) is not None else m.group(4))
    return out


def _parse_log_line(line: str) -> Optional[Dict[str, Any]]:
    line = line.strip()
    if not line:
        return None
    ts = ""
    # Pipeline logs use:
    #   timestamp | level | service | trace | event | key="value" ...
    # The generic key/value pass below would otherwise lose the bare event
    # token and therefore every resource in these primary readback exports.
    pipe = re.match(
        r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z)\s*\|\s*([^|]+?)\s*\|\s*([^|]+?)\s*\|\s*([^|]+?)\s*\|\s*([^|]+?)\s*\|\s*(.*)$",
        line,
    )
    if pipe:
        data: Dict[str, Any] = {
            "_ts": pipe.group(1),
            "_raw": pipe.group(6),
            "level": pipe.group(2).strip(),
            "service": pipe.group(3).strip(),
            "trace_id": pipe.group(4).strip(),
            "event": pipe.group(5).strip(),
        }
        data.update(_parse_kv(pipe.group(6)))
        return data
    # Bracketed application traces use:
    #   timestamp [level] [service] [trace] event: key="value" ...
    bracket = re.match(
        r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z)\s+\[([^\]]+)\]\s+\[([^\]]+)\]\s+\[([^\]]+)\]\s+([A-Za-z0-9_]+):\s*(.*)$",
        line,
    )
    if bracket:
        data = {
            "_ts": bracket.group(1),
            "_raw": bracket.group(6),
            "level": bracket.group(2).strip(),
            "service": bracket.group(3).strip(),
            "trace_id": bracket.group(4).strip(),
            "event": bracket.group(5).strip(),
        }
        data.update(_parse_kv(bracket.group(6)))
        return data
    m = re.match(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)\s+(.*)", line)
    if m:
        ts, rest = m.group(1), m.group(2)
    else:
        rest = line
    # Cloud runtime exports wrap the actual event in a quoted ``message`` and
    # escape its inner quotes.  Decode that payload before the generic parser
    # sees a truncated message value.
    escaped_message = re.search(r'message="((?:\\.|[^"\\])*)"', rest)
    if escaped_message:
        nested = escaped_message.group(1).replace('\\"', '"').replace("\\\\", "\\")
        data: Dict[str, Any] = {"_ts": ts, "_raw": rest, "message": nested}
        data.update(_parse_kv(rest[: escaped_message.start()]))
        data.update(_parse_kv(rest[escaped_message.end() :]))
        data.update(_parse_kv(nested))
        if not data.get("service") and data.get("function"):
            data["service"] = str(data["function"])
        if not data.get("trace_id"):
            request_id = str(data.get("requestId") or "")
            if request_id.startswith(("tr-", "rb-")):
                data["trace_id"] = request_id
        return data
    # Syslog priority/header prefix.
    rest = re.sub(r"^<\d+>\d+\s+\S+\s+\S+\s+\S+\s+\S+\s+\S+\s+", "", rest)
    data: Dict[str, Any] = {"_ts": ts, "_raw": rest}
    # JSON log line
    if rest.startswith("{"):
        try:
            obj = json.loads(rest)
            if isinstance(obj, dict):
                data.update({str(k): v for k, v in obj.items()})
                if isinstance(obj.get("message"), str):
                    data.update(_parse_kv(obj["message"]))
                return data
        except Exception:
            pass
    # Key/value line.  The first token may be the service.
    parts = rest.split()
    if parts and parts[0] in _SERVICE_SYSTEMS:
        data.setdefault("service", parts[0])
    # Bracketed [service] and [trace]
    for token in parts:
        if token.startswith("[") and token.endswith("]"):
            content = token[1:-1]
            if content in _SERVICE_SYSTEMS:
                data.setdefault("service", content)
            elif content.startswith("tr-") or content.startswith("rb-"):
                data.setdefault("trace_id", content)
    data.update(_parse_kv(rest))
    # Inline nested message="instance=... event=..."
    msg = data.get("message")
    if isinstance(msg, str):
        data.update(_parse_kv(msg))
    return data


_EVENT_STATE = {
    # event -> (category, state expression handled separately)
    "profile_lookup_completed": model.ACCOUNT,
    "index_query_completed": model.SEARCH,
    "analytics_profile_read": model.ANALYTICS,
    "analytics_profile_read_completed": model.ANALYTICS,
    "contact_lookup_completed": model.MARKETING,
    "analytics_profile_scan": model.ANALYTICS,
    "analytics_identity_scan": model.ANALYTICS,
    "message_pii_scan": model.SUPPORT_MESSAGE,
    "message_read_completed": model.SUPPORT_MESSAGE,
    "support_ticket_read_completed": model.SUPPORT_MESSAGE,
    "attachment_pii_scan": model.SUPPORT_ATTACHMENT,
    "attachment_read_completed": model.SUPPORT_ATTACHMENT,
    "connector_config_read_completed": model.FLOW,
    "connector_config_loaded": model.FLOW,
    "connector_heartbeat": model.FLOW,
    "restore_policy_read_completed": model.BACKUP,
    "restore_candidate_evaluated": model.BACKUP,
    "restore_authorization_completed": model.BACKUP,
    "export_download_check_completed": model.EXPORT,
    "export_read_completed": model.EXPORT,
    "export_metadata_read_completed": model.EXPORT,
    "contact_query_completed": model.SUPPORT_MESSAGE,
    "retained_order_loaded": model.LEGAL,
    "message_read": model.SUPPORT_MESSAGE,
    "audit_record_read_completed": model.SECURITY,
    "tombstone_read_completed": model.TOMBSTONE,
    "record_lookup": None,  # category comes from record_type
}


def _log_event_category(event: str, data: Dict[str, Any]) -> Tuple[Optional[str], str]:
    if event == "record_lookup":
        rt = str(data.get("record_type") or "").upper()
        mapping = {
            "USER_PROFILE": model.ACCOUNT,
            "SEARCH_INDEX_ENTRY": model.SEARCH,
            "ANALYTICS_PROFILE": model.ANALYTICS,
            "SUPPORT_MESSAGE": model.SUPPORT_MESSAGE,
            "SUPPORT_ATTACHMENT": model.SUPPORT_ATTACHMENT,
            "DATA_FLOW": model.FLOW,
            "BACKUP_ARCHIVE": model.BACKUP,
            "SECURITY_AUDIT_LOG": model.SECURITY,
            "USER_EXPORT": model.EXPORT,
            "ACCOUNT_TOMBSTONE": model.TOMBSTONE,
            "MARKETING_PROFILE": model.MARKETING,
            "EMAIL_DELIVERY": model.EMAIL_DELIVERY,
            "CACHE_ENTRY": model.CACHE,
            "LEGAL_HOLD": model.LEGAL,
        }
        return mapping.get(rt), str(data.get("system") or "")
    cat = _EVENT_STATE.get(event)
    return cat, str(data.get("service") or "")


def _log_state(event: str, category: Optional[str], data: Dict[str, Any]) -> str:
    result = str(data.get("result") or data.get("status") or "").lower()
    if event == "record_lookup":
        if result in {"not_found", "absent"}:
            return model.ABSENT
        if result in {"anonymized"}:
            return model.ANONYMIZED
        if result in {"redacted"}:
            return model.REDACTED
        if result in {"active", "enabled", "identifiable", "found", "present"}:
            if category == model.ANALYTICS:
                return model.PRESENT
            if category == model.FLOW:
                return model.ACTIVE if result in {"active", "enabled"} else model.DISABLED
            if category == model.BACKUP:
                return model.ACTIVE if result == "active" else model.BLOCKED
            return model.PRESENT
        if result in {"disabled", "blocked"}:
            return model.DISABLED if category == model.FLOW else model.BLOCKED
        return model.PRESENT
    if event in {"profile_lookup_completed"}:
        if str(data.get("http_status") or "") == "404" or _falsey(data.get("rows")):
            return model.ABSENT
        return model.PRESENT
    if event == "index_query_completed":
        return model.ABSENT if result == "miss" or data.get("total_hits") == "0" else model.PRESENT
    if event in {"analytics_profile_read", "analytics_profile_read_completed", "analytics_profile_scan", "analytics_identity_scan"}:
        if str(data.get("direct_identifiers", "")).strip() == "0":
            return model.ANONYMIZED
        if result in {"anonymized"}:
            return model.ANONYMIZED
        if result in {"identifiable"}:
            return model.PRESENT
        if data.get("email") not in (None, "", "null") or data.get("phone") not in (None, "", "null") or data.get("email_hash") not in (None, "", "null") or data.get("customer_reference") not in (None, "", "null"):
            return model.PRESENT
        # Some aggregate scans report identity_match_count / matched_identifiers.
        if _truthy(data.get("matched_identifiers")) or _truthy(data.get("identity_match_count")):
            return model.PRESENT
        return model.ANONYMIZED
    if event in {"message_pii_scan", "attachment_pii_scan"}:
        return model.PRESENT
    if event in {"message_read", "message_read_completed", "support_ticket_read_completed", "attachment_read_completed"}:
        text = data.get("message_excerpt") or data.get("message_text") or ""
        if "[REDACTED]" in str(text).upper():
            return model.REDACTED
        if result == "redacted" or data.get("redacted") == "true":
            return model.REDACTED
        if data.get("requester_name") or data.get("requester_email") or data.get("requester_phone"):
            return model.PRESENT
        return model.REDACTED
    if event in {"connector_config_read_completed", "connector_config_loaded", "connector_heartbeat"}:
        if "enabled" in data:
            return model.ACTIVE if _truthy(data.get("enabled")) else model.DISABLED
        if result in {"disabled", "inactive", "stopped"}:
            return model.DISABLED
        if result in {"active", "enabled", "healthy", "delivered"}:
            return model.ACTIVE
        return model.DISABLED
    if event in {"restore_policy_read_completed", "restore_candidate_evaluated"}:
        if "restore_allowed" in data:
            return model.ACTIVE if _truthy(data.get("restore_allowed")) else model.BLOCKED
        if "eligible" in data:
            return model.ACTIVE if _truthy(data.get("eligible")) else model.BLOCKED
        if result in {"blocked", "not_eligible", "ineligible"}:
            return model.BLOCKED
        return model.ACTIVE
    if event == "restore_authorization_completed":
        decision = str(data.get("decision") or result).lower()
        if decision in {"deny", "denied", "blocked", "not_eligible", "ineligible"}:
            return model.BLOCKED
        if "restore_allowed" in data:
            return model.ACTIVE if _truthy(data.get("restore_allowed")) else model.BLOCKED
        return model.ACTIVE
    if event in {"export_download_check_completed", "export_read_completed", "export_metadata_read_completed"}:
        if data.get("available") is not None:
            return model.PRESENT if _truthy(data.get("available")) else model.ABSENT
        if result in {"not_found", "absent"}:
            return model.ABSENT
        return model.PRESENT
    if event == "audit_record_read_completed":
        return model.ABSENT if _falsey(data.get("rows")) else model.PRESENT
    if event == "contact_query_completed":
        return model.ABSENT if _falsey(data.get("rows")) else model.PRESENT
    if event == "retained_order_loaded":
        return model.PRESENT
    if event == "tombstone_read_completed":
        if result in {"redacted", "anonymized"}:
            return model.REDACTED
        return model.PRESENT if _has_identifier(data) else model.REDACTED
    return model.PRESENT


def collect_trace_owners(
    path: Path,
    identities: IdentityIndex,
) -> Dict[str, Tuple[str, str, str]]:
    """Collect cross-file trace ownership candidates from one log file.

    A trace is only propagated when *every* identity-bearing row in the file
    (or the row that starts the trace) agrees on the owner; conflicting
    ownership is recorded under an ambiguous marker and never propagates.
    """
    owners: Dict[str, Tuple[str, str, str]] = {}
    conflict: set = set()
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        data = _parse_log_line(line)
        if not data:
            continue
        trace = str(data.get("trace_id") or "").strip()
        if not trace or trace in conflict:
            continue
        user, method, value = link_payload(data, identities)
        if not user:
            continue
        if trace in owners and owners[trace][0] != user:
            conflict.add(trace)
            owners.pop(trace, None)
            continue
        owners.setdefault(trace, (user, method, value))
    return owners


def _canonicalize_systems(observations: List[model.Observation]) -> None:
    """Harmonise system ids whose services are replicas of one platform.

    Support messages and attachments live in the support platform even when
    the log line names a regional replica service; the record's category, not
    the instance name, determines the system id used in findings.
    """
    support = {model.SUPPORT_MESSAGE, model.SUPPORT_ATTACHMENT}
    for obs in observations:
        if obs.category in support:
            obs.system_id = "support_platform"


def parse_log(
    path: Path,
    evidence_id: str,
    captured_at: str,
    identities: IdentityIndex,
    global_trace_owners: Optional[Dict[str, Tuple[str, str, str]]] = None,
) -> List[model.Observation]:
    observations: List[model.Observation] = []
    trace_owner: Dict[str, Tuple[str, str, str]] = {}
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        data = _parse_log_line(line)
        if not data:
            continue
        event = str(data.get("event") or "").strip()
        if not event:
            # Cloud runtime messages contain event inside message; handled above.
            continue
        if event.endswith("_started") or event.startswith("scan_started"):
            # Start records establish a trace but not a resource state.
            if data.get("trace_id"):
                user, method, value = link_payload(data, identities)
                if user:
                    trace_owner[str(data["trace_id"])] = (user, method, value)
            continue
        rid = str(data.get("resource_id") or "").strip()
        category, system = _log_event_category(event, data)
        if category is None and rid:
            category = category_for(rid, system, "", data)
        if category == model.ANALYTICS and (
            "contact_lookup" in event or "email_opt_in" in data
        ):
            category = model.MARKETING
        if not rid or category is None:
            # Still record owner hints for the trace.
            if data.get("trace_id"):
                user, method, value = link_payload(data, identities)
                if user:
                    trace_owner[str(data["trace_id"])] = (user, method, value)
            continue
        if not system:
            system = _system_for_category(category)
        observed_at = str(data.get("_ts") or captured_at or "")
        user, method, value = link_payload(data, identities)
        trace = str(data.get("trace_id") or "")
        if not user and trace:
            if trace in trace_owner:
                user, method, value = trace_owner[trace]
                method = "trace_" + method
            elif global_trace_owners and trace in global_trace_owners:
                user, method, value = global_trace_owners[trace]
                method = "global_" + method
        if user and trace:
            trace_owner.setdefault(trace, (user, method, value))
        state = _log_state(event, category, data)
        observations.append(
            model.Observation(
                resource_id=rid,
                system_id=system,
                category=category,
                state=state,
                evidence_id=evidence_id,
                observed_at=observed_at,
                user_id=user,
                link_method=method,
                link_value=value,
                payload=data,
                location=path.name,
            )
        )
    _canonicalize_systems(observations)
    return observations


# ---------------------------------------------------------------------------
# Text handover reader
# ---------------------------------------------------------------------------

_CATEGORY_NAMES = {
    "USER_PROFILE": model.ACCOUNT,
    "SEARCH_INDEX_ENTRY": model.SEARCH,
    "ANALYTICS_PROFILE": model.ANALYTICS,
    "SUPPORT_MESSAGE": model.SUPPORT_MESSAGE,
    "SUPPORT_ATTACHMENT": model.SUPPORT_ATTACHMENT,
    "BACKUP_ARCHIVE": model.BACKUP,
    "DATA_FLOW": model.FLOW,
    "USER_EXPORT": model.EXPORT,
    "SECURITY_AUDIT_LOG": model.SECURITY,
    "ACCOUNT_TOMBSTONE": model.TOMBSTONE,
    "MARKETING_PROFILE": model.MARKETING,
    "EMAIL_DELIVERY": model.EMAIL_DELIVERY,
    "CACHE_ENTRY": model.CACHE,
    "LEGAL_HOLD": model.LEGAL,
}


def parse_text(path: Path, evidence_id: str, captured_at: str, identities: IdentityIndex) -> List[model.Observation]:
    observations: List[model.Observation] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if "|" not in line:
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) < 6:
            continue
        if parts[0].startswith("-"):
            parts[0] = parts[0].lstrip("- ")
        ts, system, rid, category_name, state_name, subject = parts[:6]
        if not re.match(r"\d{4}-\d{2}-\d{2}T", ts):
            continue
        category = _CATEGORY_NAMES.get(category_name.upper())
        if not category:
            continue
        state = {
            "FOUND": model.PRESENT,
            "PRESENT": model.PRESENT,
            "IDENTIFIABLE": model.PRESENT,
            "NOT_FOUND": model.ABSENT,
            "ABSENT": model.ABSENT,
            "ANONYMIZED": model.ANONYMIZED,
            "REDACTED": model.REDACTED,
            "ACTIVE": model.ACTIVE,
            "ENABLED": model.ACTIVE,
            "DISABLED": model.DISABLED,
            "BLOCKED": model.BLOCKED,
        }.get(state_name.upper(), model.PRESENT)
        user, method, value = link_payload({"subject_reference": subject, "email": subject, "user_id": subject}, identities)
        observations.append(
            model.Observation(
                resource_id=rid,
                system_id=system,
                category=category,
                state=state,
                evidence_id=evidence_id,
                observed_at=ts,
                user_id=user,
                link_method=method,
                link_value=value,
                payload={"subject": subject, "state_name": state_name},
                location=path.name,
            )
        )
    return observations


# ---------------------------------------------------------------------------
# PDF document reader (generic, page-level provenance)
# ---------------------------------------------------------------------------

_RESOURCE_ID_RE = re.compile(
    r"\b(?:account|search|cache|support|analytics|export|marketing|email_delivery"
    r"|data_flow|backup|audit_record|tombstone|legal|object|secondary)_[A-Za-z0-9]+\b"
)
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w.-]+\.\w+")
_PHONE_RE = re.compile(r"\+\d[\d ()-]{8,}\d")
_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}
_DATE_TEXT_RE = re.compile(
    r"(\d{1,2})\s+([A-Za-z]{3})\s+(\d{4})\s+(\d{2}):?(\d{2})\s*(UTC)?", re.I
)
_ABSENT_WORDS = ("not found", "deleted", "removed", "purged", "no longer")
_PRESENT_WORDS = ("found", "present", "active", "ready", "uploaded", "retained", "live")


def parse_pdf(path: Path, evidence_id: str, captured_at: str, identities: IdentityIndex) -> List[model.Observation]:
    """Extract resource facts from a PDF document generically.

    Every observation carries page-level provenance in ``location`` and the
    supporting text quote in ``payload["fact"]``.  Nothing case-specific is
    embedded here: facts are found by scanning for resource identifiers,
    identity fields and state words anywhere in the rendered text.
    """
    from .pdfdocs import extract_pdf_pages

    observations: List[model.Observation] = []
    try:
        pages = extract_pdf_pages(path)
    except Exception:
        pages = []
    doc_text = "\n".join(text for _, text in pages)
    doc_attachment = any(
        word in doc_text.lower()
        for word in ("checksum", "uploader", "attachment preview", "file ")
    )
    page_cands: Dict[int, List[Tuple[str, str, str]]] = {}
    page_rids: Dict[int, List[Tuple[str, str, str]]] = {}

    def _block_links(block: str) -> Tuple[Optional[str], str, str]:
        return link_payload(
            {
                "text": block,
                "email": " ".join(_EMAIL_RE.findall(block)),
                "email_hash": " ".join(re.findall(r"\b[0-9a-fA-F]{64}\b", block)),
                "phone": " ".join(_PHONE_RE.findall(block)),
                "subject_reference": " ".join(re.findall(r"\buser_\d+\b", block)),
            },
            identities,
        )

    # Pass 1: harvest identity candidates per page and resource ids per block.
    blocks_per_page: List[Tuple[int, str, List[str]]] = []
    seen_rids: Set[Tuple[int, str]] = set()
    for locator, text in pages:
        try:
            page_no = int(locator)
        except Exception:
            page_no = 1
        blocks = [b for b in re.split(r"(?=Record\n|Record )", text) if b.strip()] or [text]
        page_blocks: List[str] = []
        for block in blocks:
            user, method, value = _block_links(block)
            if user:
                page_cands.setdefault(page_no, []).append((user, method, value))
            for rid in sorted(set(_RESOURCE_ID_RE.findall(block))):
                key = (page_no, rid)
                if key in seen_rids:
                    continue
                seen_rids.add(key)
                page_blocks.append(block)
                page_rids.setdefault(page_no, []).append((rid, block))
        if page_blocks:
            blocks_per_page.append((page_no, locator, [page_rids[page_no] and b for b in page_blocks] and page_blocks))

    # Pass 2: emit one observation per resource id found on the page.
    for page_no, candidates in sorted(page_rids.items()):
        page_users = page_cands.get(page_no, [])
        for rid, block in candidates:
            category = category_for(rid, "", "")
            lower = (block or "").lower()
            if doc_attachment and category in (model.SUPPORT_MESSAGE, model.UNKNOWN):
                category = model.SUPPORT_ATTACHMENT
            user, method, value = _block_links(block)
            if not user and len({c[0] for c in page_users}) == 1:
                user, method, value = page_users[0]
            if not user:
                continue
            if category == model.LEGAL:
                state = model.PRESENT
            elif any(word in lower for word in _ABSENT_WORDS):
                state = model.ABSENT
            else:
                state = model.PRESENT
            observed_at = ""
            m = _DATE_TEXT_RE.search(block)
            if not m:
                m = _DATE_TEXT_RE.search(doc_text)
            if m:
                day, mon, year, hh, mm = m.groups()
                try:
                    observed_at = (
                        f"{int(year):04d}-{_MONTHS[mon.lower()]:02d}-"
                        f"{int(day):02d}T{hh}:{mm}:00Z"
                    )
                except Exception:
                    observed_at = ""
            observed_at = observed_at or captured_at
            observations.append(
                model.Observation(
                    resource_id=rid,
                    system_id=_system_for_category(category),
                    category=category,
                    state=state,
                    evidence_id=evidence_id,
                    observed_at=observed_at,
                    user_id=user,
                    link_method=method,
                    link_value=value,
                    payload={"fact": " ".join(block.split())[:400]},
                    location=f"{path.name}:object_{locator}",
                )
            )
    # Collapse identical emissions that different content streams produced.
    unique: List[model.Observation] = []
    seen: Set[Tuple] = set()
    for o in sorted(observations, key=lambda o: o.location):
        key = (o.resource_id, o.category, o.state, o.user_id, o.observed_at)
        if key in seen:
            continue
        seen.add(key)
        unique.append(o)
    return unique


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------

def parse_evidence_file(
    path: Path,
    evidence_id: str,
    captured_at: str,
    identities: IdentityIndex,
    global_trace_owners: Optional[Dict[str, Tuple[str, str, str]]] = None,
) -> List[model.Observation]:
    suffix = path.suffix.lower()
    try:
        if suffix == ".sql":
            return parse_sql(path, evidence_id, identities)
        if suffix == ".json":
            return parse_json(path, evidence_id, captured_at, identities)
        if suffix in {".log"}:
            return parse_log(path, evidence_id, captured_at, identities, global_trace_owners)
        if suffix in {".txt"}:
            return parse_text(path, evidence_id, captured_at, identities)
        if suffix in {".pdf"}:
            return parse_pdf(path, evidence_id, captured_at, identities)
    except Exception:
        return []
    return []
