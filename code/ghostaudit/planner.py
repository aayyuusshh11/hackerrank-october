"""Policy application and deterministic step planning.

The policy in ``deletion_rules.csv`` is rendered as a small, explicit mapping.
Every decision is made from canonical resource states; output serialization is
kept separate so ordering and JSON encoding stay deterministic.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence

from . import model
from .accounts import CaseInfo
from .resources import ResourceBundle


# Operation order is the order used by the public sample answers and by
# AGENTS.md section 6.4.
FINDING_ORDER = [
    "REPORT_REMAINING_RECORD",
    "REPORT_EXPOSED_PII",
    "REPORT_ACTIVE_DATA_FLOW",
    "REPORT_RESTORE_RISK",
]
REMEDIATION_ORDER = [
    "DISABLE_DATA_FLOW",
    "BLOCK_RESTORE",
    "DELETE_RECORD",
    "REDACT_RECORD",
    "ANONYMIZE_RECORD",
]
VERIFICATION_ORDER = [
    "VERIFY_RECORD_ABSENT",
    "VERIFY_RECORD_PRESENT",
    "VERIFY_RECORD_REDACTED",
    "VERIFY_RECORD_ANONYMIZED",
    "VERIFY_DATA_FLOW_DISABLED",
    "VERIFY_RESTORE_BLOCKED",
]


@dataclass(frozen=True)
class PolicyOutcome:
    finding_op: Optional[str]
    finding_state: Optional[str]
    remediation_op: Optional[str]
    desired_state: str
    verification_op: str


def outcome_for(resource: model.ResourceState) -> Optional[PolicyOutcome]:
    """Return the policy outcome for one resource.

    ``None`` means the resource is not part of the required answer (for
    example an absent search document that has already been removed).
    """
    cat = resource.category
    state = resource.state

    if cat == model.ACCOUNT:
        if state == model.PRESENT:
            return PolicyOutcome(
                "REPORT_REMAINING_RECORD", model.PRESENT, "DELETE_RECORD", model.ABSENT, "VERIFY_RECORD_ABSENT"
            )
        return PolicyOutcome(None, None, None, model.ABSENT, "VERIFY_RECORD_ABSENT")

    if cat in {model.SEARCH, model.CACHE, model.EXPORT, model.MARKETING, model.SECONDARY}:
        if state == model.PRESENT:
            return PolicyOutcome(
                "REPORT_REMAINING_RECORD", model.PRESENT, "DELETE_RECORD", model.ABSENT, "VERIFY_RECORD_ABSENT"
            )
        return None

    if cat in {model.SUPPORT_ATTACHMENT, model.OBJECT}:
        if state == model.PRESENT:
            return PolicyOutcome(
                "REPORT_REMAINING_RECORD", model.PRESENT, "DELETE_RECORD", model.ABSENT, "VERIFY_RECORD_ABSENT"
            )
        return None

    if cat == model.ANALYTICS:
        if state == model.PRESENT:
            return PolicyOutcome(
                "REPORT_EXPOSED_PII", model.PRESENT, "ANONYMIZE_RECORD", model.ANONYMIZED, "VERIFY_RECORD_ANONYMIZED"
            )
        if state == model.ANONYMIZED:
            return PolicyOutcome(None, None, None, model.ANONYMIZED, "VERIFY_RECORD_ANONYMIZED")
        return None

    if cat in {model.SUPPORT_MESSAGE, model.EMAIL_DELIVERY, model.TOMBSTONE}:
        if state == model.PRESENT:
            return PolicyOutcome(
                "REPORT_EXPOSED_PII", model.PRESENT, "REDACT_RECORD", model.REDACTED, "VERIFY_RECORD_REDACTED"
            )
        if state == model.REDACTED:
            return PolicyOutcome(None, None, None, model.REDACTED, "VERIFY_RECORD_REDACTED")
        return None

    if cat == model.FLOW:
        if state == model.ACTIVE:
            return PolicyOutcome(
                "REPORT_ACTIVE_DATA_FLOW", model.ACTIVE, "DISABLE_DATA_FLOW", model.DISABLED, "VERIFY_DATA_FLOW_DISABLED"
            )
        if state == model.DISABLED:
            return PolicyOutcome(None, None, None, model.DISABLED, "VERIFY_DATA_FLOW_DISABLED")
        return None

    if cat == model.BACKUP:
        if state == model.ACTIVE:
            return PolicyOutcome(
                "REPORT_RESTORE_RISK", model.ACTIVE, "BLOCK_RESTORE", model.BLOCKED, "VERIFY_RESTORE_BLOCKED"
            )
        if state == model.BLOCKED:
            return PolicyOutcome(None, None, None, model.BLOCKED, "VERIFY_RESTORE_BLOCKED")
        return None

    if cat in {model.SECURITY, model.LEGAL}:
        if state == model.PRESENT:
            return PolicyOutcome(None, None, None, model.PRESENT, "VERIFY_RECORD_PRESENT")
        return None

    if cat == model.UNKNOWN:
        # Unknown resources are never allowed to create a finding.  They can
        # only be retained when their payload clearly indicates a security
        # record, which is handled by category assignment.
        return None

    return None


def sort_key_finding(op: str, system_id: str, resource_id: str) -> tuple:
    return (FINDING_ORDER.index(op), system_id, resource_id)


def sort_key_remediation(op: str, system_id: str, resource_id: str) -> tuple:
    return (REMEDIATION_ORDER.index(op), system_id, resource_id)


def sort_key_verification(op: str, system_id: str, resource_id: str) -> tuple:
    return (VERIFICATION_ORDER.index(op), system_id, resource_id)


def plan_case(case: CaseInfo, bundle: ResourceBundle, not_applicable: bool = False) -> model.CaseRationale:
    if not_applicable:
        return model.CaseRationale(case_id=case.case_id, deletion_status="NOT_APPLICABLE", notes=["non_deletion_request"])

    findings: List[model.Finding] = []
    remediations: List[model.Action] = []
    verifications: List[model.Verification] = []
    notes: List[str] = []

    outcomes = []
    for resource in bundle.resources:
        outcome = outcome_for(resource)
        if outcome is None:
            continue
        outcomes.append((resource, outcome))
        if outcome.finding_op:
            findings.append(
                model.Finding(
                    operation=outcome.finding_op,
                    system_id=resource.system_id,
                    resource_id=resource.resource_id,
                    state=outcome.finding_state or model.PRESENT,
                    evidence_id=resource.evidence_id,
                )
            )
            if outcome.remediation_op:
                remediations.append(
                    model.Action(
                        operation=outcome.remediation_op,
                        system_id=resource.system_id,
                        resource_id=resource.resource_id,
                        state=outcome.desired_state,
                    )
                )

    # Verification is planned only after the complete finding set is known.
    # An incomplete case's verification steps describe future work and must not
    # cite evidence.
    future = bool(findings)
    for resource, outcome in outcomes:
        evidence_id = "" if future else resource.evidence_id
        # A retained record discovered only in later evidence cannot prove a
        # COMPLETE state at audit_at.  It is still a valid future check for an
        # incomplete case.
        if (
            not future
            and outcome.verification_op == "VERIFY_RECORD_PRESENT"
            and not evidence_id
        ):
            continue
        verifications.append(
            model.Verification(
                operation=outcome.verification_op,
                system_id=resource.system_id,
                resource_id=resource.resource_id,
                state=outcome.desired_state,
                evidence_id=evidence_id,
            )
        )

    findings.sort(key=lambda f: sort_key_finding(f.operation, f.system_id, f.resource_id))
    remediations.sort(key=lambda a: sort_key_remediation(a.operation, a.system_id, a.resource_id))
    verifications.sort(key=lambda v: sort_key_verification(v.operation, v.system_id, v.resource_id))

    status = "INCOMPLETE" if findings else "COMPLETE"
    if not findings:
        # A complete case must have an explicit account absent verification.
        if not any(v.operation == "VERIFY_RECORD_ABSENT" and v.system_id == "account_service" for v in verifications):
            notes.append("missing_account_verification")
    return model.CaseRationale(
        case_id=case.case_id,
        deletion_status=status,
        findings=findings,
        remediations=remediations,
        verifications=verifications,
        notes=notes,
        resources=bundle.resources,
    )


def rationale_to_steps(rationale: model.CaseRationale) -> List[dict]:
    steps: List[dict] = []
    no = 1
    for f in rationale.findings:
        steps.append(
            {
                "step_no": no,
                "phase": "FINDING",
                "system_id": f.system_id,
                "resource_id": f.resource_id,
                "operation": f.operation,
                "state": f.state,
                "evidence_id": f.evidence_id,
            }
        )
        no += 1
    for a in rationale.remediations:
        steps.append(
            {
                "step_no": no,
                "phase": "REMEDIATION",
                "system_id": a.system_id,
                "resource_id": a.resource_id,
                "operation": a.operation,
                "state": a.state,
                "evidence_id": "",
            }
        )
        no += 1
    for v in rationale.verifications:
        steps.append(
            {
                "step_no": no,
                "phase": "VERIFICATION",
                "system_id": v.system_id,
                "resource_id": v.resource_id,
                "operation": v.operation,
                "state": v.state,
                "evidence_id": v.evidence_id,
            }
        )
        no += 1
    return steps


def steps_json(steps: Sequence[dict]) -> str:
    return json.dumps(list(steps), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
