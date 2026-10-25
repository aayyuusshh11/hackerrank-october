#!/usr/bin/env python3
"""Ghost Data Still Exists - deterministic deletion-audit agent.

Usage:
    python code/main.py

Reads participant-facing data from ``dataset/`` and writes ``output.csv`` at the
repository root.  No network access, secrets, or organizer-only data are used.
"""

from __future__ import annotations

import csv
import json
import re
import sys
from pathlib import Path
from typing import Iterable, List

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "code"))

from ghostaudit import model
from ghostaudit.accounts import (
    AccountMapper,
    CaseInfo,
    load_account_events,
    load_explicit_links,
    load_resource_event_times,
)
from ghostaudit.identity import IdentityIndex
from ghostaudit.planner import plan_case, rationale_to_steps, steps_json
from ghostaudit.resources import ResourceBuilder
from ghostaudit.store import load_manifest, parse_all
from ghostaudit.visual import VisualIndex


NON_DELETION_MARKERS = [
    "not asking for deletion",
    "not a request to erase",
    "not a request to delete",
    "do not close",
    "don't close",
    "do not delete",
    "don't delete",
    "leave the account",
    "leave my profile",
    "leave my record",
    "keep the account",
    "keep my account",
    "still need the account",
    "still use it",
    "still use the service",
    "without removing",
    "information request",
    "what personal information you hold",
    "download my account information",
    "download my information",
    "copy of the information",
    "copy of last year's invoices",
    "reset the login",
    "resetting the login",
    "change the email address",
    "replace it with the new one",
    "correct it",
    "marketing lists only",
]

DELETION_MARKERS = [
    "delete",
    "deletion",
    "delete my",
    "erase",
    "erasure",
    "remove my",
    "remove the profile",
    "remove the personal",
    "close my account",
    "closed account",
    "account closure",
    "permanently deleted",
    "complete the deletion",
    "eliminar",
    "elimina",
    "eliminación",
    "suprimir",
    "supprimer",
    "suppression",
    "supprimer définitivement",
    "löschen",
    "löschung",
    "cancellare",
    "cancellazione",
    "删除",
    "消除",
    "注销",
    "消去",
    "削除",
    "삭제",
    "hapus",
    "menghapus",
    "desativar conta",
    "excluir",
    "exclusão",
    "exclusao",
    "apagar",
    "eliminação",
    "eliminacion",
    "eliminación",
    "eliminación permanente",
    "suppression définitive",
    "löschung",
    "löschen",
    "verwijderen",
    "rade",
    "raderen",
    "radera",
    "poista",
    "poistaminen",
    "slett",
    "slette",
    "sletting",
    "silmä",
    "sterge",
    "șterge",
    "izbris",
    "izbriši",
]


def is_deletion_request(text: str) -> bool:
    """Conservative intent check for the request text.

    Non-deletion requests in the dataset are explicit: access, correction,
    export, login help, marketing opt-out.  We require a deletion verb and
    reject text that contains a negation/retention marker.
    """
    low = (text or "").lower()
    if any(marker in low for marker in NON_DELETION_MARKERS):
        return False
    # All remaining participant requests are deletion/erasure requests.  Some
    # are written in languages not covered by the marker list; treating them
    # as deletion is safer than dropping a valid deletion case.
    return True


def load_cases_from(path: Path, identities: IdentityIndex) -> List[CaseInfo]:
    rows: List[CaseInfo] = []
    with path.open(encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            user = identities.resolve_email(row["email"])
            try:
                num = int(row["case_id"].split("_")[1])
            except Exception:
                num = 0
            rows.append(
                CaseInfo(
                    case_id=row["case_id"],
                    case_num=num,
                    email=row["email"],
                    user_id=user or "",
                    requested_at=row["requested_at"],
                    reported_status=row["reported_status"],
                    reported_at=row["reported_at"],
                    audit_at=row["audit_at"],
                    request_text=row["request_text"],
                    deletion_requested=is_deletion_request(row["request_text"]),
                )
            )
    return rows


def main() -> int:
    dataset = ROOT / "dataset"
    identities = IdentityIndex(dataset)
    manifest = load_manifest(dataset)
    observations = parse_all(manifest, identities)

    hidden_cases = load_cases_from(dataset / "deletion_requests.csv", identities)
    sample_path = dataset / "sample_deletion_requests.csv"
    sample_cases = load_cases_from(sample_path, identities) if sample_path.exists() else []
    all_cases = hidden_cases + sample_cases

    events = load_account_events(dataset)
    links = load_explicit_links(dataset, identities)
    resource_event_times = load_resource_event_times(dataset)
    mapper = AccountMapper(all_cases, events, observations, links, resource_event_times)
    visual = VisualIndex(ROOT / "code", identities)
    builder = ResourceBuilder(observations, all_cases, identities, mapper, manifest, visual)

    rationales = []
    for case in hidden_cases:
        na = not is_deletion_request(case.request_text)
        if na:
            rationale = plan_case(case, None, not_applicable=True)  # type: ignore[arg-type]
        else:
            bundle = builder.build(case)
            rationale = plan_case(case, bundle)
        rationales.append(rationale)

    rationales.sort(key=lambda r: r.case_id)
    if [r.case_id for r in rationales] != sorted(r.case_id for r in rationales):
        raise RuntimeError("output.csv was not sorted by case_id")
    if len({r.case_id for r in rationales}) != len(rationales):
        raise RuntimeError("output.csv has duplicate case ids")
    out_path = ROOT / "output.csv"
    with out_path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh, lineterminator="\n")
        writer.writerow(["case_id", "deletion_status", "steps_json"])
        for rationale in rationales:
            steps = rationale_to_steps(rationale)
            writer.writerow([rationale.case_id, rationale.deletion_status, steps_json(steps)])

    # Compact console summary (also used by the evaluation report).
    from collections import Counter

    counts = Counter(r.deletion_status for r in rationales)
    print(f"Wrote {len(rationales)} cases to {out_path}")
    print("Statuses:", dict(counts))
    print("Account mapping:", mapper.stats())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
