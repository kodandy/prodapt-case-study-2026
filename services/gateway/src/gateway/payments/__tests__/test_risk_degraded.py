"""RED tests for B7.1 degraded scoring, from docs/specs/disputes-notifications.md section 5.

`assess_charge_risk(..., velocity_lookup=None)` runs the velocity lookup in a SAVEPOINT. When it raises
`SQLAlchemyError` the savepoint is rolled back and the charge is scored degraded: reason `degraded_scoring`,
only the amount rule, always flagged, and a high-amount charge is blocked with decline code
`risk_unavailable`. The lookup is called as `velocity_lookup(session, now, card_fingerprint)` and returns
`(recent payments, declines)`, the same shape as the module attribute `card_activity` it defaults to.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from gateway.accounts.models import Merchant
from gateway.accounts.service import create_merchant
from gateway.app import GatewayAppDeps, create_app
from gateway.clock import FixedClock
from gateway.payments import risk as risk_module
from gateway.payments.charge import ChargeDeps, ChargeOutcome, charge_card
from gateway.payments.providers.port import CardRef, ProviderPort, ProviderResult
from gateway.payments.providers.simulator.provider import SimulatorProvider
from gateway.payments.risk import HIGH_AMOUNT_MINOR, RiskAssessment, assess_charge_risk
from gateway.payments.schemas import ChargeRequest
from sqlalchemy import func, select, text
from sqlalchemy.exc import OperationalError, ProgrammingError, SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from conftest import TEST_API_KEY_PEPPER, TEST_CARD_FINGERPRINT_KEY

APPROVE_PAN = "4242424242424242"
FINGERPRINT = "a" * 64
SMALL_AMOUNT = 1500
HIGH_USD = HIGH_AMOUNT_MINOR["USD"]
DEGRADED_REASON = "degraded_scoring"
UNAVAILABLE = "risk_unavailable"


class RecordingLookup:
    """A velocity lookup double that records its calls and either returns a pair or raises."""

    def __init__(self, result: tuple[int, int], error: Exception | None) -> None:
        self._result = result
        self._error = error
        self.calls: list[tuple[Session, datetime, str]] = []

    def __call__(self, session: Session, now: datetime, card_fingerprint: str) -> tuple[int, int]:
        self.calls.append((session, now, card_fingerprint))
        if self._error is not None:
            raise self._error
        return self._result


def _healthy_lookup(recent: int, declines: int) -> RecordingLookup:
    return RecordingLookup(result=(recent, declines), error=None)


def _failing_lookup(error: Exception | None = None) -> RecordingLookup:
    return RecordingLookup(result=(0, 0), error=error or SQLAlchemyError("velocity store unavailable"))


def _broken_sql_lookup(session: Session, now: datetime, card_fingerprint: str) -> tuple[int, int]:
    """Fails the way a real outage does: a statement that errors inside the transaction."""
    session.execute(text("SELECT * FROM gateway.no_such_velocity_table"))
    return 0, 0


def _assess(
    session: Session,
    now: datetime,
    lookup: RecordingLookup | None,
    amount_minor: int = SMALL_AMOUNT,
    currency: str = "USD",
) -> RiskAssessment:
    return assess_charge_risk(session, now, FINGERPRINT, amount_minor, currency, velocity_lookup=lookup)


# --- assessment: healthy lookup ------------------------------------------------------------------------


def test_a_healthy_lookup_is_not_degraded_and_keeps_the_risk_blocked_decline_code(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    lookup = _healthy_lookup(recent=0, declines=0)

    # Act
    with session_factory() as session:
        assessment = _assess(session, clock(), lookup)

    # Assert
    assert assessment.degraded is False
    assert assessment.decline_code == "risk_blocked"
    assert assessment.reasons == ()
    assert (assessment.flagged, assessment.blocked) == (False, False)


def test_a_normally_blocked_charge_keeps_the_risk_blocked_decline_code(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    lookup = _healthy_lookup(recent=5, declines=5)

    # Act
    with session_factory() as session:
        assessment = _assess(session, clock(), lookup, amount_minor=HIGH_USD)

    # Assert
    assert assessment.blocked is True
    assert assessment.degraded is False
    assert assessment.decline_code == "risk_blocked"
    assert set(assessment.reasons) == {"high_amount", "card_velocity", "recent_declines"}


def test_the_velocity_lookup_is_called_once_with_the_session_the_time_and_the_fingerprint(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    lookup = _healthy_lookup(recent=0, declines=0)

    # Act
    with session_factory() as session:
        _assess(session, clock(), lookup)
        calls = [(seen is session, now, fingerprint) for seen, now, fingerprint in lookup.calls]

    # Assert
    assert calls == [(True, clock(), FINGERPRINT)]


def test_an_injected_lookup_drives_the_velocity_and_decline_rules(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    lookup = _healthy_lookup(recent=3, declines=2)

    # Act
    with session_factory() as session:
        assessment = _assess(session, clock(), lookup)

    # Assert
    assert set(assessment.reasons) == {"card_velocity", "recent_declines"}
    assert (assessment.flagged, assessment.blocked, assessment.degraded) == (True, False, False)


def test_without_an_injected_lookup_the_card_activity_module_attribute_is_used(
    session_factory: sessionmaker[Session], clock: FixedClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Arrange
    replacement = _healthy_lookup(recent=3, declines=0)
    monkeypatch.setattr(risk_module, "card_activity", replacement)

    # Act
    with session_factory() as session:
        assessment = assess_charge_risk(session, clock(), FINGERPRINT, SMALL_AMOUNT, "USD")

    # Assert
    assert len(replacement.calls) == 1
    assert assessment.reasons == ("card_velocity",)
    assert assessment.degraded is False


def test_without_an_injected_lookup_a_failing_card_activity_degrades_the_charge(
    session_factory: sessionmaker[Session], clock: FixedClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Arrange
    monkeypatch.setattr(risk_module, "card_activity", _failing_lookup())

    # Act
    with session_factory() as session:
        assessment = assess_charge_risk(session, clock(), FINGERPRINT, SMALL_AMOUNT, "USD")

    # Assert
    assert assessment.degraded is True
    assert assessment.reasons == (DEGRADED_REASON,)


def test_with_the_real_card_activity_an_empty_history_scores_clean(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange / Act
    with session_factory() as session:
        assessment = assess_charge_risk(session, clock(), FINGERPRINT, SMALL_AMOUNT, "USD")

    # Assert
    assert assessment.score == Decimal("0.00")
    assert assessment.reasons == ()
    assert (assessment.flagged, assessment.blocked, assessment.degraded) == (False, False, False)
    assert assessment.decline_code == "risk_blocked"


def test_the_assessment_with_the_new_fields_is_a_hashable_value_object() -> None:
    # Arrange
    def build() -> RiskAssessment:
        return RiskAssessment(
            score=Decimal("0.50"),
            reasons=(DEGRADED_REASON,),
            flagged=True,
            blocked=False,
            degraded=True,
            decline_code="risk_blocked",
        )

    # Act
    first, second = build(), build()

    # Assert
    assert first == second
    assert hash(first) == hash(second)


# --- assessment: failing lookup ------------------------------------------------------------------------


@pytest.mark.parametrize(
    "error",
    [
        SQLAlchemyError("generic"),
        OperationalError("SELECT 1", {}, Exception("connection lost")),
        ProgrammingError("SELECT 1", {}, Exception("relation missing")),
    ],
    ids=["generic", "operational", "programming"],
)
def test_any_sqlalchemy_error_from_the_lookup_degrades_the_charge(
    session_factory: sessionmaker[Session], clock: FixedClock, error: SQLAlchemyError
) -> None:
    # Arrange
    lookup = _failing_lookup(error)

    # Act
    with session_factory() as session:
        assessment = _assess(session, clock(), lookup)

    # Assert
    assert len(lookup.calls) == 1
    assert assessment.degraded is True
    assert assessment.reasons == (DEGRADED_REASON,)


def test_a_degraded_low_amount_charge_is_flagged_but_not_blocked(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    lookup = _failing_lookup()

    # Act
    with session_factory() as session:
        assessment = _assess(session, clock(), lookup)

    # Assert
    assert assessment.flagged is True
    assert assessment.blocked is False
    assert assessment.reasons == (DEGRADED_REASON,)


def test_a_degraded_high_amount_charge_is_flagged_and_blocked_with_risk_unavailable(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    lookup = _failing_lookup()

    # Act
    with session_factory() as session:
        assessment = _assess(session, clock(), lookup, amount_minor=HIGH_USD)

    # Assert
    assert assessment.degraded is True
    assert assessment.flagged is True
    assert assessment.blocked is True
    assert assessment.decline_code == UNAVAILABLE
    assert set(assessment.reasons) == {"high_amount", DEGRADED_REASON}


@pytest.mark.parametrize("currency", ["USD", "EUR", "JPY", "INR"])
def test_the_degraded_block_starts_exactly_at_the_high_amount_threshold(
    session_factory: sessionmaker[Session], clock: FixedClock, currency: str
) -> None:
    # Arrange
    threshold = HIGH_AMOUNT_MINOR[currency]

    # Act
    with session_factory() as session:
        just_below = _assess(session, clock(), _failing_lookup(), threshold - 1, currency)
        at_threshold = _assess(session, clock(), _failing_lookup(), threshold, currency)

    # Assert
    assert (just_below.blocked, just_below.reasons) == (False, (DEGRADED_REASON,))
    assert (at_threshold.blocked, at_threshold.decline_code) == (True, UNAVAILABLE)


def test_a_degraded_score_applies_only_the_amount_rule_so_it_ignores_what_the_lookup_would_have_said(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    failing = _failing_lookup()
    healthy_busy = _healthy_lookup(recent=9, declines=9)

    # Act
    with session_factory() as session:
        degraded = _assess(session, clock(), failing)
        busy = _assess(session, clock(), healthy_busy)

    # Assert
    assert degraded.reasons == (DEGRADED_REASON,)
    assert set(busy.reasons) == {"card_velocity", "recent_declines"}


def test_a_non_sqlalchemy_error_from_the_lookup_is_not_swallowed(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    lookup = _failing_lookup(RuntimeError("programming bug"))

    # Act / Assert
    with session_factory() as session, pytest.raises(RuntimeError, match="programming bug"):
        _assess(session, clock(), lookup)


def test_a_failed_lookup_statement_rolls_back_only_its_savepoint(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    with session_factory() as session:
        session.add(Merchant(id=uuid.uuid4(), name="Written before the lookup", created_at=clock()))
        session.flush()

        # Act
        assessment = assess_charge_risk(
            session, clock(), FINGERPRINT, SMALL_AMOUNT, "USD", velocity_lookup=_broken_sql_lookup
        )
        merchants = session.execute(select(func.count()).select_from(Merchant)).scalar_one()
        still_usable = session.execute(text("SELECT 1")).scalar_one()

    # Assert
    assert assessment.degraded is True
    assert merchants == 1
    assert still_usable == 1


# --- charge path ----------------------------------------------------------------------------------------


class CountingProvider:
    def __init__(self, inner: ProviderPort) -> None:
        self.name = inner.name
        self._inner = inner
        self.calls = 0

    def charge(self, provider_key: str, amount_minor: int, currency: str, card: CardRef) -> ProviderResult:
        self.calls += 1
        return self._inner.charge(provider_key, amount_minor, currency, card)

    def lookup(self, provider_key: str) -> ProviderResult | None:
        return self._inner.lookup(provider_key)


@dataclass(frozen=True)
class Harness:
    deps: ChargeDeps
    provider: CountingProvider
    session_factory: sessionmaker[Session]
    merchant_id: uuid.UUID
    auth: dict[str, str]
    client: TestClient


def _harness(session_factory: sessionmaker[Session], clock: FixedClock) -> Harness:
    provider = CountingProvider(SimulatorProvider(session_factory, clock))
    deps = ChargeDeps(
        session_factory=session_factory,
        redis=None,
        provider=provider,
        clock=clock,
        card_fingerprint_key=TEST_CARD_FINGERPRINT_KEY,
    )
    with session_factory() as session:
        credentials = create_merchant(session, clock, "Acme", TEST_API_KEY_PEPPER)
        session.commit()
    app = create_app(GatewayAppDeps(charge=deps, api_key_pepper=TEST_API_KEY_PEPPER))
    return Harness(
        deps,
        provider,
        session_factory,
        credentials.merchant_id,
        {"Authorization": f"Bearer {credentials.api_key}"},
        TestClient(app),
    )


def _charge(h: Harness, amount_minor: int, key: str | None = None) -> ChargeOutcome:
    request = ChargeRequest.model_validate(
        {
            "amount_minor": amount_minor,
            "currency": "USD",
            "method": "card",
            "card": {"number": APPROVE_PAN, "exp_month": 12, "exp_year": 2030, "cvc": "123"},
        }
    )
    return charge_card(h.deps, h.merchant_id, key or uuid.uuid4().hex, request)


def _payment_row(session_factory: sessionmaker[Session], payment_id: object) -> dict[str, object]:
    with session_factory() as session:
        rows = session.execute(
            text(
                "SELECT state, decline_code, risk_flagged, risk_blocked, risk_reasons "
                "FROM gateway.payments WHERE id = :id"
            ),
            {"id": payment_id},
        ).mappings()
        return dict(next(iter(rows)))


def _count(session_factory: sessionmaker[Session], query: str) -> int:
    with session_factory() as session:
        return int(session.execute(text(query)).scalar_one())


def test_a_high_amount_charge_with_a_failing_lookup_is_declined_risk_unavailable_before_the_provider(
    session_factory: sessionmaker[Session], clock: FixedClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Arrange
    h = _harness(session_factory, clock)
    monkeypatch.setattr(risk_module, "card_activity", _broken_sql_lookup)

    # Act
    outcome = _charge(h, HIGH_USD)

    # Assert
    row = _payment_row(session_factory, outcome.body["payment_id"])
    assert outcome.status_code == 201
    assert (outcome.body["state"], outcome.body["decline_code"]) == ("declined", UNAVAILABLE)
    assert h.provider.calls == 0
    assert (row["state"], row["decline_code"]) == ("declined", UNAVAILABLE)
    assert (row["risk_flagged"], row["risk_blocked"]) == (True, True)
    assert isinstance(row["risk_reasons"], list)
    assert sorted(row["risk_reasons"]) == sorted(["high_amount", DEGRADED_REASON])


def test_a_low_amount_charge_with_a_failing_lookup_still_succeeds_but_is_flagged(
    session_factory: sessionmaker[Session], clock: FixedClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Arrange
    h = _harness(session_factory, clock)
    monkeypatch.setattr(risk_module, "card_activity", _broken_sql_lookup)

    # Act
    outcome = _charge(h, SMALL_AMOUNT)

    # Assert
    row = _payment_row(session_factory, outcome.body["payment_id"])
    assert outcome.status_code == 201
    assert (outcome.body["state"], outcome.body["decline_code"]) == ("succeeded", None)
    assert h.provider.calls == 1
    assert (row["state"], row["risk_flagged"], row["risk_blocked"]) == ("succeeded", True, False)
    assert row["risk_reasons"] == [DEGRADED_REASON]


def test_a_failing_lookup_does_not_poison_the_charge_transaction(
    session_factory: sessionmaker[Session], clock: FixedClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Arrange
    h = _harness(session_factory, clock)
    monkeypatch.setattr(risk_module, "card_activity", _broken_sql_lookup)

    # Act
    outcome = _charge(h, HIGH_USD, key="degraded-key")

    # Assert
    assert outcome.status_code == 201
    assert _count(session_factory, "SELECT count(*) FROM gateway.payments") == 1
    assert _count(session_factory, "SELECT count(*) FROM gateway.idempotency_keys") == 1


def test_replaying_a_degraded_block_returns_the_stored_body_without_reassessing(
    session_factory: sessionmaker[Session], clock: FixedClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Arrange
    h = _harness(session_factory, clock)
    monkeypatch.setattr(risk_module, "card_activity", _broken_sql_lookup)
    first = _charge(h, HIGH_USD, key="degraded-replay")

    # Act
    second = _charge(h, HIGH_USD, key="degraded-replay")

    # Assert
    assert second.body == first.body
    assert _count(session_factory, "SELECT count(*) FROM gateway.payments") == 1
    assert h.provider.calls == 0


def test_the_risk_flags_endpoint_reports_a_degraded_block_as_flagged_and_blocked(
    session_factory: sessionmaker[Session], clock: FixedClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Arrange
    h = _harness(session_factory, clock)
    monkeypatch.setattr(risk_module, "card_activity", _broken_sql_lookup)
    outcome = _charge(h, HIGH_USD)

    # Act
    response = h.client.get(f"/v1/payments/{outcome.body['payment_id']}/risk", headers=h.auth)

    # Assert
    assert response.status_code == 200
    assert response.json() == {
        "payment_id": str(outcome.body["payment_id"]),
        "risk_flagged": True,
        "blocked": True,
    }


def test_the_payment_history_shows_the_risk_unavailable_decline_code(
    session_factory: sessionmaker[Session], clock: FixedClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Arrange
    h = _harness(session_factory, clock)
    monkeypatch.setattr(risk_module, "card_activity", _broken_sql_lookup)
    outcome = _charge(h, HIGH_USD)

    # Act
    response = h.client.get("/v1/payments", headers=h.auth)

    # Assert
    items = response.json()["items"]
    assert response.status_code == 200
    assert [(item["payment_id"], item["state"], item["decline_code"]) for item in items] == [
        (str(outcome.body["payment_id"]), "declined", UNAVAILABLE)
    ]


def test_with_a_working_lookup_the_charge_path_is_unchanged(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    h = _harness(session_factory, clock)

    # Act
    outcome = _charge(h, SMALL_AMOUNT)

    # Assert
    row = _payment_row(session_factory, outcome.body["payment_id"])
    assert (outcome.body["state"], outcome.body["decline_code"]) == ("succeeded", None)
    assert (row["risk_flagged"], row["risk_blocked"], row["risk_reasons"]) == (False, False, [])
