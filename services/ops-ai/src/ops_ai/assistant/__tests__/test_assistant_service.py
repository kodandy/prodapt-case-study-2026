import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime

from ops_ai.assistant.evidence import EvidenceItem
from ops_ai.assistant.llm_port import LlmPort, LlmRequest, LlmResponse, LlmUnavailableError, MockLlm
from ops_ai.assistant.service import AssistantAnswer, ask
from ops_ai.ingest.models import PaymentFact
from ops_ai.scope import Scope
from ops_ai.search.models import SearchDocument
from sqlalchemy.orm import Session, sessionmaker

OCCURRED_AT = datetime(2026, 11, 3, 11, 0, tzinfo=UTC)
QUESTION = "declined payments"
AGGREGATE_REF = "aggregate:decline_code:card_declined"


@dataclass
class UnavailableLlm:
    requests: list[LlmRequest] = field(default_factory=list)

    def complete(self, request: LlmRequest) -> LlmResponse:
        self.requests.append(request)
        raise LlmUnavailableError("provider outage")


@dataclass(frozen=True)
class SeededMerchant:
    merchant_id: uuid.UUID
    card_declined_ids: tuple[uuid.UUID, ...]
    insufficient_funds_ids: tuple[uuid.UUID, ...]
    succeeded_ids: tuple[uuid.UUID, ...]

    @property
    def all_ids(self) -> set[uuid.UUID]:
        return {*self.card_declined_ids, *self.insufficient_funds_ids, *self.succeeded_ids}


def _seed_payments(
    session_factory: sessionmaker[Session],
    *,
    card_declined: int,
    insufficient_funds: int = 0,
    succeeded: int = 0,
) -> SeededMerchant:
    merchant_id = uuid.uuid4()
    plan = (
        [("declined", "card_declined")] * card_declined
        + [("declined", "insufficient_funds")] * insufficient_funds
        + [("succeeded", None)] * succeeded
    )
    ids: dict[str | None, list[uuid.UUID]] = {"card_declined": [], "insufficient_funds": [], None: []}
    with session_factory() as session:
        for state, decline_code in plan:
            payment_id = uuid.uuid4()
            ids[decline_code].append(payment_id)
            session.add(
                PaymentFact(
                    payment_id=payment_id,
                    merchant_id=merchant_id,
                    amount_minor=1500,
                    currency="USD",
                    method="card",
                    state=state,
                    provider="acq_a",
                    decline_code=decline_code,
                    card_bin="400000",
                    entity_version=1,
                    occurred_at=OCCURRED_AT,
                )
            )
            session.add(
                SearchDocument(
                    kind="payment",
                    ref_id=payment_id,
                    merchant_id=merchant_id,
                    title=f"Card payment {state}",
                    body=f"{decline_code or 'approved'} acq_a USD",
                    occurred_at=OCCURRED_AT,
                )
            )
        session.commit()
    return SeededMerchant(
        merchant_id,
        tuple(ids["card_declined"]),
        tuple(ids["insufficient_funds"]),
        tuple(ids[None]),
    )


def _ask(session_factory: sessionmaker[Session], merchant_id: uuid.UUID, llm: LlmPort) -> AssistantAnswer:
    with session_factory() as session:
        return ask(session, Scope(merchant_id=merchant_id), QUESTION, llm)


def _by_ref(answer: AssistantAnswer) -> dict[str, EvidenceItem]:
    return {item.ref: item for item in answer.evidence}


def test_ask_with_the_mock_llm_answers_in_llm_mode(session_factory: sessionmaker[Session]) -> None:
    # Arrange
    seeded = _seed_payments(session_factory, card_declined=3, insufficient_funds=1, succeeded=1)

    # Act
    answer = _ask(session_factory, seeded.merchant_id, MockLlm())

    # Assert
    assert answer.mode == "llm"
    assert answer.model == "mock"
    assert answer.text is not None
    assert len(answer.text.strip()) > 0


def test_ask_evidence_has_the_decline_code_aggregate_with_its_count(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    seeded = _seed_payments(session_factory, card_declined=3, insufficient_funds=1, succeeded=1)

    # Act
    answer = _ask(session_factory, seeded.merchant_id, MockLlm())

    # Assert
    evidence = _by_ref(answer)
    assert AGGREGATE_REF in evidence
    assert "3" in evidence[AGGREGATE_REF].summary


def test_ask_evidence_has_payment_refs_for_the_matching_search_hits(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    seeded = _seed_payments(session_factory, card_declined=3, insufficient_funds=1, succeeded=1)
    declined = {*seeded.card_declined_ids, *seeded.insufficient_funds_ids}

    # Act
    answer = _ask(session_factory, seeded.merchant_id, MockLlm())

    # Assert
    payment_refs = {item.ref for item in answer.evidence if item.ref.startswith("payment:")}
    assert {f"payment:{payment_id}" for payment_id in declined} <= payment_refs
    assert payment_refs <= {f"payment:{payment_id}" for payment_id in seeded.all_ids}


def test_ask_evidence_never_refers_to_another_merchants_payment(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    mine = _seed_payments(session_factory, card_declined=3)
    other = _seed_payments(session_factory, card_declined=2)

    # Act
    answer = _ask(session_factory, mine.merchant_id, MockLlm())

    # Assert
    evidence = _by_ref(answer)
    rendered = " ".join(f"{item.ref} {item.summary}" for item in answer.evidence)
    assert "3" in evidence[AGGREGATE_REF].summary
    assert not {f"payment:{payment_id}" for payment_id in other.all_ids} & set(evidence)
    assert all(str(payment_id) not in rendered for payment_id in other.all_ids)
    assert str(other.merchant_id) not in rendered


def test_ask_for_a_merchant_without_payments_returns_an_answer_with_no_payment_refs(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    other = _seed_payments(session_factory, card_declined=2)

    # Act
    answer = _ask(session_factory, uuid.uuid4(), MockLlm())

    # Assert
    assert answer.mode == "llm"
    assert [item.ref for item in answer.evidence if item.ref.startswith("payment:")] == []
    assert all(
        str(payment_id) not in item.summary for item in answer.evidence for payment_id in other.all_ids
    )


def test_e3_llm_unavailable_falls_back_to_t2_with_the_same_evidence_as_the_healthy_path(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    seeded = _seed_payments(session_factory, card_declined=3, insufficient_funds=1, succeeded=1)
    healthy = _ask(session_factory, seeded.merchant_id, MockLlm())
    down = UnavailableLlm()

    # Act
    answer = _ask(session_factory, seeded.merchant_id, down)

    # Assert
    assert answer.mode == "t2_fallback"
    assert answer.text is None
    assert answer.evidence == healthy.evidence
    assert AGGREGATE_REF in _by_ref(answer)
    assert len(down.requests) == 1
    assert down.requests[0].evidence == answer.evidence
