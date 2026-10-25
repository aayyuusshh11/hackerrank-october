# Ghost Data Still Exists — usage report

This report describes the billable model usage for the final full-dataset run
that produced the repository-root `output.csv`.

## Final full-dataset run

- Command: `python code/main.py`
- Input dataset: `dataset/deletion_requests.csv`
- Output file: `output.csv`
- Rows produced: 518
- Deterministic: yes
- Network access: no
- API keys or credentials: none

## Runtime model usage

| Model | Provider | Calls | Input tokens | Output tokens | Total tokens | Estimated cost |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| _None_ | None | 0 | 0 | 0 | 0 | $0.000000 |

The submitted solution is deterministic and offline. It does **not** call a
large language model, vector database, embedding pipeline, vision model or any
other generative model at evaluation time. That the full-dataset run has zero
runtime model calls is intentional and reproducible.

## Per-request average

- Total input tokens: 0
- Total output tokens: 0
- Total tokens: 0
- Requests processed: 518
- Average tokens per request: 0
- Average cost per request: $0.000000

## Per-model breakdown

- No models are used.
- Total per-model call count: 0.
- Total per-model input token count: 0.
- Total per-model output token count: 0.
- Total per-model token count: 0.
- Total per-model estimated cost: $0.000000.

## Notes

- Any code-generation, analysis or human engineering that happened before the
  final run is outside the scope of this usage report.
- No API key, token, cookie, private key, credential, or sensitive
  configuration value is included anywhere in this file.
- The path from dataset readback to remediation and verification is entirely
  deterministic.
