#!/usr/bin/env python3
"""Evaluation utility for the Ghost Data Still Exists public sample cases.

Runs the bundled deletion-audit engine over ``dataset/sample_deletion_requests.csv``
and compares the generated answers against ``dataset/sample_output.csv``.
Reports status accuracy, step-count accuracy, ordered-step accuracy and
field-level accuracy for cases with equal step counts.  Only participant-visible
files are used.
"""

from __future__ import annotations

import csv
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "code"))

from ghostaudit.accounts import (  # noqa: E402
    AccountMapper,
    load_account_events,
    load_explicit_links,
    load_resource_event_times,
)
from ghostaudit.identity import IdentityIndex  # noqa: E402
from ghostaudit.planner import plan_case, rationale_to_steps  # noqa: E402
from ghostaudit.resources import ResourceBuilder  # noqa: E402
from ghostaudit.store import load_manifest, parse_all  # noqa: E402
from ghostaudit.visual import VisualIndex  # noqa: E402
from main import is_deletion_request, load_cases_from  # noqa: E402


def _load_rows(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def _ordered_equal(got: list[dict[str, Any]], want: list[dict[str, Any]]) -> bool:
    if len(got) != len(want):
        return False
    for a, b in zip(got, want):
        for key in ("step_no", "phase", "operation", "state", "system_id", "resource_id", "evidence_id"):
            if a.get(key) != b.get(key):
                return False
    return True


def evaluate() -> dict[str, Any]:
    dataset = ROOT / "dataset"
    identities = IdentityIndex(dataset)
    manifest = load_manifest(dataset)
    observations = parse_all(manifest, identities)

    sample_cases = load_cases_from(dataset / "sample_deletion_requests.csv", identities)
    hidden_cases = load_cases_from(dataset / "deletion_requests.csv", identities)
    all_cases = hidden_cases + sample_cases

    mapper = AccountMapper(
        all_cases,
        load_account_events(dataset),
        observations,
        load_explicit_links(dataset, identities),
        load_resource_event_times(dataset),
    )
    builder = ResourceBuilder(
        observations, all_cases, identities, mapper, manifest,
        VisualIndex(ROOT / "code", identities),
    )
    expected_rows = _load_rows(dataset / "sample_output.csv")
    expected_by_case = {row["case_id"]: row for row in expected_rows}

    case_status_hits = 0
    step_count_hits = 0
    ordered_hits = 0
    field_total = 0
    field_hits = 0
    per_case: list[dict[str, Any]] = []

    for case in sample_cases:
        bundle = builder.build(case)
        rationale = plan_case(case, bundle, not_applicable=not is_deletion_request(case.request_text))
        got_steps = rationale_to_steps(rationale)
        want_row = expected_by_case[case.case_id]
        want_steps = json.loads(want_row["steps_json"])
        status_match = rationale.deletion_status == want_row["deletion_status"]
        count_match = len(got_steps) == len(want_steps)
        if status_match:
            case_status_hits += 1
        if count_match:
            step_count_hits += 1
            for got, want in zip(got_steps, want_steps):
                for key in ("step_no", "phase", "operation", "state", "system_id", "resource_id", "evidence_id"):
                    field_total += 1
                    if got.get(key) == want.get(key):
                        field_hits += 1
        ordered_match = _ordered_equal(got_steps, want_steps)
        if ordered_match:
            ordered_hits += 1
        per_case.append(
            {
                "case_id": case.case_id,
                "status_match": status_match,
                "expected_steps": len(want_steps),
                "predicted_steps": len(got_steps),
                "ordered_match": ordered_match,
            }
        )

    total = len(sample_cases)
    return {
        "sample_cases": total,
        "status_hits": case_status_hits,
        "step_count_hits": step_count_hits,
        "ordered_hits": ordered_hits,
        "field_total": field_total,
        "field_hits": field_hits,
        "per_case": per_case,
    }


def main() -> int:
    result = evaluate()
    total_cases = result["sample_cases"]
    print("Ghost Data Still Exists - sample evaluation")
    print(f"sample cases: {total_cases}")
    print(f"status accuracy: {result['status_hits']}/{total_cases} "
          f"({result['status_hits'] / total_cases:.2%})" if total_cases else "")
    print(f"step-count accuracy: {result['step_count_hits']}/{total_cases} "
          f"({result['step_count_hits'] / total_cases:.2%})" if total_cases else "")
    print(f"ordered-step accuracy: {result['ordered_hits']}/{total_cases} "
          f"({result['ordered_hits'] / total_cases:.2%})" if total_cases else "")
    if result["field_total"]:
        print(f"field accuracy (equal-step cases): {result['field_hits']}/{result['field_total']} "
              f"({result['field_hits'] / result['field_total']:.2%})")
    mismatches = [c for c in result["per_case"] if not c["ordered_match"]]
    if mismatches:
        print("\ncases without an exact ordered-step match:")
        for item in mismatches:
            print(f"  {item['case_id']}: expected={item['expected_steps']} got={item['predicted_steps']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
