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

Verification on branch `worker/nfr-observability` (2026-11-05): `make ci` passed, `make alerts-test`
passed (promtool: 10 rules, 21 test groups), and the load test ran as described in [docs/perf](perf/README.md).

| NFR id | Requirement (short) | Status | Evidence |
| --- | --- | --- | --- |
| NFR-PERF | Card `POST /v1/payments`: p99 < 2 s, error rate < 1 % | Measured locally; latency target **not met** | [Results](perf/README.md): 2 req/s, p50 405 ms, p95 1.91 s, p99 2.82 s, 0 % errors; 40 req/s, p99 44.5 s with dropped iterations. Shared Docker VM on an Apple silicon laptop, three hot test cards; no production-load proof. [k6](../ops/perf/payments.js). |
| NFR-PERF (assistant) | Separate assistant SLO: p95 < 20 s, with fallback | Design only (latency SLO) | [Architecture §8](architecture-detail.md#8-claims-we-make-about-the-nfrs); fallback behavior is tested in [LLM tests](../services/ops-ai/src/ops_ai/observability/__tests__/test_llm_instrumentation.py), `test_complete_on_unavailable_counts_the_fallback_and_reraises_the_same_error`. |
| NFR-SCALE | 100k TPS / 100M users | Design only | [Design P2-7](design.md#p2-7-100k-tps-topology): stateless replicas, partitioning, home-region sharding and Pub/Sub; sizing is an estimate. |
| NFR-AVAIL | 99.999 % payment write-path availability (≈ 26 s/month error budget) | Design only (availability target) | [Design P2-2](design.md#p2-2-99999--availability): SPOFs, failover and error-budget policy. Single-container dependencies remain locally. |
| NFR-SEC (transport) | TLS in transit | Implemented; separate verification (dev only) | [TLS nginx](../ops/tls/nginx-tls.conf), [certificate generator](../ops/tls/gen_dev_cert.py), [compose TLS profile](../compose.yaml). Managed production certificates and [service mTLS](design.md#mtls-between-services) are design only. |
| NFR-SEC (stored data / keys) | Encryption at rest and key management | Design only (disk encryption / KMS) | [Design D6.2–D6.3](design.md#d62-totp-mfa-with-the-secret-sealed-by-aes-256-gcm), [P2-3](design.md#p2-3-pci-dss-scope-reduction), [P2-4](design.md#p2-4-data-residency-and-gdpr-erasure); TOTP field encryption exists, but does not establish disk encryption. |
| NFR-COMP | Card tokenization / PCI-DSS, GDPR, AML / KYC | Design only (compliance scope and full controls) | [Design P2-3](design.md#p2-3-pci-dss-scope-reduction), [P2-4](design.md#p2-4-data-residency-and-gdpr-erasure), [re-verification](design.md#aml--kyc-re-verification-triggers). Test-card tokenization and PII separation exist; no PCI-DSS L1 or GDPR compliance claim. |
| NFR-REL | Zero double charge; atomic transactions | Implemented and tested (bounded scenarios) | [Charge implementation](../services/gateway/src/gateway/payments/charge.py), [charge tests](../services/gateway/src/gateway/payments/__tests__/test_charge.py), [ledger tests](../services/gateway/src/gateway/payments/ledger/__tests__/test_ledger_service.py); [P0-1–4 gaps](requirements-traceability.md#p0-production-readiness--implemented-and-proved-by-tests-d-020) include missing real process-kill coverage and incomplete crash matrix. |
| NFR-REL (provider) | Deadline, per-route breaker; never reroute `unknown` | Implemented and tested | [Wrapper](../services/gateway/src/gateway/resilience/circuit_breaker.py), [tests](../services/gateway/src/gateway/resilience/__tests__/test_resilient_provider.py): `test_charge_card_with_a_slow_provider_ends_unknown_and_calls_the_provider_once`; [queued-call tests](../services/gateway/src/gateway/resilience/__tests__/test_hardening.py). |
| NFR-OBS (metrics) | HTTP rate/errors/latency, payment/fraud, provider, pipeline and LLM metrics | Implemented and tested (subset) | [Gateway metrics](../services/gateway/src/gateway/observability/metrics.py), [HTTP tests](../services/gateway/src/gateway/observability/__tests__/test_metrics_http.py): `test_request_observes_the_latency_histogram_once`; [AI metrics](../services/ops-ai/src/ops_ai/observability/metrics.py), [LLM tests](../services/ops-ai/src/ops_ai/observability/__tests__/test_llm_instrumentation.py). `gateway_http_request_duration_seconds`, `gateway_payment_events_total`, `ops_ai_payment_failures_total`, `ops_ai_fraud_flags_total`, `ops_ai_llm_tokens_total` (estimated, model `mock` for mock answers). Cost/eval score and reconciler-unknown metrics are missing. |
| NFR-OBS (traces / logs) | W3C context across API → outbox → stream → consumer; trace-correlated logs | Implemented and tested (subset) | [Propagation tests](../tests/e2e/test_trace_propagation.py): `test_trace_id_of_the_gateway_request_reaches_the_ops_ai_consumer_span`, `test_the_llm_span_joins_the_trace_of_the_request_that_created_the_event` (explicit parent context in test); [gateway tracing](../services/gateway/src/gateway/observability/tracing.py), [AI tracing](../services/ops-ai/src/ops_ai/observability/tracing.py); [log canary](../tests/test_log_canary.py): `test_no_canary_secret_or_personal_data_appears_in_any_log_record_of_the_full_flow`. Captures in-process Python logs, not container stdout from every process. |
| NFR-OBS (audit) | Append-only audit and operation correlation | Implemented and tested (operation rows only) | [Audit model](../services/gateway/src/gateway/audit/models.py), [charge tests](../services/gateway/src/gateway/payments/__tests__/test_charge.py), [design D6.6](design.md#d66-append-only-audit-written-in-the-same-transaction). P0-10 remains partial: no app-role grants, UPDATE/DELETE rejection test or rollback-on-audit-failure test. |
| NFR-OBS (export / tools) | Production trace backend and tool spans | Design only | [Tracing setup](../services/gateway/src/gateway/observability/tracing.py) installs an SDK without an exporter; no OTLP deployment or tool spans. |
| NFR-OBS (alerts / dashboard) | Failure spike, provider, lag and fallback detection; operational dashboard | Implemented; separate verification | [Rules](../ops/prometheus/alerts.yml), [promtool fixtures](../ops/prometheus/alerts.test.yml), [scrapes](../ops/prometheus/prometheus.yml), [PayPlat overview](../ops/grafana/dashboards/payplat-overview.json), [runbook](#runbook-card-failure-spike). `make alerts-test` is outside CI; no reconciler-unknown rule or configured external paging delivery. Live demo not recorded. |
| NFR-GEO | Multi-region / cross-region failover and data residency | Design only | [Design P2-1](design.md#p2-1-multi-region-active-active) compares Spanner with per-region Cloud SQL plus saga/reconciliation; [P2-4](design.md#p2-4-data-residency-and-gdpr-erasure) specifies residency. No deployed multi-region system or failover drill. |
| NFR-B2X | Consumer and merchant payment interfaces | Implemented and tested (subset) | [B1–B3](requirements-traceability.md#b1-user-account-management-with-sign-up-login-mfa-and-wallet-management), [B8](requirements-traceability.md#b8-restgrpc-apis-for-merchant-integrations-with-input-validation-guardrails-and-idempotency-enforcement), [B13](requirements-traceability.md#b13-simple-front-end-interface-for-user-and-merchant-interaction); [transfer tests](../services/gateway/src/gateway/wallets/__tests__/test_transfers.py), [charge tests](../services/gateway/src/gateway/payments/__tests__/test_charge.py). gRPC, FX and external payment rails remain unbuilt; not proof of all B2X capabilities. |

Payment event counters increment at outbox insert, before commit, so rollback can overcount. They
count state changes, not distinct payments. Failure metrics use decline codes, not a complete
mapping to the investigation taxonomy. The current unknown-outcome charge path emits no outbox event,
so these event counters do not cover all stored unknown payments. Histogram quantiles are estimates
from buckets.

## Security transport and data at rest

`make tls-up` generates a self-signed EC P-256 certificate inside Docker into the `dev-cert` volume;
keys/certificates are never committed. [`.gitignore`](../.gitignore) blocks `*.key`, `*.crt` and
`*.pem`. nginx accepts TLS 1.2/1.3 on `127.0.0.1:8443` by default. This is a dev configuration,
not a trusted production endpoint. Production managed TLS and service-to-service mTLS are design only.

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
- **M3:** Non-timeout provider exceptions are re-raised; safe unknown-outcome classification is incomplete.
- **M4:** Breaker admission tokens / stale outcomes are not tracked across state transitions.
- **M5:** `/metrics` remains unauthenticated on the app port; only the nginx public proxy blocks it.
- **L2:** Span exception recording can capture sensitive exception details.
- **L4:** Merchant bucket uses key hash, not merchant id; multiple keys get independent capacity.
- **L5:** Grafana uses a default dev password; container images use version tags rather than digests.
- **L7:** Dev certificates expire after 30 days but existing pairs are reused without expiry renewal;
  dev nginx sets HSTS (`max-age=300`).
- **L8:** Provider `lookup()` passes through without a deadline.

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
