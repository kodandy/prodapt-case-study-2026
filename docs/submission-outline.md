# Submission outline (scope anchor, 2026-11-03)

Draft. This file fixes what the submission claims, what it does not, and what the demo shows. The design doc, the
diagram, the README and the slides are written from it. Agreed with Sol on 2026-11-03
(`~/.claude/logs/consults/20261103-145215-consult.md`). Open scope decisions were settled on 2026-11-03 (section 7).

## 1. One-line goal

When a payment fails, is declined, looks fraudulent or settles differently than expected, a merchant or an ops agent
gets the cause, the evidence and a suggested fix, while the payment path stays correct (no double charge, balanced
ledger, no card numbers stored) and keeps working when the LLM is down.

## 2. Coverage of the 13 Basic items

Depth: **Demo** = runs in `make demo` and is covered by tests. **Thin** = minimal implementation with tests, not in the
live walkthrough. **Design** = described in `docs/design.md` only; reported as a gap.

| Item | Today (11-03) | Target by 11-05 | Depth |
|---|---|---|---|
| B1 accounts, MFA, wallets | Sign-up (argon2id) + wallet account | Login, TOTP enrol/verify, wallet balance read | Thin |
| B2 P2P + merchant payments, multi-currency | Merchant card charge (tokenized, idempotent, ledger) | + P2P wallet transfer, per-currency wallets; FX conversion **Design** | Demo (card) / Thin |
| B3 history, refunds, disputes | — | `GET /v1/payments`, full/partial refund; dispute state machine (`opened → under_review → won | lost`) | Thin |
| B4 NL failure-reason detection | — | Rules-first taxonomy + suggested fix, through SQ1 | Demo |
| B5 RAG support assistant | Assistant with evidence, T2 fallback | Retrieval of incidents + transactions, grounded answer | Demo |
| B6 hybrid search | Postgres FTS | + pgvector, RRF fusion; default embedder is the local feature hash (spelling-level, not semantic), OpenAI embeddings optional via `OPS_AI_EMBEDDER=openai` + backfill | Thin |
| B7 fraud scoring + flagging | — | Rule score at charge; card-testing detector (SQ3) | Demo |
| B8 REST/gRPC, validation, idempotency | REST charge, validation, idempotency | REST history/refund; gRPC **Design** | Demo (REST) |
| B9 tool calling | — | Read-only tools per SQ (`aggregate_failures`, `get_payment_status`, `analyze_fraud`, `explain_settlement`, `search_incidents`) | Demo |
| B10 memory + session | — | Redis session (last N turns, tenant-owned); long-term memory **Design** | Thin |
| B11 notifications | Outbox to stream | Payment confirmation + suspicious-activity alert records | Thin |
| B12 ticket routing | — | Rule router to `{fraud, payments_ops, settlement, compliance}` | Thin |
| B13 front end | — | Minimal merchant/ops screen (assistant + evidence + payments list), built 11-05 | Thin |

## 3. Explicitly not in this submission

- Real money movement, real card networks, real KYC. A simulator and seeded records stand in for them.
- SQ2: the platform investigates a cross-border transfer **recorded by an external transfer system**. It does not
  execute transfers or make KYC decisions. The record carries what is needed to tell insufficient funds apart from a
  KYC limit: amount and currency, the applied limit, the running total at decision time, the decline code, the rule id
  and the sender balance.
- The 12k historical records are seeded into ops-ai through the same projection function the stream consumer uses.
  They are not replayed through the gateway, so they have no ledger rows. `ingest_source` (`seed` / `live`) records
  how a record arrived. That is a different thing from which business system it came from.
- Not built: reconciler and lease reclaim, separate DB roles, DB-level ledger invariants, rate limits, key revocation,
  gRPC, long-term memory, Grafana dashboards, multi-region. Each is covered in the design doc with the reason it was
  cut and how it would be built.
- Observability is limited to structured logs, a correlation id and `/healthz` + `/readyz`.
- Without `OPENAI_API_KEY`, answers are evidence only (T2) and labeled that way. The OpenAI adapter is tested against
  a fake HTTP server. That is not a test of the real connection, which runs once the key arrives.

## 4. Demo script (`make demo`)

Starts with no API key. Re-running it does not duplicate seed data. It waits until the consumer has caught up.

1. `docker compose up` brings up postgres, redis, gateway-api, relay, ops-ai-api and ops-ai-consumer, then waits for
   `/readyz`.
2. The seed step loads the 12k-record dataset and 5 incidents into ops-ai.
3. The live step: sign-up, merchant and API key, then three charges over HTTP:
   - one approved charge;
   - one declined charge on the SQ1 BIN;
   - one retry with the same `Idempotency-Key`, which returns the stored response and does not charge again.
4. The demo waits until the consumer has caught up with the stream.
5. It asks SQ1 to SQ4. Each answer prints:
   - the cause label;
   - the key numbers (exact counts and denominators);
   - the cited evidence ids;
   - the tools that were called.
   For SQ1, the live declined charge appears in the evidence as the increment over the seed.

Golden checks (E2E):
- The seed is checked at a fixed reference time with exact counts, denominators and rates.
- The live increment is checked separately.
- Evidence is checked by whether each cited record supports the claim, not by matching a memorized id set.

## 5. Document and slide skeleton

`docs/design.md` (DL2), with every decision written as Decision / Why / What we gave up / Alternatives:
1. Problem and users
2. Architecture: two services, outbox, stream, projection (the diagram, DL1)
3. Payment correctness: idempotency, ledger, tokenization, unknown outcomes
4. Operations intelligence: tools-first answers, LLM as summarizer, T2 fallback, evidence contract
5. Security: PAN handling, argon2id + TOTP, secrets, tenant scope, LLM input masking
6. What was cut and the production path (P2: GCP mapping, 99.999 %, PCI scope, 100k TPS)

`docs/presentation.md` (DL4, 10 min):
- 0:30 goal
- 1:00 architecture
- 4:00 SQ1–SQ4 live
- 3:00 production design: idempotency under concurrency, failure spike to answer, what is demonstrated vs. design-only
- 0:30 wrap-up
- 1:00 buffer

## 6. Schedule

- **11-03**: this outline; compose services + `make demo` with readiness; SQ1 vertical.
- **11-04**: SQ2–SQ4; evidence checks; OpenAI adapter against a fake HTTP server; the lost-response retry check. Thin
  Basic items, delegated in parallel where they do not overlap.
- **11-05**: feature freeze. A clean-clone run from the README only. Design doc, diagram, slides and a recorded demo.
  UI if time remains.
- **11-06**: submission check, rehearsal, reviewer invite.

## 7. Scope decisions (settled 2026-11-03)

1. B13: build a minimal merchant/ops screen. B13 is a Basic item, so an API-only submission would leave it unmet.
2. B2 FX: per-currency wallets only. FX conversion legs are described in the design doc. A balanced multi-currency
   ledger test does not fit in the time left.
3. B3 disputes: implement the state machine (Thin). It lives in the same layer as refunds, so it costs little extra.
