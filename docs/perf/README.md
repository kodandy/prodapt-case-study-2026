# Load test: POST /v1/payments (P1-5)

Script: [`ops/perf/payments.js`](../../ops/perf/payments.js) (k6 1.2.3, `constant-arrival-rate`, 90 s).
Run: `make perf` (optionally `RATE=<req/s> make perf`). k6 runs inside the compose network against
`gateway-api:8000`. Each run writes `docs/perf/k6-summary.json`; the runs below were renamed after each run.

Target (NFR-PERF): p99 < 2 s, error rate < 1 %. A 402 decline counts as a correct answer, not an error.

## Environment (read before quoting the numbers)

- One laptop (Apple silicon), Docker Desktop VM of about 7.8 GB **shared by about 14 compose stacks** of
  parallel workers, several of them running test suites while this ran. Numbers are noisy and pessimistic.
- Gateway: one uvicorn process in the runs up to 2026-11-06; four uvicorn workers (the committed compose) from
  2026-11-07. Local Postgres 16 and Redis 7, simulator acquirer.
- Card data: only the 5 allowlisted test PANs are accepted (`gw/payments/tokenization/cards.py`); the script
  draws from 3 of them. Charges on one card take the per-card advisory lock in `gw/payments/risk.py::card_activity`,
  but only inside the short claim transaction; it is released before the provider call. The 40 req/s re-runs
  below show this lock is not the bottleneck.

## Results (2026-11-05, measured once each)

| Run | Offered rate | Requests completed | p50 | p95 | p99 | max | Error rate | p99 < 2 s |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| [rate40](k6-summary-rate40.json) | 40 req/s | 745 (2,856 iterations dropped: 200 VUs exhausted) | 24.9 s | 41.4 s | 44.5 s | 45.1 s | 0.00 % | No |
| [rate2](k6-summary-rate2.json) | 2 req/s | 181 (none dropped) | 405 ms | 1.91 s | 2.82 s | 2.90 s | 0.00 % | No |

## Re-run on a less loaded VM (2026-11-06, measured once)

Same script and gateway, `RATE=2 make perf`, against a fresh `make demo` stack. The VM held 3 other compose stacks
(two idle demo stacks and one running the web CI at about 120 % CPU) instead of about 14.

| Run | Offered rate | Requests completed | p50 | p95 | p99 | max | Error rate | p99 < 2 s |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| [rate2-quiet](k6-summary-rate2-quiet.json) | 2 req/s | 181 (none dropped) | 72 ms | 524 ms | 1.25 s | 1.32 s | 0.00 % | Yes |

## 40 req/s re-run: one process vs four workers (2026-11-06, measured once each)

Same VM state as the 2 req/s re-run, `RATE=40 make perf` against a fresh `make demo` stack. The second run differs
only in `--workers 4` on the gateway-api uvicorn command, set by a local compose override that is **not** in the
repository (at that time the committed compose ran one process).

| Run | Gateway | Offered rate | Requests completed | p50 | p95 | p99 | max | Error rate | p99 < 2 s |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| [rate40-quiet](k6-summary-rate40-quiet.json) | 1 process (then committed) | 40 req/s | 3,044 (556 iterations dropped) | 3.98 s | 11.96 s | 13.77 s | 15.63 s | 0.00 % | No |
| [rate40-workers4](k6-summary-rate40-workers4.json) | 4 uvicorn workers (override) | 40 req/s | 3,601 (none dropped) | 81 ms | 314 ms | 564 ms | 1.03 s | 0.00 % | Yes |

During the one-process run the gateway-api container sat at about 100–130 % CPU (sampled with `docker stats`
every 10 s): one Python process is CPU-bound. With four workers the same three hot cards met the target, so the
per-card lock was not what limited throughput.

Four workers became the committed default on 2026-11-07 (next section). Before that, gateway metrics lived in each
process's own registry, so a scrape of `/metrics` would have seen one worker's counters.

## 40 req/s with the committed four workers (2026-11-07, measured once)

`compose.yaml` now starts gateway-api with `--workers ${GATEWAY_WORKERS:-4}` and `PROMETHEUS_MULTIPROC_DIR`, so
`/metrics` sums every worker (`gw/observability/__tests__/test_metrics_multiprocess.py`). `RATE=40 make perf`
against a fresh `make demo` stack, no override. The VM held 19 containers of other compose stacks.

| Run | Gateway | Offered rate | Requests completed | p50 | p95 | p99 | max | Error rate | p99 < 2 s |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| [rate40-committed](k6-summary-rate40-committed.json) | 4 uvicorn workers (committed) | 40 req/s | 3,601 (none dropped) | 37 ms | 110 ms | 338 ms | 616 ms | 0.00 % | Yes |

gateway-api CPU (`docker stats`, every 10 s) ranged from 28 % to 110 %, below the four cores the workers can use.
After the run, four scrapes of `/metrics` each reported 3,616 `POST /v1/payments` requests (3,601 from k6 plus 15
from the demo), whichever worker answered. Each worker keeps its own circuit breaker; the circuit-state gauge reports
the worst live worker.

## Reading

- No request failed (0 % errors in every run), and none was answered 5xx.
- At 2 req/s the target is **met** on the less loaded VM (p99 1.25 s); the 2.82 s of the first run came from the
  shared VM. At 40 req/s one gateway process did **not** meet it (p99 13.77 s, CPU-bound); the committed
  compose with four uvicorn workers does (p99 338 ms on 2026-11-07, 564 ms in the 2026-11-06 override run).
- What this does and does not show: it is a 3-hot-card test on a shared laptop. It does not
  measure the production design (many cards, horizontal gateway replicas, see
  [Design P2-7](../design.md#p2-7-100k-tps-topology)). A clean measurement needs a dedicated machine and a load mix with
  many distinct (tokenised) cards. That is not possible today because only 5 test PANs are allowlisted.
