"""Tests for the payment_ids cap in POST /v1/assistant/investigate-fraud, from docs/specs/sq3.md H-3."""

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

import jwt
import ops_ai.app as app_module
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from ops_ai.app import FraudAnalysisOut, OpsAiAppDeps, create_app
from ops_ai.assistant.llm_port import LlmRequest, LlmResponse
from ops_ai.fraud_watch import CardTestingEpisode, FraudAnalysis
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from conftest import TEST_NOW

ROUTE = "/v1/assistant/investigate-fraud"
QUESTION = "We're seeing a series of tiny card payments - is this card testing, and what should we do?"
MERCHANT = uuid.UUID("0b1c6a52-3f1e-4c55-9d3a-6f7e2a1b9c01")
BURST_START = TEST_NOW - timedelta(hours=2)
LIMIT = 100
TOP_LEVEL_KEYS = {
    "intent",
    "cause_label",
    "mode",
    "text",
    "model",
    "analysis",
    "alerts",
    "suggested_fixes",
    "tool_calls",
    "evidence",
}
ANALYSIS_KEYS = {"start", "end", "micro_payments", "episodes"}
EPISODE_KEYS = {
    "first_payment_id",
    "start",
    "end",
    "payment_ids",
    "micro_payments",
    "card_payments",
    "micro_share",
    "distinct_cards",
    "declined",
    "decline_ratio",
    "card_bins",
    "currencies",
    "flagged",
    "reasons",
}
INSERT_FACT = text(
    "INSERT INTO ops_ai.payment_facts (payment_id, merchant_id, amount_minor, currency, method, state, "
    "provider, decline_code, card_bin, card_ref, entity_version, occurred_at) VALUES "
    "(:payment_id, :merchant_id, 50, 'USD', 'card', 'declined', 'acq_a', NULL, '400000', :card_ref, 1, "
    ":occurred_at)"
)


@dataclass
class KeyPair:
    private_pem: str
    public_pem: str


@pytest.fixture(scope="module")
def key_pair() -> KeyPair:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()
    public_pem = (
        key.public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode()
    )
    return KeyPair(private_pem=private_pem, public_pem=public_pem)


class _Llm:
    def complete(self, request: LlmRequest) -> LlmResponse:
        return LlmResponse(text="narration", model="recording-model-1")


def _client(
    session_factory: sessionmaker[Session], clock: Callable[[], datetime], key: KeyPair
) -> TestClient:
    deps = OpsAiAppDeps(
        session_factory=session_factory, llm=_Llm(), jwt_public_key_pem=key.public_pem, clock=clock
    )
    return TestClient(create_app(deps))


def _token(private_pem: str) -> str:
    issued_at = int(TEST_NOW.timestamp())
    claims: dict[str, object] = {
        "iss": "gateway",
        "aud": "ops-ai",
        "sub": f"merchant:{MERCHANT}",
        "merchant_id": str(MERCHANT),
        "roles": ["merchant_admin"],
        "iat": issued_at,
        "exp": issued_at + 900,
        "jti": str(uuid.uuid4()),
    }
    return jwt.encode(claims, private_pem, algorithm="RS256")


def _burst(session_factory: sessionmaker[Session], count: int, *, step_seconds: int = 1) -> list[uuid.UUID]:
    """`count` micro card payments `step_seconds` apart on 4 cards, ids ascending with time."""
    ids = [uuid.UUID(int=1 + index) for index in range(count)]
    with session_factory.begin() as session:
        session.execute(
            INSERT_FACT,
            [
                {
                    "payment_id": payment_id,
                    "merchant_id": MERCHANT,
                    "card_ref": f"{index % 4 + 1:032x}",
                    "occurred_at": BURST_START + timedelta(seconds=index * step_seconds),
                }
                for index, payment_id in enumerate(ids)
            ],
        )
    return ids


def _post(session_factory: sessionmaker[Session], key: KeyPair) -> dict[str, object]:
    client = _client(session_factory, lambda: TEST_NOW, key)
    response = client.post(
        ROUTE, json={"question": QUESTION}, headers={"Authorization": f"Bearer {_token(key.private_pem)}"}
    )
    assert response.status_code == 200
    parsed: dict[str, object] = response.json()
    return parsed


def _as_dict(value: object) -> dict[str, object]:
    assert isinstance(value, dict)
    return value


def _as_list(value: object) -> list[object]:
    assert isinstance(value, list)
    return value


def _episode(body: dict[str, object]) -> dict[str, object]:
    episodes = _as_list(_as_dict(body["analysis"])["episodes"])
    assert len(episodes) == 1
    return _as_dict(episodes[0])


def _evidence(body: dict[str, object]) -> list[tuple[str, str]]:
    return [
        (str(_as_dict(item)["ref"]), str(_as_dict(item)["summary"])) for item in _as_list(body["evidence"])
    ]


# --- the limit constant ---------------------------------------------------------------------------------


def test_the_episode_payment_ids_limit_is_100() -> None:
    # Arrange / Act
    limit = vars(app_module).get("EPISODE_PAYMENT_IDS_LIMIT")

    # Assert
    assert limit == LIMIT


# --- the route --------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("count", "expected_returned"),
    [(12, 12), (99, 99), (100, 100), (101, 100), (130, 100)],
    ids=["twelve", "99", "exactly_100", "101", "130"],
)
def test_the_route_returns_at_most_the_first_100_payment_ids_of_the_episode_in_time_order(
    session_factory: sessionmaker[Session], key_pair: KeyPair, count: int, expected_returned: int
) -> None:
    # Arrange
    ids = _burst(session_factory, count)

    # Act
    episode = _episode(_post(session_factory, key_pair))

    # Assert
    assert episode["payment_ids"] == [str(payment_id) for payment_id in ids[:expected_returned]]
    assert len(_as_list(episode["payment_ids"])) == expected_returned
    assert episode["first_payment_id"] == str(ids[0])


def test_the_full_count_stays_in_micro_payments_so_a_shorter_list_means_truncation(
    session_factory: sessionmaker[Session], key_pair: KeyPair
) -> None:
    # Arrange
    _burst(session_factory, 130)

    # Act
    body = _post(session_factory, key_pair)

    # Assert
    episode = _episode(body)
    assert episode["micro_payments"] == 130
    assert episode["card_payments"] == 130
    assert _as_dict(body["analysis"])["micro_payments"] == 130
    assert episode["flagged"] is True
    assert len(_as_list(episode["payment_ids"])) == LIMIT


def test_payments_sharing_one_instant_are_cut_by_payment_id_order(
    session_factory: sessionmaker[Session], key_pair: KeyPair
) -> None:
    # Arrange: 130 payments at one instant, so the first 100 by (occurred_at, payment_id) are the smallest
    ids = _burst(session_factory, 130, step_seconds=0)

    # Act
    episode = _episode(_post(session_factory, key_pair))

    # Assert
    assert episode["payment_ids"] == [str(payment_id) for payment_id in sorted(ids)[:LIMIT]]
    assert episode["micro_payments"] == 130


def test_the_response_shape_does_not_change_when_the_list_is_capped(
    session_factory: sessionmaker[Session], key_pair: KeyPair
) -> None:
    # Arrange
    _burst(session_factory, 130)

    # Act
    body = _post(session_factory, key_pair)

    # Assert
    assert set(body) == TOP_LEVEL_KEYS
    assert set(_as_dict(body["analysis"])) == ANALYSIS_KEYS
    assert set(_episode(body)) == EPISODE_KEYS


def test_the_payment_evidence_still_cites_the_five_newest_payments_not_the_capped_first_hundred(
    session_factory: sessionmaker[Session], key_pair: KeyPair
) -> None:
    # Arrange
    ids = _burst(session_factory, 130)

    # Act
    body = _post(session_factory, key_pair)

    # Assert
    refs = [ref for ref, _summary in _evidence(body) if ref.startswith("payment:")]
    assert refs == [f"payment:{payment_id}" for payment_id in reversed(ids[-5:])]


def test_the_episode_evidence_still_states_the_full_micro_payment_count(
    session_factory: sessionmaker[Session], key_pair: KeyPair
) -> None:
    # Arrange
    ids = _burst(session_factory, 130)

    # Act
    body = _post(session_factory, key_pair)

    # Assert
    summaries = dict(_evidence(body))
    assert summaries[f"episode:{ids[0]}"].startswith("130 micro card payments over ")


# --- the serializer ---------------------------------------------------------------------------------------


def _analysis_with(payment_count: int) -> tuple[FraudAnalysis, list[uuid.UUID]]:
    ids = [uuid.UUID(int=1 + index) for index in range(payment_count)]
    start = BURST_START
    episode = CardTestingEpisode(
        first_payment_id=ids[0],
        start=start,
        end=start + timedelta(seconds=payment_count),
        payment_ids=tuple(ids),
        micro_payments=payment_count,
        card_payments=payment_count,
        micro_share=1.0,
        distinct_cards=4,
        declined=payment_count,
        decline_ratio=1.0,
        card_bins=("400000",),
        currencies=("USD",),
        flagged=True,
        reasons=("micro_burst", "many_cards", "high_decline_ratio"),
    )
    analysis = FraudAnalysis(
        start=start - timedelta(hours=1),
        end=start + timedelta(hours=1),
        micro_payments=payment_count,
        episodes=(episode,),
    )
    return analysis, ids


def test_the_serializer_keeps_the_first_100_ids_of_a_longer_episode_and_the_full_count() -> None:
    # Arrange
    analysis, ids = _analysis_with(150)

    # Act
    out = FraudAnalysisOut.of(analysis)

    # Assert
    assert out is not None
    (episode,) = out.episodes
    assert episode.payment_ids == ids[:LIMIT]
    assert episode.micro_payments == 150
    assert out.micro_payments == 150


def test_the_serializer_leaves_an_episode_of_exactly_100_ids_whole() -> None:
    # Arrange
    analysis, ids = _analysis_with(100)

    # Act
    out = FraudAnalysisOut.of(analysis)

    # Assert
    assert out is not None
    assert out.episodes[0].payment_ids == ids
