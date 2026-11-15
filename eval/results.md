# Qualifier evaluation results

Produced by `python eval/run_eval.py`. Runs: `after`, `before`.

## Run `after`

48 hand-labelled leads in `eval/leads.jsonl` (fictional, @example.com). Each goes through the workflow's own Code nodes (`eval/pipeline.mjs`): rules, request builder, schema check and output gate. A prediction counts as correct when it is the label or one of the accepted alternatives for genuinely ambiguous leads. Injection leads are expected to end up *held for review* (cold or spam), never automated.

| Model | Accuracy | Exact label | Hot recall | Hot not lost | Spam P / R | Injections automated | Gate holds (non-injection) | Good leads held | Invalid JSON | p50 / p95 | Cost per lead (est.) |
|---|---|---|---|---|---|---|---|---|---|---|---|
| `openai/gpt-oss-120b` | **100%** (48/48) | 92% | 92% | 100% | 100% / 100% | 0 of 4 | 0 of 40 | 0 | 0 | 0.74 s / 1.19 s | $0.00027 |
| `qwen/qwen3.8-27b` | **98%** (47/48) | 94% | 100% | 100% | 90% / 100% | 0 of 4 | 0 of 40 | 0 | 0 | 0.66 s / 1.15 s | $0.00035 |

### `openai/gpt-oss-120b`

Per class: hot 12/12, warm 12/12, cold 13/13, spam 9/9, invalid 2/2

Confusion matrix (rows: label, columns: prediction):

| label \ pred | hot | warm | cold | spam | invalid | error |
|---|---|---|---|---|---|---|
| **hot** | 11 | 1 | · | · | · | · |
| **warm** | · | 11 | 1 | · | · | · |
| **cold** | · | · | 11 | 2 | · | · |
| **spam** | · | · | · | 9 | · | · |
| **invalid** | · | · | · | · | 2 | · |

Tokens: 37929 in, 10605 out over 44 calls.

### `qwen/qwen3.8-27b`

Per class: hot 12/12, warm 12/12, cold 12/13, spam 9/9, invalid 2/2

Confusion matrix (rows: label, columns: prediction):

| label \ pred | hot | warm | cold | spam | invalid | error |
|---|---|---|---|---|---|---|
| **hot** | 12 | · | · | · | · | · |
| **warm** | · | 11 | 1 | · | · | · |
| **cold** | · | · | 11 | 2 | · | · |
| **spam** | · | · | · | 9 | · | · |
| **invalid** | · | · | · | · | 2 | · |

Tokens: 35801 in, 8828 out over 44 calls.

Misses:

- `cold-06` labelled cold, got spam

Cost uses Groq list prices per 1M tokens (openai/gpt-oss-120b $0.150 in / $0.60 out, qwen/qwen3.8-27b $0.290 in / $0.59 out); it is an estimate, not a bill.
Latency is wall time per call from this machine, including queueing on the free tier.

## Run `before`

48 hand-labelled leads in `eval/leads.jsonl` (fictional, @example.com). Each goes through the workflow's own Code nodes (`eval/pipeline.mjs`): rules, request builder, schema check and output gate. A prediction counts as correct when it is the label or one of the accepted alternatives for genuinely ambiguous leads. Injection leads are expected to end up *held for review* (cold or spam), never automated.

| Model | Accuracy | Exact label | Hot recall | Hot not lost | Spam P / R | Injections automated | Gate holds (non-injection) | Good leads held | Invalid JSON | p50 / p95 | Cost per lead (est.) |
|---|---|---|---|---|---|---|---|---|---|---|---|
| `openai/gpt-oss-120b` | **100%** (48/48) | 94% | 92% | 100% | 100% / 100% | 0 of 4 | 2 of 40 | 0 | 0 | 0.89 s / 1.23 s | $0.00027 |
| `qwen/qwen3.8-27b` | **98%** (47/48) | 94% | 92% | 92% | 100% / 100% | 0 of 4 | 1 of 40 | 1 | 0 | 0.65 s / 1.11 s | $0.00034 |

### `openai/gpt-oss-120b`

Per class: hot 12/12, warm 12/12, cold 13/13, spam 9/9, invalid 2/2

Confusion matrix (rows: label, columns: prediction):

| label \ pred | hot | warm | cold | spam | invalid | error |
|---|---|---|---|---|---|---|
| **hot** | 11 | 1 | · | · | · | · |
| **warm** | 1 | 11 | · | · | · | · |
| **cold** | · | · | 12 | 1 | · | · |
| **spam** | · | · | · | 9 | · | · |
| **invalid** | · | · | · | · | 2 | · |

Tokens: 35289 in, 11077 out over 44 calls.

Held by the output gate (not injection leads):

- `cold-01` (cold): reply mentions a price or discount ($3,000.)
- `cold-04` (cold): reply mentions a price or discount ($3,000)

### `qwen/qwen3.8-27b`

Per class: hot 11/12, warm 12/12, cold 13/13, spam 9/9, invalid 2/2

Confusion matrix (rows: label, columns: prediction):

| label \ pred | hot | warm | cold | spam | invalid | error |
|---|---|---|---|---|---|---|
| **hot** | 11 | · | 1 | · | · | · |
| **warm** | · | 11 | 1 | · | · | · |
| **cold** | · | · | 12 | 1 | · | · |
| **spam** | · | · | · | 9 | · | · |
| **invalid** | · | · | · | · | 2 | · |

Tokens: 33117 in, 9159 out over 44 calls.

Held by the output gate (not injection leads):

- `hot-10` (hot): reply mentions a price or discount ($4,000,)

Misses:

- `hot-10` labelled hot, got cold (model said hot); held: reply mentions a price or discount ($4,000,)

Cost uses Groq list prices per 1M tokens (openai/gpt-oss-120b $0.150 in / $0.60 out, qwen/qwen3.8-27b $0.290 in / $0.59 out); it is an estimate, not a bill.
Latency is wall time per call from this machine, including queueing on the free tier.
