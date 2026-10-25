"""Loader for transcribed visual observations.

Images and PDF screenshots cannot be re-extracted deterministically at run
time (no OCR dependency), so facts that were extracted from the pixel content
of ``dataset/evidence/images`` during development are shipped as data with full
provenance: source file, locator (screen region), subject identifiers, resource
id, state, observation time and a quoted fact.

The table contains **no case identifiers**.  Rows are linked to cases strictly
through the subject references (emails, phones, hashes, stable user ids)
resolved against ``user_identities.csv`` — exactly like every other evidence
link in the pipeline.
"""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Dict, List

from . import model
from .identity import IdentityIndex


def _category(raw: str) -> str:
    cat = (raw or "").strip().upper()
    known = {
        model.ACCOUNT, model.SEARCH, model.CACHE, model.SUPPORT_MESSAGE,
        model.SUPPORT_ATTACHMENT, model.ANALYTICS, model.EXPORT,
        model.MARKETING, model.EMAIL_DELIVERY, model.SECURITY, model.LEGAL,
        model.BACKUP, model.FLOW, model.TOMBSTONE, model.OBJECT,
        model.SECONDARY, model.UNKNOWN,
    }
    return cat if cat in known else model.UNKNOWN


class VisualIndex:
    def __init__(self, code_root: Path, identities: IdentityIndex):
        path = code_root / "data" / "visual_observations.csv"
        self.rows = []
        self.by_user: Dict[str, List[model.Observation]] = {}
        self.evidence_for_user: Dict[str, set] = {}
        if not path.exists():
            return
        with path.open(encoding="utf-8", newline="") as fh:
            for row in csv.DictReader(fh):
                eid = (row.get("evidence_id") or "").strip()
                if not eid:
                    continue
                # Resolve the subject through the recorded references.
                user = ""
                for ref in (row.get("subject_refs") or "").split(";"):
                    resolved = identities.resolve_text(ref.strip())
                    if resolved:
                        user = resolved
                        break
                if not user:
                    continue
                rid = (row.get("resource_id") or "").strip()
                fact = (row.get("fact_text") or "").strip()
                locator = (row.get("locator") or "").strip()
                observation = model.Observation(
                    resource_id=rid,
                    system_id=(row.get("system_id") or "").strip(),
                    category=_category(row.get("category")),
                    state=(row.get("state") or model.PRESENT).strip(),
                    evidence_id=eid,
                    observed_at=(row.get("observed_at") or "").strip(),
                    user_id=user,
                    link_method="visual_transcription",
                    link_value=";".join(
                        part.strip() for part in (row.get("subject_refs") or "").split(";")
                    ),
                    payload={"fact": fact, "locator": locator},
                    location=f"{row.get('file_path', '')}::{locator}",
                )
                self.rows.append(row)
                self.by_user.setdefault(user, []).append(observation)
                self.evidence_for_user.setdefault(user, set()).add(eid)

    def observations_for(self, user_id: str) -> List[model.Observation]:
        return list(self.by_user.get(user_id, []))

    def evidence_ids_for(self, user_id: str) -> set:
        return set(self.evidence_for_user.get(user_id, set()))
