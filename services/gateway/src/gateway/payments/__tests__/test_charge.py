import dataclasses
import threading
import time
import uuid
from concurrent.futures import Future, ThreadPoolExecutor

import pytest
from contracts.events import PAYMENTS_TOPIC, PaymentEventV1
from gateway.accounts.models import Merchant
from gateway.clock import FixedClock
from gateway.payments.charge import ChargeDeps, ChargeHooks, ChargeOutcome, charge_card
from gateway.payments.providers.port import CardRef, ProviderPort, ProviderResult, ProviderTimeoutError
from gateway.payments.providers.simulator.provider import SimulatorProvider
from gateway.payments.schemas import CardSummary, ChargeRequest, ChargeResponse
from gateway.payments.tokenization.cards import card_fingerprint
from redis import Redis
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from conftest import TEST_CARD_FINGERPRINT_KEY

APPROVE_PAN = "4242424242424242"
CARD_DECLINED_PAN = "4000000000000002"
INSUFFICIENT_FUNDS_PAN = "4000000000009995"
CONCURRENCY = 20


class _InjectedCrashError(Exception):
    pass


class SpyProvider:
    """Wraps a provider, counts `charge` calls (thread-safe) and can hold calls at the provider."""

    def __init__(self, inner: ProviderPort, hold: threading.Event | None = None) -> None:
        self.name = inner.name
        self.entered = threading.Event()
        self._inner = inner
        self._hold = hold
        self._lock = threading.Lock()
        self._calls = 0

    @property
    def calls(self) -> int:
        with self._lock:
            return self._calls

    def charge(self, provider_key: str, amount_minor: int, currency: str, card: CardRef) -> ProviderResult:
        with self._lock:
            self._calls += 1
        self.entered.set()
        if self._hold is not None:
            self._hold.wait(timeout=30)
        return self._inner.charge(provider_key, amount_minor, currency, card)

    def lookup(self, provider_key: str) -> ProviderResult | None:
        return self._inner.lookup(provider_key)


@dataclasses.dataclass(frozen=True)
class Harness:
    deps: ChargeDeps
    spy: SpyProvider
    simulator: SimulatorProvider
    session_factory: sessionmaker[Session]
    merchant_id: uuid.UUID


def _insert_merchant(
    session_factory: sessionmaker[Session], clock: FixedClock, name: str = "Acme"
) -> uuid.UUID:
    merchant_id = uuid.uuid4()
    with session_factory() as session:
        session.add(Merchant(id=merchant_id, name=name, created_at=clock()))
        session.commit()
    return merchant_id


def _harness(
    session_factory: sessionmaker[Session],
    clock: FixedClock,
    redis: Redis | None,
    hold: threading.Event | None = None,
    idempotency_wait_seconds: float = 5.0,
) -> Harness:
    simulator = SimulatorProvider(session_factory, clock)
    spy = SpyProvider(simulator, hold)
    deps = ChargeDeps(
        session_factory=session_factory,
        redis=redis,
        provider=spy,
        clock=clock,
        card_fingerprint_key=TEST_CARD_FINGERPRINT_KEY,
        idempotency_wait_seconds=idempotency_wait_seconds,
    )
    return Harness(deps, spy, simulator, session_factory, _insert_merchant(session_factory, clock))


def _request(pan: str = APPROVE_PAN, amount_minor: int = 1500, currency: str = "USD") -> ChargeRequest:
    return ChargeRequest.model_validate(
        {
            "amount_minor": amount_minor,
            "currency": currency,
            "method": "card",
            "card": {"number": pan, "exp_month": 12, "exp_year": 2030, "cvc": "123"},
        }
    )


def _rows(session_factory: sessionmaker[Session], query: str, **params: object) -> list[dict[str, object]]:
    with session_factory() as session:
        return [dict(row) for row in session.execute(text(query), params).mappings()]


def _count(session_factory: sessionmaker[Session], table: str) -> int:
    queries = {
        "journals": "SELECT count(*) FROM gateway.journal_entries",
        "postings": "SELECT count(*) FROM gateway.postings",
        "outbox": "SELECT count(*) FROM gateway.outbox",
        "audit": "SELECT count(*) FROM gateway.audit_log",
        "payments": "SELECT count(*) FROM gateway.payments",
    }
    with session_factory() as session:
        return int(session.execute(text(queries[table])).scalar_one())


def _all_written_text(session_factory: sessionmaker[Session]) -> str:
    """Every column of every row in the gateway and simulator schemas, rendered as XML text."""
    query = (
        "SELECT query_to_xml(format('select * from %I.%I', table_schema, table_name), "
        "false, false, '')::text AS dump FROM information_schema.tables "
        "WHERE table_schema IN ('gateway', 'simulator') AND table_type = 'BASE TABLE'"
    )
    return "\n".join(str(row["dump"]) for row in _rows(session_factory, query))


def _expected_body(
    body: dict[str, object],
    *,
    state: str,
    bin_: str,
    last4: str,
    decline_code: str | None,
    amount: int = 1500,
) -> dict[str, object]:
    return ChargeResponse(
        payment_id=uuid.UUID(str(body["payment_id"])),
        state=state,
        amount_minor=amount,
        currency="USD",
        method="card",
        card=CardSummary(bin=bin_, last4=last4),
        decline_code=decline_code,
    ).model_dump(mode="json")


def _run_concurrently(h: Harness, key: str, request: ChargeRequest) -> list[ChargeOutcome]:
    barrier = threading.Barrier(CONCURRENCY)

    def worker() -> ChargeOutcome:
        barrier.wait(timeout=30)
        return charge_card(h.deps, h.merchant_id, key, request)

    with ThreadPoolExecutor(max_workers=CONCURRENCY) as pool:
        futures = [pool.submit(worker) for _ in range(CONCURRENCY)]
        return [future.result(timeout=60) for future in futures]


def _wait_until_at_provider(spy: SpyProvider, in_flight: Future[ChargeOutcome]) -> None:
    deadline = time.monotonic() + 10
    while not spy.entered.is_set() and time.monotonic() < deadline:
        if in_flight.done():
            in_flight.result()  # surfaces the failure instead of waiting out the deadline
        time.sleep(0.02)
    assert spy.entered.is_set()


# --- approve -------------------------------------------------------------------------------------------


def test_charge_card_approves_and_returns_the_charge_response_body(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    h = _harness(session_factory, clock, redis=None)

    # Act
    outcome = charge_card(h.deps, h.merchant_id, "key-approve", _request())

    # Assert
    assert outcome.status_code == 201
    assert outcome.body == _expected_body(
        outcome.body, state="succeeded", bin_="424242", last4="4242", decline_code=None
    )
    payments = _rows(
        session_factory,
        "SELECT id, merchant_id, amount_minor, currency, method, state, decline_code FROM gateway.payments",
    )
    assert payments == [
        {
            "id": uuid.UUID(str(outcome.body["payment_id"])),
            "merchant_id": h.merchant_id,
            "amount_minor": 1500,
            "currency": "USD",
            "method": "card",
            "state": "succeeded",
            "decline_code": None,
        }
    ]


def test_charge_card_approve_posts_one_balanced_journal_of_receivable_against_payable(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    h = _harness(session_factory, clock, redis=None)

    # Act
    charge_card(h.deps, h.merchant_id, "key-ledger", _request(amount_minor=1500))

    # Assert
    postings = _rows(
        session_factory,
        "SELECT p.amount_minor, p.currency, a.owner_type, a.owner_id, a.kind "
        "FROM gateway.postings p JOIN gateway.ledger_accounts a ON a.id = p.account_id "
        "ORDER BY p.amount_minor",
    )
    assert _count(session_factory, "journals") == 1
    assert len(postings) == 2
    payable, receivable = postings
    assert (payable["amount_minor"], payable["currency"]) == (-1500, "USD")
    assert (payable["owner_type"], payable["owner_id"]) == ("merchant", h.merchant_id)
    assert "payable" in str(payable["kind"])
    assert (receivable["amount_minor"], receivable["currency"]) == (1500, "USD")
    assert (receivable["owner_type"], receivable["owner_id"]) == ("platform", None)
    assert "receivable" in str(receivable["kind"])


def test_charge_card_approve_writes_a_payment_succeeded_outbox_event(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    h = _harness(session_factory, clock, redis=None)

    # Act
    outcome = charge_card(h.deps, h.merchant_id, "key-outbox", _request())

    # Assert
    rows = _rows(session_factory, "SELECT event_id, topic, payload FROM gateway.outbox")
    assert len(rows) == 1
    assert rows[0]["topic"] == PAYMENTS_TOPIC
    event = PaymentEventV1.model_validate(rows[0]["payload"])
    assert event.event_id == rows[0]["event_id"]
    assert event.event_type == "payment.succeeded"
    assert event.state == "succeeded"
    assert event.payment_id == uuid.UUID(str(outcome.body["payment_id"]))
    assert event.merchant_id == h.merchant_id
    assert (event.amount_minor, event.currency, event.method) == (1500, "USD", "card")
    assert (event.card_bin, event.card_last4, event.decline_code) == ("424242", "4242", None)
    assert event.provider == h.spy.name


def test_charge_card_approve_writes_one_audit_row_and_stores_the_response_on_the_idempotency_row(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    h = _harness(session_factory, clock, redis=None)

    # Act
    outcome = charge_card(h.deps, h.merchant_id, "key-store", _request())

    # Assert
    records = _rows(
        session_factory,
        "SELECT merchant_id, key, payment_id, response_status, response_body FROM gateway.idempotency_keys",
    )
    assert _count(session_factory, "audit") == 1
    assert records == [
        {
            "merchant_id": h.merchant_id,
            "key": "key-store",
            "payment_id": uuid.UUID(str(outcome.body["payment_id"])),
            "response_status": 201,
            "response_body": outcome.body,
        }
    ]


@pytest.mark.parametrize("pan", [APPROVE_PAN, CARD_DECLINED_PAN])
def test_charge_card_never_writes_the_full_pan_to_any_gateway_or_simulator_table(
    session_factory: sessionmaker[Session], clock: FixedClock, pan: str
) -> None:
    # Arrange
    h = _harness(session_factory, clock, redis=None)

    # Act
    charge_card(h.deps, h.merchant_id, "key-pan", _request(pan=pan))

    # Assert
    written = _all_written_text(session_factory)
    assert pan not in written
    assert pan[:6] in written  # sanity: the dump does cover the card_tokens BIN


def test_charge_card_vaults_the_card_as_token_bin_last4_and_keyed_fingerprint(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    h = _harness(session_factory, clock, redis=None)

    # Act
    charge_card(h.deps, h.merchant_id, "key-vault", _request())

    # Assert
    tokens = _rows(
        session_factory, "SELECT bin, last4, exp_month, exp_year, fingerprint FROM gateway.card_tokens"
    )
    assert tokens == [
        {
            "bin": "424242",
            "last4": "4242",
            "exp_month": 12,
            "exp_year": 2030,
            "fingerprint": card_fingerprint(APPROVE_PAN, TEST_CARD_FINGERPRINT_KEY),
        }
    ]


# --- decline -------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("pan", "decline_code", "last4"),
    [(CARD_DECLINED_PAN, "card_declined", "0002"), (INSUFFICIENT_FUNDS_PAN, "insufficient_funds", "9995")],
)
def test_charge_card_decline_returns_201_declined_without_a_journal_and_with_a_declined_event(
    session_factory: sessionmaker[Session], clock: FixedClock, pan: str, decline_code: str, last4: str
) -> None:
    # Arrange
    h = _harness(session_factory, clock, redis=None)

    # Act
    outcome = charge_card(h.deps, h.merchant_id, "key-decline", _request(pan=pan))

    # Assert
    assert outcome.status_code == 201
    assert outcome.body == _expected_body(
        outcome.body, state="declined", bin_="400000", last4=last4, decline_code=decline_code
    )
    assert _count(session_factory, "journals") == 0
    assert _count(session_factory, "postings") == 0
    events = _rows(session_factory, "SELECT topic, payload FROM gateway.outbox")
    assert len(events) == 1
    assert events[0]["topic"] == PAYMENTS_TOPIC
    event = PaymentEventV1.model_validate(events[0]["payload"])
    assert event.event_type == "payment.declined"
    assert event.state == "declined"
    assert event.decline_code == decline_code
    assert event.payment_id == uuid.UUID(str(outcome.body["payment_id"]))


# --- idempotency ---------------------------------------------------------------------------------------


def test_charge_card_same_key_and_request_sequentially_returns_the_same_body_with_one_provider_call(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    h = _harness(session_factory, clock, redis=None)
    request = _request()

    # Act
    first = charge_card(h.deps, h.merchant_id, "key-repeat", request)
    second = charge_card(h.deps, h.merchant_id, "key-repeat", request)

    # Assert
    assert (first.status_code, second.status_code) == (201, 201)
    assert second.body == first.body
    assert h.spy.calls == 1
    assert h.simulator.charge_count() == 1
    assert _count(session_factory, "payments") == 1
    assert _count(session_factory, "journals") == 1


def test_charge_card_same_key_with_a_different_amount_is_rejected_as_key_reuse(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    h = _harness(session_factory, clock, redis=None)
    charge_card(h.deps, h.merchant_id, "key-reuse", _request(amount_minor=1500))

    # Act
    outcome = charge_card(h.deps, h.merchant_id, "key-reuse", _request(amount_minor=1501))

    # Assert
    assert outcome.status_code == 422
    assert outcome.body == {"error": "idempotency_key_reused"}
    assert h.spy.calls == 1
    assert _count(session_factory, "payments") == 1


def test_charge_card_second_caller_gets_409_while_the_first_is_still_at_the_provider(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    release = threading.Event()
    h = _harness(session_factory, clock, redis=None, hold=release, idempotency_wait_seconds=0.2)
    request = _request()
    pool = ThreadPoolExecutor(max_workers=1)
    try:
        in_flight = pool.submit(charge_card, h.deps, h.merchant_id, "key-slow", request)
        _wait_until_at_provider(h.spy, in_flight)

        # Act
        second = charge_card(h.deps, h.merchant_id, "key-slow", request)
    finally:
        release.set()
        pool.shutdown(wait=True)

    # Assert
    assert second.status_code == 409
    assert second.body == {"error": "idempotency_in_progress"}
    first = in_flight.result(timeout=30)
    assert first.status_code == 201
    assert first.body["state"] == "succeeded"
    assert h.spy.calls == 1
    assert h.simulator.charge_count() == 1


def test_charge_card_different_merchants_use_the_same_key_independently(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    h = _harness(session_factory, clock, redis=None)
    other_merchant_id = _insert_merchant(session_factory, clock, name="Globex")
    request = _request()

    # Act
    first = charge_card(h.deps, h.merchant_id, "shared-key", request)
    second = charge_card(h.deps, other_merchant_id, "shared-key", request)

    # Assert
    assert (first.status_code, second.status_code) == (201, 201)
    assert first.body["payment_id"] != second.body["payment_id"]
    assert h.spy.calls == 2
    assert _count(session_factory, "journals") == 2
    owners = _rows(session_factory, "SELECT merchant_id FROM gateway.payments ORDER BY merchant_id")
    assert owners == sorted(
        [{"merchant_id": h.merchant_id}, {"merchant_id": other_merchant_id}], key=lambda r: r["merchant_id"]
    )


# --- E1 ------------------------------------------------------------------------------------------------


def _assert_single_charge_for_all_callers(h: Harness, outcomes: list[ChargeOutcome]) -> None:
    assert len(outcomes) == CONCURRENCY
    assert {outcome.status_code for outcome in outcomes} == {201}
    assert all(outcome.body == outcomes[0].body for outcome in outcomes)
    assert outcomes[0].body["state"] == "succeeded"
    assert h.spy.calls == 1
    assert h.simulator.charge_count() == 1
    assert _count(h.session_factory, "journals") == 1
    assert _count(h.session_factory, "payments") == 1
    assert _count(h.session_factory, "outbox") == 1


def test_e1_twenty_concurrent_charges_with_one_key_make_one_provider_charge_with_redis(
    session_factory: sessionmaker[Session], clock: FixedClock, redis_client: Redis
) -> None:
    # Arrange
    h = _harness(session_factory, clock, redis=redis_client)

    # Act
    outcomes = _run_concurrently(h, "key-e1", _request())

    # Assert
    _assert_single_charge_for_all_callers(h, outcomes)


def test_e1_twenty_concurrent_charges_with_one_key_make_one_provider_charge_without_redis(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    h = _harness(session_factory, clock, redis=None)

    # Act
    outcomes = _run_concurrently(h, "key-e1-no-redis", _request())

    # Assert
    _assert_single_charge_for_all_callers(h, outcomes)


# --- E2 ------------------------------------------------------------------------------------------------


def _raise_injected_crash() -> None:
    raise _InjectedCrashError("crash after tx2 commit")


def test_e2_crash_after_tx2_commit_then_retry_returns_the_stored_body_without_a_second_charge(
    session_factory: sessionmaker[Session], clock: FixedClock, redis_client: Redis
) -> None:
    # Arrange
    h = _harness(session_factory, clock, redis=redis_client)
    crashing = dataclasses.replace(h.deps, hooks=ChargeHooks(after_tx2_commit=_raise_injected_crash))
    request = _request()

    # Act
    with pytest.raises(_InjectedCrashError):
        charge_card(crashing, h.merchant_id, "key-e2", request)
    retry = charge_card(h.deps, h.merchant_id, "key-e2", request)

    # Assert
    stored = _rows(
        session_factory,
        "SELECT response_status, response_body FROM gateway.idempotency_keys WHERE key = 'key-e2'",
    )
    assert retry.status_code == 201
    assert stored == [{"response_status": 201, "response_body": retry.body}]
    assert retry.body["state"] == "succeeded"
    assert h.spy.calls == 1
    assert h.simulator.charge_count() == 1
    assert _count(session_factory, "journals") == 1
    assert _count(session_factory, "outbox") == 1


# --- H1 / H2: unknown outcome is stored and replayed; only transport failures count as unknown ---------


class FailingProvider:
    """Provider whose `charge` always raises the given error type; counts calls (thread-safe)."""

    def __init__(self, error_type: type[Exception], name: str = "acq_failing") -> None:
        self.name = name
        self._error_type = error_type
        self._lock = threading.Lock()
        self._calls = 0

    @property
    def calls(self) -> int:
        with self._lock:
            return self._calls

    def charge(self, provider_key: str, amount_minor: int, currency: str, card: CardRef) -> ProviderResult:
        with self._lock:
            self._calls += 1
        raise self._error_type("boom")

    def lookup(self, provider_key: str) -> ProviderResult | None:
        return None


def _failing_harness(
    session_factory: sessionmaker[Session],
    clock: FixedClock,
    error_type: type[Exception],
    redis: Redis | None = None,
    idempotency_wait_seconds: float = 5.0,
) -> tuple[ChargeDeps, FailingProvider, uuid.UUID]:
    base = _harness(session_factory, clock, redis=redis, idempotency_wait_seconds=idempotency_wait_seconds)
    provider = FailingProvider(error_type)
    return dataclasses.replace(base.deps, provider=provider), provider, base.merchant_id


def _stored_idempotency_rows(session_factory: sessionmaker[Session]) -> list[dict[str, object]]:
    return _rows(
        session_factory,
        "SELECT key, payment_id, response_status, response_body FROM gateway.idempotency_keys",
    )


UNKNOWN_ERROR_TYPES = [ProviderTimeoutError, ConnectionError, TimeoutError]
PROPAGATING_ERROR_TYPES = [ValueError, RuntimeError]


def test_h1_unknown_outcome_returns_202_and_stores_status_and_body_on_the_idempotency_row(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    deps, _, merchant_id = _failing_harness(session_factory, clock, ProviderTimeoutError)

    # Act
    outcome = charge_card(deps, merchant_id, "key-unknown", _request())

    # Assert
    assert outcome.status_code == 202
    assert outcome.body == _expected_body(
        outcome.body, state="unknown", bin_="424242", last4="4242", decline_code=None
    )
    assert _stored_idempotency_rows(session_factory) == [
        {
            "key": "key-unknown",
            "payment_id": uuid.UUID(str(outcome.body["payment_id"])),
            "response_status": 202,
            "response_body": outcome.body,
        }
    ]


@pytest.mark.parametrize("use_redis", [False, True], ids=["without_redis", "with_redis"])
def test_h1_retry_after_unknown_replays_the_202_body_without_waiting_or_calling_the_provider_again(
    session_factory: sessionmaker[Session], clock: FixedClock, redis_client: Redis, use_redis: bool
) -> None:
    # Arrange
    deps, provider, merchant_id = _failing_harness(
        session_factory,
        clock,
        ProviderTimeoutError,
        redis=redis_client if use_redis else None,
        idempotency_wait_seconds=5.0,
    )
    request = _request()
    first = charge_card(deps, merchant_id, "key-unknown-retry", request)

    # Act
    started = time.monotonic()
    retry = charge_card(deps, merchant_id, "key-unknown-retry", request)
    elapsed = time.monotonic() - started

    # Assert
    assert first.status_code == 202
    assert retry.status_code == 202
    assert retry.body == first.body
    assert elapsed < 2.0
    assert provider.calls == 1
    assert _count(session_factory, "payments") == 1


def test_h1_retry_after_unknown_with_a_different_body_is_rejected_as_key_reuse(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    deps, provider, merchant_id = _failing_harness(session_factory, clock, ProviderTimeoutError)
    charge_card(deps, merchant_id, "key-unknown-reuse", _request(amount_minor=1500))

    # Act
    outcome = charge_card(deps, merchant_id, "key-unknown-reuse", _request(amount_minor=1501))

    # Assert
    assert outcome.status_code == 422
    assert outcome.body == {"error": "idempotency_key_reused"}
    assert provider.calls == 1
    assert _count(session_factory, "payments") == 1


def test_h1_unknown_outcome_writes_no_journal_no_outbox_row_and_no_audit_row(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    deps, _, merchant_id = _failing_harness(session_factory, clock, ProviderTimeoutError)

    # Act
    charge_card(deps, merchant_id, "key-unknown-quiet", _request())

    # Assert
    assert _count(session_factory, "journals") == 0
    assert _count(session_factory, "postings") == 0
    assert _count(session_factory, "outbox") == 0
    assert _count(session_factory, "audit") == 0
    assert _rows(session_factory, "SELECT state FROM gateway.payments") == [{"state": "unknown"}]


@pytest.mark.parametrize("error_type", UNKNOWN_ERROR_TYPES)
def test_h2_transport_level_provider_failures_are_an_unknown_outcome_returned_as_202(
    session_factory: sessionmaker[Session], clock: FixedClock, error_type: type[Exception]
) -> None:
    # Arrange
    deps, provider, merchant_id = _failing_harness(session_factory, clock, error_type)

    # Act
    outcome = charge_card(deps, merchant_id, "key-transport", _request())

    # Assert
    assert outcome.status_code == 202
    assert outcome.body == _expected_body(
        outcome.body, state="unknown", bin_="424242", last4="4242", decline_code=None
    )
    assert provider.calls == 1
    assert _rows(session_factory, "SELECT state FROM gateway.payments") == [{"state": "unknown"}]


@pytest.mark.parametrize("error_type", PROPAGATING_ERROR_TYPES)
def test_h2_any_other_provider_exception_propagates_with_its_type_and_is_not_turned_into_202(
    session_factory: sessionmaker[Session], clock: FixedClock, error_type: type[Exception]
) -> None:
    # Arrange
    deps, provider, merchant_id = _failing_harness(session_factory, clock, error_type)

    # Act
    with pytest.raises(error_type) as raised:
        charge_card(deps, merchant_id, "key-bug", _request())

    # Assert
    assert type(raised.value) is error_type
    assert str(raised.value) == "boom"
    assert provider.calls == 1


@pytest.mark.parametrize("error_type", PROPAGATING_ERROR_TYPES)
def test_h2_a_propagated_provider_exception_leaves_the_payment_unfinalized(
    session_factory: sessionmaker[Session], clock: FixedClock, error_type: type[Exception]
) -> None:
    # Arrange
    deps, _, merchant_id = _failing_harness(session_factory, clock, error_type)

    # Act
    with pytest.raises(error_type):
        charge_card(deps, merchant_id, "key-bug-state", _request())

    # Assert
    assert _rows(session_factory, "SELECT state FROM gateway.payments") == [{"state": "created"}]
    assert _rows(session_factory, "SELECT state FROM gateway.payment_attempts") == [{"state": "created"}]
    rows = _stored_idempotency_rows(session_factory)
    assert [(row["key"], row["response_status"], row["response_body"]) for row in rows] == [
        ("key-bug-state", None, None)
    ]
    assert _count(session_factory, "journals") == 0
    assert _count(session_factory, "outbox") == 0
    assert _count(session_factory, "audit") == 0
