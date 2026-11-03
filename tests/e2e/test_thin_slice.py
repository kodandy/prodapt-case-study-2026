"""Day 1 thin slice: signup, wallet, charge, ledger and outbox, relay, projection, search, answer."""

import uuid
from dataclasses import dataclass, field

from gateway.accounts.service import authenticate_api_key, create_merchant, sign_up
from gateway.clock import FixedClock
from gateway.outbox.relay import publish_pending
from gateway.payments.charge import ChargeDeps, ChargeOutcome, charge_card
from gateway.payments.providers.simulator.provider import SimulatorProvider
from gateway.payments.schemas import ChargeRequest
from ops_ai.assistant.llm_port import LlmRequest, LlmResponse, LlmUnavailableError, MockLlm
from ops_ai.assistant.service import ask
from ops_ai.db import make_session_factory as make_ops_ai_session_factory
from ops_ai.ingest.consumer import PAYMENT_FACTS_GROUP, consume_payments, ensure_group
from ops_ai.scope import Scope
from ops_ai.search.service import search_documents
from redis import Redis
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from conftest import TEST_API_KEY_PEPPER, TEST_CARD_FINGERPRINT_KEY

APPROVE_PAN = "4242424242424242"
DECLINE_PAN = "4000000000000002"
QUESTION = "declined payments"
STREAM = "gw.payments.v1"


@dataclass
class DownLlm:
    calls: list[LlmRequest] = field(default_factory=list)

    def complete(self, request: LlmRequest) -> LlmResponse:
        self.calls.append(request)
        raise LlmUnavailableError("provider outage")


@dataclass(frozen=True)
class Slice:
    deps: ChargeDeps
    merchant_id: uuid.UUID
    gateway_sessions: sessionmaker[Session]
    ops_ai_sessions: sessionmaker[Session]
    redis: Redis
    clock: FixedClock


def _request(pan: str, amount_minor: int) -> ChargeRequest:
    return ChargeRequest.model_validate(
        {
            "amount_minor": amount_minor,
            "currency": "USD",
            "method": "card",
            "card": {"number": pan, "exp_month": 12, "exp_year": 2030, "cvc": "123"},
        }
    )


def _build_slice(
    session_factory: sessionmaker[Session], engine: Engine, redis_client: Redis, clock: FixedClock
) -> Slice:
    with session_factory() as session:
        sign_up(session, clock, "alice@example.com", "correct horse battery staple")
        credentials = create_merchant(session, clock, "Acme", TEST_API_KEY_PEPPER)
        session.commit()
    with session_factory() as session:
        merchant_id = authenticate_api_key(session, credentials.api_key, TEST_API_KEY_PEPPER)
    assert merchant_id == credentials.merchant_id
    deps = ChargeDeps(
        session_factory=session_factory,
        redis=redis_client,
        provider=SimulatorProvider(session_factory, clock),
        clock=clock,
        card_fingerprint_key=TEST_CARD_FINGERPRINT_KEY,
    )
    return Slice(
        deps,
        credentials.merchant_id,
        session_factory,
        make_ops_ai_session_factory(engine),
        redis_client,
        clock,
    )


def _project_to_ops_ai(s: Slice) -> None:
    ensure_group(s.redis, STREAM, PAYMENT_FACTS_GROUP)
    published = publish_pending(s.gateway_sessions, s.redis, s.clock)
    consumed = consume_payments(s.ops_ai_sessions, s.redis, s.clock, "e2e-consumer")
    assert (published, consumed) == (2, 2)


def _charge_both(s: Slice) -> tuple[ChargeOutcome, ChargeOutcome]:
    approved = charge_card(s.deps, s.merchant_id, "key-approve", _request(APPROVE_PAN, 2500))
    declined = charge_card(s.deps, s.merchant_id, "key-decline", _request(DECLINE_PAN, 1200))
    return approved, declined


def _dump_schemas(session_factory: sessionmaker[Session]) -> str:
    query = (
        "SELECT query_to_xml(format('select * from %I.%I', table_schema, table_name), "
        "false, false, '')::text AS dump FROM information_schema.tables "
        "WHERE table_schema IN ('gateway', 'simulator', 'ops_ai') AND table_type = 'BASE TABLE'"
    )
    with session_factory() as session:
        return "\n".join(str(dump) for dump in session.execute(text(query)).scalars())


def test_thin_slice_declined_payment_flows_from_charge_to_a_fallback_answer(
    session_factory: sessionmaker[Session], engine: Engine, redis_client: Redis, clock: FixedClock
) -> None:
    # Arrange
    s = _build_slice(session_factory, engine, redis_client, clock)
    approved, declined = _charge_both(s)
    _project_to_ops_ai(s)
    declined_id = uuid.UUID(str(declined.body["payment_id"]))
    llm = DownLlm()

    # Act
    with s.ops_ai_sessions() as session:
        hits = search_documents(session, Scope(merchant_id=s.merchant_id), "declined")
        answer = ask(session, Scope(merchant_id=s.merchant_id), QUESTION, llm)

    # Assert
    assert (approved.status_code, approved.body["state"]) == (201, "succeeded")
    assert (declined.status_code, declined.body["state"], declined.body["decline_code"]) == (
        201,
        "declined",
        "card_declined",
    )
    assert [hit.ref_id for hit in hits] == [declined_id]
    assert answer.mode == "t2_fallback"
    assert answer.text is None
    refs = {item.ref for item in answer.evidence}
    assert f"payment:{declined_id}" in refs
    assert "aggregate:decline_code:card_declined" in refs
    assert f"payment:{approved.body['payment_id']}" not in refs


def test_thin_slice_moves_one_payment_per_charge_through_ledger_outbox_and_projection(
    session_factory: sessionmaker[Session], engine: Engine, redis_client: Redis, clock: FixedClock
) -> None:
    # Arrange
    s = _build_slice(session_factory, engine, redis_client, clock)
    approved, declined = _charge_both(s)

    # Act
    _project_to_ops_ai(s)

    # Assert
    with s.gateway_sessions() as session:
        journals = session.execute(text("SELECT count(*) FROM gateway.journal_entries")).scalar_one()
        unpublished = session.execute(
            text("SELECT count(*) FROM gateway.outbox WHERE published_at IS NULL")
        ).scalar_one()
    with s.ops_ai_sessions() as session:
        facts = session.execute(
            text("SELECT payment_id, state FROM ops_ai.payment_facts ORDER BY state")
        ).all()
    assert (journals, unpublished) == (1, 0)
    assert [(str(payment_id), state) for payment_id, state in facts] == [
        (str(declined.body["payment_id"]), "declined"),
        (str(approved.body["payment_id"]), "succeeded"),
    ]


def test_thin_slice_never_leaves_a_full_pan_in_any_schema_or_on_the_stream(
    session_factory: sessionmaker[Session], engine: Engine, redis_client: Redis, clock: FixedClock
) -> None:
    # Arrange
    s = _build_slice(session_factory, engine, redis_client, clock)
    _charge_both(s)

    # Act
    _project_to_ops_ai(s)

    # Assert
    stored = _dump_schemas(session_factory)
    streamed = str(redis_client.xrange(STREAM))
    for pan in (APPROVE_PAN, DECLINE_PAN):
        assert pan not in stored
        assert pan not in streamed
    assert "424242" in stored


def test_e3_llm_down_keeps_charges_succeeding_and_serves_the_same_evidence(
    session_factory: sessionmaker[Session], engine: Engine, redis_client: Redis, clock: FixedClock
) -> None:
    # Arrange
    s = _build_slice(session_factory, engine, redis_client, clock)
    _charge_both(s)
    _project_to_ops_ai(s)
    scope = Scope(merchant_id=s.merchant_id)
    down = DownLlm()
    with s.ops_ai_sessions() as session:
        healthy = ask(session, scope, QUESTION, MockLlm())

    # Act
    with s.ops_ai_sessions() as session:
        degraded = ask(session, scope, QUESTION, down)
    new_charge = charge_card(s.deps, s.merchant_id, "key-after-outage", _request(APPROVE_PAN, 999))

    # Assert
    assert len(down.calls) == 1
    assert (degraded.mode, degraded.text) == ("t2_fallback", None)
    assert degraded.evidence == healthy.evidence
    assert (healthy.mode, healthy.model) == ("llm", "mock")
    assert (new_charge.status_code, new_charge.body["state"]) == (201, "succeeded")
