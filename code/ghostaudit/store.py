"""Evidence manifest loading and the shared observation store."""

from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List

from . import model
from .evidence import collect_trace_owners, parse_evidence_file
from .identity import IdentityIndex


@dataclass
class EvidenceFile:
    file_id: str
    path: Path
    rel_path: str
    origin: str
    captured_at: str


def load_manifest(dataset_dir: Path) -> Dict[str, EvidenceFile]:
    out: Dict[str, EvidenceFile] = {}
    with (dataset_dir / "evidence_files.csv").open(encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            rel = row["file_path"]
            out[row["file_id"]] = EvidenceFile(
                file_id=row["file_id"],
                path=dataset_dir / rel,
                rel_path=rel,
                origin=row["origin"],
                captured_at=row["captured_at"],
            )
    return out


def parse_all(manifest: Dict[str, EvidenceFile], identities: IdentityIndex) -> List[model.Observation]:
    """Parse every manifest entry with cross-file trace propagation.

    Pass 1 gathers trace owners from every log file (a trace owner recorded in
    any file lets rows that only carry the trace id be linked in any other
    file).  Pass 2 parses each file with that shared ownership map.
    """
    log_files = [manifest[fid] for fid in sorted(manifest) if manifest[fid].path.suffix.lower() == ".log"]
    global_owners: Dict[str, Tuple[str, str, str]] = {}
    for entry in log_files:
        if not entry.path.exists():
            continue
        for trace, owner in collect_trace_owners(entry.path, identities).items():
            if trace in global_owners and global_owners[trace][0] != owner[0]:
                global_owners[trace] = ("AMBIGUOUS", "conflict", trace)
                continue
            global_owners.setdefault(trace, owner)
    global_owners = {
        trace: owner
        for trace, owner in global_owners.items()
        if owner[0] != "AMBIGUOUS"
    }

    observations: List[model.Observation] = []
    for file_id in sorted(manifest):
        entry = manifest[file_id]
        if not entry.path.exists():
            continue
        observations.extend(
            parse_evidence_file(
                entry.path,
                entry.file_id,
                entry.captured_at,
                identities,
                global_trace_owners=global_owners,
            )
        )
    return observations
