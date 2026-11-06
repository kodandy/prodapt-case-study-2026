# Sample queries (SQ1–SQ4)

All four sample queries from the brief run against the live HTTP APIs of a freshly seeded stack. To reproduce:

```bash
make demo-down && docker compose down -v   # start from empty volumes (drops the demo database)
make demo                                  # seed, run SQ1–SQ4 with checks; exits 0 only if every check passed
docker compose --profile demo run --rm --no-deps -T demo python scripts/sample_queries.py   # raw JSON, one line per answer
make demo-down
```

`scripts/sample_queries.py` sends the same questions with the same principals as `scripts/demo.py` (merchant token
from `POST /v1/merchant-tokens`, SQ2 user token from `POST /v1/user-tokens`) and prints each response body. It never
prints an API key, a password or a token.

The outputs below were captured on 2026-11-05 from a fresh stack (`docker compose down -v`, then `make demo` with
`all checks passed`, then `scripts/sample_queries.py`). They are trimmed to the fields that carry the answer; ids are
dropped and timestamps follow the seed, which shifts the dataset so the SQ1 afternoon ends at seed time, so they
differ on every run. A few fields are condensed for reading (`top_bin` is the first `by_bin` entry, `timeline_changes`
lists `kind subject: detail`, `lines` is `[kind, amount_minor, payments]`); the raw objects are in the script output.
No `OPENAI_API_KEY` was set, so every answer reports `"mode": "llm", "model": "mock"`: the
deterministic `MockLlm` writes the text from the evidence. Labels, numbers, fixes and evidence come from the tools in
both cases; a real key changes only the narrated `text`.

## SQ1: card failures spiking this afternoon — root cause and fix

Input: merchant `mch_0001` (merchant token), `POST /v1/assistant/investigate`,
`{"question": "15% of my card payments are failing this afternoon — what is the root cause and how do we fix it?"}`

```json
{
  "cause_label": "issuer_outage_on_bin",
  "window":   {"total": 241, "failed": 36, "rate": 0.1494,
               "top_bin": {"card_bin": "400000", "failed": 32, "total": 52},
               "by_code": [{"decline_code": "issuer_unavailable", "count": 32}, {"decline_code": "expired_card", "count": 4}]},
  "baseline": {"total": 390, "failed": 7, "rate": 0.0179},
  "suggested_fixes": ["Rerouted BIN 400000 to acquirer acq_b until the issuer recovered."],
  "tool_calls": ["aggregate_failures", "compare_baseline", "search_incidents"],
  "evidence_refs": ["aggregate:window", "aggregate:baseline", "aggregate:bin:400000", "incident:inc_0001", "payment:…"]
}
```

The same question on the failure assistant, `POST /v1/assistant/failures`, separates observed facts, hypotheses and
how to confirm them, and ranks fixes from the playbook:

```json
{
  "comparison": {"window_rate": 0.1494, "baseline_rate": 0.0179, "ratio": 8.32, "elevated": true},
  "timeline_changes": ["failure_onset card", "bin_onset 400000: 32 of 52 payments failed", "new_decline_code expired_card: 4 failures"],
  "reasons": [{"taxonomy_code": "issuer_unavailable", "owner": "issuer", "retryable": false, "count": 32, "share": 0.889},
              {"taxonomy_code": "expired_card", "owner": "cardholder", "retryable": false, "count": 4, "share": 0.111}],
  "facts": ["Window: 36 of 241 card payments failed (14.9%).", "Baseline: 7 of 390 card payments failed (1.8%).",
            "The failure rate is elevated.", "BIN 400000 accounts for 88.9% of failures."],
  "hypotheses": ["The issuer (or its processor) is not answering authorisation requests, so charges time out or are rejected as issuer unavailable.",
                 "Stored or entered cards are past their expiry date."],
  "how_to_confirm": ["Check whether the failures concentrate on one card BIN or issuer.",
                     "Compare the issuer_unavailable rate in the window with the 7-day baseline.",
                     "Check the acquirer status page and open incidents for the same issuer."],
  "fixes": [{"rank": 1, "taxonomy_code": "issuer_unavailable", "fix": "Route the affected BIN range to the secondary acquirer and do not auto-retry timed-out charges; reconcile their outcome with the issuer first.", "incident_refs": []},
            {"rank": 2, "taxonomy_code": "expired_card", "fix": "Ask the cardholder to update the card, and enable account updater for stored cards.", "incident_refs": []}],
  "tool_calls": ["compare_baseline", "change_timeline", "failure_reasons", "search_incidents", "list_failed_payments"]
}
```

What it shows: the failure rate is 14.9% in the last 6 hours against 1.8% over the 7 days before (8.3×), 32 of the 36
failures are `issuer_unavailable` on BIN 400000, and the fix is to reroute that BIN; `/investigate` cites the past
incident `inc_0001` and the live declined charge from `make demo`. On `/failures` the ranked fixes come from the
playbook and carry no incident reference in this run (`incident_refs` is empty): `/failures` searches incidents by
keyword only and finds none on the seed. `make eval` runs SQ1 on both routes: `sq1_bin_outage` on `/investigate`
(with the incident citation) and `sq1_failure_route` on `/failures` (BIN 400000, `issuer_unavailable` as top reason
and top fix, no `incident:` ref expected).

## SQ2: international transfer declined despite sufficient funds — which rule fired

Input: the seeded SQ2 sender (user token, role `consumer`), `POST /v1/assistant/investigate-transfer`,
`{"question": "My international transfer was declined even though I had enough money — what triggered it?"}`

```json
{
  "cause_code": "cross_border_limit_for_kyc_tier",
  "remediation_owner": "sender",
  "balance_covered_amount": true,
  "transfer": {"amount_minor": 180000, "currency": "USD", "status": "failed", "country_sender": "US", "country_receiver": "IN",
               "kyc_tier": 1, "limit_minor": 100000, "limit_currency": "USD", "next_tier_limit_minor": 1000000,
               "sender_balance_minor": 525000},
  "suggested_fixes": ["Upgrade to KYC tier 2: its cross-border limit of 10000.00 USD per transfer covers 1800.00 USD."],
  "tool_calls": ["get_payment_status"],
  "evidence": ["Transfer of 1800.00 USD from US to IN, failed at …",
               "Sender balance at decision time 5250.00 USD vs amount 1800.00 USD",
               "Cross-border per-transfer limit for KYC tier 1 1000.00 USD vs amount 1800.00 USD"]
}
```

What it shows: the balance (5250.00 USD) covered the 1800.00 USD transfer, so it was not insufficient funds; the rule
that fired is the KYC tier 1 cross-border limit of 1000.00 USD per transfer, and the fix is KYC tier 2. The transfer is a
seeded decision record (`scripts/transfer_fixture.py`), not a transfer executed through the gateway.

## SQ3: card-testing micro-transaction series

Input: merchant `mch_0002` (merchant token), `POST /v1/assistant/investigate-fraud`,
`{"question": "We're seeing a series of tiny card payments — is this card testing, and what should we do?"}`.
`make demo` first sends a live burst of 12 charges of 0.50 USD over 4 test cards through `POST /v1/payments`.

```json
{
  "cause_label": "card_testing",
  "episodes": [
    {"micro_payments": 12, "distinct_cards": 4, "declined": 6, "decline_ratio": 0.5,
     "card_bins": ["400000", "424242", "555555"], "flagged": true, "reasons": ["micro_burst", "many_cards", "high_decline_ratio"]},
    {"micro_payments": 40, "distinct_cards": null, "declined": 40, "decline_ratio": 1.0,
     "card_bins": ["411111", "424242", "522222", "555555", "601100"], "flagged": true,
     "reasons": ["micro_burst", "card_identity_unknown", "high_decline_ratio"]}
  ],
  "alerts": [{"queue": "fraud", "ticket_id": "…"}, {"queue": "fraud", "ticket_id": "…"}],
  "suggested_fixes": ["Add a velocity limit on card payments of 1.00 or less, per card and per client IP.",
                      "Require 3-D Secure for card payments of 1.00 or less until the burst stops.",
                      "Do not refund or contact the cards in the burst from this channel; the fraud team works the ticket."],
  "tool_calls": ["analyze_fraud"]
}
```

What it shows: both the live burst (12 micro payments, 4 cards, half declined) and the seeded burst (40 micro payments,
all declined) are flagged as card testing with the features that tripped the rule, and each raised an alert with a
ticket in the `fraud` queue: the seed CLI delivers the last 24 hours before its anchor as the live stream
(`--live-tail-hours`, default 24; 0 = backfill only), so the planted burst goes through the same watcher as the demo's
live charges. Seeded payments carry no card identity. Each new alert also enqueues one outbound notification in the
watcher transaction, which `ops-ai-notifier` (started by `make demo`) delivers to a signed webhook when
`OPS_AI_ALERT_WEBHOOK_URL` is set, else to a log line; the demo wires no receiver, so the notification shows only in
that service's log, not in the output above. SQ3 is Done in `docs/requirements-traceability.md`.

## SQ4: plain-English settlement summary — why the payout differs

Input: merchant `mch_0003` (merchant token), `POST /v1/assistant/explain-settlement`,
`{"question": "Why is my October payout lower than expected?"}`

```json
{
  "period": "2026-10", "currency": "USD", "verified": true, "mismatches": [], "projection_checked": 202,
  "gross_minor": 1125137, "expected_minor": 1092508, "net_minor": 983882, "gap_minor": 108626,
  "lines": [["gross", 1125137, 202], ["mdr", -32629, 202], ["fixed_fees", -6060, 202], ["fx", -876, 10],
            ["refunds", -28393, 8], ["chargebacks", -12540, 3], ["chargeback_fees", -4500, 3], ["reserve", -56257, 202]],
  "tool_calls": ["explain_settlement"],
  "summary": "Your 2026-10 payout is 9838.82 USD, 1086.26 USD less than the 10925.08 USD you would expect from gross sales of 11251.37 USD minus the 2.90% MDR. Fixed fees: 60.60 USD (202 payments at 0.30 USD each). FX conversion: 8.76 USD on 10 non-USD payments (rate set fx-2026-10). Refunds: 283.93 USD on 8 payments. Chargebacks: 125.40 USD on 3 payments. Chargeback fees: 45.00 USD (3 at 15.00 USD each). Rolling reserve: 562.57 USD held (5.00% of gross), released in a later payout."
}
```

What it shows: the eight typed lines
sum to the net payout, the statement was verified against 202 payments in the payment projection before the summary
was written, and the 1086.26 USD gap is explained line by line. Settlement entries are seeded as the acquirer's report;
the gateway settlement batch and payouts are Design (see [Known gaps](known-gaps.md#known-gaps)).

To reproduce or read the walkthrough checks, see [running.md](running.md#run-the-services-make-demo).
