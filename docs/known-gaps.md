# Security notes and known gaps

## Security notes

- Only the allow-listed test card numbers are accepted. Full card numbers are never stored, logged or published.
- Passwords are hashed with argon2id. API keys are stored as HMAC-SHA256 hashes.
- Idempotency: the DB row keyed by (merchant, key) is the source of truth, and a Redis lock is only an optimisation. A retry
  returns the stored response. The same key with a different body gives 422, and an in-flight request gives 409. An
  unknown provider outcome is stored as 202 and is never reported as failed.

## Known gaps

The platform-level open gaps (reconciler, k6 p99, gRPC TLS in the demo, `/metrics` exposure, the interim ops admin
token, provider breaker accounting, FX chargebacks, R2 partials, a load-sensitive test) are collected in
[production-readiness.md](production-readiness.md#known-gaps). The list below started from the Day 1 audit
and is kept current per item:

- A reconciler that resolves `unknown` payments with `provider.lookup` and reclaims expired attempt leases. Until then, a
  crash between the provider call and finalization leaves that key answering 409.
- Database roles: the gateway API and gRPC server run as `gateway_app` through `SET ROLE` (`GATEWAY_DB_ROLE`), which
  has no UPDATE, DELETE or TRUNCATE on `audit_log` (P0-10). The compose demo still logs in as the Postgres superuser,
  so code with SQL access can `RESET ROLE`; the relay, notifier and ops-ai do not use a restricted role.
- DB-level ledger invariants (unique journal per reference, balanced postings per currency).
- API key revocation, `maxlen` on the stream, and structured logging. (Rate limiting exists: token buckets per API
  key and per user, see P1-4 in the traceability matrix.)
- The consumer dead-letters a malformed entry (see "Stream" below) but has no retry with backoff for other failures.
- Merchant tokens cannot be revoked before they expire (15 minutes), and there is no key rotation (`kid`) yet.
- `demo-secrets` recreates only missing files. If only the private key is deleted, the old public key stays and tokens
  stop verifying; drop the volumes (`make demo-down && docker compose --profile demo down -v`, which also drops the
  demo database) to start over.
- Both runtime engines set `hide_parameters=True` (`services/gateway/src/gateway/db.py`,
  `services/ops-ai/src/ops_ai/db.py`), but only the gateway has a test for it
  (`gateway/__tests__/test_db.py`); the Alembic `migrations/env.py` engines of both services do not set it.
- RBAC (P0-9): routes merged after the policy table was fixed (wallets, top-ups, payouts, consumer payments,
  disputes, notifications) reuse an existing action with the same role set instead of their own action names, so the
  policy cannot yet grant them separately. The generated route × role matrix checks every route with exact 401/403.
- Redis has no ACL and every process uses the same Postgres superuser. Anyone who can reach Redis can append forged
  payment events. The consumer refuses to move a payment to another merchant, but it can still record payments that
  never happened. Fix: per-service Redis ACL users and DB roles with schema grants.
- No request body size limit in front of the APIs (oversized bodies are rejected only after they are read).
- The token verifier checks `iss`/`aud`/`exp`/`iat` (`iat` at most 30 s ahead, lifetime at most 900 s), exactly one
  canonical `merchant_id`/`user_id`, the role list, and `sub` on user tokens, but does not check `sub` on merchant
  tokens yet.
- `POST /v1/sessions` and `POST /v1/user-tokens` lock an account for 15 minutes after 10 wrong passwords in 15
  minutes (429 with Retry-After), but there is no per-IP limit, so guesses spread across many accounts are not
  slowed, and anyone who knows an email can lock that account out. A locked account answers 429 where an unknown
  email answers 401, so a lockout reveals that the email exists. Unknown email and wrong password otherwise cost the
  same argon2 verification and give the identical 401. The number of concurrent argon2 verifications is capped (4
  per app; a request that waits more than 2 s gets 503). The routes are sync, so a request waiting for a slot holds a
  threadpool worker; a login flood slows other sync routes. Fix: async route with an AnyIO `CapacityLimiter`, plus a
  per-IP rate limit. `POST /v1/signup` hashes with no cap.
- `POST /v1/signup` answers 409 for a registered email, which reveals that the email exists. This bypasses the
  identical 401 on login.
- No password rehash on login when the argon2 parameters change.
- `GET /v1/me/transfers` masks the counterparty email to its first character and domain, but does not strip
  control or bidi characters (such as RLO) from that first character. Its 401 has no `Cache-Control: no-store`
  (the same as `/v1/me/wallets`), and only the recipient side has a `(user, created_at)` index, so a large sent
  history is sorted in full. `POST /v1/me/transfers` still returns the recipient's `recipient_user_id` on 201, and
  its 404 `recipient_not_found` reveals whether an email is registered (like the signup 409).
- Web client: `/healthz` and
  `/readyz` are reachable through nginx (the smoke test uses them). A user-side 401 or enabling TOTP still clears the
  merchant credentials too. The api client has no single-flight on the ops token refresh, does not validate response
  shapes, and there is no error boundary. The nginx container runs with default privileges, and the login form has
  no password-manager hints.
- Accepted risk (dev only): `npm audit` reports GHSA-g3hv-75wg-m249 (minimatch ReDoS) through eslint and
  typescript-eslint. minimatch only reads the repo's own lint globs and is not in the production bundle (which has
  react and react-dom only). `npm audit fix --force` would downgrade eslint, so this waits for an upstream eslint major.
- `bootstrap_user` replaces the password of an existing id, so it is for the demo database only.
- The OpenAI adapter is in place and runs only when `OPENAI_API_KEY` is set, but output guards are not (no split or
  structuring advice, no numbers absent from the evidence, and cited `[ref]`s are not checked against the evidence
  refs). Before sending it masks Luhn-valid card numbers, strips evidence tags and caps sizes, but sends no less data
  than that. It has been tested against a local fake server only, not the real API.
- RAG `/v1/assistant/ask` ties each sentence to a retrieved source only with `?grounded=true` (R1-5); the default
  body is unchanged. In grounded mode a sentence made only of `[ref]`s passes as grounded although it states nothing,
  and refs past the OpenAI prompt cap (`MAX_EVIDENCE_ITEMS`) still validate if the model guesses them (they are this
  turn's relevant evidence, so no scope leak).
- Card masking is a heuristic: a card number written with separators other than space, tab, `-`, `.`, `/` or `_`
  (such as `,`, `:` or `|`), or with more than three of them between digits, reaches the model unmasked.
- LLM failures fall back to the deterministic answer and increment `ops_ai_llm_fallback_total`, but the counter has no
  reason-code label and no reason is logged.
- `decline_code`, `provider` and `card_bin` have no format pattern in the event contract.
- `OPENAI_API_KEY` reaches the container as a plain environment variable, and `database_url` is a plain string in the
  ops-ai settings (not a secret type).
- User tokens cannot be revoked before they expire (15 minutes).
- SQ2's transfer records in ops-ai are a fixture (`scripts/transfer_fixture.py`) loaded by the seed. The gateway does
  execute transfers (`POST /v1/me/transfers`, with FX legs), but publishes no stream event for them, so ops-ai never
  sees a live transfer.
- Base images are pinned by tag, not by digest, and CI does not run osv-scanner yet.
- The relay loads the full gateway settings, so it mounts the gateway secrets (JWT private key, HMAC keys) although it
  needs only the database and Redis URLs. Fix: a relay-only settings loader.
- An incident with `merchant_id` NULL is shown to every merchant. A NULL written by mistake would leak that incident.
  Fix: an explicit `visibility` column with a CHECK constraint.
- `card_bin` on the event has no format check and goes into the incident search query (bound parameter, so no SQL
  injection, but it can change which incidents match).
- `/v1/assistant/investigate` and `/v1/assistant/investigate-fraud` have no rate limit (`payment_facts` has a
  `(merchant_id, method, occurred_at)` index since SQ3).
- API keys have no `revoked_at`; every `make demo` adds one more key to the demo merchant.
- The seed is not re-anchored: run more than 6 hours after the first seed, SQ1 answers `no_anomaly`. Start over with
  `make demo-down && docker compose --profile demo down -v` (or truncate `ops_ai`). The seeded SQ3 burst leaves the
  24-hour window about 9 hours after the first seed. Its alert and `fraud` ticket are raised once, by the first seed
  (the seed CLI's live tail); a later seed with a new anchor dedupes on `event_id` and raises nothing new.
- Card testing (SQ3): the gateway blocks on its synchronous card-velocity rule (B7.1, B7.3); the ops-ai watch only
  raises an alert and a ticket after the payment.
- Card testing: an earlier payment arriving after the alert (out-of-order delivery) can start a second alert for one
  burst.
- Card testing: seeded payments carry no card identity, so seeded episodes report distinct cards as unknown.
- Card testing: false-positive feedback raises versioned per-merchant thresholds (R2-7) that the live watch applies,
  falling back to the baseline; the SQ3 `investigate-fraud` assistant applies the same active version
  (`X-Fraud-Rule-Version`). The rule actor is the merchant principal and role, not a person.
- Tickets: the lifecycle (`open → assigned → resolved`) exists on merchant-token routes; ops-staff principals are not
  wired to the ticket routes.
- Card testing: the rule is easy to evade once known (amounts just above 1.00, gaps over 120 s, or diluting with
  larger card payments). A burst of more than 5000 micro payments in the window is analysed on its newest 5000 only.
- Card testing: alerts and tickets are not capped per merchant, and an alert refresh from an older event can move
  `last_seen_at` back (one consumer today, so latent). A failure in the watch rolls back the projection of that event.
- Card testing: `origin == "live"` is self-declared by the event, so anyone who can write to the stream (no Redis ACL
  yet) can raise alerts.
- Card testing: each new alert is sent once to an outbound channel (`ops-ai-notifier`, `docs/specs/sq3.md` N-4):
  a signed HTTPS webhook when `OPS_AI_ALERT_WEBHOOK_URL` and `OPS_AI_ALERT_WEBHOOK_SECRET` (32+ characters) are set,
  otherwise a log line, which is what the demo uses. Delivery is at least once (receivers dedupe on
  `Idempotency-Key`); attempts are retried each pass with no backoff, and after 5 failures the row is `failed` with
  nothing that replays or alerts on it. The payload is the snapshot at raise time, not later alert refreshes. The
  secret is a plain environment variable, like `OPENAI_API_KEY`, with no key id for rotation. Retries do not tell 4xx
  from 429/5xx, and a `failed` row neither logs nor counts in metrics. The URL is operator configuration only, so
  private or internal destinations are not refused (`https` makes TLS fail for an internal host); a
  merchant-configurable URL would need a resolved-address deny list first. Hardened after the security audit: the
  URL never reaches a log (httpx's request log is raised to WARNING), the response body is never read, proxy env vars
  are ignored, and each row is claimed, sent and updated in its own transaction.
- Stream: a malformed entry goes to `gw.payments.v1.dead` and is acked; nothing replays or alerts on that stream yet.
- Settlement (SQ4): the gateway settlement batch, `gw.settlement.v1` and payouts are Design; settlement entries are
  seeded as the acquirer's report.
- Settlement: one contract and one FX snapshot for every merchant; per-merchant terms and live rates are Design.
- Settlement: a chargeback is debited when the dispute opens, whatever its outcome; reversal on a won dispute is Design.
- Settlement: the reserve is held only; its release in a later payout is Design.
- Settlement: the cross-check against the payment projection proves the two sources agree, not that both are right.
  It covers amount, currency, card method and the succeeded state. Refund and chargeback flags come from the
  settlement report only, because the projection has no refund or dispute state until B3.
- Settlement: the fee lines are recomputed per payment from the same contract constants that built them, so a wrong
  contract term is not caught.
- Settlement: a period with more than 5000 entries is always reported unverified (`too_many_entries`); streaming
  aggregation is Design.
- Settlement: the DB session stays open during the LLM call, and the contract and FX snapshot ignore the period.
- Settlement: a negative net is shown as a balance the merchant owes; collecting it is Design.
- Web tests: under heavy machine load (for example two `make ci` runs at once), `make web-ci` sometimes fails with
  "Test timed out in 5000ms" on a different vitest test each run (seen in `MerchantArea.pendingWrites.test.tsx` and
  `FailureQueryPanel.test.tsx`). On fd449e3, 1 of 3 reruns passed 527 of 527; the other two failed 1–2 tests each.
  The tests are unchanged; rerun `make web-ci` on an idle machine.
