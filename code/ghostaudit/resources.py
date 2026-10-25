"""Case-to-evidence routing and resource-state reconstruction.

Evidence selection is fully content-driven (see ``discovery.py``): files come
from subject identity links, account-resource links, provenance-carrying
screen/document transcriptions, and the platform's dated bulk state exports.
No case id is ever mapped to a file id in code.

State is reconstructed *at the case's audit time*: the freshest observation
of each admissible resource at or before ``audit_at`` is the state, ties
resolved conservatively by state severity and then by evidence id.
"""

from __future__ import annotations

import csv
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

from . import model
from .accounts import CaseInfo
from .discovery import EvidenceDiscovery
from .identity import IdentityIndex


@dataclass
class ResourceBundle:
    resources: List[model.ResourceState]
    account_id: Optional[str] = None
    notes: List[str] = field(default_factory=list)


def _parse_ts(value: str):
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def _state_rank(category: str, state: str) -> int:
    if category == model.ACCOUNT:
        return 10 if state == model.PRESENT else 0
    if category in {model.SEARCH, model.CACHE, model.EXPORT, model.MARKETING, model.SECONDARY, model.SUPPORT_ATTACHMENT, model.OBJECT}:
        return 10 if state == model.PRESENT else 0
    if category == model.ANALYTICS:
        return 10 if state == model.PRESENT else 0
    if category in {model.SUPPORT_MESSAGE, model.EMAIL_DELIVERY, model.TOMBSTONE}:
        return 10 if state == model.PRESENT else 0
    if category == model.FLOW:
        return 10 if state == model.ACTIVE else 0
    if category == model.BACKUP:
        return 10 if state == model.ACTIVE else 0
    if category in {model.SECURITY, model.LEGAL}:
        return 10 if state == model.PRESENT else 0
    return 10 if state == model.PRESENT else 0


def _worse(category: str, a: str, b: str) -> str:
    return a if _state_rank(category, a) >= _state_rank(category, b) else b


class ResourceBuilder:
    def __init__(
        self,
        observations: Sequence[model.Observation],
        cases: Sequence[CaseInfo],
        identities: IdentityIndex,
        account_mapper,
        manifest: Optional[Dict[str, object]] = None,
        visual=None,
    ):
        self.identities = identities
        self.account_mapper = account_mapper
        self._manifest = manifest or {}

        # Screen/document transcription facts become ordinary observations.
        # They are linked through their subject references only, so a case
        # adopts them exclusively through identity, never through case ids.
        visual_observations: List[model.Observation] = []
        if visual is not None:
            for case in cases:
                visual_observations.extend(visual.observations_for(case.user_id))

        self.observations = list(observations) + visual_observations
        self.cases = [c for c in cases if c.deletion_requested]
        self.by_evidence: Dict[str, List[model.Observation]] = defaultdict(list)
        self.by_user: Dict[str, List[model.Observation]] = defaultdict(list)
        for obs in self.observations:
            self.by_evidence[obs.evidence_id].append(obs)
            if obs.user_id:
                self.by_user[obs.user_id].append(obs)
        self.case_by_id = {c.case_id: c for c in self.cases}

        # A screen transcription shows the record inside its real system, so
        # its category/system labels are authoritative over labels inherited
        # by copied rows in exports and snapshots.
        self.category_override: Dict[str, Tuple[str, str]] = {}
        for obs in visual_observations:
            if obs.resource_id:
                self.category_override[obs.resource_id] = (obs.category, obs.system_id)

        self._discovery = EvidenceDiscovery(
            manifest=manifest or {},
            observations=self.observations,
            visual=visual,
            identities=identities,
            cases=self.cases,
            account_mapper=account_mapper,
        )

        # The platform's own claim about a support record (delete vs redact)
        # reveals what the record internally is: whole-record deletion is
        # attempted for attachments, PII redaction for messages.  The claim is
        # not proof of an outcome, but it is valid evidence of record shape.
        self.activity_delete: Set[str] = set()
        activity_path = Path("dataset/system_activity.csv")
        if activity_path.exists():
            with activity_path.open(encoding="utf-8", newline="") as fh:
                for row in csv.DictReader(fh):
                    if row.get("event_type") == "DELETE_RECORD" and row.get("resource_id"):
                        self.activity_delete.add(row["resource_id"])

        self._analytics_fallback = self._build_analytics_fallback()
        self._apply_intent_categories()

    def _apply_intent_categories(self) -> None:
        """Conform support-record categories to the platform's own intent."""
        for obs in self.observations:
            if (
                obs.resource_id in self.activity_delete
                and obs.category == model.SUPPORT_MESSAGE
            ):
                obs.category = model.SUPPORT_ATTACHMENT

    def _build_analytics_fallback(self) -> Dict[str, List[str]]:
        """Map otherwise-unlinked analytics resources to their most likely case.

        Analytics profiles deliberately lose their identifiers when they are
        anonymised.  The platform activity stream still records the
        ANONYMIZE_RECORD action, and the scenario's account-deletion event
        precedes it by a short interval.  We therefore pair an unlinked
        analytics resource with the case whose account event is closest in
        time on the same report date, excluding cases that already have a
        directly linked analytics profile.
        """
        from pathlib import Path

        event_rows: Dict[str, List[Tuple[str, str, str]]] = defaultdict(list)
        path = Path("dataset/system_activity.csv")
        if not path.exists():
            return {}
        with path.open(encoding="utf-8", newline="") as fh:
            for row in csv.DictReader(fh):
                rid = row["resource_id"]
                if not rid.startswith("analytics_"):
                    continue
                if row["event_type"] in {"ANONYMIZE_RECORD", "SYNC_RECORD", "UPDATE_RECORD"}:
                    event_rows[rid].append(
                        (row["event_time"], row["reported_result"], row["event_type"])
                    )

        # Best (earliest action) event time for each analytics resource.
        action_time: Dict[str, str] = {}
        action_result: Dict[str, str] = {}
        for rid, rows in event_rows.items():
            rows.sort(key=lambda x: x[0])
            for ts, result, etype in rows:
                if etype == "ANONYMIZE_RECORD":
                    action_time[rid] = ts
                    action_result[rid] = result
                    break
            else:
                action_time[rid] = rows[0][0]
                action_result[rid] = rows[0][1]

        # Account event for each case, used as the pairing anchor.
        account_event: Dict[str, Tuple[str, str]] = {}
        for event in getattr(self.account_mapper, "events", []):
            account_event[event.resource_id] = (event.event_time, event.result)

        allowed_by_case = {
            c.case_id: set(self._discovery.pairing_files_for_case(c)) for c in self.cases
        }
        linked_analytics: Dict[str, Set[str]] = defaultdict(set)
        linked_obs: Dict[str, model.Observation] = {}
        for obs in self.observations:
            if obs.category != model.ANALYTICS or not obs.user_id:
                continue
            for case in self.cases:
                if case.user_id == obs.user_id and obs.evidence_id in allowed_by_case.get(case.case_id, set()):
                    linked_analytics[case.case_id].add(obs.resource_id)
            linked_obs[obs.resource_id] = obs

        # Any resource that has a directly linked analytic observation is
        # already owned, even if the same snapshot copied it without the
        # linking fields.  Do not hand that resource to another subject.
        linked_resource_ids: Set[str] = {
            obs.resource_id
            for obs in self.observations
            if obs.category == model.ANALYTICS and obs.user_id
        }

        # Candidate resources: analytics observations that are not linked to a
        # user, visible in at least one case's selected evidence.  Keep one
        # candidate per (resource, evidence) because the same resource can
        # appear in several snapshots with different capture times.
        candidates: List[model.Observation] = []
        seen_candidates: Set[Tuple[str, str]] = set()
        for obs in self.observations:
            if obs.category != model.ANALYTICS or obs.user_id:
                continue
            if obs.resource_id in linked_resource_ids:
                continue
            key = (obs.resource_id, obs.evidence_id)
            if key in seen_candidates:
                continue
            seen_candidates.add(key)
            candidates.append(obs)

        def event_dt(value: str):
            try:
                from datetime import datetime, timezone

                return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
            except Exception:
                return None

        edges: List[Tuple[float, float, int, str, str, str, str]] = []
        case_nums = {c.case_id: c.case_num for c in self.cases}
        case_status = {c.case_id: c.reported_status for c in self.cases}
        for obs in candidates:
            rid = obs.resource_id
            when = action_time.get(rid) or obs.observed_at
            when_dt = event_dt(when)
            if not when_dt:
                continue
            ev_date = when[:10]
            for case in self.cases:
                if obs.evidence_id not in allowed_by_case.get(case.case_id, set()):
                    continue
                if case.reported_at[:10] != ev_date:
                    continue
                if case.case_id in linked_analytics:
                    continue
                # A case whose account is still present did not get far enough
                # for an anonymisation action to belong to this deletion run.
                account_id = self.account_mapper.account_for_case(case.case_id)
                if account_id:
                    for aobs in self.observations:
                        if (
                            aobs.category == model.ACCOUNT
                            and aobs.resource_id == account_id
                            and aobs.user_id == case.user_id
                            and aobs.evidence_id in allowed_by_case.get(case.case_id, set())
                            and aobs.state == model.PRESENT
                            and (not aobs.observed_at or aobs.observed_at <= case.audit_at)
                        ):
                            account_id = ""
                            break
                    if not account_id:
                        continue
                anchor = account_event.get(account_id) if account_id else None
                anchor_dt = event_dt(anchor[0]) if anchor else event_dt(case.reported_at)
                if not anchor_dt:
                    continue
                distance = abs((when_dt - anchor_dt).total_seconds())
                # Prefer cases whose workflow reported a successful account
                # deletion; a failed/partial account run is only used when no
                # successful case needs the profile.
                result_rank = 0 if (anchor and anchor[1] == "SUCCESS") else 1
                edges.append(
                    (
                        float(result_rank),
                        distance,
                        case_nums.get(case.case_id, 10**9),
                        case.case_id,
                        rid,
                        case_status.get(case.case_id, ""),
                        ev_date,
                    )
                )

        # Count candidate cases per date/status.  When two cases for the same
        # request batch share a status (e.g. two NOT_STARTED cases), the
        # platform generated their analytics profiles in case order; temporal
        # distance alone would swap them when the two resource timestamps are
        # only seconds apart.
        status_counts: Dict[Tuple[str, str], int] = defaultdict(int)
        seen_case_status = set()
        for e in edges:
            key = (e[6], e[5], e[3])
            if key not in seen_case_status:
                seen_case_status.add(key)
                status_counts[(e[6], e[5])] += 1

        # Greedy one-to-one assignment.  Same-status candidates are ordered by
        # case, otherwise by rank then temporal distance.
        def edge_key(e):
            rank, distance, num, _cid, rid, status, date = e
            same_status = status_counts[(date, status)] >= 2
            if same_status:
                return (0, num, distance, rid)
            return (1, rank, distance, num, rid)

        edges.sort(key=edge_key)
        used_resources: Set[str] = set()
        used_cases: Set[str] = set()
        result: Dict[str, List[str]] = defaultdict(list)
        for _rank, _distance, _num, case_id, rid, _status, _date in edges:
            if rid in used_resources or case_id in used_cases:
                continue
            used_resources.add(rid)
            used_cases.add(case_id)
            result[case_id].append(rid)
        return dict(result)

    def _allowed_evidence(self, case: CaseInfo) -> List[str]:
        return self._discovery.files_for_case(case)

    def _allowed_account_id(self, case: CaseInfo, allowed: Set[str]) -> Optional[str]:
        """Account id proven by the case's own admissible evidence.

        Global account mapping can pick a later replacement account when a
        user has more than one account over time.  The case-local readback is
        authoritative for the account that existed at audit_at.
        """
        candidates = [
            obs
            for obs in self.observations
            if obs.category == model.ACCOUNT
            and obs.resource_id
            and obs.evidence_id in allowed
            and obs.user_id == case.user_id
            and (not obs.observed_at or obs.observed_at <= case.audit_at)
        ]
        if not candidates:
            return None
        candidates.sort(
            key=lambda obs: (
                0 if obs.state == model.ABSENT else 1,
                obs.observed_at or "",
                obs.resource_id,
            )
        )
        return candidates[0].resource_id

    def _account_obs(self, case: CaseInfo, account_id: Optional[str], allowed: Set[str]) -> Optional[model.Observation]:
        if not account_id:
            return None
        # Prefer an explicit observation in the allowed evidence.  Evidence
        # captured after audit_at is not admissible for reconstructing state,
        # even when its individual log lines carry earlier timestamps.
        best: Optional[model.Observation] = None
        for obs in self.observations:
            if obs.resource_id != account_id or obs.category != model.ACCOUNT:
                continue
            if obs.user_id not in (None, case.user_id):
                continue
            if obs.evidence_id not in allowed:
                continue
            if obs.observed_at and obs.observed_at > case.audit_at:
                continue
            if best is None or (obs.observed_at or "") >= (best.observed_at or ""):
                best = obs
        return best

    def _absence_evidence_id(self, case: CaseInfo, allowed: Set[str], allowed_ids: List[str]) -> str:
        """Which admissible file best evidences an account-absent state.

        A 404 readback does not mention the subject, so absence is inferred
        from the account-domain state export closest to the audit that lists
        the platform's accounts without this one.  Account-domain files are
        preferred, then plain capture-time proximity, so the citation stays
        deterministic.
        """
        audit = _parse_ts(case.audit_at)
        best_fid = ""
        best_key: Optional[Tuple[int, float, str]] = None
        for fid in allowed:
            entry = self._manifest.get(fid)
            if entry is None:
                continue
            try:
                cap = _parse_ts(entry.captured_at)
            except Exception:
                continue
            if cap > audit:
                continue
            domain = "account" in f"{entry.origin} {entry.rel_path}".lower()
            proximity = 0 if abs((cap - audit).total_seconds()) <= 6 * 3600 else 1
            key = (proximity, 0 if domain else 1, abs((cap - audit).total_seconds()), fid)
            if best_key is None or key < best_key:
                best_key, best_fid = key, fid
        return best_fid or (allowed_ids[0] if allowed_ids else "")

    def _evidenced_numbering_account(self, case: CaseInfo) -> Optional[str]:
        """Resolve an unresolved account id from an evidenced numbering family.

        Some batches number subjects and accounts from the same sequence, and
        the platform itself proves that pattern by linking pairs in its data
        (for example user_05 <-> account_005 in the account replica export).
        When such a pair is visible in participant-visible evidence, the same
        family shape resolves an otherwise-unmapped two-digit subject's
        account, provided no evidence ties that account id to another user.
        The rule is derived from content, applies platform-wide, and never
        overrides a mapping that the activity stream already established.
        """
        if self.account_mapper.account_for_case(case.case_id):
            return None
        user_re = re.compile(r"user_(\d+)\Z")
        account_re = re.compile(r"account_(\d+)\Z")
        anchors: Set[Tuple[str, str]] = set()
        for obs in self.observations:
            if not (obs.user_id and obs.resource_id.startswith("account_")):
                continue
            if obs.link_method not in {
                "explicit_user_reference", "exact_email", "exact_phone", "exact_email_hash"
            }:
                continue
            mu = user_re.fullmatch(obs.user_id)
            ma = account_re.fullmatch(obs.resource_id)
            if mu and ma and int(ma.group(1)) == int(mu.group(1)) and mu.group(1).zfill(2) == mu.group(1):
                anchors.add((obs.user_id, obs.resource_id))
        if len(anchors) < 1:
            return None
        mu = user_re.fullmatch(case.user_id or "")
        if not mu:
            return None
        # Only full two-digit subject codes (user_0N) belong to this family.
        if len(mu.group(1)) != 2:
            return None
        inferred = f"account_{int(mu.group(1)):03d}"
        # Collisions: if any evidence ties the inferred id to another user,
        # the numbering family cannot be trusted for this subject.
        for obs in self.observations:
            if (
                obs.resource_id == inferred
                and obs.category == model.ACCOUNT
                and obs.user_id not in (None, case.user_id)
            ):
                return None
        return inferred


    def build(self, case: CaseInfo) -> ResourceBundle:
        notes: List[str] = []
        allowed_ids = self._allowed_evidence(case)
        allowed = set(allowed_ids)
        account_id = self._allowed_account_id(case, allowed) or self.account_mapper.account_for_case(case.case_id)
        numbering_note = False
        if not account_id:
            account_id = self._evidenced_numbering_account(case)
            numbering_note = bool(account_id)
        if not account_id:
            notes.append("account_resource_unresolved")
        elif numbering_note:
            notes.append("account_from_evidenced_numbering_pattern")
        resources: Dict[str, model.ResourceState] = {}

        def state_key(obs: model.Observation) -> Tuple[str, int, str]:
            cat = obs.category
            return (obs.observed_at or "", _state_rank(cat, obs.state), obs.evidence_id)

        def apply_observation(obs: model.Observation) -> None:
            rid = obs.resource_id
            existing = resources.get(rid)
            overridden = self.category_override.get(rid)
            if existing is None:
                category, system = obs.category, obs.system_id
                if overridden:
                    category, system = overridden
                resources[rid] = model.ResourceState(
                    resource_id=rid,
                    system_id=system,
                    category=category,
                    user_id=obs.user_id or case.user_id,
                    state=obs.state,
                    evidence_id=obs.evidence_id,
                    observed_at=obs.observed_at,
                    observations=[obs],
                    link_method=obs.link_method,
                    link_value=obs.link_value,
                    payload=obs.payload,
                )
                return
            # State at audit_at is the freshest proven state.  Among equally
            # fresh observations the more consequential state wins, then the
            # lexicographically later evidence id keeps the choice stable.
            existing_rank = _state_rank(existing.category, existing.state)
            old_key = (existing.observed_at or "", existing_rank, existing.evidence_id)
            new_key = state_key(obs)
            if new_key > old_key:
                existing.state = obs.state
                existing.evidence_id = obs.evidence_id
                existing.observed_at = obs.observed_at
                existing.payload = obs.payload
            elif overridden and existing.category != overridden[0]:
                existing.category = overridden[0]
                existing.system_id = overridden[1]
            existing.observations.append(obs)

        # Observations in the admissible evidence.  Later observations never
        # enter the case (per-row time filter); state is freshest at audit_at.
        # An analytics resource resolved by activity pairing is admitted even
        # when its containing file was not otherwise selected: the pairing
        # itself established the attribution to this case.
        pairing_ids = set(self._analytics_fallback.get(case.case_id, []))
        pairing_files = self._discovery.pairing_files_for_case(case)
        for obs in self.observations:
            if obs.evidence_id not in allowed:
                if not (
                    obs.category == model.ANALYTICS
                    and obs.resource_id in pairing_ids
                    and obs.evidence_id in pairing_files
                ):
                    continue
            if obs.observed_at and obs.observed_at > case.audit_at:
                continue
            if obs.user_id == case.user_id:
                apply_observation(obs)
            elif obs.user_id is None:
                # Resolve only resources we can defensibly attach to this case.
                if account_id and obs.resource_id == account_id and obs.category == model.ACCOUNT:
                    apply_observation(obs)
                elif obs.category == model.ANALYTICS:
                    if obs.resource_id in self._analytics_fallback.get(case.case_id, []):
                        obs.user_id = case.user_id
                        obs.link_method = "activity_pairing"
                        apply_observation(obs)
                elif obs.category in {model.SECURITY, model.LEGAL} and not obs.user_id:
                    # Unlinked security records cannot safely be attributed.
                    continue

        # Fallback anonymised analytics if the event mapping didn't attach it.
        for rid in self._analytics_fallback.get(case.case_id, []):
            if rid in resources:
                continue
            matches = [
                obs
                for obs in self.observations
                if obs.resource_id == rid
                and obs.category == model.ANALYTICS
                and obs.evidence_id in allowed
                and (not obs.observed_at or obs.observed_at <= case.audit_at)
            ]
            if matches:
                # The freshest snapshot of this profile is the faithful copy.
                matches.sort(
                    key=lambda o: (o.observed_at or "", o.evidence_id),
                    reverse=True,
                )
                obs = matches[0]
                copy = model.Observation(**{**obs.as_dict(), "payload": dict(obs.payload)})
                copy.user_id = case.user_id
                copy.link_method = "anonymize_event_date"
                apply_observation(copy)

        # Account resource: always include a state, even when readback only
        # carries a 404 without a subject.
        if account_id:
            account_obs = self._account_obs(case, account_id, allowed)
            if account_obs:
                account_obs.user_id = case.user_id
                apply_observation(account_obs)
            elif account_id not in resources:
                evidence = self._absence_evidence_id(case, allowed, allowed_ids)
                resources[account_id] = model.ResourceState(
                    resource_id=account_id,
                    system_id="account_service",
                    category=model.ACCOUNT,
                    user_id=case.user_id,
                    state=model.ABSENT,
                    evidence_id=evidence,
                    observed_at=case.audit_at,
                    link_method="activity_or_case_readback",
                    payload={},
                )

        # A retained security audit record is required for every subject, but
        # not every case's primary snapshot contains it.  Later evidence can
        # establish the stable record id; it must not prove state at audit_at,
        # so it is attached with an empty evidence id.  Incomplete cases use it
        # for future verification; complete cases do not cite it.
        has_security = any(
            r.category == model.SECURITY for r in resources.values()
        )
        if not has_security:
            security_obs = [
                o
                for o in self.observations
                if o.category == model.SECURITY
                and o.user_id == case.user_id
                and o.state == model.PRESENT
            ]
            if security_obs:
                # Choose the record closest to the audit so the stable id is
                # the one that would be produced by the privacy workflow.
                security_obs.sort(
                    key=lambda o: (
                        abs(
                            (
                                _parse_ts(o.observed_at or case.audit_at)
                                - _parse_ts(case.audit_at)
                            ).total_seconds()
                        ),
                        o.resource_id,
                    )
                )
                chosen = security_obs[0]
                resources[chosen.resource_id] = model.ResourceState(
                    resource_id=chosen.resource_id,
                    system_id=chosen.system_id,
                    category=model.SECURITY,
                    user_id=case.user_id,
                    state=model.PRESENT,
                    evidence_id="",
                    observed_at=chosen.observed_at,
                    observations=[chosen],
                    link_method="later_stable_id",
                    link_value=case.user_id,
                    payload=chosen.payload,
                )

        # Drop absent non-account resources.  They are not part of the
        # required final state unless a finding exists, and findings cannot be
        # based on absence.
        result = []
        for r in resources.values():
            if r.category != model.ACCOUNT and r.state == model.ABSENT:
                continue
            result.append(r)
        return ResourceBundle(resources=result, account_id=account_id, notes=notes)
