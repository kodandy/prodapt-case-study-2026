"""End-to-end walkthrough for `make demo`: charges over HTTP, wait for the consumer, ask the assistant,
investigate SQ1 over the seeded history (docs/specs/sq1.md section 9), then log in as the seeded SQ2 sender
and investigate the declined transfer (docs/specs/sq2.md section 8), then run a live card-testing burst for
the SQ3 merchant and investigate it next to the seeded one (docs/specs/sq3.md section 7), then explain the
SQ4 merchant's October payout from its verified settlement statement (docs/specs/sq4.md section 7).

Prints one ✓/✗ line per check and exits 0 only if every check passed. Never prints the API key in full, a
password or a token.
"""

import os
import secrets
import sys
import time
import uuid
from collections.abc import Callable

import httpx
from contracts.events import PAYMENTS_TOPIC
from gateway.admin import bootstrap_merchant, bootstrap_user
from gateway.clock import system_clock
from gateway.config import load_gateway_settings
from gateway.db import make_engine, make_session_factory
from ops_ai.seed import merchant_uuid
from redis import Redis
from sqlalchemy.orm import Session, sessionmaker

from scripts.generate_dataset import generate

PAYMENT_FACTS_GROUP = "ops-ai.payment-facts"
APPROVED_PAN = "4242424242424242"
DECLINED_PAN = "4000000000000119"
CATCH_UP_TIMEOUT_SECONDS = 30.0
QUESTION = "declined payments"
SQ1_MERCHANT = "mch_0001"
SQ1_BIN = "400000"
SQ1_INCIDENT_REF = "incident:inc_0001"
SQ1_QUESTION = (
    "15% of my card payments are failing this afternoon — what is the root cause and how do we fix it?"
)
INVESTIGATION_TOOLS = ["aggregate_failures", "compare_baseline", "search_incidents"]
SQ2_SENDER_EMAIL = "sq2-sender@example.com"
SQ2_QUESTION = "My international transfer was declined even though I had enough money — what triggered it?"
SQ3_MERCHANT = "mch_0002"
SQ3_QUESTION = "We're seeing a series of tiny card payments — is this card testing, and what should we do?"
SQ3_MICRO_AMOUNT_MINOR = 50
SQ3_BURST_SIZE = 12
SQ3_PANS = ("4242424242424242", "5555555555554444", "4000000000009995", "4000000000000002")
SQ4_MERCHANT = "mch_0003"
SQ4_QUESTION = "Why is my October payout lower than expected?"
SQ4_LINE_KINDS = ["gross", "mdr", "fixed_fees", "fx", "refunds", "chargebacks", "chargeback_fees", "reserve"]


class Demo:
    def __init__(self) -> None:
        self.failures = 0

    def check(self, label: str, ok: bool, detail: str = "") -> bool:
        mark = "✓" if ok else "✗"
        print(f"{mark} {label}" + (f" — {detail}" if detail else ""), flush=True)
        if not ok:
            self.failures += 1
        return ok


def _card(pan: str) -> dict[str, object]:
    return {"number": pan, "exp_month": 12, "exp_year": 2030, "cvc": "123"}


def _charge(
    client: httpx.Client, api_key: str, idempotency_key: str, pan: str, amount_minor: int = 1250
) -> httpx.Response:
    return client.post(
        "/v1/payments",
        headers={"Authorization": f"Bearer {api_key}", "Idempotency-Key": idempotency_key},
        json={"amount_minor": amount_minor, "currency": "USD", "card": _card(pan)},
    )


def _wait_until(predicate: Callable[[], bool], timeout_seconds: float) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.25)
    return predicate()


def _published(redis: Redis, payment_ids: set[str]) -> bool:
    """Lag 0 alone is not enough: before the relay publishes our events the group is trivially caught up."""
    entries = redis.xrevrange(PAYMENTS_TOPIC, count=200) or []
    seen = {str(fields.get("payload", "")) for _, fields in entries if fields}
    return all(any(payment_id in payload for payload in seen) for payment_id in payment_ids)


def _consumer_caught_up(redis: Redis, payment_ids: set[str]) -> bool:
    if not _published(redis, payment_ids):
        return False
    groups = redis.xinfo_groups(PAYMENTS_TOPIC)
    for group in groups:
        if group["name"] == PAYMENT_FACTS_GROUP:
            return bool(group["lag"] == 0 and group["pending"] == 0)
    return False


def _investigate_sq1(demo: Demo, ops_ai: httpx.Client, token: str, declined_ref: str) -> None:
    print("\nSQ1: " + SQ1_QUESTION, flush=True)
    response = ops_ai.post(
        "/v1/assistant/investigate",
        headers={"Authorization": f"Bearer {token}"},
        json={"question": SQ1_QUESTION},
    )
    body = response.json() if response.status_code == 200 else {}
    window = body.get("window") or {}
    baseline = body.get("baseline") or {}
    by_bin = window.get("by_bin") or [{}]
    tools = [call["name"] for call in body.get("tool_calls", [])]
    refs = [item["ref"] for item in body.get("evidence", [])]
    demo.check(
        "investigation answered",
        response.status_code == 200,
        f"{response.status_code} mode={body.get('mode')}",
    )
    demo.check(
        "cause label is issuer_outage_on_bin",
        body.get("cause_label") == "issuer_outage_on_bin",
        str(body.get("cause_label")),
    )
    demo.check(f"top failing BIN is {SQ1_BIN}", by_bin[0].get("card_bin") == SQ1_BIN, str(by_bin[0]))
    window_rate = float(window.get("rate", -1.0))
    baseline_rate = float(baseline.get("rate", 1.0))
    demo.check(
        "window failure rate between 10% and 20%",
        0.10 <= window_rate <= 0.20,
        f"{window.get('failed')} of {window.get('total')} ({window_rate:.1%}), "
        f"{window.get('start')} to {window.get('end')}",
    )
    demo.check(
        "baseline failure rate below 5%",
        baseline_rate < 0.05,
        f"{baseline.get('failed')} of {baseline.get('total')} ({baseline_rate:.1%}) over the 7 days before",
    )
    demo.check("tools called in order", tools == INVESTIGATION_TOOLS, ", ".join(tools))
    demo.check("evidence cites incident inc_0001", SQ1_INCIDENT_REF in refs)
    demo.check("evidence cites the live declined charge", declined_ref in refs, declined_ref)
    for fix in body.get("suggested_fixes", []):
        print(f"    suggested fix: {fix}", flush=True)
    for ref in refs:
        print(f"    evidence {ref}", flush=True)


def _sq2_sender_id() -> uuid.UUID:
    """The planted SQ2 sender of the same dataset the seed loads (seed 42, default record count)."""
    log = generate(seed=42)
    return next(tx for tx in log.transactions if tx.transaction_id == log.planted.sq2_transaction_id).user_id


def _investigate_sq2(
    demo: Demo,
    gateway: httpx.Client,
    ops_ai: httpx.Client,
    merchant_token: str,
    session_factory: sessionmaker[Session],
) -> str:
    """Returns the SQ2 sender's user token, reused for the SQ3 403 check."""
    print("\nSQ2: " + SQ2_QUESTION, flush=True)
    password = secrets.token_urlsafe(18)
    sender_id = bootstrap_user(session_factory, system_clock, SQ2_SENDER_EMAIL, password, _sq2_sender_id())
    demo.check("SQ2 sender ready", True, f"user {sender_id}")
    login = gateway.post("/v1/user-tokens", json={"email": SQ2_SENDER_EMAIL, "password": password})
    demo.check("user token issued (email + password login)", login.status_code == 201, f"{login.status_code}")
    user_token = login.json().get("access_token", "") if login.status_code == 201 else ""

    response = ops_ai.post(
        "/v1/assistant/investigate-transfer",
        headers={"Authorization": f"Bearer {user_token}"},
        json={"question": SQ2_QUESTION},
    )
    body = response.json() if response.status_code == 200 else {}
    transfer = body.get("transfer") or {}
    amount, limit = transfer.get("amount_minor", 0), transfer.get("limit_minor")
    tools = [call["name"] for call in body.get("tool_calls", [])]
    refs = [item["ref"] for item in body.get("evidence", [])]
    fixes = body.get("suggested_fixes", [])
    transfer_id = transfer.get("transfer_id")
    demo.check(
        "transfer investigation answered",
        response.status_code == 200,
        f"{response.status_code} mode={body.get('mode')}",
    )
    demo.check(
        "cause code is cross_border_limit_for_kyc_tier",
        body.get("cause_code") == "cross_border_limit_for_kyc_tier",
        str(body.get("cause_code")),
    )
    demo.check(
        "the sender can fix it", body.get("remediation_owner") == "sender", str(body.get("remediation_owner"))
    )
    demo.check(
        "balance covered the amount",
        body.get("balance_covered_amount") is True,
        f"balance {transfer.get('sender_balance_minor')} vs amount {amount} "
        f"{transfer.get('currency')} (minor)",
    )
    demo.check(
        "amount above the KYC tier limit",
        isinstance(limit, int) and amount > limit,
        f"amount {amount} > tier {transfer.get('kyc_tier')} limit {limit} {transfer.get('limit_currency')} "
        f"(minor), {transfer.get('country_sender')} to {transfer.get('country_receiver')}",
    )
    demo.check(
        "one fix: upgrade to KYC tier 2",
        len(fixes) == 1 and "tier 2" in str(fixes[0]).lower(),
        str(fixes[0]) if fixes else "none",
    )
    demo.check("tool called", tools == ["get_payment_status"], ", ".join(tools))
    demo.check(
        "evidence cites the transfer, the balance and the limit",
        refs == [f"transfer:{transfer_id}", f"balance:{transfer_id}", f"limit:{transfer_id}"],
    )
    for item in body.get("evidence", []):
        print(f"    evidence {item['ref']}: {item['summary']}", flush=True)

    as_merchant = ops_ai.post(
        "/v1/assistant/investigate-transfer",
        headers={"Authorization": f"Bearer {merchant_token}"},
        json={"question": SQ2_QUESTION},
    )
    demo.check("merchant token gets 403 on the transfer route", as_merchant.status_code == 403)
    as_user = ops_ai.post(
        "/v1/assistant/investigate",
        headers={"Authorization": f"Bearer {user_token}"},
        json={"question": SQ1_QUESTION},
    )
    demo.check("user token gets 403 on /investigate", as_user.status_code == 403)
    return str(user_token)


def _investigate_sq3(
    demo: Demo,
    gateway: httpx.Client,
    ops_ai: httpx.Client,
    redis: Redis,
    session_factory: sessionmaker[Session],
    pepper: str,
    user_token: str,
) -> None:
    print("\nSQ3: " + SQ3_QUESTION, flush=True)
    credentials = bootstrap_merchant(
        session_factory, system_clock, "demo-sq3", pepper, merchant_id=merchant_uuid(SQ3_MERCHANT)
    )
    demo.check("SQ3 merchant ready", True, f"merchant {credentials.merchant_id}")
    issued = gateway.post("/v1/merchant-tokens", headers={"Authorization": f"Bearer {credentials.api_key}"})
    demo.check("SQ3 merchant token issued", issued.status_code == 201, f"{issued.status_code}")
    token = issued.json().get("access_token", "") if issued.status_code == 201 else ""

    states: list[str] = []
    live_ids: list[str] = []
    for index in range(SQ3_BURST_SIZE):
        charge = _charge(
            gateway,
            credentials.api_key,
            str(uuid.uuid4()),
            SQ3_PANS[index % len(SQ3_PANS)],
            amount_minor=SQ3_MICRO_AMOUNT_MINOR,
        )
        charge_body = charge.json()
        states.append(str(charge_body.get("state")))
        live_ids.append(str(charge_body.get("payment_id")))
    declined = states.count("declined")
    demo.check(
        f"live burst: {SQ3_BURST_SIZE} charges of {SQ3_MICRO_AMOUNT_MINOR} USD (minor) "
        f"on {len(SQ3_PANS)} cards",
        len(states) == SQ3_BURST_SIZE and declined == SQ3_BURST_SIZE // 2,
        f"{declined} declined",
    )
    caught_up = _wait_until(lambda: _consumer_caught_up(redis, set(live_ids)), CATCH_UP_TIMEOUT_SECONDS)
    demo.check("burst events published and consumer caught up", caught_up)

    response = ops_ai.post(
        "/v1/assistant/investigate-fraud",
        headers={"Authorization": f"Bearer {token}"},
        json={"question": SQ3_QUESTION},
    )
    body = response.json() if response.status_code == 200 else {}
    demo.check(
        "fraud investigation answered",
        response.status_code == 200,
        f"{response.status_code} mode={body.get('mode')}",
    )
    demo.check(
        "cause label is card_testing", body.get("cause_label") == "card_testing", str(body.get("cause_label"))
    )
    tools = [call["name"] for call in body.get("tool_calls", [])]
    demo.check("tool called", tools == ["analyze_fraud"], ", ".join(tools))

    episodes = [e for e in (body.get("analysis") or {}).get("episodes", []) if e.get("flagged")]
    for episode in episodes:
        print(
            f"    episode {episode['first_payment_id']}: {episode['micro_payments']} micro of "
            f"{episode['card_payments']} card payments, {episode['start']} to {episode['end']}, "
            f"distinct cards {episode['distinct_cards']}, declined {episode['declined']} "
            f"(ratio {episode['decline_ratio']}), BINs {', '.join(episode['card_bins'])}, "
            f"reasons {', '.join(episode['reasons'])}",
            flush=True,
        )
    planted = {str(i) for i in generate(seed=42).planted.sq3_transaction_ids}
    seeded = next((e for e in episodes if set(e["payment_ids"]) == planted), None)
    demo.check(
        "a flagged episode holds exactly the seeded SQ3 burst, card identity unknown",
        seeded is not None and seeded["distinct_cards"] is None,
        f"{len(planted)} planted payments",
    )
    live = next((e for e in episodes if set(live_ids) <= set(e["payment_ids"])), None)
    demo.check(
        "a flagged episode holds the live burst on 4 distinct cards, half declined",
        live is not None and live["distinct_cards"] == len(SQ3_PANS) and live["decline_ratio"] == 0.5,
        "" if live is None else f"{live['micro_payments']} micro payments",
    )
    alerts = body.get("alerts", [])
    for alert in alerts:
        print(
            f"    alert {alert['alert_id']}: ticket {alert['ticket_id']} in queue {alert['queue']}, "
            f"episode {alert['first_payment_id']}",
            flush=True,
        )
    live_alert = next(
        (a for a in alerts if live is not None and a["first_payment_id"] == live["first_payment_id"]), None
    )
    demo.check(
        "the live episode raised an alert with a ticket in the fraud queue",
        live_alert is not None and live_alert["ticket_id"] is not None and live_alert["queue"] == "fraud",
    )
    refs = [item["ref"] for item in body.get("evidence", [])]
    demo.check(
        "evidence cites the live episode and its alert",
        live is not None
        and live_alert is not None
        and f"episode:{live['first_payment_id']}" in refs
        and f"alert:{live_alert['alert_id']}" in refs,
    )
    print(f"    evidence refs: {', '.join(refs)}", flush=True)

    as_user = ops_ai.post(
        "/v1/assistant/investigate-fraud",
        headers={"Authorization": f"Bearer {user_token}"},
        json={"question": SQ3_QUESTION},
    )
    demo.check("user token gets 403 on /investigate-fraud", as_user.status_code == 403)


def _money(minor: int, currency: str) -> str:
    sign = "-" if minor < 0 else ""
    units, cents = divmod(abs(minor), 100)
    return f"{sign}{units}.{cents:02d} {currency}"


def _explain_sq4(
    demo: Demo,
    gateway: httpx.Client,
    ops_ai: httpx.Client,
    session_factory: sessionmaker[Session],
    pepper: str,
    user_token: str,
) -> None:
    print("\nSQ4: " + SQ4_QUESTION, flush=True)
    credentials = bootstrap_merchant(
        session_factory, system_clock, "demo-sq4", pepper, merchant_id=merchant_uuid(SQ4_MERCHANT)
    )
    demo.check("SQ4 merchant ready", True, f"merchant {credentials.merchant_id}")
    issued = gateway.post("/v1/merchant-tokens", headers={"Authorization": f"Bearer {credentials.api_key}"})
    demo.check("SQ4 merchant token issued", issued.status_code == 201, f"{issued.status_code}")
    token = issued.json().get("access_token", "") if issued.status_code == 201 else ""

    response = ops_ai.post(
        "/v1/assistant/explain-settlement",
        headers={"Authorization": f"Bearer {token}"},
        json={"question": SQ4_QUESTION},
    )
    body = response.json() if response.status_code == 200 else {}
    currency = str(body.get("currency", ""))
    lines = body.get("lines", [])
    amounts = {line["kind"]: line["amount_minor"] for line in lines}
    net, expected = body.get("net_minor", 0), body.get("expected_minor", 0)
    demo.check(
        "settlement explained",
        response.status_code == 200,
        f"{response.status_code} period={body.get('period')} mode={body.get('mode')}",
    )
    demo.check(
        "statement verified, no mismatches",
        body.get("verified") is True and body.get("mismatches") == [],
        f"{body.get('projection_checked')} payments matched the payment projection",
    )
    demo.check("the eight lines in order", [line["kind"] for line in lines] == SQ4_LINE_KINDS)
    demo.check("the lines sum to the net payout", sum(amounts.values()) == net, _money(net, currency))
    demo.check(
        "net payout below the expected payout",
        net < expected,
        f"{_money(net, currency)} < {_money(expected, currency)}",
    )
    demo.check(
        "refunds, chargebacks and FX lines are non-zero",
        all(amounts.get(kind, 0) != 0 for kind in ("refunds", "chargebacks", "fx")),
    )
    for line in lines:
        print(
            f"    line {line['kind']}: {_money(line['amount_minor'], currency)} ({line['count']} payments)",
            flush=True,
        )
    print(
        f"    gap: {_money(body.get('gap_minor', 0), currency)} "
        f"(expected {_money(expected, currency)} - net {_money(net, currency)})",
        flush=True,
    )
    print(f"    summary: {body.get('summary')}", flush=True)
    tools = [call["name"] for call in body.get("tool_calls", [])]
    demo.check("tool called", tools == ["explain_settlement"], ", ".join(tools))
    refs = [item["ref"] for item in body.get("evidence", [])]
    period = body.get("period")
    demo.check(
        "evidence cites the statement, refunds, chargebacks, FX and the cross-check",
        all(
            ref in refs
            for ref in (
                f"statement:{period}",
                "line:refunds",
                "line:chargebacks",
                "line:fx",
                f"crosscheck:{period}",
            )
        ),
    )
    print(f"    evidence refs: {', '.join(refs)}", flush=True)

    as_user = ops_ai.post(
        "/v1/assistant/explain-settlement",
        headers={"Authorization": f"Bearer {user_token}"},
        json={"question": SQ4_QUESTION},
    )
    demo.check("user token gets 403 on /explain-settlement", as_user.status_code == 403)


def run() -> int:
    demo = Demo()
    settings = load_gateway_settings()
    session_factory = make_session_factory(make_engine(settings.database_url))
    redis = Redis.from_url(settings.redis_url, decode_responses=True)

    pepper = settings.api_key_pepper.get_secret_value()
    credentials = bootstrap_merchant(
        session_factory, system_clock, "demo-sq1", pepper, merchant_id=merchant_uuid(SQ1_MERCHANT)
    )
    api_key = credentials.api_key
    demo.check("merchant ready", True, f"merchant {credentials.merchant_id}, api key {api_key[:8]}…")

    gateway = httpx.Client(base_url=os.environ["GATEWAY_URL"], timeout=10.0)
    ops_ai = httpx.Client(base_url=os.environ["OPS_AI_URL"], timeout=10.0)

    email = f"demo+{secrets.token_hex(6)}@example.com"
    signup = gateway.post("/v1/signup", json={"email": email, "password": secrets.token_urlsafe(18)})
    demo.check("sign-up", signup.status_code == 201, f"{email} → {signup.status_code}")

    approved_key = str(uuid.uuid4())
    approved = _charge(gateway, api_key, approved_key, APPROVED_PAN)
    approved_body = approved.json()
    demo.check(
        "approved charge",
        approved.status_code == 201 and approved_body.get("state") == "succeeded",
        f"{approved.status_code} {approved_body.get('state')} payment {approved_body.get('payment_id')}",
    )

    declined = _charge(gateway, api_key, str(uuid.uuid4()), DECLINED_PAN)
    declined_body = declined.json()
    demo.check(
        "declined charge",
        declined_body.get("state") == "declined",
        f"{declined.status_code} {declined_body.get('state')} ({declined_body.get('decline_code')})"
        f" payment {declined_body.get('payment_id')}",
    )

    retry = _charge(gateway, api_key, approved_key, APPROVED_PAN)
    demo.check(
        "retry with the same Idempotency-Key returns the stored response",
        retry.status_code == approved.status_code and retry.json() == approved_body,
        f"{retry.status_code}, same payment {retry.json().get('payment_id')}",
    )

    token_response = gateway.post("/v1/merchant-tokens", headers={"Authorization": f"Bearer {api_key}"})
    demo.check("merchant token issued", token_response.status_code == 201, f"{token_response.status_code}")
    token = token_response.json().get("access_token", "") if token_response.status_code == 201 else ""

    our_payments = {str(approved_body.get("payment_id")), str(declined_body.get("payment_id"))}
    caught_up = _wait_until(lambda: _consumer_caught_up(redis, our_payments), CATCH_UP_TIMEOUT_SECONDS)
    demo.check("events published and consumer caught up (lag 0, pending 0)", caught_up)

    answer = ops_ai.post(
        "/v1/assistant/ask", headers={"Authorization": f"Bearer {token}"}, json={"question": QUESTION}
    )
    answer_body = answer.json() if answer.status_code == 200 else {}
    refs = {item["ref"] for item in answer_body.get("evidence", [])}
    demo.check(
        "assistant answered",
        answer.status_code == 200,
        f"{answer.status_code} mode={answer_body.get('mode')}",
    )
    for item in answer_body.get("evidence", []):
        print(f"    evidence {item['ref']}: {item['summary']}", flush=True)
    declined_ref = f"payment:{declined_body.get('payment_id')}"
    approved_ref = f"payment:{approved_body.get('payment_id')}"
    demo.check("evidence cites the declined charge", declined_ref in refs)
    demo.check("evidence leaves out the approved charge", approved_ref not in refs)

    _investigate_sq1(demo, ops_ai, token, declined_ref)
    user_token = _investigate_sq2(demo, gateway, ops_ai, token, session_factory)
    _investigate_sq3(demo, gateway, ops_ai, redis, session_factory, pepper, user_token)
    _explain_sq4(demo, gateway, ops_ai, session_factory, pepper, user_token)

    summary = "all checks passed" if demo.failures == 0 else f"{demo.failures} check(s) failed"
    print(f"\n{summary}", flush=True)
    return 0 if demo.failures == 0 else 1


if __name__ == "__main__":
    sys.exit(run())
