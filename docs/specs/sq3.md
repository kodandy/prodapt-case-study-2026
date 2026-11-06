# SQ3 vertical — test specification (11-03)

Source: `docs/submission-outline.md` §2 (B7, B9, B11, B12) and §4, `docs/requirements-traceability.md` SQ3 / B7.3 /
B9.2 / B11.2 / B12, `docs/architecture-detail.md` §5 fraud_watch and Tickets, `docs/specs/dataset.md` (planted SQ3).
Query: "We're seeing a series of tiny card payments — is this card testing, and what should we do?"

Done = `make demo` seeds the dataset, sends a live burst of micro-amount charges through the merchant API as the SQ3
merchant, the consumer raises one alert and one ticket in the `fraud` queue, and the merchant asks SQ3 and gets the
flagged payment ids, the pattern features, the alert and ticket, the tools called and the evidence.

## Decisions

1. **Detection is ops-ai only (async), rules only, no LLM.** The gateway's synchronous rule score and real-time block
   (B7.1, B7.3 gateway half) are not in this slice; they stay Planned.
2. **Card identity on the port = `card_ref`.** A new optional event field, a keyed derivation of the gateway card
   fingerprint with its own domain tag, so ops-ai can count distinct cards without holding the gateway fingerprint or
   anything PAN-derived it could look up elsewhere.
3. **The seed carries no card identity.** The dataset has no card column, so seeded payments have `card_ref = None` and
   their episodes report `distinct_cards = None` ("unknown"). The seed mapping is unchanged.
4. **Episodes, not fixed buckets.** Micro-amount card payments of one merchant, ordered by time, are split where the gap
   between two consecutive ones exceeds 120 s. A burst that crosses a clock boundary stays one episode.
5. **Side effects only for live events.** Backfilled (seed) events never raise alerts or tickets
   (`architecture-detail.md` §2). Seeded bursts are found by `analyze_fraud` at question time.
6. **New route** `POST /v1/assistant/investigate-fraud` (merchant token). `/investigate` and its response are unchanged.

## 1. Contracts — `contracts/events.py`

`PaymentEventV1.card_ref: str | None = None`, pattern `^[0-9a-f]{32}$` when present. Old payloads without the field
still validate (it defaults to None).

## 2. Gateway

`gateway/payments/tokenization/cards.py`:
```python
def derive_card_ref(fingerprint: str, key: str) -> str   # HMAC-SHA256(key, "card_ref:v1:" + fingerprint) hex[:32]
```
- Same fingerprint and key → same value; different fingerprints → different values; the result is not a prefix of
  the fingerprint; 32 lowercase hex characters.

`gateway/payments/charge.py`: the outbox event of a charge carries
`card_ref = derive_card_ref(<the card's fingerprint>, deps.card_fingerprint_key)`. Two charges with the same test PAN
carry the same `card_ref`; charges with two different test PANs carry different ones. Nothing else in the event
changes.

## 3. ops-ai schema — one Alembic migration after `7c2e9a4b1d05`

- `ops_ai.payment_facts.card_ref` String(32), nullable. The consumer copies `event.card_ref` (None stays None).
- `ops_ai.fraud_alerts`: `alert_id` UUID PK; `window_key` Text unique; `merchant_id` UUID indexed; `kind` String(32)
  (`card_testing`); `first_payment_id` UUID; `started_at`, `last_seen_at`; `payment_count` Integer; `features` JSONB;
  `created_at`.
- `ops_ai.tickets`: `ticket_id` UUID PK; `source_key` Text unique; `queue` (CHECK in `fraud`, `payments_ops`,
  `settlement`, `compliance`); `priority` (CHECK in `high`, `normal`, `low`); `status` (CHECK in `open`, `assigned`,
  `resolved`), default `open`; `merchant_id` UUID nullable, indexed; `title` Text; `reason` Text;
  `alert_id` UUID nullable FK → `fraud_alerts.alert_id`; `created_at`.
- Upgrade and downgrade both run in CI (existing migration test pattern).

## 4. fraud_watch — `ops_ai/fraud_watch/`

```python
MICRO_MAX_UNITS = 1                     # ≤ 1.00 in the payment's currency
CURRENCY_EXPONENT = {"JPY": 0}          # every other currency: 2
EPISODE_GAP_SECONDS = 120
MIN_MICRO_PAYMENTS = 10
MIN_DISTINCT_CARDS = 3
MIN_MICRO_SHARE = 0.8
LIVE_LOOKBACK_MINUTES = 30

def is_micro(amount_minor: int, currency: str) -> bool  # amount_minor <= MICRO_MAX_UNITS * 10**exponent

@dataclass(frozen=True) class CardTestingEpisode(
    first_payment_id: uuid.UUID, start: datetime, end: datetime,
    payment_ids: tuple[uuid.UUID, ...],      # the micro payments, ordered by (occurred_at, payment_id)
    micro_payments: int, card_payments: int, micro_share: float,
    distinct_cards: int | None, declined: int, decline_ratio: float,
    card_bins: tuple[str, ...], currencies: tuple[str, ...],
    flagged: bool, reasons: tuple[str, ...])
@dataclass(frozen=True) class FraudAnalysis(start: datetime, end: datetime, micro_payments: int,
    episodes: tuple[CardTestingEpisode, ...])
def analyze_fraud(session, scope: Scope, start: datetime, end: datetime) -> FraudAnalysis
def watch_payment(session, clock, event: PaymentEventV1) -> uuid.UUID | None   # the alert id, or None
```

`analyze_fraud` (read-only, `method == "card"`, `merchant_id == scope.merchant_id`, `start <= occurred_at < end`):
- Micro payments are split into episodes by the 120 s gap rule (decision 4). Episodes with fewer than
  `MIN_MICRO_PAYMENTS` micro payments are left out. `episodes` is ordered newest `start` first.
- `start` / `end` = first / last micro payment's `occurred_at`. `card_payments` = all card payments of the merchant
  with `start <= occurred_at <= end` (micro or not). `micro_share = micro_payments / card_payments`.
- `distinct_cards` = number of distinct `card_ref` among the micro payments; None if any of them has `card_ref` None.
- `declined` = micro payments with `state in ("declined", "failed")`; `decline_ratio = declined / micro_payments`.
- `card_bins`, `currencies`: sorted distinct non-null values among the micro payments.
- `flagged` iff `micro_payments >= MIN_MICRO_PAYMENTS` and `micro_share >= MIN_MICRO_SHARE` and
  (`distinct_cards is None` or `distinct_cards >= MIN_DISTINCT_CARDS`).
- `reasons` (in this order, only those that apply): `micro_burst` (count and share met), `many_cards`
  (`distinct_cards >= 3`), `card_identity_unknown` (`distinct_cards is None`), `high_decline_ratio`
  (`decline_ratio >= 0.5`). An unflagged episode has `reasons == ()` .
- Another merchant's payments never appear.

`watch_payment` (called by `apply_payment_event` after the projection upsert, same transaction, first delivery only):
- Does nothing (None) unless `event.origin == "live"` and `is_micro(event.amount_minor, event.currency)`.
- Runs `analyze_fraud` for `event.merchant_id` over `[event.occurred_at - 30 min, event.occurred_at + 1 µs)` and takes
  the episode containing `event.payment_id`. Not flagged → None.
- Flagged → upserts `fraud_alerts` on `window_key = f"card_testing:{merchant_id}:{first_payment_id}"`: insert, or
  update `last_seen_at`, `payment_count`, `features`. `features` = the episode's counts, ratios, bins, currencies and
  reasons (no payment ids, no card refs).
- On the insert (not on the update) it creates one ticket through `route_ticket("card_testing")` →
  `("fraud", "high")`, `source_key = window_key`, `status = "open"`, `merchant_id`, a title and a reason naming the
  count, the distinct cards and the span. Ticket insert is `ON CONFLICT (source_key) DO NOTHING`.
- On the insert it logs one line `fraud alert raised` with alert id, merchant id and payment count, nothing else.
- Redelivery of the same event (dedupe in `processed_events`) creates no second alert and no second ticket. A later
  payment in the same episode updates the alert and creates no second ticket.

`ops_ai/tickets/`:
```python
TicketSource = Literal["card_testing"]
def route_ticket(source: TicketSource) -> tuple[Queue, Priority]   # card_testing → ("fraud", "high")
```

Golden (seed `generate(seed=42)`, anchor `A`, scope `merchant_uuid("mch_0002")`):
- `analyze_fraud(A - 24 h, A)` has exactly one flagged episode; its `payment_ids` set equals
  `planted.sq3_transaction_ids`; `distinct_cards is None`; `decline_ratio == 1.0`; `reasons ==
  ("micro_burst", "card_identity_unknown", "high_decline_ratio")`; `card_bins` non-empty; `currencies == ("USD",)`.
- For every other seeded merchant, `analyze_fraud` over the whole seeded history has no flagged episode.
- Seeding creates no `fraud_alerts` and no `tickets` rows.

Live (unit/integration, events applied through `apply_payment_event` with `origin = "live"`):
- 12 micro USD events 10 s apart, 4 distinct `card_ref`, 6 declined → exactly one alert, one ticket (`fraud`, `high`,
  `open`), `payment_count == 12`, alert features `distinct_cards == 4`.
- The same 12 events delivered again → still one alert, one ticket.
- A 13th event 10 s later → same alert, `payment_count == 13`, still one ticket.
- 12 micro events from a single `card_ref` (a legitimate top-up series) → no alert (distinct cards 1).
- 12 micro events with a 3-minute gap after the 6th → two episodes of 6, no alert.
- 12 micro events interleaved with 12 non-micro card payments in the same span → share 0.5, no alert.
- The same events with `origin = "backfill"` → no alert.

## 5. Investigation — `ops_ai/fraud_watch/service.py`

```python
FraudIntent = Literal["card_testing", "unsupported"]
FraudCause = Literal["card_testing", "no_card_testing", "unsupported"]
@dataclass(frozen=True) class AlertRef(alert_id: uuid.UUID, ticket_id: uuid.UUID | None, queue: str | None,
    first_payment_id: uuid.UUID)
@dataclass(frozen=True) class FraudInvestigation(intent: FraudIntent, cause_label: FraudCause,
    analysis: FraudAnalysis | None, alerts: tuple[AlertRef, ...], suggested_fixes: tuple[str, ...],
    tool_calls: tuple[ToolCall, ...], evidence: tuple[EvidenceItem, ...], mode: AnswerMode, text: str | None,
    model: str | None)
def investigate_fraud(session, scope: Scope, question: str, now: datetime, llm: LlmPort) -> FraudInvestigation
```

- Intent `card_testing` when the lower-cased question contains "card testing", "card-testing" or "micro". Otherwise
  `unsupported` (same shape as SQ1: no tools, no evidence, `t2_fallback`, `text` None).
- Calls `analyze_fraud(start=now-24h, end=now)`, recorded as `ToolCall("analyze_fraud", {start, end})` (ISO-8601).
- `cause_label`: `card_testing` if any episode is flagged, else `no_card_testing`.
- `alerts`: the scope merchant's `fraud_alerts` whose `first_payment_id` is the first payment of a flagged episode
  in the analysis, with their ticket (if any). Never another merchant's.
- `suggested_fixes` (only for `card_testing`), fixed playbook in this order:
  1. "Add a velocity limit on card payments of 1.00 or less, per card and per client IP."
  2. "Require 3-D Secure for card payments of 1.00 or less until the burst stops."
  3. "Do not refund or contact the cards in the burst from this channel; the fraud team works the ticket."
- Evidence, in this order:
  - `episode:<first_payment_id>` for each flagged episode, newest first — summary names the micro payment count, the
    span in minutes and seconds, the distinct cards (or "card identity unknown"), the micro share and decline ratio as
    percentages with one decimal, and the BINs;
  - `alert:<alert_id>` for each alert — ticket id and queue;
  - `payment:<payment_id>` for the 5 most recent payments of the newest flagged episode, newest first.
- Then the LLM narrates, as in SQ1; `LlmUnavailableError` → `t2_fallback`. Label, ids and numbers never come from the
  LLM.

## 6. API — `POST /v1/assistant/investigate-fraud`

Merchant token only (user token → 403, invalid → 401 `{"detail": "invalid token"}`), body `{question}` (extra fields
422). `now = deps.clock()`. 200 carries `Cache-Control: no-store`. Response:

```json
{"intent": "...", "cause_label": "...", "mode": "...", "text": null, "model": null,
 "analysis": {"start": "...", "end": "...", "micro_payments": 0,
   "episodes": [{"first_payment_id": "...", "start": "...", "end": "...", "payment_ids": ["..."],
     "micro_payments": 0, "card_payments": 0, "micro_share": 0.0, "distinct_cards": 0, "declined": 0,
     "decline_ratio": 0.0, "card_bins": ["..."], "currencies": ["..."], "flagged": true, "reasons": ["..."]}]} | null,
 "alerts": [{"alert_id": "...", "ticket_id": "...", "queue": "fraud", "first_payment_id": "..."}],
 "suggested_fixes": ["..."], "tool_calls": [{"name": "...", "args": {"...": "..."}}],
 "evidence": [{"ref": "...", "summary": "..."}]}
```

## 7. Demo

- `bootstrap_merchant(..., merchant_id=merchant_uuid("mch_0002"))`, merchant token for it.
- Live burst: 12 charges of 50 minor USD, round-robin over `4242424242424242`, `5555555555554444`,
  `4000000000009995`, `4000000000000002` (so 6 declined), each with its own `Idempotency-Key`.
- After the consumer catches up, it asks the SQ3 question on `/investigate-fraud` and checks:
  `cause_label == "card_testing"`; tool `analyze_fraud`; a flagged episode whose ids equal the planted SQ3 ids
  (seed, card identity unknown); a flagged episode that holds all 12 live payment ids with `distinct_cards == 4` and
  `decline_ratio == 0.5`; an alert for the live episode with a ticket in the `fraud` queue; the episode and alert
  cited in the evidence; a user token gets 403 on this route. It prints the label, the episodes' features, the alert
  and ticket, the tools and the evidence refs. Earlier checks stay.

## Known gaps (README)

- No gateway-side risk score or real-time block yet (B7.1, B7.3 gateway half).
- An earlier payment arriving after the alert (out-of-order delivery) can start a second alert for one burst.
- Seeded payments carry no card identity, so seeded episodes report distinct cards as unknown.
- Thresholds are fixed constants; tuning from human verdicts is Design.
- Ticket lifecycle (`assigned`, `resolved`) and ops-staff routes are not in this slice (B12 Thin).

## Out of scope here

SQ4, gateway risk scoring, ticket lifecycle endpoints, notifications delivery beyond the alert row and log line.

## Clarifications (settled after the RED tests, 11-03)

1. `FraudAnalysis.start` / `end` are the query window arguments. `micro_payments` counts every micro payment in the
   window, including those in episodes that were left out.
2. `fraud_alerts.features` keys are exactly `micro_payments`, `card_payments`, `micro_share`, `distinct_cards`,
   `declined`, `decline_ratio`, `card_bins`, `currencies`, `reasons` (episode field names and values; lists for tuples).
3. `fraud_alerts.created_at` = injected clock; `started_at` / `last_seen_at` = the episode's `start` / `end`.
4. Columns not marked nullable are NOT NULL. `queue`, `priority`, `status` are varchar with the CHECKs.
5. `payment:` evidence = the 5 most recent **micro** payments of the newest flagged episode.
6. `alerts` and `alert:` evidence follow the order of the flagged episodes (newest first).
7. Public paths: `ops_ai.fraud_watch` exports the §4 names and `investigate_fraud`; the service lives in
   `ops_ai.fraud_watch.service`; `route_ticket`, `Queue`, `Priority`, `TicketSource` live in `ops_ai.tickets`.
8. An unflagged episode has `reasons == ()`; a flagged one lists every reason that applies.
9. The "fraud alert raised" line uses the module logger (`logging.getLogger(__name__)`) at INFO.
10. Intent is the keyword rule only.
11. The contracts test lives in `tests/test_events_card_ref.py`: a `contracts/src/contracts/__tests__/` package
    shadows `contracts` under pytest's importlib mode.

## Hardening after the audit (11-03)

From the security-auditor and red-team pass on the first SQ3 cut. Each item is a behaviour change with its own tests.

### H-1. A malformed stream entry is dead-lettered, not retried forever (red-team RT-002)

`consume_payments` handles each entry on its own. When an entry has no `payload` field, the payload is not JSON, or
`PaymentEventV1` rejects it:

- it is `XADD`ed to `PAYMENTS_DEAD_LETTER_TOPIC = "gw.payments.v1.dead"` (constant in `ops_ai.ingest.consumer`,
  `maxlen=10_000`, approximate) with fields `source_entry_id` (the original entry id), `error` (the exception class
  name) and `payload` (the raw payload string, `""` when missing);
- the original entry is `XACK`ed, so it is not re-read;
- a WARNING `dead-lettered event entry_id=<id> error=<class name>` is logged through the module logger; the payload
  and the validation message are never logged;
- the next entries of the same batch are still applied, and the dead-lettered entry counts in the return value;
- nothing is written to Postgres for it.

Errors raised while applying a valid event (DB, Redis) still propagate unchanged, so the worker retries them.

### H-2. Bounded fraud analysis per event (auditor H1)

- `analyze_fraud` reads only micro card payments from Postgres: the micro rule runs in SQL (`micro_condition()` in
  `ops_ai.fraud_watch.rules`, same result as `is_micro` for every currency) instead of loading every card payment.
- It reads at most `MAX_ANALYZED_MICRO_PAYMENTS = 5000` micro payments: the newest ones in the window by
  `(occurred_at, payment_id)`. `FraudAnalysis.micro_payments` still counts every micro payment in the window (one
  `COUNT`). New field `FraudAnalysis.truncated: bool = False` (last field) is True when more than the limit exist.
- `CardTestingEpisode.card_payments` comes from one `COUNT` of card payments in `[episode.start, episode.end]`
  (inclusive, same scope), run only for episodes with at least `MIN_MICRO_PAYMENTS`.
- Results for inputs under the limit are identical to the first cut.
- The migration `9d4f1b7e2c63` also creates index `ix_ops_ai_payment_facts_merchant_method_occurred_at` on
  `payment_facts (merchant_id, method, occurred_at)`, declared on the `PaymentFact` model as well so
  `migrate-check` stays clean.

### H-3. Capped payment ids in the route response (auditor M1)

`EpisodeOut.payment_ids` holds at most `EPISODE_PAYMENT_IDS_LIMIT = 100` ids: the first 100 of the episode in
`(occurred_at, payment_id)` order. `micro_payments` still gives the full count, so a shorter list means truncation.
No new response field.

### H-4. card_ref uses its own subkey and is bound to the merchant (auditor L1, L2)

```
subkey   = HMAC-SHA256(key, "card_ref-key:v1").digest()
card_ref = HMAC-SHA256(subkey, f"{merchant_id}:{fingerprint}").hexdigest()[:32]
```

- Signature: `derive_card_ref(fingerprint: str, merchant_id: uuid.UUID, key: str) -> str`. `merchant_id` is
  rendered as `str(uuid)` (lowercase, hyphenated). `key` is still `GATEWAY_CARD_FINGERPRINT_KEY`; no new secret.
- The charge passes its own merchant id. The same test PAN gives the same card_ref at one merchant and different
  card_refs at two merchants.
- The existing tests that pinned the first formula (`tokenization/__tests__/test_card_ref.py`,
  `payments/__tests__/test_sq3_card_ref.py`) are rewritten for this formula (user-approved, 11-03).

### Hardening clarifications (settled after the RED tests)

1. `MAX_ANALYZED_MICRO_PAYMENTS` is read at call time (no default-argument copy), so tests can patch it.
2. Above the limit, the newest `MAX_ANALYZED_MICRO_PAYMENTS` micro payments by `(occurred_at, payment_id)` descending
   are kept; an older episode cut under `MIN_MICRO_PAYMENTS` is dropped. `CardTestingEpisode.micro_payments` counts
   the loaded payments; `FraudAnalysis.micro_payments` counts the whole window.
3. `micro_condition()` takes no arguments and returns a SQLAlchemy boolean expression.
4. The dead-letter `XADD` passes `maxlen=10_000` as a keyword, `approximate` left at its default (True).
5. `error` is the exception class name (`JSONDecodeError`, `ValidationError`, or the missing-payload error's class).
6. `EPISODE_PAYMENT_IDS_LIMIT` lives in `ops_ai.app`; only `EpisodeOut` is capped. `payment:` and `episode:` evidence
   are unchanged.
7. The index is a plain btree, declared on the model under the same name.

## Feedback-adjusted thresholds in the assistant and a live tail in the seed (11-06)

Closes two gaps recorded in `requirements-traceability.md`: R2-7 "the SQ3 `investigate-fraud` assistant still flags
with the baseline" and SQ3 "seeded events raise no alert, so the E2E golden checks episode + payment refs only".

### Root cause of "seeded events raise no alert"

`seed.to_payment_event` stamps every seeded event `origin = "backfill"`, and `watch_payment` returns at once for any
event that is not `live` (decision 5). The planted burst therefore lands in `payment_facts` and is found by
`analyze_fraud`, but the watcher never sees it. The detection rules are not the cause: `analyze_fraud` already flags
the planted episode (`card_identity_unknown` passes the card rule). The seed is what is unrealistic: in production
the recent history reaches ops-ai on the live stream after the backfill cut-over, so it does go through the watcher.

### S-1. Live tail in `seed_dataset` — `ops_ai/seed/dataset.py`

```python
def seed_dataset(session_factory, clock, log, anchor, *, live_since: datetime | None = None) -> SeedResult
def to_payment_event(tx, shift, origin: Literal["live", "backfill"] = "backfill") -> PaymentEventV1 | None
```

- `live_since` None (default) → unchanged: every event is `backfill`, no alert, ticket or ops alert (decision 5 and
  the existing golden stay true for direct callers; the seed CLI passes a live tail by default since S-3).
- `live_since` set → kept events with shifted `occurred_at >= live_since` are delivered with `origin = "live"`, after
  every backfill event and in `(occurred_at, payment_id)` order, through the same `apply_payment_event`, so
  `watch_payment` (and the active rule version) runs on them exactly as for the consumer. Events before it stay
  `backfill`. `event_id`, ids and every other field are the same as in a backfill seed.
- With `generate(seed=42)`, anchor `A` and `live_since = A - 24 h`: exactly one `fraud_alerts` row, for
  `merchant_uuid("mch_0002")`, `first_payment_id` = the first planted SQ3 id, `payment_count` = 40,
  `features.distinct_cards` None; exactly one ticket (`fraud`, `high`, `open`) linked to it; one ops alert; applied
  rule version 0. No other merchant gets an alert. A second run with the same arguments adds nothing (dedupe on
  `event_id`).
- `live_since` after the burst (e.g. `A`) → no alert: the burst stayed backfill.
- `run_full_eval` seeds with `live_since = anchor - 24 h` (the assistant's window). The `sq3_card_testing` golden
  additionally requires an `alert:` evidence ref.

### S-2. `investigate_fraud` uses the merchant's active rule version (R2-7)

- After `analyze_fraud`, it reads `rule = active_rule_version(session, scope)` in the same session. An episode counts
  as flagged iff `episode.flagged and is_flagged(episode, rule)` (same rule as the live watcher, r2-agents.md §6.2:
  the active version can only tighten the baseline). Episodes that the rule unflags are returned with
  `flagged = False` and `reasons = ()`; the rest of the analysis is unchanged.
- `cause_label`, `alerts`, `suggested_fixes` and evidence follow those flags.
- New last field `FraudInvestigation.rule_version: int | None = None`: the applied version (0 = baseline) for the
  `card_testing` intent; None for `unsupported` (no tool, no DB read).
- Each `episode:` evidence summary ends with `, fraud rule v<version>`.
- The tool list stays `["analyze_fraud"]`; one merchant's version never changes another merchant's result.
- Route: the JSON body keys are unchanged (pinned by the existing route tests); the applied version is returned in the
  response header `X-Fraud-Rule-Version: <n>` when it is not None, and in the episode evidence summary.

Examples (12 micro USD payments 10 s apart, 4 distinct cards, 6 declined): baseline → `card_testing`, version 0;
steps 1 (12, 0.83, 4) → `card_testing`, version 1; steps 2 (14, 0.86, 5) → `no_card_testing`, the episode returned
unflagged with `reasons == ()`, no alert, no fixes, no evidence, version 2.

### S-3. Live tail in the seed CLI that `make demo` runs (11-06)

`python -m ops_ai.seed` (the compose `seed` service, which `make demo` waits for) gains `--live-tail-hours H`
(integer, default 24: the assistant's window and the eval suite's `LIVE_TAIL`). The Makefile and compose are unchanged.

- `H > 0` → `seed_dataset(..., live_since = anchor - H hours)`: the planted card-testing burst goes through
  `watch_payment` as in S-1, so a fresh `make demo` holds one alert for `mch_0002` (`card_testing`, 40 payments,
  `first_payment_id` = the first planted SQ3 id), one `fraud` ticket (`high`, `open`) linked to it and one ops alert,
  before the demo's own live burst adds its alert.
- `H = 0` → `live_since` None: backfill only, no alert, ticket or ops alert (the previous behaviour).
- `H < 0` → argparse error, exit code 2, nothing seeded.
- The printed line is unchanged; `payment_facts` are the same with or without the tail (S-1).
- A second run with the same arguments adds no alert, ticket or ops alert and reports 0 payments applied.
- Tests: `ops_ai/seed/__tests__/test_seed_cli_live_tail.py`.

## Outbound alert notification channel (11-06)

Closes the SQ3 residual "notification stops at the `ops_alerts` row and the ticket, no external channel"
(`requirements-traceability.md` SQ3). The gateway's merchant notifications read the gateway outbox, which ops-ai
must not write, and `ops_alert_deliveries` is pinned to one `redis_stream` row per ops alert by its tests; so the
channel is its own outbox table in ops-ai, written in the watcher transaction and drained by a separate worker that
makes the only network call.

### N-1. Outbox table — one Alembic migration after `d2b7e4a9c1f6`

`ops_ai.alert_notifications` (model `AlertNotification` in `ops_ai.notify.models`, registered in `ops_ai.schema`):
`notification_id` UUID PK; `fraud_alert_id` UUID NOT NULL, unique, FK → `fraud_alerts.alert_id` ON DELETE CASCADE;
`merchant_id` UUID NOT NULL; `payload` JSONB NOT NULL; `status` varchar(16) CHECK in `pending`, `delivered`,
`failed`; `attempts` Integer NOT NULL server default 0; `last_error` varchar(64) nullable; `created_at` NOT NULL;
`delivered_at` nullable; index `ix_alert_notifications_status_created_at` on `(status, created_at)`.
Upgrade and downgrade both run (existing migration test pattern); `alembic check` stays clean.

### N-2. Enqueue — `ops_ai.notify`

```python
ALERT_NOTIFICATION_SCHEMA = "payplat.fraud_alert.v1"
NOTIFIED_FEATURES = ("micro_payments", "card_payments", "micro_share", "distinct_cards", "declined",
                     "decline_ratio", "currencies", "reasons")
def alert_notification_id(fraud_alert_id: uuid.UUID) -> uuid.UUID   # uuid5, deterministic per alert
def enqueue_alert_notification(session, clock, *, fraud_alert_id, merchant_id, kind, payment_count,
                               features: Mapping[str, object]) -> uuid.UUID | None
```

- Inserts one `pending` row, `attempts = 0`, `created_at = clock()`, `ON CONFLICT (fraud_alert_id) DO NOTHING`;
  returns the notification id, or None when the alert already has one. Does not commit; no network call.
- `payload` = `{"schema", "notification_id", "alert_id" (the fraud alert id), "kind", "merchant_id",
  "payment_count", "features", "raised_at"}` (ids as strings, `raised_at` ISO-8601 of `clock()`).
  `features` keeps only the `NOTIFIED_FEATURES` keys present in the input: `card_bins` (leading PAN digits) and any
  other key are dropped. No payment id, card ref, BIN, last 4, token or secret is ever in the payload.
- `watch_payment` calls it in the same transaction when it inserts a new fraud alert (after the ticket and ops
  alert), with the episode features and `payment_count = micro_payments` at raise time. An alert refresh enqueues
  nothing, so a redelivered or later event of the same episode adds no second notification.

### N-3. Delivery — at least once, outside the watcher transaction

```python
MAX_NOTIFICATION_ATTEMPTS = 5
class AlertSinkError(Exception): reason: str        # short, fixed-form; never a URL, header or body
@dataclass(frozen=True) class AlertNotificationMessage(notification_id: uuid.UUID, body: bytes)
class AlertSink(Protocol): def send(self, message: AlertNotificationMessage, now: datetime) -> None
def deliver_alert_notifications(session_factory, sink: AlertSink, clock, limit: int = 100) -> int
```

- Reads `pending` rows oldest first (`created_at`, `notification_id`), `FOR UPDATE SKIP LOCKED`, at most `limit`.
  `body` = the payload as compact JSON with sorted keys (UTF-8).
- Success → `delivered`, `delivered_at = clock()`, `last_error` None; counts in the return value.
- `AlertSinkError` → `attempts + 1`, `last_error = reason[:64]`; at `MAX_NOTIFICATION_ATTEMPTS` → `failed`, never
  retried. A delivered or failed row is never sent again. A crash after the send and before the commit resends: the
  receiver dedupes on `notification_id` (= `Idempotency-Key`).

### N-4. Sinks and configuration

- `LogAlertSink` (default): logs one INFO line `alert notification sent notification_id=<id> sink=log` through
  `ops_ai.notify.sinks`; offline, so `make demo` and `make eval` need no network.
- `WebhookAlertSink(url, secret, client: httpx.Client)`: `POST url` with the body and the headers
  `Content-Type: application/json`, `Idempotency-Key: <notification_id>`, `X-Payplat-Timestamp: <int(now) unix s>`,
  `X-Payplat-Signature: v1=<hex HMAC-SHA256(secret, f"{timestamp}." + body)>`. 2xx → success; any other status →
  `AlertSinkError("http_<status>")` (redirects are not followed); `httpx.HTTPError` → `AlertSinkError(<class name>)`.
  Its `repr` holds neither the URL nor the secret.
- `load_alert_sink(environ: Mapping[str, str]) -> AlertSink`:
  - `OPS_AI_ALERT_WEBHOOK_URL` unset or empty → `LogAlertSink`.
  - Set → the URL must be `https`, or `http` with a loopback host (`127.0.0.1`, `::1`, `localhost`), with a host and
    no userinfo, query or fragment; `OPS_AI_ALERT_WEBHOOK_SECRET` must be set with at least 32 characters. Otherwise
    `ValueError` with a fixed message that holds neither the URL nor the secret. The client has a 5 s timeout and
    `follow_redirects=False`.
- Worker: `python -m ops_ai.notify [--once]` (compose service `ops-ai-notifier`, also started by the `demo` service).
  Nothing logs the URL, the secret, the signature or the body.
- Hardening (security audit of SQ3, findings M1/M2/L1/L4):
  - Building a webhook sink with `load_alert_sink` raises the `httpx` and `httpcore` loggers to `WARNING`, so even
    with the root logger at `INFO` a delivered POST leaves no log line holding the URL (httpx logs every request
    URL at `INFO`).
  - `WebhookAlertSink.send` uses `client.stream` and never reads the response body: only the status counts, so a
    huge or endless body cannot exhaust memory or hold the delivery.
  - Any exception other than `httpx.HTTPError` raised by the client also becomes `AlertSinkError(<class name>)`, so
    one bad send cannot roll back the batch.
  - The client from `load_alert_sink` has `trust_env=False`: proxy and certificate env vars are ignored.
  - `deliver_alert_notifications` claims, sends and updates one row per transaction (`FOR UPDATE SKIP LOCKED`,
    oldest first, at most `limit` rows, each row tried at most once per call). While the sink is sending one row,
    every other pending row stays unlocked, so a slow receiver holds one row lock at a time, never the batch.
  - Private or internal IP destinations are not refused: the URL comes only from operator configuration, https
    makes TLS verification fail for any internal host, and an internal receiver is a legitimate setup. A
    merchant-configurable URL would need a resolved-address deny list first.

### N-5. Tests (new files only)

`ops_ai/notify/__tests__/test_alert_notification_table.py`, `test_enqueue_alert_notification.py`,
`test_deliver_alert_notifications.py`, `test_alert_sinks.py`, and
`ops_ai/fraud_watch/__tests__/test_watch_alert_notification.py`.
