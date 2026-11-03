# SQ1 vertical — test specification (11-03)

Source: `docs/submission-outline.md` §2 (B4, B5, B9) and §4, `docs/requirements-traceability.md` SQ1,
`docs/specs/dataset.md` (planted SQ1). Query: "15% of my card payments are failing this afternoon — what is the root
cause and how do we fix it?"

Done = `make demo` seeds the dataset, makes a live declined charge on the SQ1 BIN as the SQ1 merchant, asks SQ1, and
prints the cause label, the window and baseline numbers, the tools called and the evidence, where the evidence holds the
live charge.

## Decisions

1. **One merchant id across both services.** Dataset merchants (`mch_0001`) map to
   `merchant_uuid(external_id) = uuid5(MERCHANT_NAMESPACE, external_id)`. The demo creates (or reuses) the gateway
   merchant with that id, so the seed history and the live charges share one tenant scope.
2. **The seed is shifted to "now".** `shift = anchor - planted.sq1_window_end` is added to every timestamp, so the
   planted afternoon ends at the anchor. Records whose shifted time is after the anchor are skipped. A re-run does not
   re-seed (dedupe on `event_id`), so the history keeps the first anchor.
3. **SQ1 BIN = 400000.** The live declined charge uses a published test card on that BIN:
   `4000000000000119` (processing error) → decline code `issuer_unavailable`. No new BIN, no non-test PAN.
4. **New route, old route unchanged.** `POST /v1/assistant/investigate` is the tools-first path (B4, B9).
   `POST /v1/assistant/ask` (B5) keeps its response shape.
5. **"This afternoon" = the 6 hours before `now`** (`now` = injected clock). Baseline = the 7 days before the window.
6. `change_timeline` is **Design** (no deploy/config data exists). The tools here are `aggregate_failures`,
   `compare_baseline` and `search_incidents`.

## 1. Gateway (test card)

`gateway/payments/tokenization/cards.py`: `TEST_PANS` adds `"4000000000000119"`.
`gateway/payments/providers/simulator/provider.py`: `DECLINES_BY_CARD[("400000", "0119")] == "issuer_unavailable"`.
- A charge with that card → 201, `state == "declined"`, `decline_code == "issuer_unavailable"`; the event carries
  `card_bin == "400000"`.

`gateway/accounts/service.py`:
`create_merchant(session, clock, name, api_key_pepper, merchant_id: uuid.UUID | None = None)`.
- `merchant_id` given → the merchant row has that id. None → a random id, as before.
- `issue_api_key(session, clock, merchant_id, api_key_pepper) -> MerchantCredentials`: a new key for an existing
  merchant; earlier keys keep working. Unknown merchant → `UnknownMerchantError`. Does not commit.

`gateway/admin.py`: `bootstrap_merchant(..., merchant_id: uuid.UUID | None = None)`.
- Merchant with that id exists → a new API key for it (no second merchant row). Otherwise creates it.

## 2. Dataset

`scripts/generate_dataset.py`: `SQ1_BIN = "400000"`. Every D3 SQ1 property in `dataset.md` still holds with
`planted.sq1_card_bin == "400000"`. The window failure reason on the BIN stays
`f"Issuer unavailable (timeout) for BIN {SQ1_BIN}"`.

## 3. Failure taxonomy (B4, rules first) — `ops_ai/investigation/taxonomy.py`

`failure_code(failure_reason: str) -> str`, case-insensitive, first matching rule wins:

| `failure_reason` contains | code |
|---|---|
| "issuer unavailable" or "timeout" | `issuer_unavailable` |
| "do_not_honor" | `do_not_honor` |
| "insufficient funds" | `insufficient_funds` |
| "expired" | `expired_card` |
| "cvc" | `incorrect_cvc` |
| "fraud" | `fraud_flagged` |
| anything else | `other` |

## 4. Seed — `ops_ai/seed/`

```python
MERCHANT_NAMESPACE: uuid.UUID          # fixed constant
def merchant_uuid(external_id: str) -> uuid.UUID
def seed_shift(planted: PlantedScenarios, anchor: datetime) -> timedelta      # anchor - planted.sq1_window_end
def to_payment_event(tx: CanonicalTransaction, shift: timedelta) -> PaymentEventV1 | None
def seed_dataset(session_factory, clock, log: ScenarioLog, anchor: datetime) -> SeedResult
@dataclass(frozen=True) class SeedResult(payments_applied: int, payments_skipped: int, incidents_upserted: int)
def main(argv: list[str] | None = None) -> int   # python -m ops_ai.seed [--seed 42] [--records 12000] [--anchor ISO]
```

`to_payment_event`:
- Returns None unless `record_kind == "merchant_payment"` and `payment_method == "card"`, or when
  `tx.timestamp + shift > anchor` (the caller passes only records it keeps; see `seed_dataset`).
- `payment_id = tx.transaction_id`; `event_id = uuid5(MERCHANT_NAMESPACE, f"seed:{tx.transaction_id}")`;
  `merchant_id = merchant_uuid(tx.merchant_id)`; `origin = "backfill"`; `entity_version = 1`; `method = "card"`;
  `amount_minor`, `currency`, `card_bin` copied; `card_last4 = None`; `provider = tx.acquirer`;
  `occurred_at = tx.timestamp + shift`.
- State: `success` / `reversed` → `succeeded` (`payment.succeeded`, `decline_code` None).
  `failed` / `flagged` → `declined` (`payment.declined`, `decline_code = failure_code(tx.failure_reason)`).
  `pending` → `unknown` (`payment.unknown`, `decline_code` None).

`seed_dataset`:
- Applies every kept record through `ops_ai.ingest.consumer.apply_payment_event` (the consumer's projection), one
  transaction per batch. A record with shifted time > anchor is counted in `payments_skipped`.
- Upserts every incident into `ops_ai.incidents` with shifted `started_at` / `resolved_at`; `merchant_id` is
  `merchant_uuid(...)` or NULL (platform-wide).
- Running it twice with the same log adds nothing: the second `payments_applied == 0` and row counts are unchanged.

`main` writes one line with the three counts and returns 0. It never prints merchant data beyond counts.

## 5. Incidents table — `ops_ai/investigation/models.py` + Alembic migration

`ops_ai.incidents`: `incident_id` (String 32, PK), `merchant_id` (UUID, nullable, indexed), `title`, `root_cause`,
`fix_applied`, `resolution_notes` (Text), `started_at`, `resolved_at` (nullable), `recurrence_within_7d` (bool),
`tsv` (generated `to_tsvector('english', title || ' ' || root_cause || ' ' || fix_applied)`, GIN index).
Upgrade and downgrade both run in CI (existing migration test pattern).

## 6. Tools — `ops_ai/investigation/tools.py`

All take `scope: Scope`; every query filters on `scope.merchant_id` (incidents: that merchant or NULL).
"Failed" = `state in ("declined", "failed")`. Only `method == "card"`.

```python
@dataclass(frozen=True) class BinShare(card_bin: str, failed: int, total: int)
@dataclass(frozen=True) class CodeCount(decline_code: str, count: int)
@dataclass(frozen=True) class FailureAggregate(start: datetime, end: datetime, total: int, failed: int,
    rate: float, by_bin: tuple[BinShare, ...], by_code: tuple[CodeCount, ...])
def aggregate_failures(session, scope, start: datetime, end: datetime) -> FailureAggregate
def compare_baseline(session, scope, window_start: datetime, days: int = 7) -> FailureAggregate
@dataclass(frozen=True) class IncidentHit(incident_id: str, title: str, root_cause: str, fix_applied: str,
    resolved_at: datetime | None, recurrence_within_7d: bool)
def search_incidents(session, scope, query: str, limit: int = 5) -> list[IncidentHit]
```

- `aggregate_failures`: records with `start <= occurred_at < end`. `rate = failed / total` (0.0 when total is 0).
  `by_bin` sorted by `failed` desc then `card_bin`, bins with `failed > 0` only, `card_bin` NULL left out.
  `by_code` sorted by count desc then code.
- `compare_baseline` = `aggregate_failures(window_start - days, window_start)`.
- `search_incidents`: `websearch_to_tsquery('english', query)` with OR semantics is not required; resolved incidents
  first, then rank. Another merchant's incident is never returned.

## 7. Investigation — `ops_ai/investigation/service.py`

```python
Intent = Literal["failure_spike", "unsupported"]
CauseLabel = Literal["issuer_outage_on_bin", "elevated_failures", "no_anomaly", "unsupported"]
@dataclass(frozen=True) class ToolCall(name: str, args: dict[str, str])
@dataclass(frozen=True) class Investigation(intent: Intent, cause_label: CauseLabel, window: FailureAggregate | None,
    baseline: FailureAggregate | None, suggested_fixes: tuple[str, ...], tool_calls: tuple[ToolCall, ...],
    evidence: tuple[EvidenceItem, ...], mode: AnswerMode, text: str | None, model: str | None)
def investigate(session, scope, question: str, now: datetime, llm: LlmPort) -> Investigation
```

- Intent: `failure_spike` when the lower-cased question contains ("fail" or "declin") and ("card" or "payment").
  Otherwise `unsupported`: `cause_label == "unsupported"`, no tool calls, no evidence, `mode == "t2_fallback"`,
  `text` None.
- `failure_spike` calls, in this order (recorded in `tool_calls` with ISO-8601 args):
  1. `aggregate_failures(start=now-6h, end=now)`
  2. `compare_baseline(window_start=now-6h, days=7)`
  3. `search_incidents(query=f"BIN {top_bin} issuer")` when a top BIN exists, else `search_incidents("card failures")`.
- Cause label:
  - `issuer_outage_on_bin` if `window.rate >= 2 * baseline.rate`, `window.failed >= 10`, and the top BIN holds
    ≥ 50 % of `window.failed`;
  - else `elevated_failures` if `window.rate >= 2 * baseline.rate` and `window.failed >= 10`;
  - else `no_anomaly`.
- `suggested_fixes`: `fix_applied` of the returned incidents, in order, de-duplicated. Empty for `no_anomaly`.
- Evidence, in this order (refs):
  - `aggregate:window` — summary contains total, failed and the rate as a percentage with one decimal;
  - `aggregate:baseline` — same fields;
  - `aggregate:bin:<bin>` for the top BIN — its failed count and share of window failures;
  - `incident:<incident_id>` for each incident hit — title and fix;
  - `payment:<payment_id>` for the 5 most recent failed card payments on the top BIN in the window, newest first.
- Then the LLM narrates (same `LlmPort`, `LlmRequest(question, evidence)`); `LlmUnavailableError` → `t2_fallback`
  with the same fields and `text` None. The cause label and numbers never come from the LLM.

Golden (fixed reference time): seed `generate(seed=42)` with `anchor = A`, investigate at `now = A`, scope =
`merchant_uuid("mch_0001")`:
- `window.total`, `window.failed` equal the counts computed directly from the export rows for the planted window
  (`sq1_window_start .. sq1_window_end`, card, that merchant); `window.rate` between 0.12 and 0.18;
- `baseline.rate < 0.05`; `cause_label == "issuer_outage_on_bin"`; `by_bin[0].card_bin == "400000"`;
- `"incident:inc_0001"` in the evidence refs; `suggested_fixes[0]` mentions "acq_b";
- `tool_calls` names == `["aggregate_failures", "compare_baseline", "search_incidents"]`;
- the same question with another merchant's scope returns `no_anomaly` and no `inc_0001`.
Live increment: after the seed, one more declined event on BIN 400000 at `now - 1 min` for that merchant →
`window.failed` and `window.total` each grow by exactly 1, and its `payment:<id>` is the first payment evidence.

## 8. API — `POST /v1/assistant/investigate`

Same auth as `/ask` (Bearer merchant token → scope; 401 `{"detail": "invalid token"}`), same `{question}` body
(extra fields 422). `now` = `deps.clock()`. Response:

```json
{"intent": "...", "cause_label": "...", "mode": "...", "text": null, "model": null,
 "window": {"start": "...", "end": "...", "total": 0, "failed": 0, "rate": 0.0,
            "by_bin": [{"card_bin": "...", "failed": 0, "total": 0}],
            "by_code": [{"decline_code": "...", "count": 0}]} | null,
 "baseline": {...same...} | null,
 "suggested_fixes": ["..."], "tool_calls": [{"name": "...", "args": {"...": "..."}}],
 "evidence": [{"ref": "...", "summary": "..."}]}
```

`/v1/assistant/ask` response is unchanged.

## 9. Demo and compose

- compose: a `seed` service (`python -m ops_ai.seed`), after `migrate`; `ops-ai-api` and `ops-ai-consumer` wait for it
  to complete. Idempotent across `make demo` runs.
- `scripts/demo.py`: the merchant is `bootstrap_merchant(..., merchant_id=merchant_uuid("mch_0001"))`. The declined
  charge uses `4000000000000119`. After the consumer catches up it calls `/investigate` with the SQ1 question and checks:
  `cause_label == "issuer_outage_on_bin"`; top BIN `400000`; window rate between 0.10 and 0.20; baseline rate < 0.05;
  the three tool names; `inc_0001` cited; the live declined payment id cited. It prints the label, the numbers, the
  tools and the evidence refs. The existing `/ask` checks stay.

## Out of scope here

SQ2–SQ4, `change_timeline`, pgvector, the OpenAI adapter, re-anchoring an existing seed.
