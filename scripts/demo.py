"""End-to-end walkthrough for `make demo`: charges over HTTP, wait for the consumer, ask the assistant, then
investigate SQ1 over the seeded history (docs/specs/sq1.md section 9).

Prints one ✓/✗ line per check and exits 0 only if every check passed. Never prints the API key in full or the
merchant token.
"""

import os
import secrets
import sys
import time
import uuid
from collections.abc import Callable

import httpx
from contracts.events import PAYMENTS_TOPIC
from gateway.admin import bootstrap_merchant
from gateway.clock import system_clock
from gateway.config import load_gateway_settings
from gateway.db import make_engine, make_session_factory
from ops_ai.seed import merchant_uuid
from redis import Redis

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


def _charge(client: httpx.Client, api_key: str, idempotency_key: str, pan: str) -> httpx.Response:
    return client.post(
        "/v1/payments",
        headers={"Authorization": f"Bearer {api_key}", "Idempotency-Key": idempotency_key},
        json={"amount_minor": 1250, "currency": "USD", "card": _card(pan)},
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

    summary = "all checks passed" if demo.failures == 0 else f"{demo.failures} check(s) failed"
    print(f"\n{summary}", flush=True)
    return 0 if demo.failures == 0 else 1


if __name__ == "__main__":
    sys.exit(run())
