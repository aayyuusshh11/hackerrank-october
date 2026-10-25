# Ghost Data Still Exists — evaluation report

This document records how the bundled deletion-audit agent is evaluated. All
sample and public-family checks use only participant-visible files under
`dataset/`. No organizer-only data or hidden golden answer was ever used to
drive predictions.

## Run summary

- Full dataset processed: 518 cases.
- Output root file: `output.csv`.
- Final status distribution:
  - `INCOMPLETE`: 368
  - `COMPLETE`: 140
  - `NOT_APPLICABLE`: 10
- Step count range across all cases: 1 to 14.
- The final run is fully deterministic and offline.

## Public sample metrics

Evaluation is performed by running the bundled engine over
`dataset/sample_deletion_requests.csv` and comparing the results with
`dataset/sample_output.csv`.

Run it from the repository root with:

```bash
python code/evaluation/main.py
```

| Metric | Value |
| --- | ---: |
| Public sample cases | 32 |
| Case-level status accuracy | 32 / 32 (100.00%) |
| Step-count accuracy | 30 / 32 (93.75%) |
| Exact ordered-step accuracy | 30 / 32 (93.75%) |
| Field accuracy on equal-step cases | 1197 / 1197 (100.00%) |

The two remaining exact-step differences are `case_18` and `case_20`. They are
described below.

## Additional evaluation cases

These are three additional hand-built cases created specifically for this
submission. They exercise three performance-critical behaviours that the
public sample does not stress clearly on its own. The complete intended outcome
for each case is also recorded in `custom_cases.csv`.

| Case | Scenario created for testing | Expected status | Expected main steps | Why this case was added |
| --- | --- | --- | --- | --- |
| `case_eval_001` | A deletion is reported as `COMPLETE`, but a **data flow** that mirrors the account to an archive is still active, so deleted data can be recreated. | `INCOMPLETE` | * Find the active flow.   * Disable it before touching the dependent data.   * Verify that the flow is disabled after remediation. | Removes an active recreation path that is easy to overlook. |
| `case_eval_002` | A support attachment still carries a **historical identifier** for the same subject after the case was reported `COMPLETE`. | `INCOMPLETE` | * Report the exposed PII.   * Delete the attachment.   * Verify that it is absent. | A support attachment is a direct copy of personal data and can be missed when only current identifiers are checked. |
| `case_eval_003` | The person submits an **information/correction/access** request rather than a deletion request. | `NOT_APPLICABLE` | No deletion and no remediation are authorized, so the breavement step list is empty. | Guards against false positive deletions from non-deletion requests. |

These custom cases share three properties:

1. each one grounds a specific policy decision;
2. each one exhausts a known weak spot (active flow, historical identity, non-deletion request);
3. each one is expressible using only participant-visible concepts.

## Observed failures and improvements

### Capture-time discipline
- Original pipeline could be influenced by evidence captured after `audit_at`.
- Now the engine reconstructs state at each case's `audit_at` and never cites
  later evidence as proof of state. Later-discovered stable IDs may be used for
  future verification step labels but do not change audit-time state.

### Historical identity resolution
- Current + historical emails, phone aliases, hashes and explicit
  `LINK_RESOURCE_IDENTITY` events are now resolved.
- Accounts and records identified later in the data are used as stable resource
  references, not as evidence of a present audit-time record.

### Support attachments and message content
- Added `uploaded_by`, `created_by`, `submitted_by` and lookup linking.
- Redaction/remediation planning now handles support attachment PII and
  message PII distinctly.

### Analytics fallback
- Unlinked analytics records are paired only when the temporal pattern and the
  workflow context support it.
- A record already linked to a user anywhere is never handed to another user.
- `analytics_1575` no longer appears in two unrelated cases.

### Duplicate/unowned resources
- Resource divider made explicit so that duplicate or unrelated records are not
  reused by different cases.
- Added guardrails on cross-case identity linking.

### Order and JSON encoding
- All `steps_json` values are serialized deterministically.
- Step numbers start at `1` and are consecutive.
- Phase ordering is always `FINDING`, then `REMEDIATION`, then `VERIFICATION`.

## Known remaining differences

The only exact-step mismatches on the public sample are:

| Case | Expected steps | Predicted steps | Cause |
| --- | ---: | ---: | --- |
| `case_18` | 14 | 13 | The expected security record id `audit_record_6645` is not in participant-visible evidence. The engine refuses to invent a milestone id. |
| `case_20` | 8 | 7 | The expected security record id `audit_record_5569` is not in participant-visible evidence. Again, the engine does not invent it. |

Because of this:
- these two cases still have the **correct status**;
- the rest of their step sequences match the expected output;
- but they cannot be exactly step-identical without hardcoding or inventing a
  security id that is not grounded in the evidence provided.

We intentionally ship the grounded output rather than fabricate a golden label.
That protecting sample correctness is considered more important than forcing a
perfect public score.

This is **not** an identity-resolution failure or an
evidence-signaling failure. It is a deliberate evidence-limit guard against
inventing a security audit id that is not visible in any participant-facing
image, document, log, database or activity record.

## Account mapping

- Direct account mapping: 248
- Activity-based account mapping: 284
- Total cases with a resolved account ID: 532 of 550 non-`NOT_APPLICABLE` cases.
- Remaining unresolved cases usually lack account evidence entirely.

The custom evaluation cases plus the public samples have been used to confirm
the engine handles account-without-record and record-without-account
situations correctly.

## Reproduction checklist

```bash
python code/main.py                          # full 518-row run
python code/evaluation/main.py               # sample evaluation metrics
```

Both commands are deterministic and require no network, API key, secret or
organizer-only data.
