# SQ4 vertical — test specification (11-03)

Source: `docs/requirements-traceability.md` SQ4 / B9.3, `docs/architecture-detail.md` §"Settlement (SQ4)",
`docs/specs/dataset.md` (planted SQ4). Query: "Why is my October payout lower than expected?"

Done = `make demo` asks SQ4 as the planted SQ4 merchant over HTTP and prints a verified typed breakdown
(gross → MDR → fixed fees → FX → refunds → chargebacks + fees → reserve → net), the gap to the expected payout, a
plain-English summary, the tool call and the evidence.

## Decisions

1. **The numbers are computed and verified before anything is narrated.** The breakdown is integer minor units in the
   settlement currency. It is checked (lines sum to net, signs, gross recomputed, every payment agrees with the
   ops-ai payment projection). Only a verified statement goes to the LLM. An unverified one is returned with its
   mismatches, no LLM call, and a summary that says it could not be verified.
2. **"Expected" = gross − contract MDR**, the number a merchant computes naively. `gap = expected − net` and equals
   minus the sum of every line except gross and MDR.
3. **The plain-English summary is deterministic** (a template over the verified lines). The LLM text is an extra
   narration; numbers, ids and labels never come from the LLM.
4. **The settlement source is external here.** The gateway settlement batch and `gw.settlement.v1` are Design. The
   seed stands in for the acquirer's settlement report and loads one row per captured merchant card payment into
   ops-ai (`ingest_source = "seed"`), as SQ2 does for transfers. The payment projection (`payment_facts`, from the
   stream) is the independent second source the statement is checked against.
5. **One contract and one FX snapshot**, as constants. Contract terms per merchant and live rates are Design.
6. **Merchant token only.** The merchant comes from the token, never from the request.

## 1. Contract and FX — `ops_ai/settlement/contract.py`

```python
@dataclass(frozen=True) class Contract(version: str, settlement_currency: str, mdr_bps: int, fixed_fee_minor: int,
    fx_markup_bps: int, chargeback_fee_minor: int, reserve_bps: int)
STANDARD_CONTRACT = Contract("std-2026-10", "USD", mdr_bps=290, fixed_fee_minor=30, fx_markup_bps=200,
    chargeback_fee_minor=1500, reserve_bps=500)
@dataclass(frozen=True) class FxSnapshot(rate_id: str, usd_per_unit: Mapping[str, Decimal])
FX_SNAPSHOT = FxSnapshot("fx-2026-10", {"USD": Decimal("1"), "EUR": Decimal("1.0870"),
    "INR": Decimal("0.0119"), "JPY": Decimal("0.0067")})
MINOR_EXPONENT = {"USD": 2, "EUR": 2, "INR": 2, "JPY": 0}
def to_settlement_minor(amount_minor: int, currency: str, fx: FxSnapshot = FX_SNAPSHOT) -> int
def apply_bps(amount_minor: int, bps: int) -> int
```

- `to_settlement_minor`: `amount_minor / 10**MINOR_EXPONENT[currency] * usd_per_unit[currency] * 100`, rounded
  half-up to an integer (Decimal, no float). USD is unchanged. A currency missing from either table →
  `UnsupportedCurrencyError(currency)`.
- `apply_bps(amount, bps)` = `amount * bps / 10_000` rounded half-up (Decimal).

## 2. Settlement entries — `ops_ai/settlement/models.py`, `seed.py`, `__main__.py`

`ops_ai.settlement_entries`: `payment_id` (UUID PK, the same id as `payment_facts.payment_id`), `merchant_id` (UUID),
`amount_minor` (bigint), `currency` (3), `captured_at`, `statement_period` (varchar 7, `YYYY-MM`), `refunded` (bool),
`chargeback` (bool), `ingest_source` (16). Index `ix_ops_ai_settlement_entries_merchant_period` on
`(merchant_id, statement_period)`. All NOT NULL. Alembic migration with upgrade/downgrade (existing round-trip test).

`seed_settlement(session_factory, log, anchor) -> int`: one row per record with `record_kind == "merchant_payment"`,
`payment_method == "card"`, `merchant_id` not None, `status in ("success", "reversed")`, whose shifted time
(`seed_shift`, as the payment seed) is ≤ anchor. `merchant_id = merchant_uuid(tx.merchant_id)`,
`captured_at` = shifted time, `statement_period = tx.timestamp.strftime("%Y-%m")` (the **unshifted** time: the period
is the label on the acquirer's report), `refunded = status == "reversed"`, `chargeback = tx.chargeback_flag`,
`ingest_source "seed"`. `ON CONFLICT DO NOTHING`; returns rows inserted (a re-run returns 0).
`python -m ops_ai.settlement [--seed 42] [--records 12000] [--anchor ISO]` prints one line with that count only.

## 3. Statement and verification — `ops_ai/settlement/statement.py`

```python
LineKind = Literal["gross", "mdr", "fixed_fees", "fx", "refunds", "chargebacks", "chargeback_fees", "reserve"]
@dataclass(frozen=True) class SettlementLine(kind: LineKind, amount_minor: int, count: int)
@dataclass(frozen=True) class EntryRow(payment_id, amount_minor, currency, refunded: bool, chargeback: bool)
@dataclass(frozen=True) class SettlementStatement(period: str, currency: str, contract_version: str, fx_rate_id: str,
    lines: tuple[SettlementLine, ...], gross_minor: int, expected_minor: int, net_minor: int, gap_minor: int,
    verified: bool, mismatches: tuple[str, ...], projection_checked: int)
def build_lines(entries: Sequence[EntryRow], contract: Contract, fx: FxSnapshot) -> tuple[SettlementLine, ...]
def verify_lines(lines, entries, contract, fx) -> tuple[str, ...]
```

`build_lines` — always the eight kinds in the order of `LineKind`. Let `c(e) = to_settlement_minor(e)`. Entries with an
unsupported currency are left out of every line (and flagged by `verify_lines`).
- `gross` = `+Σ c(e)` over all entries; count = entries.
- `mdr` = `−apply_bps(gross, mdr_bps)`; count = entries.
- `fixed_fees` = `−count × fixed_fee_minor`; count = entries.
- `fx` = `−apply_bps(Σ c(e) for e.currency != settlement_currency, fx_markup_bps)`; count = those entries.
- `refunds` = `−Σ c(e)` for `refunded and not chargeback`; count = those.
- `chargebacks` = `−Σ c(e)` for `chargeback` (a payment that is both is a chargeback only, so refund + chargeback
  never exceed the captured amount); count = those.
- `chargeback_fees` = `−chargebacks.count × chargeback_fee_minor`; same count.
- `reserve` = `−apply_bps(gross, reserve_bps)`; count = entries.
- A kind with nothing in it has amount 0 and count 0.

`net = Σ amounts`, `expected = gross + mdr`, `gap = expected − net`.

`verify_lines` returns mismatch codes, in this order, each at most once (empty = verified):
- `unsupported_currency` — some entry has a currency not in the FX snapshot / exponent table;
- `line_order` — the kinds are not exactly the eight in order;
- `sign` — gross < 0, or any other line > 0;
- `gross_mismatch` — gross ≠ `Σ c(e)` recomputed from the entries;
- `returns_exceed_gross` — `−(refunds + chargebacks) > gross`.
`net`, `expected`, `gap` are always derived from the lines, so they cannot disagree with them.

## 4. Tool — `explain_settlement` in `ops_ai/settlement/tools.py`

```python
MAX_SETTLEMENT_ENTRIES = 5000
def explain_settlement(session, scope: Scope, period: str | None) -> SettlementStatement | None
```

- `period` None → the newest `statement_period` that has entries for `scope.merchant_id`. No entries → None.
- Loads the period's entries; the query filters `merchant_id == scope.merchant_id`. More than
  `MAX_SETTLEMENT_ENTRIES` → loads the first `MAX` by `captured_at, payment_id` and adds mismatch `too_many_entries`.
- Builds the lines with `STANDARD_CONTRACT` and `FX_SNAPSHOT`, then `verify_lines`.
- **Projection cross-check**: loads `payment_facts` for those payment ids, filtered by
  `merchant_id == scope.merchant_id`. Each entry must have a row with `state == "succeeded"`, the same `amount_minor`
  and the same `currency`. Any entry without such a row → mismatch `projection_mismatch` (once).
  `projection_checked` = entries compared.
- `verified = mismatches == ()`. Mismatch order: §3 codes, then `too_many_entries`, then `projection_mismatch`.

## 5. Explanation — `ops_ai/settlement/service.py`

```python
@dataclass(frozen=True) class SettlementExplanation(statement: SettlementStatement, summary: str,
    tool_calls: tuple[ToolCall, ...], evidence: tuple[EvidenceItem, ...], mode: AnswerMode, text: str | None,
    model: str | None)
def explain_payout(session, scope: Scope, question: str, period: str | None, llm: LlmPort)
    -> SettlementExplanation | None                                     # None = no statement for that period
```

- One tool call: `ToolCall("explain_settlement", {"period": period or "latest"})`.
- Money is `f"{minor / 100:.2f} {currency}"` of the absolute value (as SQ2), e.g. `1234.50 USD`.
- `summary` (verified), sentences joined by one space:
  1. `"Your {period} payout is {net}, {gap} less than the {expected} you would expect from gross sales of {gross}
     minus the {mdr_bps/100:.2f}% MDR."` — with `gap == 0`: `"Your {period} payout is {net}, the same as expected
     from gross sales of {gross} minus the {pct}% MDR."`
  2. then one sentence per non-zero line, in line order, skipping gross and MDR:
     - `"Fixed fees: {x} ({n} payments at {fee} each)."`
     - `"FX conversion: {x} on {n} non-USD payments (rate set {fx_rate_id})."`
     - `"Refunds: {x} on {n} payments."`
     - `"Chargebacks: {x} on {n} payments."`
     - `"Chargeback fees: {x} ({n} at {fee} each)."`
     - `"Rolling reserve: {x} held ({reserve_bps/100:.2f}% of gross), released in a later payout."`
- `summary` (not verified): `"The {period} settlement could not be verified ({codes joined by ', '}); contact
  support with reference {period}."` No LLM call, `mode "t2_fallback"`, `text` None, `model` None.
- Evidence (verified), in order: `statement:{period}` (gross, expected, net, gap), `line:{kind}` for each non-zero
  line except gross and MDR (amount and count), `contract:{version}` (MDR %, fixed fee, chargeback fee, reserve %),
  `fx:{rate_id}` only when the FX line is non-zero, `crosscheck:{period}` (`projection_checked` payments matched the
  payment projection). Not verified: `statement:{period}` and `crosscheck:{period}` only, the latter naming the
  mismatch codes.
- Verified → the LLM narrates the summary and evidence; `LlmUnavailableError` → `t2_fallback`, same fields, `text`
  None.

## 6. API — `POST /v1/assistant/explain-settlement`

Merchant token only (user token → 403 `{"detail": "forbidden"}`, invalid → 401 `{"detail": "invalid token"}`).
Body `{question (1..2000), period?: "YYYY-MM"}` (month 01–12; anything else or extra fields → 422). No statement →
404 `{"detail": "settlement not found"}`. 200 carries `Cache-Control: no-store`. Response:

```json
{"period": "2026-10", "currency": "USD", "contract_version": "...", "fx_rate_id": "...",
 "lines": [{"kind": "gross", "amount_minor": 0, "count": 0}],
 "gross_minor": 0, "expected_minor": 0, "net_minor": 0, "gap_minor": 0,
 "verified": true, "mismatches": [], "projection_checked": 0,
 "summary": "...", "mode": "...", "text": null, "model": null,
 "tool_calls": [{"name": "explain_settlement", "args": {"period": "latest"}}],
 "evidence": [{"ref": "...", "summary": "..."}]}
```

Golden (seed 42, fixed anchor, after the payment seed and the settlement seed): the planted SQ4 merchant, `period`
omitted → period `2026-10`; `verified` true; `projection_checked` == its entries; `chargebacks.count` == its
captured card records with `chargeback_flag` (≥ 3); `refunds.count` == its `reversed` records without a chargeback
(≥ 5); `fx.count` == its non-USD captured records (≥ 10); all three amounts < 0; `Σ lines == net`;
`gap == expected − net > 0`; the summary contains the "Refunds:", "Chargebacks:" and "FX conversion:" sentences.
Another merchant's token sees its own statement only. A changed `payment_facts.amount_minor` for one of its payments →
`verified` false, `mismatches == ["projection_mismatch"]`, the LLM is not called, `text` None.

## 7. Demo and compose

- compose `seed`: `... && python -m ops_ai.settlement` after the transfer seed.
- `scripts/demo.py`: `bootstrap_merchant(..., merchant_id=merchant_uuid("mch_0003"))`, merchant token, then
  `/v1/assistant/explain-settlement` with "Why is my October payout lower than expected?" and no period. Checks:
  `verified`, no mismatches, the eight line kinds in order, `Σ lines == net`, `net < expected`, refunds /
  chargebacks / FX lines non-zero, tool `explain_settlement`, the `statement:`, `line:refunds`, `line:chargebacks`,
  `line:fx`, `crosscheck:` evidence refs, and a user token gets 403 on this route. It prints the lines, the gap and
  the summary. Earlier checks stay.

## Known gaps ([docs/known-gaps.md](../known-gaps.md))

- The gateway settlement batch, `gw.settlement.v1` and payouts are Design; settlement entries are seeded.
- One contract and one FX snapshot for every merchant; per-merchant terms and live rates are Design.
- A chargeback is debited when the dispute opens, whatever its outcome; reversal on a won dispute is Design.
- The reserve is held only; its release in a later payout is Design.
- The cross-check proves the two sources agree, not that both are right.

## Out of scope here

Gateway refunds / disputes (B3), payouts, ticket routing to `settlement`, multi-period comparison.

## Clarifications (settled after the RED tests, 11-03)

1. `statement_period` is the month of the **unshifted** record time (e.g. 2026-10-31 17:00 → `2026-10` even when the
   shifted time falls in November).
2. `period` must fully match `^\d{4}-(0[1-9]|1[0-2])$`; anything else (whitespace, newline, "latest", non-ASCII
   digits, non-string) → 422. A well-formed month with no entries → 404, the same body whether another merchant has
   that period or nobody does; no LLM call on 404. A 422 does not echo the submitted values.
3. A payment that is both refunded and a chargeback counts in `chargebacks` (amount and count) only. `reserve.count`
   = gross count.
4. `returns_exceed_gross` fires when `−(refunds + chargebacks) > gross`; equal is fine.
5. `projection_checked` = every entry loaded, whatever the mismatches. `projection_mismatch` covers a missing row,
   another amount, another currency, a state other than `succeeded`, and a row of another merchant.
6. Supported currencies are exactly USD, EUR, INR, JPY. A currency with a rate but no exponent (or the reverse) →
   `UnsupportedCurrencyError(currency)`.
7. Money is always two decimals; percentages are `2.90%` / `5.00%`. Singular / plural is not varied.
8. `Cache-Control: no-store` is required on 200; other statuses are not pinned.
9. The golden oracle is the whole generated log for `mch_0003` (baseline records included), not the 60 planted ones.
10. The SQ3 migration test now pins revision `9d4f1b7e2c63` by id instead of the head (approved 11-03).

## Hardening after the security review (11-03)

Source: security-auditor (M1, M2, L3, L4) and red-team (RT-001..004) on the uncommitted SQ4 change. RT-001 and RT-002
decided by the user ("推奨案で進める", 11-03). Existing SQ4 tests stay as they are; everything below is additive.

### H1. A negative net is a balance owed, never a payout (RT-001 / M1)

A verified statement can have `net < 0` (e.g. two USD 5.00 payments, one charged back: chargeback, chargeback fee
and reserve exceed what is left). The numbers stay signed in the API (`net_minor < 0`). The text changes:
- `summary` first sentence when `net_minor < 0`: `"Your {period} settlement has no payout: you owe {|net|}, {gap} less
  than the {expected} you would expect from gross sales of {gross} minus the {pct}% MDR."` (the line sentences that
  follow are unchanged). `net == 0` keeps the payout wording (`payout is 0.00 USD`).
- `statement:{period}` evidence: `net payout {x}` when `net_minor >= 0`, `net balance owed by you {|net|}` when
  `net_minor < 0`; the words "net payout" never appear for a negative net.
- The summary of a negative-net statement never contains `"payout is"`.

### H2. What "verified" covers (RT-002 / L4)

- **Projection cross-check also matches the method**: each entry's `payment_facts` row must also have
  `method == "card"` (the settlement report lists card payments only). A `wallet` / `upi` / `bank_transfer` row with
  the same id, amount and currency → `projection_mismatch`.
- **Independent recomputation** — new pure function in `statement.py`:
  `def recompute_lines(lines, entries, contract, fx) -> tuple[str, ...]` returns `("line_mismatch",)` when any of the
  eight lines (matched by kind) differs in amount or count from a per-entry recomputation from `entries`, `contract`
  and `fx`, else `()`. The recomputation is written per entry (fees accumulated payment by payment, then the
  bps lines from the recomputed gross); it does not call `build_lines`. Entries in an unsupported currency are
  skipped, as in `build_lines`. Lines with a duplicated or missing kind → `("line_mismatch",)`.
  It is a separate function so `verify_lines` keeps its structural codes unchanged.
- `explain_settlement` runs it after `verify_lines`. Tool-level mismatch order becomes: §3 codes, `line_mismatch`,
  `too_many_entries`, `projection_mismatch`.
- `crosscheck:{period}` evidence (verified) states the scope: `"{n} payments of {period} matched the payment
  projection (same amount, currency and card method, succeeded); refund and chargeback flags come from the settlement
  report only"`.
- Known gaps ([docs/known-gaps.md](../known-gaps.md)): refund / chargeback flags are not cross-checked (the projection has no refund or dispute state
  until B3); fee lines are recomputed from the contract constants, which are the same constants that built them.

### H3. Entry integrity (RT-003 / RT-004 / L3)

- `verify_lines` adds a last code `non_positive_amount` — some entry has `amount_minor <= 0` (whatever its currency).
  Order: `unsupported_currency, line_order, sign, gross_mismatch, returns_exceed_gross, non_positive_amount`.
- New migration (after `5e8a3c1f7b42`) adds CHECK constraints on `ops_ai.settlement_entries` (also declared on the
  model so the migration check stays clean):
  - `ck_settlement_entries_amount_positive`: `amount_minor > 0`;
  - `ck_settlement_entries_currency_format`: `currency ~ '^[A-Z]{3}$'` (format only; supported currencies stay an
    application check so `unsupported_currency` remains reachable);
  - `ck_settlement_entries_period_format`: `statement_period ~ '^[0-9]{4}-(0[1-9]|1[0-2])$'`.
  Insert of `amount_minor` 0 or -1, currency `usd` / `US`, period `2026-9` / `2026-13` / `<b>` → IntegrityError.
  Downgrade drops the three constraints.
- `explain_settlement` with `period=None` picks the newest period among well-formed ones only (filter by the same
  pattern), so a malformed row cannot become "latest" even without the constraint.

### Left as known gaps ([docs/known-gaps.md](../known-gaps.md))

- More than `MAX_SETTLEMENT_ENTRIES` entries in one period → always `too_many_entries` (M2); streaming aggregation is
  Design.
- The DB session is held during the LLM call (L6); the contract and FX snapshot ignore the period (L7).

11. The SQ4 migration test now pins revision `5e8a3c1f7b42` by id (its `down_revision` is `9d4f1b7e2c63`) instead of
    the head, since the H3 constraints migration `a3f7c9e15b28` is the new head (approved 11-03).
