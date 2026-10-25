"""Reconstruct account-resource ownership and map deletion events to cases.

Account IDs are often visible in readback evidence even when the profile itself
has already been deleted.  When a 404 readback omits the subject, the global
activity stream still contains the deletion event.  We use the workflow's
reported result (SUCCESS/PARTIAL/FAILED) as a weak but useful status hint and
assign scenario events to cases within the same report-time cluster.
"""

from __future__ import annotations

import csv
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

from . import model
from .identity import IdentityIndex

_STATUS_TO_EVENT = {
    "COMPLETE": "SUCCESS",
    "PARTIALLY_COMPLETE": "PARTIAL",
    "NOT_STARTED": "FAILED",
}


@dataclass
class CaseInfo:
    case_id: str
    case_num: int
    email: str
    user_id: str
    requested_at: str
    reported_status: str
    reported_at: str
    audit_at: str
    request_text: str
    deletion_requested: bool = True


@dataclass
class AccountEvent:
    activity_id: str
    event_time: str
    resource_id: str
    result: str
    details: str


def parse_ts(value: str) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except Exception:
        return None


def load_cases(dataset_dir: Path, identities: IdentityIndex) -> List[CaseInfo]:
    path = dataset_dir / "deletion_requests.csv"
    rows: List[CaseInfo] = []
    with path.open(encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            user = identities.resolve_email(row["email"])
            if not user:
                continue
            try:
                num = int(row["case_id"].split("_")[1])
            except Exception:
                num = 0
            rows.append(
                CaseInfo(
                    case_id=row["case_id"],
                    case_num=num,
                    email=row["email"],
                    user_id=user,
                    requested_at=row["requested_at"],
                    reported_status=row["reported_status"],
                    reported_at=row["reported_at"],
                    audit_at=row["audit_at"],
                    request_text=row["request_text"],
                )
            )
    return rows


def load_account_events(dataset_dir: Path) -> List[AccountEvent]:
    out: List[AccountEvent] = []
    with (dataset_dir / "system_activity.csv").open(encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            if row["event_type"] != "DELETE_RECORD":
                continue
            if row["target_system"] != "account_service":
                continue
            out.append(
                AccountEvent(
                    activity_id=row["activity_id"],
                    event_time=row["event_time"],
                    resource_id=row["resource_id"],
                    result=row["reported_result"],
                    details=row["details"],
                )
            )
    out.sort(key=lambda e: (e.event_time, e.activity_id))
    return out


def load_explicit_links(dataset_dir: Path, identities: IdentityIndex) -> Dict[str, Tuple[str, str]]:
    """Resource-id -> (user_id, activity_id) from LINK_RESOURCE_IDENTITY."""
    links: Dict[str, Tuple[str, str]] = {}
    with (dataset_dir / "system_activity.csv").open(encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            if row["event_type"] != "LINK_RESOURCE_IDENTITY":
                continue
            details = row["details"]
            m = re.search(r"\b(user_\d+)\b", details)
            if m and m.group(1) in identities.identities:
                links[row["resource_id"]] = (m.group(1), row["activity_id"])
    return links


def load_resource_event_times(dataset_dir: Path) -> Dict[str, str]:
    """Earliest workflow event time for each non-account resource."""
    times: Dict[str, str] = {}
    with (dataset_dir / "system_activity.csv").open(encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            rid = row["resource_id"]
            if rid.startswith("account_"):
                continue
            if row["event_type"] in {"LINK_RESOURCE_IDENTITY", "UPDATE_RECORD"}:
                continue
            prev = times.get(rid)
            if prev is None or row["event_time"] < prev:
                times[rid] = row["event_time"]
    return times


class AccountMapper:
    def __init__(
        self,
        cases: List[CaseInfo],
        events: List[AccountEvent],
        observations: Iterable[model.Observation],
        explicit_links: Dict[str, Tuple[str, str]],
        resource_event_times: Optional[Dict[str, str]] = None,
    ):
        self.cases = cases
        self.events = events
        self.explicit_links = explicit_links
        self.resource_event_times = resource_event_times or {}
        self.direct: Dict[str, str] = {}
        self.account_owner: Dict[str, str] = {}
        self.assignment: Dict[str, str] = {}
        self.assignment_source: Dict[str, str] = {}
        self.unresolved: List[str] = []
        self.observations = list(observations)
        self._load_direct(self.observations)
        self._assign()

    def _load_direct(self, observations: Iterable[model.Observation]) -> None:
        # Prefer the most recent observation for each account, but keep all
        # account->user links for elimination.
        best: Dict[str, Tuple[str, str]] = {}
        for obs in observations:
            if obs.category != model.ACCOUNT or not obs.user_id:
                continue
            rid = obs.resource_id
            self.account_owner.setdefault(rid, obs.user_id)
            prev = best.get(obs.user_id)
            key = (obs.observed_at or "", obs.evidence_id)
            if prev is None or key >= prev[0]:
                best[obs.user_id] = (key, rid)
        for user, (_, rid) in best.items():
            self.direct[user] = rid
        for rid, (user, _activity) in self.explicit_links.items():
            if rid.startswith("account_"):
                self.direct.setdefault(user, rid)
                self.account_owner.setdefault(rid, user)

    def _case_fingerprint(self, case: CaseInfo) -> str:
        """Earliest non-account workflow event linked to the subject.

        Workflow events for one scenario are generated as a contiguous batch.
        Their order is a stronger account-event anchor than the report time,
        especially when several cases share one report timestamp.
        """
        times = []
        for obs in self.observations:
            if obs.user_id != case.user_id or obs.category == model.ACCOUNT:
                continue
            ts = self.resource_event_times.get(obs.resource_id)
            if ts:
                times.append(ts)
        return min(times) if times else case.reported_at

    def _assign(self) -> None:
        for case in self.cases:
            if not case.deletion_requested:
                continue
            if case.user_id in self.direct:
                self.assignment[case.case_id] = self.direct[case.user_id]
                self.assignment_source[case.case_id] = "direct_evidence"
        used = {rid for rid, user in self.account_owner.items() if user in self.direct}
        # Group missing cases by exact report timestamp.
        clusters: Dict[str, List[CaseInfo]] = defaultdict(list)
        for case in self.cases:
            if not case.deletion_requested:
                continue
            if case.case_id in self.assignment:
                continue
            if case.reported_status == "COMPLETE" or case.reported_status in _STATUS_TO_EVENT:
                clusters[case.reported_at].append(case)
        for reported_at, cases in clusters.items():
            report_dt = parse_ts(reported_at)
            if not report_dt:
                self.unresolved.extend(c.case_id for c in cases)
                continue
            case_status = {c.case_id: c.reported_status for c in cases}
            candidates = []
            for event in self.events:
                if event.resource_id in used:
                    continue
                ev_dt = parse_ts(event.event_time)
                if not ev_dt:
                    continue
                # Scenario events are same-day and precede the report by a few
                # hours.  Allow a small previous-day window for late-night work.
                delta = (report_dt - ev_dt).total_seconds()
                if delta < -300 or delta > 6 * 3600:
                    continue
                if event.event_time[:10] != reported_at[:10]:
                    continue
                candidates.append(event)
            # Match by reported-status hint; this is how the generated
            # workflow writes its result.  Remove events already tied to a
            # direct account link.
            by_result: Dict[str, List[AccountEvent]] = defaultdict(list)
            for event in candidates:
                by_result[event.result].append(event)
            # Scenario batches are ordered by their first downstream workflow
            # event, not by report time or request time.  This keeps account
            # events aligned with the rest of each case's resources even when
            # several cases share a batch timestamp.
            fingerprints = {c.case_id: self._case_fingerprint(c) for c in cases}
            cases_sorted = sorted(cases, key=lambda c: (fingerprints[c.case_id], c.case_num))
            for status in sorted({c.reported_status for c in cases_sorted}):
                want = _STATUS_TO_EVENT.get(status)
                group = [c for c in cases_sorted if c.reported_status == status]
                evs = by_result.get(want, [])
                evs.sort(key=lambda e: (e.event_time, e.activity_id))
                if not evs and status == "COMPLETE":
                    evs = by_result.get("SUCCESS", [])
                if not evs and status == "PARTIALLY_COMPLETE":
                    evs = by_result.get("PARTIAL", [])
                if not evs and status == "NOT_STARTED":
                    evs = by_result.get("FAILED", [])
                # Prefer events in the tight pre-report window when there are
                # decoys.  If there are more candidates than cases, keep the
                # closest ones to the report time.
                if len(evs) > len(group):
                    evs = sorted(
                        evs,
                        key=lambda e: abs((report_dt - parse_ts(e.event_time)).total_seconds()),  # type: ignore[arg-type]
                    )[: len(group)]
                    evs.sort(key=lambda e: (e.event_time, e.activity_id))
                for case, event in zip(group, evs):
                    self.assignment[case.case_id] = event.resource_id
                    self.assignment_source[case.case_id] = "activity_cluster"
                    self.account_owner.setdefault(event.resource_id, case.user_id)
                    used.add(event.resource_id)
            self.unresolved.extend(c.case_id for c in cases_sorted if c.case_id not in self.assignment)

    def account_for_case(self, case_id: str) -> Optional[str]:
        return self.assignment.get(case_id)

    def stats(self) -> Dict[str, object]:
        return {
            "cases": len(self.cases),
            "assigned": len(self.assignment),
            "direct": sum(1 for v in self.assignment_source.values() if v == "direct_evidence"),
            "activity": sum(1 for v in self.assignment_source.values() if v == "activity_cluster"),
            "unresolved": self.unresolved,
        }
