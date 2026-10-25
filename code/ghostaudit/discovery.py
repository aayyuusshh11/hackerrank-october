"""Generic, content-driven evidence discovery.

This module replaces the case-keyed routing tables of the first iteration.
For every case it determines which participant-visible evidence files may be
consulted, using only content signals that travel with the files themselves:

1. **Subject linkage** — a file is relevant when its parsed facts (observations)
   are linked to the case subject through explicit identity fields or through
   trace-ownership propagated across files.
2. **Account-resource linkage** — a file is relevant when it carries facts
   about an account resource that belongs to the subject.
3. **Screen/document transcriptions** — image/PDF facts recorded with
   provenance in ``code/data/visual_observations.csv`` are linked through the
   subject references they record, never through case ids.
4. **Batch state exports** — the platform writes one or more bulk state
   exports around each deletion workflow batch.  Those are identified from
   the manifest metadata (``origin``/file path vocabulary) rather than from a
   hard-coded list, and selected per batch date with a deterministic family
   preference.

All selections honour the audit-time admissibility contract: a file is only
admissible for state reconstruction when it was captured at or before the
case's ``audit_at`` (non-bulk artifacts get a small tolerance because consoles
and documents are exported shortly before an audit closes out), and never
more than 45 days before it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

from . import model
from .accounts import CaseInfo
from .identity import IdentityIndex

_ARTIFACT_WINDOW = timedelta(days=45)
_ARTIFACT_FUTURE_TOLERANCE = timedelta(hours=2)

# Vocabulary that identifies a bulk state export in manifest metadata.  The
# list is derived from the way the platform names families of exports
# (replica captures, snapshot bundles, inventory captures, readback logs,
# observability exports...); it contains no case or date specifics.
_BULK_KEYWORDS = (
    "export",
    "replica",
    "snapshot",
    "inventory",
    "readback",
    "observability",
    "handover",
    "ledger",
    "diagnostic",
    "connector",
    "delivery",
    "audit_service",
)

# Deterministic preference between equally dated bulk exports.  The canonical
# resource inventory of the batch (service resource export) is the most
# authoritative readback; assurance snapshots are last-resort documentation.
_BULK_FAMILY_RANK = (
    ("service_resource_export", 0),
    ("inventory", 1),
    ("readback", 2),
    ("replica", 3),
    ("observability", 4),
    ("snapshot", 5),
)

_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg"}


def _ts(value: str) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except Exception:
        return None


def _bulk_rank(origin: str, rel_path: str) -> Optional[int]:
    text = f"{origin} {rel_path}".lower()
    if not any(keyword in text for keyword in _BULK_KEYWORDS):
        return None
    if any(text.endswith(suffix) for suffix in _IMAGE_SUFFIXES):
        return None
    for keyword, rank in _BULK_FAMILY_RANK:
        if keyword in text:
            return rank
    return 6


@dataclass
class CaseTiming:
    audit_at: datetime
    reported_date: str
    audit_date: str


class EvidenceDiscovery:
    def __init__(
        self,
        manifest: Dict[str, object],
        observations: Sequence[model.Observation],
        visual,
        identities: IdentityIndex,
        cases: Sequence[CaseInfo],
        account_mapper,
    ):
        self.manifest = manifest
        self.visual = visual
        self.identities = identities
        self.account_mapper = account_mapper

        self.obs_by_user: Dict[str, Set[str]] = {}
        self.obs_by_resource: Dict[str, Set[str]] = {}
        for obs in observations:
            if obs.user_id:
                self.obs_by_user.setdefault(obs.user_id, set()).add(obs.evidence_id)
            if obs.resource_id:
                self.obs_by_resource.setdefault(obs.resource_id, set()).add(obs.evidence_id)

        self.timing: Dict[str, CaseTiming] = {}
        for case in cases:
            audit = _ts(case.audit_at)
            if not audit:
                continue
            self.timing[case.case_id] = CaseTiming(
                audit_at=audit,
                reported_date=case.reported_at[:10],
                audit_date=case.audit_at[:10],
            )

        self.account_ids: Dict[str, Set[str]] = {}
        for obs in observations:
            if obs.category == model.ACCOUNT and obs.user_id and obs.resource_id:
                self.account_ids.setdefault(obs.user_id, set()).add(obs.resource_id)
        for event in getattr(account_mapper, "events", []):
            for case in cases:
                if case.user_id and event.resource_id and event.resource_id.startswith("account_"):
                    self.account_ids.setdefault(case.user_id, set()).add(event.resource_id)

        # Bulk exports grouped by capture date.
        self.bulk_by_date: Dict[str, List[Tuple[int, str]]] = {}
        for fid, entry in sorted(manifest.items()):
            rank = _bulk_rank(entry.origin, entry.rel_path)
            if rank is None:
                continue
            cap = _ts(entry.captured_at)
            if not cap:
                continue
            self.bulk_by_date.setdefault(entry.captured_at[:10], []).append((rank, fid))

    # -- window helpers ----------------------------------------------------

    def _artifact_ok(self, fid: str, case: CaseInfo) -> bool:
        entry = self.manifest.get(fid)
        audit = _ts(case.audit_at) if case is not None else None
        if entry is None or audit is None:
            return False
        cap = _ts(entry.captured_at)
        if cap is None:
            return False
        lower = audit - _ARTIFACT_WINDOW
        upper = audit + _ARTIFACT_FUTURE_TOLERANCE
        return lower <= cap <= upper

    def _bulk_for_dates(self, case: CaseInfo) -> List[str]:
        audit = _ts(case.audit_at) if case is not None else None
        chosen: Set[str] = set()
        if audit is None:
            return []
        for date in {case.reported_at[:10], case.audit_at[:10]}:
            candidates = [
                (rank, fid)
                for rank, fid in self.bulk_by_date.get(date, [])
                if _ts(self.manifest[fid].captured_at) <= audit
            ]
            if not candidates:
                continue
            candidates.sort(key=lambda pair: (pair[0], pair[1]))
            chosen.add(candidates[0][1])
        return sorted(chosen)

    # -- main entry point --------------------------------------------------

    def files_for_case(self, case: CaseInfo) -> List[str]:
        files: Set[str] = set()

        for fid in self.obs_by_user.get(case.user_id, set()):
            if self._artifact_ok(fid, case):
                files.add(fid)

        for rid in self.account_ids.get(case.user_id, set()):
            for fid in self.obs_by_resource.get(rid, set()):
                if self._artifact_ok(fid, case):
                    files.add(fid)

        if self.visual is not None:
            for fid in self.visual.evidence_ids_for(case.user_id):
                if self._artifact_ok(fid, case):
                    files.add(fid)

        files.update(self._bulk_for_dates(case))
        return sorted(files)

    def pairing_files_for_case(self, case: CaseInfo) -> Set[str]:
        """Files whose records may be attributed to a case by pairing.

        An unlinked resource (an anonymised profile, for example) may only be
        attributed to a case from a state document that describes the case's
        batch at or directly before the audit: either a bulk batch export or a
        file captured on the audit date.  Older copies of the same resource in
        unrelated snapshots are stale and must not drive attribution.
        """
        out = set(self._bulk_for_dates(case))
        for fid, entry in self.manifest.items():
            if entry.captured_at[:10] == case.audit_at[:10] and self._artifact_ok(fid, case):
                out.add(fid)
        return out
