# Load test: POST /v1/payments (P1-5)

Script: [`ops/perf/payments.js`](../../ops/perf/payments.js) (k6 1.2.3, `constant-arrival-rate`, 90 s).
Run: `make perf` (optionally `RATE=<req/s> make perf`). k6 runs inside the compose network against
`gateway-api:8000`. Each run writes `docs/perf/k6-summary.json`; the two runs below were renamed after the run.

Target (NFR-PERF): p99 < 2 s, error rate < 1 %. A 402 decline counts as a correct answer, not an error.

## Environment (read before quoting the numbers)

- One laptop (Apple silicon), Docker Desktop VM of about 7.8 GB **shared by about 14 compose stacks** of
  parallel workers, several of them running test suites while this ran. Numbers are noisy and pessimistic.
- One gateway process (single uvicorn worker), local Postgres 16 and Redis 7, simulator acquirer.
- Card data: only the 5 allowlisted test PANs are accepted (`gw/payments/tokenization/cards.py`); the script
  draws from 3 of them. Every charge on one card takes the per-card advisory lock in
  `gw/payments/risk.py::_card_activity` and holds it until its transaction ends. So all load lands on
  3 hot cards and charges on the same card are serialised. That is a worst case that real traffic (many
  distinct cards) does not have.

## Results (2026-11-05, measured once each)

| Run | Offered rate | Requests completed | p50 | p95 | p99 | max | Error rate | p99 < 2 s |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| [rate40](k6-summary-rate40.json) | 40 req/s | 745 (2,856 iterations dropped: 200 VUs exhausted) | 24.9 s | 41.4 s | 44.5 s | 45.1 s | 0.00 % | No |
| [rate2](k6-summary-rate2.json) | 2 req/s | 181 (none dropped) | 405 ms | 1.91 s | 2.82 s | 2.90 s | 0.00 % | No |

## Reading

- No request failed (0 % errors in both runs), and none was answered 5xx.
- The latency target is **not met** in this environment. At 40 req/s the service saturates at about
  7 completed req/s: requests queue behind the per-card lock on 3 cards, and latency grows to tens of
  seconds. At 2 req/s the median is 405 ms, but the tail (p95 1.9 s, p99 2.8 s) is above target on the
  shared VM.
- What this does and does not show: it is a hot-card contention test on an overloaded laptop. It does not
  measure the production design (many cards, horizontal gateway replicas, see
  [Design P2-7](../design.md#p2-7-100k-tps-topology)). A clean measurement needs a dedicated machine and a load mix with
  many distinct (tokenised) cards. That is not possible today because only 5 test PANs are allowlisted.
