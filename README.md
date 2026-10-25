# Ghost Data Still Exists — offline deletion-audit agent

This solution audits whether personal data was actually removed. For each
request it returns:

- `NOT_APPLICABLE` for a non-deletion request;
- `COMPLETE` for an evidence-backed, fully-remediated deletion; or
- `INCOMPLETE` with the exact findings, ordered remediation plan and future
  verification checks.

The final run is deterministic and offline. It requires no API key, no network
access, and no organizer-only data.

## How to run

From the repository root:

```bash
python code/main.py
```

This will read only `dataset/` and write the finished `output.csv` at the
repository root. There are no external dependencies beyond the Python standard
library.

To generate the sample evaluation metrics:

```bash
python code/evaluation/main.py
```

This deterministic run-inspection run compares the engine on the public sample
cases (`dataset/sample_deletion_requests.csv`) against the public sample
answers (`dataset/sample_output.csv`) and reports status accuracy, step-count
accuracy, ordered-step accuracy and field accuracy.

## Architecture snapshot

- `code/main.py` — request classification, full-dataset orchestration and
  deterministic `output.csv` writing.
- `code/ghostaudit/identity.py` — current + historical identity resolution.
- `code/ghostaudit/store.py` — evidence manifest loading and parsing.
- `code/ghostaudit/evidence.py` — evidence extraction, subject resolution,
  content linking, capture-time filtering and state inference.
- `code/ghostaudit/accounts.py` — account-resource linking, stable account ids
  and activity clustering.
- `code/ghostaudit/resources.py` — per-case evidence routing, primary readbacks,
  image/PDF facts, analytics fallback and reconciliation.
- `code/ghostaudit/planner.py` — deterministic finding/remediation/verification
  progression and step ordering.

The engine intentionally does not trust workflow reports. Every assertion comes
from participant-visible evidence.

## Output contract

`output.csv` has exactly these columns, in this order:

```text
case_id,deletion_status,steps_json
```

- Exactly 518 data rows.
- Rows are ordered by the exact `case_id` string, ascending.
- `deletion_status` is `COMPLETE`, `INCOMPLETE`, or `NOT_APPLICABLE`.
- `steps_json` is a valid JSON array.
- `NOT_APPLICABLE` cases use `[]`.
- Step numbers start at `1` and are consecutive.
- Phase order is always `FINDING` → `REMEDIATION` → `VERIFICATION`.

For an `INCOMPLETE` case, future remediation and verification steps use an empty
`evidence_id` because they have not yet happened. A `COMPLETE` case contains
only evidence-backed verification steps proving the current compliant state.

## Evaluation files

Bundled evaluation reports live under `code/evaluation/`:

- `evaluation_report.md`
- `usage_report.md`
- `custom_cases.csv`
- `evaluate_samples.py`
- `main.py`

Duplicate copies of the report/metric files also exist under `evaluation/` so
the submission is understandable whether you inspect the archive from the
`code.zip` root or from the extracted packaging path.

## Packaging

Create `code.zip` from the repository root using:

```powershell
Compress-Archive -Path README.md, code, evaluation -DestinationPath code.zip -Force
```

```bash
zip -r code.zip README.md code evaluation \
  -x "*/__pycache__/*" "*/.venv/*" "*/venv/*" "*/node_modules/*" "*/.git/*" "*/.DS_Store"
```

Do **not** include `dataset/`, any `data/` corpus folder, virtual environments
such as `.venv`, `node_modules`, build caches, or nested archive files.
`output.csv` is not shipped in `code.zip`; it is a separate submission artifact.

## Compliance statement

- Only participant-facing files under `dataset/` are read.
- No organizer-only truth, sprint labels, hidden golden labels, or private
  generator internals are used.
- No API keys, tokens, credentials, image binaries, datasets, or personal data
  are shipped in `code.zip`.
- The generated answers do not hardcode sample answers.
- `case_18` and `case_20` are the only public sample cases whose step sequences
  cannot be made exact without inventing a participant-invisible security
  record id. The engine intentionally refuses to do that.
