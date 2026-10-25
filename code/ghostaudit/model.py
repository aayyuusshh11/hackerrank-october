"""Core data model for the deletion-audit engine.

The engine deliberately keeps *facts* (observations) separate from *policy*.
Observations are extracted deterministically from participant-visible evidence;
policy then turns those observations into findings, remediation and verification
steps.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional

# Canonical resource categories.  The names track the policy vocabulary rather
# than the source-system names so that policy stays independent of formatting.
ACCOUNT = "ACCOUNT"
SEARCH = "SEARCH_INDEX_ENTRY"
CACHE = "CACHE_ENTRY"
SUPPORT_MESSAGE = "SUPPORT_MESSAGE"
SUPPORT_ATTACHMENT = "SUPPORT_ATTACHMENT"
ANALYTICS = "ANALYTICS_PROFILE"
TELEMETRY = "PRODUCT_TELEMETRY"
EXPORT = "USER_EXPORT"
MARKETING = "MARKETING_PROFILE"
EMAIL_DELIVERY = "EMAIL_DELIVERY"
SECURITY = "SECURITY_AUDIT_LOG"
LEGAL = "LEGAL_HOLD"
BACKUP = "BACKUP_ARCHIVE"
FLOW = "DATA_FLOW"
TOMBSTONE = "ACCOUNT_TOMBSTONE"
SECONDARY = "SECONDARY_IDENTIFIER"
OBJECT = "OBJECT_ATTACHMENT"
UNKNOWN = "UNKNOWN"

# States are already expressed in the vocabulary required by the output
# contract.  Keeping them canonical avoids a second translation layer later.
PRESENT = "PRESENT"
ABSENT = "ABSENT"
REDACTED = "REDACTED"
ANONYMIZED = "ANONYMIZED"
ACTIVE = "ACTIVE"
DISABLED = "DISABLED"
BLOCKED = "BLOCKED"


@dataclass
class Observation:
    """A single point-in-time observation of one resource.

    ``observed_at`` is the event/snapshot time, not necessarily the capture
    time of the containing file.  ``link_method`` records *why* we believe the
    resource belongs to ``user_id``; this is useful for conflict handling and
    for explaining decisions in the evaluation report.
    """

    resource_id: str
    system_id: str
    category: str
    state: str
    evidence_id: str
    observed_at: str
    user_id: Optional[str] = None
    link_method: str = "unlinked"
    link_value: str = ""
    payload: Dict[str, Any] = field(default_factory=dict)
    location: str = ""

    @property
    def sort_key(self) -> tuple:
        return (self.observed_at or "", self.evidence_id, self.resource_id)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "resource_id": self.resource_id,
            "system_id": self.system_id,
            "category": self.category,
            "state": self.state,
            "evidence_id": self.evidence_id,
            "observed_at": self.observed_at,
            "user_id": self.user_id,
            "link_method": self.link_method,
            "link_value": self.link_value,
            "payload": self.payload,
            "location": self.location,
        }


@dataclass
class ResourceState:
    """The reconstructed state of one resource at a case's audit time."""

    resource_id: str
    system_id: str
    category: str
    user_id: Optional[str]
    state: str
    evidence_id: str
    observed_at: str
    observations: list[Observation] = field(default_factory=list)
    link_method: str = ""
    link_value: str = ""
    payload: Dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "resource_id": self.resource_id,
            "system_id": self.system_id,
            "category": self.category,
            "user_id": self.user_id,
            "state": self.state,
            "evidence_id": self.evidence_id,
            "observed_at": self.observed_at,
            "observations": [o.as_dict() for o in self.observations],
            "link_method": self.link_method,
            "link_value": self.link_value,
            "payload": self.payload,
        }


@dataclass
class Finding:
    operation: str
    system_id: str
    resource_id: str
    state: str
    evidence_id: str


@dataclass
class Action:
    operation: str
    system_id: str
    resource_id: str
    state: str


@dataclass
class Verification:
    operation: str
    system_id: str
    resource_id: str
    state: str
    evidence_id: str


@dataclass
class CaseRationale:
    case_id: str
    deletion_status: str
    findings: list[Finding] = field(default_factory=list)
    remediations: list[Action] = field(default_factory=list)
    verifications: list[Verification] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    resources: list[ResourceState] = field(default_factory=list)
