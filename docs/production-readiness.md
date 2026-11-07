# Production readiness

This is the single source for NFR status on this branch. Evidence is scoped to the behavior
actually exercised, not certification or a production SLO. Earlier snapshots in `design.md` §8 and
`architecture-detail.md` §8 are superseded here. Requirement summaries below retain the targets in
those documents; the nine NFR ids are the traceability labels used here.

Status legend:

- **Implemented and tested**: implementation has an automated test included in `make ci`.
- **Measured locally**: run once per stated load on a dev laptop; numbers and limitations are in
  [docs/perf](perf/README.md). This does not mean the target was met.
- **Design only**: described, not built.
- **Implemented; separate verification**: local operational configuration exists, but is outside
  `make ci`; do not confuse it with CI-tested behavior or a measured deployment.

Verification on branch `worker/nfr-observability` (2026-11-05, laptop VM shared with other test runs):
`make ci` ran ruff, format, mypy (298 files) and migrate-check clean; pytest gave 3861 passed and 1 failed
(`test_charge.py::test_e1_twenty_concurrent_charges_with_one_key_make_one_provider_charge_with_redis`: one of
20 same-key callers got `409 idempotency_in_progress` after the 5 s wait budget; this branch does not touch
that path, and the test passed 3 of 3 runs in isolation). `make web-ci` passed on a separate run (23 files,
257 tests). `make alerts-test` passed (promtool: 10 rules, 21 test groups), and the load test ran as described
in [docs/perf](perf/README.md).

| NFR id | Requirement (short) | Status | Evidence |
| --- | --- | --- | --- |
| NFR-PERF | Card `POST /v1/payments`: p99 < 2 s, error rate < 1 % | Measured locally; target **met at 2 req/s and 40 req/s** | [Results](perf/README.md): 2 req/s on a less loaded VM (2026-11-06), p50 72 ms, p95 524 ms, p99 1.25 s, 0 % errors (first run on the shared VM: p99 2.82 s); 40 req/s with the committed compose (four uvicorn workers, multiprocess metrics; 2026-11-07): p50 37 ms, p95 110 ms, p99 338 ms, 0 % errors, none dropped. The earlier one-process gateway was CPU-bound (p99 13.77 s, 2026-11-06). Shared Docker VM on an Apple silicon laptop, three hot test cards; no production-load proof. [k6](../ops/perf/payments.js). |
| NFR-PERF (assistant) | Separate assistant SLO: p95 < 20 s, with fallback | Design only (latency SLO) | [Architecture §8](architecture-detail.md#8-claims-we-make-about-the-nfrs); fallback behavior is tested in [LLM tests](../services/ops-ai/src/ops_ai/observability/__tests__/test_llm_instrumentation.py), `test_complete_on_unavailable_counts_the_fallback_and_reraises_the_same_error`. |
| NFR-SCALE | 100k TPS / 100M users | Design only | [Design P2-7](design.md#p2-7-100k-tps-topology): stateless replicas, partitioning, home-region sharding and Pub/Sub; sizing is an estimate. |
| NFR-AVAIL | 99.999 % payment write-path availability (≈ 26 s/month error budget) | Design only (availability target) | [Design P2-2](design.md#p2-2-99999--availability): SPOFs, failover and error-budget policy. Single-container dependencies remain locally. |
| NFR-SEC (transport) | TLS in transit | Partial (browser → nginx only, dev certificate) | `make tls-up` encrypts only browser → nginx; nginx → web, web → gateway / ops-ai APIs, Postgres and Redis traffic is plaintext on the compose network. [TLS nginx](../ops/tls/nginx-tls.conf), [certificate generator](../ops/tls/gen_dev_cert.py), [compose TLS profile](../compose.yaml). Merchant gRPC serves TLS from `GATEWAY_GRPC_TLS_CERT_PATH` / `GATEWAY_GRPC_TLS_KEY_PATH` and refuses to start without them unless `GATEWAY_GRPC_INSECURE=true` ([server](../services/gateway/src/gateway/merchant_api/grpc_server.py), [TLS tests](../services/gateway/src/gateway/merchant_api/__tests__/test_grpc_tls.py)); the compose demo sets that flag and publishes plaintext gRPC on 127.0.0.1 only. Managed production certificates and [service mTLS](design.md#mtls-between-services) are design only. |
| NFR-SEC (stored data / keys) | Encryption at rest and key management | Design only (disk encryption / KMS) | [Design D6.2–D6.3](design.md#d62-totp-mfa-with-the-secret-sealed-by-aes-256-gcm), [P2-3](design.md#p2-3-pci-dss-scope-reduction), [P2-4](design.md#p2-4-data-residency-and-gdpr-erasure); TOTP field encryption exists, but does not establish disk encryption. |
| NFR-COMP | Card tokenization / PCI-DSS, GDPR, AML / KYC | Partial (GDPR and KYC endpoints implemented; PCI-DSS, crypto-shredding and identity proofing design only) | Implemented: GDPR data-subject requests ([`compliance/gdpr.py`](../services/gateway/src/gateway/compliance/gdpr.py); `GET`/`PATCH /v1/me/profile`, `POST /v1/me/data-requests/export`, `POST /v1/me/data-requests/erasure`, `GET /v1/me/data-requests` in [`app.py`](../services/gateway/src/gateway/app.py)) and KYC re-verification screening with ops review ([`compliance/kyc.py`](../services/gateway/src/gateway/compliance/kyc.py); `GET /v1/ops/kyc-reviews`, `POST /v1/ops/users/{user_id}/kyc/verify`); test-card tokenization and PII separation. Gaps: PCI-DSS scope reduction and any L1 claim ([P2-3](design.md#p2-3-pci-dss-scope-reduction)); per-user-key crypto-shredding of free text ([P2-4](design.md#p2-4-data-residency-and-gdpr-erasure); erasure today pseudonymises the user row and deletes the TOTP secret row); identity proofing (documents / liveness), since KYC `verified` is an ops decision; there is no sanctions screening in the code. Design: [re-verification](design.md#aml--kyc-re-verification-triggers). No PCI-DSS L1 or GDPR compliance claim. |
| NFR-REL | Zero double charge; atomic transactions | Implemented and tested (bounded scenarios) | [Charge implementation](../services/gateway/src/gateway/payments/charge.py), [charge tests](../services/gateway/src/gateway/payments/__tests__/test_charge.py), [ledger tests](../services/gateway/src/gateway/payments/ledger/__tests__/test_ledger_service.py); [P0-1–4 gaps](requirements-traceability.md#p0-production-readiness--implemented-and-proved-by-tests-d-020) include missing real process-kill coverage and incomplete crash matrix. |
| NFR-REL (provider) | Deadline, per-route breaker; never reroute `unknown` | Implemented and tested | [Wrapper](../services/gateway/src/gateway/resilience/circuit_breaker.py), [tests](../services/gateway/src/gateway/resilience/__tests__/test_resilient_provider.py): `test_charge_card_with_a_slow_provider_ends_unknown_and_calls_the_provider_once`; [queued-call tests](../services/gateway/src/gateway/resilience/__tests__/test_hardening.py). |
| NFR-OBS (metrics) | HTTP rate/errors/latency, payment/fraud, provider, pipeline and LLM metrics | Implemented and tested (subset) | [Gateway metrics](../services/gateway/src/gateway/observability/metrics.py), [HTTP tests](../services/gateway/src/gateway/observability/__tests__/test_metrics_http.py): `test_request_observes_the_latency_histogram_once`; [AI metrics](../services/ops-ai/src/ops_ai/observability/metrics.py), [LLM tests](../services/ops-ai/src/ops_ai/observability/__tests__/test_llm_instrumentation.py). `gateway_http_request_duration_seconds`, `gateway_payment_events_total`, `ops_ai_payment_failures_total`, `ops_ai_fraud_flags_total`, `ops_ai_llm_tokens_total` (estimated, model `mock` for mock answers). Cost/eval score and reconciler-unknown metrics are missing. |
| NFR-OBS (traces / logs) | W3C context across API → outbox → stream → consumer; trace-correlated logs | Implemented and tested (subset) | [Propagation tests](../tests/e2e/test_trace_propagation.py): `test_trace_id_of_the_gateway_request_reaches_the_ops_ai_consumer_span`, `test_the_llm_span_joins_the_trace_of_the_request_that_created_the_event` (explicit parent context in test); [gateway tracing](../services/gateway/src/gateway/observability/tracing.py), [AI tracing](../services/ops-ai/src/ops_ai/observability/tracing.py); [log canary](../tests/test_log_canary.py): `test_no_canary_secret_or_personal_data_appears_in_any_log_record_of_the_full_flow`. Captures in-process Python logs, not container stdout from every process. |
| NFR-OBS (audit) | Append-only audit and operation correlation | Implemented and tested (operation rows only) | [Audit model](../services/gateway/src/gateway/audit/models.py), [charge tests](../services/gateway/src/gateway/payments/__tests__/test_charge.py), [append-only tests](../services/gateway/src/gateway/audit/__tests__/test_audit_append_only.py) (UPDATE/DELETE/TRUNCATE rejected as `gateway_app`, rollback when the audit insert fails), [design D6.6](design.md#d66-append-only-audit-written-in-the-same-transaction). P0-10 remains partial: the compose demo logs in as a superuser and only `SET ROLE`s to `gateway_app`, the relay and notifier do not use the role, and there is no hash chain. |
| NFR-OBS (export / tools) | Production trace backend and tool spans | Design only | [Tracing setup](../services/gateway/src/gateway/observability/tracing.py) installs an SDK without an exporter; no OTLP deployment or tool spans. |
| NFR-OBS (alerts / dashboard) | Failure spike, provider, lag and fallback detection; operational dashboard | Implemented; separate verification | [Rules](../ops/prometheus/alerts.yml), [promtool fixtures](../ops/prometheus/alerts.test.yml), [scrapes](../ops/prometheus/prometheus.yml), [PayPlat overview](../ops/grafana/dashboards/payplat-overview.json), [runbook](#runbook-card-failure-spike). `make alerts-test` is outside CI; no reconciler-unknown rule or configured external paging delivery. Live demo not recorded. |
| NFR-GEO | Multi-region / cross-region failover and data residency | Design only | [Design P2-1](design.md#p2-1-multi-region-active-active) compares Spanner with per-region Cloud SQL plus saga/reconciliation; [P2-4](design.md#p2-4-data-residency-and-gdpr-erasure) specifies residency. No deployed multi-region system or failover drill. |
| NFR-B2X | Consumer and merchant payment interfaces | Implemented and tested (subset) | [B1–B3](requirements-traceability.md#b1-user-account-management-with-sign-up-login-mfa-and-wallet-management), [B8](requirements-traceability.md#b8-restgrpc-apis-for-merchant-integrations-with-input-validation-guardrails-and-idempotency-enforcement), [B13](requirements-traceability.md#b13-simple-front-end-interface-for-user-and-merchant-interaction); [transfer tests](../services/gateway/src/gateway/wallets/__tests__/test_transfers.py), [charge tests](../services/gateway/src/gateway/payments/__tests__/test_charge.py). Merchant gRPC, FX legs and simulated `bank_transfer` / `upi` payouts exist (traceability B2.3, B2.5, B8.2); the payout rail is a simulator with no reconciler, and chargebacks of FX-settled charges are not FX-aware. Not proof of all B2X capabilities. |

Payment event counters increment at outbox insert, before commit, so rollback can overcount. They
count state changes, not distinct payments. Failure metrics use decline codes, not a complete
mapping to the investigation taxonomy. The current unknown-outcome charge path emits no outbox event,
so these event counters do not cover all stored unknown payments. Histogram quantiles are estimates
from buckets.

## Security transport and data at rest

`make tls-up` generates a self-signed EC P-256 certificate inside Docker into the `dev-cert` volume;
keys/certificates are never committed. [`.gitignore`](../.gitignore) blocks `*.key`, `*.crt` and
`*.pem`. nginx accepts TLS 1.2/1.3 on `127.0.0.1:8443` by default. This is a dev configuration,
not a trusted production endpoint. It encrypts only browser → nginx: the `web-tls` nginx proxies to
`web` over plain HTTP, `web` proxies to the gateway and ops-ai APIs over plain HTTP, and the services
reach Postgres and Redis without TLS. The demo publishes merchant gRPC in plaintext on 127.0.0.1
(`GATEWAY_GRPC_INSECURE=true`). The only field-level encryption is the TOTP secret (AES-256-GCM,
[`accounts/mfa.py`](../services/gateway/src/gateway/accounts/mfa.py)); other personal data is not
field-encrypted. Production managed TLS and service-to-service mTLS are design only.

Postgres volume/disk encryption is **design only**: use cloud-managed disk encryption with a KMS
customer-managed key (CMK). The local volume has no application-provisioned disk encryption.
[Payment models](../services/gateway/src/gateway/payments/models.py) and
[token models](../services/gateway/src/gateway/payments/tokenization/models.py) store test tokens,
keyed fingerprints, BIN, last4 and expiry metadata, not raw PAN or CVC. Raw test PAN transits the
charge API, so this does not remove the gateway from PCI scope.

Runtime secrets come from environment inputs; the dev bootstrap generates secrets into a Docker
volume and injects them at launch. Production key management is **design only**: KMS envelope
encryption, data-key rotation and rewrapping, separate JWT signing keys from data-encryption and
fingerprint keys, audited access and no private key material in the repository. Current environment
keys and manual rotation are not a real KMS integration.

## Resilience

- Provider charge deadline: `GATEWAY_PROVIDER_TIMEOUT_SECONDS` (default 5 s), wired in
  [gateway main](../services/gateway/src/gateway/main.py). The wrapper bounds waiting; a running
  provider thread can continue after timeout. Timeout becomes stored `unknown` and is never retried
  or rerouted as a fresh charge.
- The process-local, per-route breaker opens at 50 % failures after at least 10 outcomes in a
  20-outcome window, then allows one half-open probe after 30 s. Provider declines are not failures.
  Open circuits return `provider_unavailable`. If a deadline cancels a queued call before it starts,
  it also declines `provider_unavailable`, without recording a breaker failure.
- Such declines are excluded from decline-based risk scoring in [risk.py](../services/gateway/src/gateway/payments/risk.py),
  proved by [risk tests](../services/gateway/src/gateway/payments/__tests__/test_risk_provider_unavailable.py).
  They still count toward card velocity.
- [Redis token buckets](../services/gateway/src/gateway/resilience/rate_limit.py) limit merchant API-key
  hashes on selected routes and valid users on `/v1/me/*`. Exhaustion returns 429 plus `Retry-After`.
  Redis errors fail open and increment `gateway_rate_limit_fail_open_total`; no Redis client disables
  checks without that error count. [Limiter tests](../services/gateway/src/gateway/resilience/__tests__/test_rate_limit.py)
  and [HTTP tests](../services/gateway/src/gateway/resilience/__tests__/test_rate_limit_app.py) cover these paths.
- Both uvicorn servers have a 20 s graceful-shutdown budget. Workers handle stop signals and finish
  their current batch; the consumer commits and XACKs before exit.
  [Relay stop tests](../services/gateway/src/gateway/outbox/__tests__/test_worker_shutdown.py) and
  [consumer stop tests](../services/ops-ai/src/ops_ai/ingest/__tests__/test_worker_shutdown.py) exercise
  mid-processing stop events and restart/deduplication, not actual SIGTERM or API connection draining.
  `/healthz` and dependency-aware `/readyz` exist on both APIs, not every worker process.

## Known gaps

Open security-audit items (H1-H3, M6, L1 and L3 from the same audit are fixed and covered by
`test_hardening.py` / `test_risk_provider_unavailable.py` / `test_method_label.py`; L1: non-standard
HTTP methods are labelled `OTHER`):

- **M1:** Limiter shares the Redis pool and its timeouts with payment coordination.
- **M2:** Per-bearer buckets do not bound API-key guessing; edge `limit_req` is design only.
- **M3:** Provider timeouts, connection errors and `ProviderRequestError` (a non-decline rejection such as a Stripe
  4xx) finalize as `unknown`; any other provider exception is still re-raised, leaving the attempt `created` until
  its lease expires.
- **M4:** Breaker admission tokens / stale outcomes are not tracked across state transitions.
- **M5:** `/metrics` remains unauthenticated on the app port; only the nginx public proxy blocks it.
- **L2:** Span exception recording can capture sensitive exception details.
- **L4:** Merchant bucket uses key hash, not merchant id; multiple keys get independent capacity.
- **L5:** Grafana uses a default dev password; container images use version tags rather than digests.
- **L7:** Dev certificates expire after 30 days but existing pairs are reused without expiry renewal;
  dev nginx sets HSTS (`max-age=300`).
- **L8:** Provider `lookup()` passes through without a deadline.
- **Breaker accounting:** `ResilientProvider` counts every provider exception as a breaker failure, including
  `ProviderRequestError` (a request the provider rejected, e.g. a Stripe 4xx that is not a card decline), so a burst
  of bad requests can open the circuit for healthy traffic
  ([wrapper](../services/gateway/src/gateway/resilience/circuit_breaker.py), `ResilientProvider.charge`).

Other open gaps, with the document that holds the detail:

- **Reconciler:** nothing resolves `unknown` card payments or `unknown` payouts, or reclaims expired attempt leases;
  a crash between the provider call and finalization leaves that key answering 409. `settle_payout` is the entry
  point a payout reconciler would call (traceability B2.5, P0-4; [design D3.3](design.md#d33-an-unknown-outcome-is-stored-as-202-and-never-retried-with-a-new-key)).
- **Card payment latency:** the k6 p99 < 2 s target for card `POST /v1/payments` is met at 2 req/s (p99 1.25 s on a
  less loaded VM) and at 40 req/s with the committed four uvicorn workers (p99 338 ms). Each worker keeps its own
  circuit breaker; `/metrics` sums every worker (see NFR-PERF above and [docs/perf](perf/README.md)). Not measured:
  dedicated hardware, many distinct cards, horizontal replicas.
- **gRPC TLS in the demo:** TLS is implemented and tested, but the compose demo runs plaintext gRPC bound to
  127.0.0.1 (`GATEWAY_GRPC_INSECURE=true`); any deployment beyond loopback must set the certificate paths.
- **`/metrics` without auth:** see M5. Both APIs serve `/metrics` with no credential on their app port; only the web
  nginx proxy returns 404 for it.
- **Interim ops admin token:** ops dispute resolution still accepts `X-Ops-Admin-Token` when no Bearer credential is
  sent (traceability P0-9, B3.3).
- **FX chargebacks:** a refund of an FX-settled charge reverses at the charge's rate, but a chargeback posts in the
  payment currency without FX legs (traceability B2.3; `gw/payments/disputes.py`).
- **KYC-rejected users:** a `rejected` user is refused every transfer, payout and consumer card payment
  (`ensure_not_rejected`, screening); the accepted residuals (check-then-act window, audit rows bounded only by the
  fail-open rate limit, block per account not per person) are in
  [compliance-rbac spec, "Known residuals of M3"](specs/compliance-rbac.md).
- **Ops analytics (R2-8):** ops reads and `POST /v1/ops/ops-ai-tokens` issuance are not audited, the stateless ops-ai
  token stays valid up to 900 s after an ops user is disabled or demoted, and both ops roles read every merchant
  ([analytics spec §4](specs/analytics.md#4-api-ops_aianalyticsroutescreate_analytics_router)).
- **R2 partials:** R2-1 and R2-2 (no dispute records reach ops-ai, so the dispute judge answers `skipped` by default;
  the root-cause judge is opt-in with `POST /v1/assistant/investigate?judge=true`), R2-7 (the rule actor is per
  merchant, not per person; rule history is unpaged), R2-8 (above) and the other rows marked Partial in
  [requirements-traceability.md](requirements-traceability.md).
- **Load-sensitive test:** `test_openai_llm.py::test_complete_gives_the_fallback_only_the_remaining_budget` failed once
  under heavy machine load (`openai: http 503` where it expected `openai: timeout`) and passed on rerun.

## Runbook: card failure spike

`CardFailureRateSpike` means the 5-minute card failure ratio (`declined|failed|unknown` events / all
card events) exceeds 3 times its trailing 1-day baseline, at more than 0.2 card events/s, for 5 minutes.
It is an event ratio, not a count of customers or distinct attempts. Check scrape health and the
baseline before treating the alert as a provider outage. No external paging integration is configured.

1. Open Grafana **PayPlat overview** and Prometheus Alerts. Check **Card failure ratio vs 1d baseline**,
   **Payment events by state**, latency p50/p95/p99, **Provider calls by outcome**, **Circuit state**,
   **Outbox backlog** and **Consumer lag / pending**. Confirm which targets are up.
2. Compare `gateway_provider_calls_total` by `route,outcome` and `gateway_circuit_state`
   (0 closed, 1 open, 2 half-open). Query failures by code:
   `sum by (code) (rate(ops_ai_payment_failures_total[5m]))`.
   These are decline codes; use the SQ1 investigation tools to map evidence to taxonomy, narrow by
   merchant/BIN/time and inspect `change_timeline`. The dashboard does not have a dedicated taxonomy panel.
3. Compare recent deployment/config changes and provider status. Correlate request/consumer logs by
   `trace_id`; deployed trace search needs an exporter/backend, which is not configured locally.
   Do not copy PAN, tokens or secrets into incident records.
4. Roll back an identified bad release/config through the deployment procedure. If the provider is
   failing, let the breaker contain new calls; do not force it closed or increase deadlines blindly.
   The demo has one simulator route; alternate-provider routing is design only. In production, any
   approved reroute applies only to new, unattempted payments. Never retry/reroute an `unknown` attempt;
   verify its original provider outcome before reconciliation or money movement.
5. If pipeline lag is high, restore Redis/DB/worker reachability, then confirm backlog drains and
   failure ratios recover. Do not purge streams, pending messages or idempotency records.
6. Escalate persistent provider errors to the payments on-call and provider support; escalate suspected
   card testing to fraud operations. Record onset, affected route/merchant scope, codes, unknown counts,
   change evidence, mitigation and recovery. Assign reconciliation of unresolved outcomes to payments
   operations; document follow-up ownership. Production contact/rotation details are not configured here.

## How to verify locally

The following commands use Docker.

```sh
make ci              # automated Python checks/tests and web CI
make alerts-test     # standalone promtool config/rule fixtures; outside make ci
make monitoring-up   # Prometheus 127.0.0.1:9090; Grafana 127.0.0.1:3000
make tls-up          # self-signed dev HTTPS at 127.0.0.1:8443
make perf            # k6; stated RATE and environment must accompany results
```

Check Prometheus targets and **PayPlat overview** after starting monitoring. TLS requires accepting
the local self-signed certificate. `make perf` needs the demo merchant setup and running gateway
(see [load-test notes](perf/README.md)); it does not establish 100k TPS capacity.
