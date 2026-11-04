"""RED tests for B3 refunds, from docs/specs/gateway-basic.md (refunds.py + POST .../refunds)."""

import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

import pytest
from fastapi.testclient import TestClient
from gateway.accounts.service import create_merchant
from gateway.app import GatewayAppDeps, create_app
from gateway.clock import FixedClock
from gateway.payments.charge import ChargeDeps, charge_card
from gateway.payments.providers.simulator.provider import SimulatorProvider
from gateway.payments.refunds import RefundOutcome, refund_payment
from gateway.payments.schemas import MAX_AMOUNT_MINOR, ChargeRequest
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from conftest import TEST_API_KEY_PEPPER, TEST_CARD_FINGERPRINT_KEY

APPROVE_PAN = "4242424242424242"
DECLINED_PAN = "4000000000000002"
CONCURRENCY = 8


@dataclass(frozen=True)
class Harness:
    deps: ChargeDeps
    session_factory: sessionmaker[Session]
    clock: FixedClock
    merchant_id: uuid.UUID
    auth: dict[str, str]


def _harness(
    session_factory: sessionmaker[Session],
    clock: FixedClock,
    deps: ChargeDeps | None = None,
    name: str = "Acme",
) -> Harness:
    charge_deps = deps or ChargeDeps(
        session_factory=session_factory,
        redis=None,
        provider=SimulatorProvider(session_factory, clock),
        clock=clock,
        card_fingerprint_key=TEST_CARD_FINGERPRINT_KEY,
    )
    with session_factory() as session:
        credentials = create_merchant(session, clock, name, TEST_API_KEY_PEPPER)
        session.commit()
    return Harness(
        charge_deps,
        session_factory,
        clock,
        credentials.merchant_id,
        {"Authorization": f"Bearer {credentials.api_key}"},
    )


def _client(h: Harness) -> TestClient:
    return TestClient(create_app(GatewayAppDeps(charge=h.deps, api_key_pepper=TEST_API_KEY_PEPPER)))


def _charge(h: Harness, pan: str = APPROVE_PAN, amount_minor: int = 1500, currency: str = "USD") -> uuid.UUID:
    request = ChargeRequest.model_validate(
        {
            "amount_minor": amount_minor,
            "currency": currency,
            "method": "card",
            "card": {"number": pan, "exp_month": 12, "exp_year": 2030, "cvc": "123"},
        }
    )
    outcome = charge_card(h.deps, h.merchant_id, uuid.uuid4().hex, request)
    assert outcome.status_code == 201
    return uuid.UUID(str(outcome.body["payment_id"]))


def _refund(h: Harness, payment_id: uuid.UUID, key: str, amount_minor: int | None = None) -> RefundOutcome:
    return refund_payment(h.session_factory, h.clock, h.merchant_id, payment_id, key, amount_minor)


def _rows(session_factory: sessionmaker[Session], query: str, **params: object) -> list[dict[str, object]]:
    with session_factory() as session:
        return [dict(row) for row in session.execute(text(query), params).mappings()]


def _payment_totals(session_factory: sessionmaker[Session], payment_id: uuid.UUID) -> dict[str, object]:
    return _rows(
        session_factory,
        "SELECT refunded_minor, disputed_minor, chargeback_minor FROM gateway.payments WHERE id = :id",
        id=payment_id,
    )[0]


def _set_totals(
    session_factory: sessionmaker[Session],
    payment_id: uuid.UUID,
    refunded: int,
    disputed: int,
    chargeback: int,
) -> None:
    with session_factory() as session:
        session.execute(
            text(
                "UPDATE gateway.payments SET refunded_minor = :r, disputed_minor = :d, chargeback_minor = :c "
                "WHERE id = :id"
            ),
            {"r": refunded, "d": disputed, "c": chargeback, "id": payment_id},
        )
        session.commit()


def _count(session_factory: sessionmaker[Session], table: str) -> int:
    queries = {
        "refunds": "SELECT count(*) FROM gateway.refunds",
        "refund_journals": "SELECT count(*) FROM gateway.journal_entries WHERE ref_type = 'refund'",
        "journals": "SELECT count(*) FROM gateway.journal_entries",
    }
    return int(str(_rows(session_factory, queries[table])[0]["count"]))


def _balance(
    session_factory: sessionmaker[Session],
    owner_type: str,
    owner_id: uuid.UUID | None,
    kind: str,
    currency: str,
) -> int:
    rows = _rows(
        session_factory,
        "SELECT coalesce(sum(p.amount_minor), 0) AS balance FROM gateway.postings p "
        "JOIN gateway.ledger_accounts a ON a.id = p.account_id "
        "WHERE a.owner_type = :owner_type AND a.owner_id IS NOT DISTINCT FROM :owner_id "
        "AND a.kind = :kind AND a.currency = :currency",
        owner_type=owner_type,
        owner_id=owner_id,
        kind=kind,
        currency=currency,
    )
    return int(str(rows[0]["balance"]))


def _journal_sums_by_currency(session_factory: sessionmaker[Session]) -> dict[str, int]:
    rows = _rows(
        session_factory,
        "SELECT journal_id, currency, sum(amount_minor) AS total FROM gateway.postings "
        "GROUP BY journal_id, currency",
    )
    return {f"{row['journal_id']}:{row['currency']}": int(str(row["total"])) for row in rows}


def _ledger_sum_by_currency(session_factory: sessionmaker[Session]) -> dict[str, int]:
    rows = _rows(
        session_factory, "SELECT currency, sum(amount_minor) AS total FROM gateway.postings GROUP BY currency"
    )
    return {str(row["currency"]): int(str(row["total"])) for row in rows}


# --- refund_payment ------------------------------------------------------------------------------------


def test_refund_payment_without_an_amount_refunds_the_full_amount_and_returns_the_201_body(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    h = _harness(session_factory, clock)
    payment_id = _charge(h, amount_minor=1500)

    # Act
    outcome = _refund(h, payment_id, "refund-full")

    # Assert
    refunds = _rows(
        session_factory,
        "SELECT id, payment_id, merchant_id, amount_minor, currency, state FROM gateway.refunds",
    )
    assert outcome.status_code == 201
    assert outcome.body == {
        "refund_id": str(refunds[0]["id"]),
        "payment_id": str(payment_id),
        "state": "succeeded",
        "amount_minor": 1500,
        "currency": "USD",
        "payment_refunded_minor": 1500,
        "payment_refundable_minor": 0,
    }
    assert refunds == [
        {
            "id": refunds[0]["id"],
            "payment_id": payment_id,
            "merchant_id": h.merchant_id,
            "amount_minor": 1500,
            "currency": "USD",
            "state": "succeeded",
        }
    ]
    assert _payment_totals(session_factory, payment_id) == {
        "refunded_minor": 1500,
        "disputed_minor": 0,
        "chargeback_minor": 0,
    }


def test_refund_payment_with_a_partial_amount_reports_the_remaining_refundable(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    h = _harness(session_factory, clock)
    payment_id = _charge(h, amount_minor=1500)

    # Act
    outcome = _refund(h, payment_id, "refund-partial", 500)

    # Assert
    assert outcome.status_code == 201
    assert outcome.body["amount_minor"] == 500
    assert outcome.body["payment_refunded_minor"] == 500
    assert outcome.body["payment_refundable_minor"] == 1000


def test_refund_payment_two_partial_refunds_accumulate_on_the_payment(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    h = _harness(session_factory, clock)
    payment_id = _charge(h, amount_minor=1500)
    _refund(h, payment_id, "refund-a", 500)

    # Act
    second = _refund(h, payment_id, "refund-b", 700)
    third = _refund(h, payment_id, "refund-c", None)

    # Assert
    assert (second.status_code, second.body["payment_refunded_minor"]) == (201, 1200)
    assert (third.status_code, third.body["amount_minor"], third.body["payment_refundable_minor"]) == (
        201,
        300,
        0,
    )
    assert _payment_totals(session_factory, payment_id)["refunded_minor"] == 1500
    assert _count(session_factory, "refunds") == 3


def test_refund_payment_posts_a_balanced_reversal_journal_referencing_the_refund(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    h = _harness(session_factory, clock)
    payment_id = _charge(h, amount_minor=1500)

    # Act
    outcome = _refund(h, payment_id, "refund-ledger", 600)

    # Assert
    journals = _rows(
        session_factory, "SELECT id, ref_type, ref_id FROM gateway.journal_entries WHERE ref_type = 'refund'"
    )
    refund_id = uuid.UUID(str(outcome.body["refund_id"]))
    assert [(j["ref_type"], j["ref_id"]) for j in journals] == [("refund", refund_id)]
    postings = _rows(
        session_factory,
        "SELECT a.owner_type, a.owner_id, a.kind, p.currency, p.amount_minor "
        "FROM gateway.postings p JOIN gateway.ledger_accounts a ON a.id = p.account_id "
        "WHERE p.journal_id = :journal_id ORDER BY p.amount_minor",
        journal_id=journals[0]["id"],
    )
    assert postings == [
        {
            "owner_type": "platform",
            "owner_id": None,
            "kind": "provider_receivable",
            "currency": "USD",
            "amount_minor": -600,
        },
        {
            "owner_type": "merchant",
            "owner_id": h.merchant_id,
            "kind": "merchant_payable",
            "currency": "USD",
            "amount_minor": 600,
        },
    ]


def test_refund_payment_full_refund_nets_the_charge_to_zero_on_both_accounts(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    h = _harness(session_factory, clock)
    payment_id = _charge(h, amount_minor=1500)

    # Act
    _refund(h, payment_id, "refund-net")

    # Assert
    assert _balance(session_factory, "merchant", h.merchant_id, "merchant_payable", "USD") == 0
    assert _balance(session_factory, "platform", None, "provider_receivable", "USD") == 0


def test_refund_payment_keeps_the_ledger_balanced_per_currency_across_currencies(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    h = _harness(session_factory, clock)
    usd = _charge(h, amount_minor=1500, currency="USD")
    jpy = _charge(h, amount_minor=3000, currency="JPY")

    # Act
    _refund(h, usd, "refund-usd", 400)
    _refund(h, jpy, "refund-jpy", 3000)

    # Assert
    assert _ledger_sum_by_currency(session_factory) == {"USD": 0, "JPY": 0}
    assert set(_journal_sums_by_currency(session_factory).values()) == {0}
    assert _balance(session_factory, "merchant", h.merchant_id, "merchant_payable", "USD") == -1100
    assert _balance(session_factory, "merchant", h.merchant_id, "merchant_payable", "JPY") == 0


def test_refund_payment_writes_a_payment_refund_audit_row(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    h = _harness(session_factory, clock)
    payment_id = _charge(h)

    # Act
    _refund(h, payment_id, "refund-audit", 500)

    # Assert
    audit = _rows(
        session_factory,
        "SELECT actor_id, action, target FROM gateway.audit_log WHERE action = 'payment.refund'",
    )
    assert audit == [
        {
            "actor_id": f"merchant:{h.merchant_id}",
            "action": "payment.refund",
            "target": f"payment:{payment_id}",
        }
    ]


def test_refund_payment_replay_with_the_same_key_and_amount_returns_the_stored_body_without_a_second_journal(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    h = _harness(session_factory, clock)
    payment_id = _charge(h, amount_minor=1500)
    first = _refund(h, payment_id, "refund-replay", 500)

    # Act
    second = _refund(h, payment_id, "refund-replay", 500)

    # Assert
    assert (first.status_code, second.status_code) == (201, 201)
    assert second.body == first.body
    assert _count(session_factory, "refunds") == 1
    assert _count(session_factory, "refund_journals") == 1
    assert _payment_totals(session_factory, payment_id)["refunded_minor"] == 500


def test_refund_payment_replay_of_a_full_refund_returns_the_stored_body_even_though_nothing_is_refundable(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    h = _harness(session_factory, clock)
    payment_id = _charge(h, amount_minor=1500)
    first = _refund(h, payment_id, "refund-full-replay")

    # Act
    second = _refund(h, payment_id, "refund-full-replay")

    # Assert
    assert second == first
    assert _count(session_factory, "refunds") == 1


def test_refund_payment_same_key_with_a_different_amount_returns_422_idempotency_key_reused(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    h = _harness(session_factory, clock)
    payment_id = _charge(h, amount_minor=1500)
    _refund(h, payment_id, "refund-reuse", 500)

    # Act
    outcome = _refund(h, payment_id, "refund-reuse", 600)

    # Assert
    assert outcome.status_code == 422
    assert outcome.body == {"error": "idempotency_key_reused"}
    assert _count(session_factory, "refunds") == 1
    assert _payment_totals(session_factory, payment_id)["refunded_minor"] == 500


def test_refund_payment_same_key_on_a_different_payment_returns_422_idempotency_key_reused(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    h = _harness(session_factory, clock)
    first_payment = _charge(h)
    second_payment = _charge(h)
    _refund(h, first_payment, "refund-reuse-payment", 500)

    # Act
    outcome = _refund(h, second_payment, "refund-reuse-payment", 500)

    # Assert
    assert outcome.status_code == 422
    assert outcome.body == {"error": "idempotency_key_reused"}
    assert _payment_totals(session_factory, second_payment)["refunded_minor"] == 0


def test_refund_payment_the_same_key_is_scoped_per_merchant(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    h = _harness(session_factory, clock)
    other = _harness(session_factory, clock, deps=h.deps, name="Other")
    mine = _charge(h)
    theirs = _charge(other)
    _refund(h, mine, "shared-key", 500)

    # Act
    outcome = _refund(other, theirs, "shared-key", 500)

    # Assert
    assert outcome.status_code == 201
    assert outcome.body["payment_id"] == str(theirs)
    assert _count(session_factory, "refunds") == 2


def test_refund_payment_for_an_unknown_payment_returns_404_payment_not_found(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    h = _harness(session_factory, clock)

    # Act
    outcome = _refund(h, uuid.uuid4(), "refund-unknown", 100)

    # Assert
    assert outcome == RefundOutcome(404, {"error": "payment_not_found"})
    assert _count(session_factory, "refunds") == 0


def test_refund_payment_for_another_merchants_payment_returns_404_and_changes_nothing(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    h = _harness(session_factory, clock)
    other = _harness(session_factory, clock, deps=h.deps, name="Other")
    theirs = _charge(other)
    journals_before = _count(session_factory, "journals")

    # Act
    outcome = _refund(h, theirs, "refund-foreign", 100)

    # Assert
    assert outcome == RefundOutcome(404, {"error": "payment_not_found"})
    assert _payment_totals(session_factory, theirs)["refunded_minor"] == 0
    assert _count(session_factory, "journals") == journals_before


def test_refund_payment_of_a_declined_payment_returns_409_payment_not_refundable(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    h = _harness(session_factory, clock)
    payment_id = _charge(h, pan=DECLINED_PAN)

    # Act
    outcome = _refund(h, payment_id, "refund-declined", 100)

    # Assert
    assert outcome == RefundOutcome(409, {"error": "payment_not_refundable"})
    assert _count(session_factory, "refunds") == 0


def test_refund_payment_above_the_refundable_amount_returns_409_with_the_refundable_amount(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    h = _harness(session_factory, clock)
    payment_id = _charge(h, amount_minor=1500)
    _refund(h, payment_id, "refund-first", 1000)

    # Act
    outcome = _refund(h, payment_id, "refund-too-much", 501)

    # Assert
    assert outcome == RefundOutcome(409, {"error": "refund_exceeds_refundable", "refundable_minor": 500})
    assert _payment_totals(session_factory, payment_id)["refunded_minor"] == 1000
    assert _count(session_factory, "refund_journals") == 1


def test_refund_payment_of_exactly_the_refundable_amount_succeeds(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    h = _harness(session_factory, clock)
    payment_id = _charge(h, amount_minor=1500)
    _refund(h, payment_id, "refund-first", 1000)

    # Act
    outcome = _refund(h, payment_id, "refund-exact", 500)

    # Assert
    assert outcome.status_code == 201
    assert outcome.body["payment_refundable_minor"] == 0


def test_refund_payment_when_nothing_is_refundable_returns_409_with_zero_refundable(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    h = _harness(session_factory, clock)
    payment_id = _charge(h, amount_minor=1500)
    _refund(h, payment_id, "refund-all")

    # Act
    with_amount = _refund(h, payment_id, "refund-more", 1)
    without_amount = _refund(h, payment_id, "refund-more-full")

    # Assert
    expected = RefundOutcome(409, {"error": "refund_exceeds_refundable", "refundable_minor": 0})
    assert with_amount == expected
    assert without_amount == expected


def test_refund_payment_error_responses_are_not_stored_under_the_key(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    h = _harness(session_factory, clock)
    payment_id = _charge(h, amount_minor=1500)
    failed = _refund(h, payment_id, "refund-retry", 2000)

    # Act
    retried = _refund(h, payment_id, "refund-retry", 500)

    # Assert
    assert failed.status_code == 409
    assert retried.status_code == 201
    assert retried.body["amount_minor"] == 500
    assert _count(session_factory, "refunds") == 1


def test_refund_payment_leaves_out_the_amount_reserved_by_a_dispute_and_a_chargeback(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    h = _harness(session_factory, clock)
    payment_id = _charge(h, amount_minor=1500)
    _set_totals(session_factory, payment_id, refunded=0, disputed=700, chargeback=300)

    # Act
    too_much = _refund(h, payment_id, "refund-over", 501)
    full = _refund(h, payment_id, "refund-rest")

    # Assert
    assert too_much == RefundOutcome(409, {"error": "refund_exceeds_refundable", "refundable_minor": 500})
    assert full.status_code == 201
    assert full.body["amount_minor"] == 500
    assert full.body["payment_refundable_minor"] == 0
    assert _payment_totals(session_factory, payment_id) == {
        "refunded_minor": 500,
        "disputed_minor": 700,
        "chargeback_minor": 300,
    }


def test_refund_payment_concurrent_refunds_with_different_keys_never_exceed_the_payment_amount(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    h = _harness(session_factory, clock)
    payment_id = _charge(h, amount_minor=1500)
    barrier = threading.Barrier(CONCURRENCY)

    def worker(index: int) -> RefundOutcome:
        barrier.wait(timeout=30)
        return _refund(h, payment_id, f"refund-race-{index}", 400)

    # Act
    with ThreadPoolExecutor(max_workers=CONCURRENCY) as pool:
        futures = [pool.submit(worker, index) for index in range(CONCURRENCY)]
        outcomes = [future.result(timeout=60) for future in futures]

    # Assert
    succeeded = [o for o in outcomes if o.status_code == 201]
    rejected = [o for o in outcomes if o.status_code == 409]
    assert (len(succeeded), len(rejected)) == (3, CONCURRENCY - 3)
    assert {o.body["error"] for o in rejected} == {"refund_exceeds_refundable"}
    assert _payment_totals(session_factory, payment_id)["refunded_minor"] == 1200
    assert _count(session_factory, "refunds") == 3
    assert _count(session_factory, "refund_journals") == 3
    assert _ledger_sum_by_currency(session_factory) == {"USD": 0}
    assert _balance(session_factory, "merchant", h.merchant_id, "merchant_payable", "USD") == -300
    assert _balance(session_factory, "platform", None, "provider_receivable", "USD") == 300


def test_refund_payment_concurrent_full_refunds_let_exactly_one_through(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    h = _harness(session_factory, clock)
    payment_id = _charge(h, amount_minor=1500, currency="EUR")
    barrier = threading.Barrier(CONCURRENCY)

    def worker(index: int) -> RefundOutcome:
        barrier.wait(timeout=30)
        return _refund(h, payment_id, f"refund-full-race-{index}", None)

    # Act
    with ThreadPoolExecutor(max_workers=CONCURRENCY) as pool:
        futures = [pool.submit(worker, index) for index in range(CONCURRENCY)]
        outcomes = [future.result(timeout=60) for future in futures]

    # Assert
    assert sorted(o.status_code for o in outcomes) == [201] + [409] * (CONCURRENCY - 1)
    assert _payment_totals(session_factory, payment_id)["refunded_minor"] == 1500
    assert _ledger_sum_by_currency(session_factory) == {"EUR": 0}
    assert _balance(session_factory, "merchant", h.merchant_id, "merchant_payable", "EUR") == 0


# --- POST /v1/payments/{payment_id}/refunds ------------------------------------------------------------


def _refund_url(payment_id: uuid.UUID | str) -> str:
    return f"/v1/payments/{payment_id}/refunds"


@pytest.mark.parametrize(
    "headers",
    [{}, {"Authorization": "Bearer sk_test_unknown"}, {"Authorization": "Basic c2tfdGVzdF94Og=="}],
    ids=["no_auth", "unknown_bearer_key", "not_a_bearer_scheme"],
)
def test_post_refund_without_a_valid_api_key_returns_401(
    session_factory: sessionmaker[Session], clock: FixedClock, headers: dict[str, str]
) -> None:
    # Arrange
    h = _harness(session_factory, clock)
    payment_id = _charge(h)
    client = _client(h)

    # Act
    response = client.post(
        _refund_url(payment_id), json={"amount_minor": 100}, headers={**headers, "Idempotency-Key": "k"}
    )

    # Assert
    assert response.status_code == 401
    assert _count(session_factory, "refunds") == 0


def test_post_refund_without_an_idempotency_key_returns_400(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    h = _harness(session_factory, clock)
    payment_id = _charge(h)
    client = _client(h)

    # Act
    response = client.post(_refund_url(payment_id), json={"amount_minor": 100}, headers=h.auth)

    # Assert
    assert response.status_code == 400
    assert _count(session_factory, "refunds") == 0


def test_post_refund_returns_201_with_the_refund_body(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    h = _harness(session_factory, clock)
    payment_id = _charge(h, amount_minor=1500)
    client = _client(h)

    # Act
    response = client.post(
        _refund_url(payment_id),
        json={"amount_minor": 500},
        headers={**h.auth, "Idempotency-Key": "http-refund"},
    )

    # Assert
    body = response.json()
    assert response.status_code == 201
    assert body == {
        "refund_id": body["refund_id"],
        "payment_id": str(payment_id),
        "state": "succeeded",
        "amount_minor": 500,
        "currency": "USD",
        "payment_refunded_minor": 500,
        "payment_refundable_minor": 1000,
    }
    assert uuid.UUID(body["refund_id"]).version == 4


@pytest.mark.parametrize("body", [None, {}], ids=["no_body", "empty_object"])
def test_post_refund_without_an_amount_refunds_everything_refundable(
    session_factory: sessionmaker[Session], clock: FixedClock, body: dict[str, int] | None
) -> None:
    # Arrange
    h = _harness(session_factory, clock)
    payment_id = _charge(h, amount_minor=1500)
    client = _client(h)

    # Act
    response = client.post(
        _refund_url(payment_id), json=body, headers={**h.auth, "Idempotency-Key": "http-full"}
    )

    # Assert
    assert response.status_code == 201
    assert response.json()["amount_minor"] == 1500
    assert response.json()["payment_refundable_minor"] == 0


def test_post_refund_replay_with_the_same_key_returns_the_same_body(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    h = _harness(session_factory, clock)
    payment_id = _charge(h)
    client = _client(h)
    headers = {**h.auth, "Idempotency-Key": "http-replay"}
    first = client.post(_refund_url(payment_id), json={"amount_minor": 500}, headers=headers)

    # Act
    second = client.post(_refund_url(payment_id), json={"amount_minor": 500}, headers=headers)

    # Assert
    assert (first.status_code, second.status_code) == (201, 201)
    assert second.json() == first.json()
    assert _count(session_factory, "refunds") == 1


def test_post_refund_same_key_with_a_different_amount_returns_422_idempotency_key_reused(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    h = _harness(session_factory, clock)
    payment_id = _charge(h)
    client = _client(h)
    headers = {**h.auth, "Idempotency-Key": "http-reuse"}
    client.post(_refund_url(payment_id), json={"amount_minor": 500}, headers=headers)

    # Act
    response = client.post(_refund_url(payment_id), json={"amount_minor": 600}, headers=headers)

    # Assert
    assert response.status_code == 422
    assert response.json() == {"error": "idempotency_key_reused"}


def test_post_refund_above_the_refundable_amount_but_within_the_max_returns_409(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    h = _harness(session_factory, clock)
    payment_id = _charge(h, amount_minor=1500)
    client = _client(h)

    # Act
    response = client.post(
        _refund_url(payment_id),
        json={"amount_minor": 1501},
        headers={**h.auth, "Idempotency-Key": "http-exceeds"},
    )

    # Assert
    assert response.status_code == 409
    assert response.json() == {"error": "refund_exceeds_refundable", "refundable_minor": 1500}


def test_post_refund_for_an_unknown_payment_returns_404(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    h = _harness(session_factory, clock)
    client = _client(h)

    # Act
    response = client.post(
        _refund_url(uuid.uuid4()),
        json={"amount_minor": 100},
        headers={**h.auth, "Idempotency-Key": "http-404"},
    )

    # Assert
    assert response.status_code == 404
    assert response.json() == {"error": "payment_not_found"}


def test_post_refund_for_another_merchants_payment_returns_404(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    h = _harness(session_factory, clock)
    other = _harness(session_factory, clock, deps=h.deps, name="Other")
    theirs = _charge(other)
    client = _client(h)

    # Act
    response = client.post(
        _refund_url(theirs), json={"amount_minor": 100}, headers={**h.auth, "Idempotency-Key": "http-foreign"}
    )

    # Assert
    assert response.status_code == 404
    assert _payment_totals(session_factory, theirs)["refunded_minor"] == 0


def test_post_refund_of_a_declined_payment_returns_409_payment_not_refundable(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    h = _harness(session_factory, clock)
    payment_id = _charge(h, pan=DECLINED_PAN)
    client = _client(h)

    # Act
    response = client.post(
        _refund_url(payment_id), json={}, headers={**h.auth, "Idempotency-Key": "http-declined"}
    )

    # Assert
    assert response.status_code == 409
    assert response.json() == {"error": "payment_not_refundable"}


@pytest.mark.parametrize(
    "body",
    [
        {"amount_minor": 0},
        {"amount_minor": -1},
        {"amount_minor": MAX_AMOUNT_MINOR + 1},
        {"amount_minor": "abc"},
        {"amount_minor": 100, "reason": "x"},
    ],
    ids=["zero", "negative", "above_max", "not_an_int", "extra_field"],
)
def test_post_refund_with_an_invalid_body_returns_422_and_refunds_nothing(
    session_factory: sessionmaker[Session], clock: FixedClock, body: dict[str, object]
) -> None:
    # Arrange
    h = _harness(session_factory, clock)
    payment_id = _charge(h)
    client = _client(h)

    # Act
    response = client.post(
        _refund_url(payment_id), json=body, headers={**h.auth, "Idempotency-Key": "http-invalid"}
    )

    # Assert
    assert response.status_code == 422
    assert _count(session_factory, "refunds") == 0
    assert _payment_totals(session_factory, payment_id)["refunded_minor"] == 0


def test_post_refund_with_a_payment_id_that_is_not_a_uuid_returns_422(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    h = _harness(session_factory, clock)
    client = _client(h)

    # Act
    response = client.post(
        _refund_url("not-a-uuid"),
        json={"amount_minor": 100},
        headers={**h.auth, "Idempotency-Key": "http-uuid"},
    )

    # Assert
    assert response.status_code == 422
