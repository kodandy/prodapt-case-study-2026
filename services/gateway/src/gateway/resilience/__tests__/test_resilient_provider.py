"""Tests for gateway.resilience.circuit_breaker.ResilientProvider, alone and through charge_card."""

import re
import threading
import time
import uuid
from collections.abc import Callable, Iterator

import pytest
from gateway.accounts.models import Merchant
from gateway.clock import FixedClock
from gateway.observability.metrics import REGISTRY
from gateway.payments.charge import ChargeDeps, charge_card
from gateway.payments.providers.port import CardRef, ProviderResult, ProviderTimeoutError
from gateway.payments.schemas import ChargeRequest
from gateway.resilience.circuit_breaker import CircuitBreaker, ResilientProvider
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from conftest import TEST_CARD_FINGERPRINT_KEY

CARD = CardRef(token="tok_test", bin="424242", last4="4242")
SUCCEEDED = ProviderResult(status="succeeded", provider_ref="acq_a_ok", decline_code=None)
DECLINED = ProviderResult(status="declined", provider_ref="acq_a_no", decline_code="card_declined")
COOL_DOWN = 30.0


class _FakeInner:
    """Counts charge calls (thread-safe) and delegates to a behaviour callable."""

    name = "acq_a"

    def __init__(self, behaviour: Callable[[], ProviderResult]) -> None:
        self._behaviour = behaviour
        self._lock = threading.Lock()
        self._calls = 0
        self.lookups: list[str] = []
        self.lookup_result: ProviderResult | None = SUCCEEDED

    @property
    def calls(self) -> int:
        with self._lock:
            return self._calls

    def charge(self, provider_key: str, amount_minor: int, currency: str, card: CardRef) -> ProviderResult:
        with self._lock:
            self._calls += 1
        return self._behaviour()

    def lookup(self, provider_key: str) -> ProviderResult | None:
        self.lookups.append(provider_key)
        return self.lookup_result


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def release() -> Iterator[threading.Event]:
    """Frees any inner call still blocked when the test ends."""
    event = threading.Event()
    yield event
    event.set()


def _route() -> str:
    return f"route-{uuid.uuid4().hex}"


def _breaker(route: str, clock: Callable[[], float] = time.monotonic, min_calls: int = 10) -> CircuitBreaker:
    return CircuitBreaker(route, min_calls=min_calls, cool_down_seconds=COOL_DOWN, monotonic=clock)


def _calls(route: str, outcome: str) -> float:
    value = REGISTRY.get_sample_value("gateway_provider_calls_total", {"route": route, "outcome": outcome})
    return 0.0 if value is None else value


def _returning(result: ProviderResult) -> Callable[[], ProviderResult]:
    return lambda: result


def _raising(error: Exception) -> Callable[[], ProviderResult]:
    def behaviour() -> ProviderResult:
        raise error

    return behaviour


def _blocking(release: threading.Event, max_seconds: float = 0.5) -> Callable[[], ProviderResult]:
    def behaviour() -> ProviderResult:
        release.wait(max_seconds)
        return SUCCEEDED

    return behaviour


def _charge(provider: ResilientProvider) -> ProviderResult:
    return provider.charge("key-1", 1500, "USD", CARD)


def _open_breaker(route: str, clock: Callable[[], float] = time.monotonic) -> CircuitBreaker:
    breaker = _breaker(route, clock, min_calls=1)
    breaker.record_failure()
    return breaker


def test_name_is_the_inner_providers_name() -> None:
    # Arrange
    provider = ResilientProvider(_FakeInner(_returning(SUCCEEDED)), _breaker(_route()), timeout_seconds=1.0)

    # Act
    name = provider.name

    # Assert
    assert name == "acq_a"


def test_charge_returns_a_succeeded_result_unchanged_and_calls_inner_once() -> None:
    # Arrange
    route = _route()
    inner = _FakeInner(_returning(SUCCEEDED))
    provider = ResilientProvider(inner, _breaker(route), timeout_seconds=1.0)
    before = _calls(route, "succeeded")

    # Act
    result = _charge(provider)

    # Assert
    assert result == SUCCEEDED
    assert inner.calls == 1
    assert _calls(route, "succeeded") - before == 1.0


def test_charge_returns_a_declined_result_unchanged_and_counts_it_as_declined() -> None:
    # Arrange
    route = _route()
    inner = _FakeInner(_returning(DECLINED))
    provider = ResilientProvider(inner, _breaker(route), timeout_seconds=1.0)
    before = _calls(route, "declined")

    # Act
    result = _charge(provider)

    # Assert
    assert result == DECLINED
    assert inner.calls == 1
    assert _calls(route, "declined") - before == 1.0


def test_declines_count_as_breaker_successes_and_never_open_it() -> None:
    # Arrange
    breaker = _breaker(_route())
    provider = ResilientProvider(_FakeInner(_returning(DECLINED)), breaker, timeout_seconds=1.0)

    # Act
    results = [_charge(provider) for _ in range(12)]

    # Assert
    assert results == [DECLINED] * 12
    assert breaker.state == "closed"


def test_inner_not_done_in_time_raises_provider_timeout_and_is_called_exactly_once(
    release: threading.Event,
) -> None:
    # Arrange
    route = _route()
    inner = _FakeInner(_blocking(release))
    breaker = _breaker(route, min_calls=1)
    provider = ResilientProvider(inner, breaker, timeout_seconds=0.05)
    before = _calls(route, "timeout")

    # Act
    started = time.monotonic()
    with pytest.raises(ProviderTimeoutError):
        _charge(provider)
    elapsed = time.monotonic() - started

    # Assert
    assert elapsed < 0.4
    assert inner.calls == 1
    assert breaker.state == "open"
    assert _calls(route, "timeout") - before == 1.0


@pytest.mark.parametrize(
    ("error", "outcome"),
    [
        (ProviderTimeoutError("slow"), "timeout"),
        (ConnectionError("refused"), "error"),
    ],
    ids=["provider_timeout", "connection_error"],
)
def test_inner_errors_are_reraised_as_the_same_type_and_recorded_as_breaker_failures(
    error: Exception, outcome: str
) -> None:
    # Arrange
    route = _route()
    inner = _FakeInner(_raising(error))
    breaker = _breaker(route, min_calls=1)
    provider = ResilientProvider(inner, breaker, timeout_seconds=1.0)
    before = _calls(route, outcome)

    # Act
    with pytest.raises(type(error)):
        _charge(provider)

    # Assert
    assert inner.calls == 1
    assert breaker.state == "open"
    assert _calls(route, outcome) - before == 1.0


def test_inner_timeout_error_is_reraised_as_timeout_error_and_recorded_as_a_failure() -> None:
    # Arrange
    inner = _FakeInner(_raising(TimeoutError("socket")))
    breaker = _breaker(_route(), min_calls=1)
    provider = ResilientProvider(inner, breaker, timeout_seconds=1.0)

    # Act
    with pytest.raises(TimeoutError):
        _charge(provider)

    # Assert
    assert inner.calls == 1
    assert breaker.state == "open"


def test_open_breaker_short_circuits_without_calling_inner() -> None:
    # Arrange
    route = _route()
    inner = _FakeInner(_returning(SUCCEEDED))
    provider = ResilientProvider(inner, _open_breaker(route), timeout_seconds=1.0)
    before = _calls(route, "short_circuited")

    # Act
    result = _charge(provider)

    # Assert
    assert result.status == "declined"
    assert result.decline_code == "provider_unavailable"
    assert re.fullmatch(r"breaker_[0-9a-f]{20}", result.provider_ref) is not None
    assert inner.calls == 0
    assert _calls(route, "short_circuited") - before == 1.0


def test_each_short_circuited_result_gets_a_distinct_provider_ref() -> None:
    # Arrange
    provider = ResilientProvider(
        _FakeInner(_returning(SUCCEEDED)), _open_breaker(_route()), timeout_seconds=1.0
    )

    # Act
    first = _charge(provider)
    second = _charge(provider)

    # Assert
    assert first.provider_ref != second.provider_ref


def test_lookup_passes_through_to_inner_even_when_the_breaker_is_open() -> None:
    # Arrange
    inner = _FakeInner(_returning(SUCCEEDED))
    inner.lookup_result = DECLINED
    provider = ResilientProvider(inner, _open_breaker(_route()), timeout_seconds=1.0)

    # Act
    found = provider.lookup("key-9")

    # Assert
    assert found == DECLINED
    assert inner.lookups == ["key-9"]
    assert inner.calls == 0


def test_lookup_returns_none_when_inner_has_no_record() -> None:
    # Arrange
    inner = _FakeInner(_returning(SUCCEEDED))
    inner.lookup_result = None
    provider = ResilientProvider(inner, _breaker(_route()), timeout_seconds=1.0)

    # Act
    found = provider.lookup("unknown-key")

    # Assert
    assert found is None
    assert inner.lookups == ["unknown-key"]


def test_probe_after_the_cool_down_reaches_inner_and_a_success_closes_the_breaker() -> None:
    # Arrange
    clock = _Clock()
    route = _route()
    inner = _FakeInner(_returning(SUCCEEDED))
    breaker = _open_breaker(route, clock)
    provider = ResilientProvider(inner, breaker, timeout_seconds=1.0)
    clock.now += COOL_DOWN + 1.0

    # Act
    result = _charge(provider)

    # Assert
    assert result == SUCCEEDED
    assert inner.calls == 1
    assert breaker.state == "closed"


def test_probe_after_the_cool_down_that_fails_reopens_the_breaker() -> None:
    # Arrange
    clock = _Clock()
    inner = _FakeInner(_raising(ConnectionError("still down")))
    breaker = _open_breaker(_route(), clock)
    provider = ResilientProvider(inner, breaker, timeout_seconds=1.0)
    clock.now += COOL_DOWN + 1.0

    # Act
    with pytest.raises(ConnectionError):
        _charge(provider)
    short_circuited = _charge(provider)

    # Assert
    assert inner.calls == 1
    assert breaker.state == "open"
    assert short_circuited.decline_code == "provider_unavailable"


# --- integration with charge_card ---


def _request() -> ChargeRequest:
    return ChargeRequest.model_validate(
        {
            "amount_minor": 1500,
            "currency": "USD",
            "method": "card",
            "card": {"number": "4242424242424242", "exp_month": 12, "exp_year": 2030, "cvc": "123"},
        }
    )


def _merchant(session_factory: sessionmaker[Session], clock: FixedClock) -> uuid.UUID:
    merchant_id = uuid.uuid4()
    with session_factory() as session:
        session.add(Merchant(id=merchant_id, name="Acme", created_at=clock()))
        session.commit()
    return merchant_id


def _deps(
    session_factory: sessionmaker[Session], clock: FixedClock, provider: ResilientProvider
) -> ChargeDeps:
    return ChargeDeps(
        session_factory=session_factory,
        redis=None,
        provider=provider,
        clock=clock,
        card_fingerprint_key=TEST_CARD_FINGERPRINT_KEY,
    )


def _payment_rows(session_factory: sessionmaker[Session]) -> list[tuple[str, str | None]]:
    with session_factory() as session:
        rows = session.execute(text("SELECT state, decline_code FROM gateway.payments")).all()
    return [(row[0], row[1]) for row in rows]


def test_charge_card_with_a_slow_provider_ends_unknown_and_calls_the_provider_once(
    session_factory: sessionmaker[Session], clock: FixedClock, release: threading.Event
) -> None:
    # Arrange
    inner = _FakeInner(_blocking(release))
    provider = ResilientProvider(inner, _breaker(_route()), timeout_seconds=0.05)
    deps = _deps(session_factory, clock, provider)
    merchant_id = _merchant(session_factory, clock)

    # Act
    outcome = charge_card(deps, merchant_id, "key-slow", _request())

    # Assert
    assert outcome.body["state"] == "unknown"
    assert outcome.status_code == 202
    assert inner.calls == 1
    assert _payment_rows(session_factory) == [("unknown", None)]


def test_charge_card_with_an_open_breaker_declines_provider_unavailable_without_calling_inner(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    inner = _FakeInner(_returning(SUCCEEDED))
    provider = ResilientProvider(inner, _open_breaker(_route()), timeout_seconds=1.0)
    deps = _deps(session_factory, clock, provider)
    merchant_id = _merchant(session_factory, clock)

    # Act
    outcome = charge_card(deps, merchant_id, "key-open", _request())

    # Assert
    assert outcome.status_code == 201
    assert outcome.body["state"] == "declined"
    assert outcome.body["decline_code"] == "provider_unavailable"
    assert inner.calls == 0
    assert _payment_rows(session_factory) == [("declined", "provider_unavailable")]
