"""Tests for POST /v1/feedback, GET /v1/feedback/fraud-rules and POST /v1/feedback/fraud-rules/revert,
written from docs/specs/r2-agents.md sections 0, 1 and 5. They also close the loop: a false positive
tightens the rules that GET reports."""

import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from httpx import Response
from ops_ai.app import OpsAiAppDeps, create_app
from ops_ai.assistant.llm_port import LlmRequest, LlmResponse
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from conftest import TEST_NOW

FEEDBACK_ROUTE = "/v1/feedback"
RULES_ROUTE = "/v1/feedback/fraud-rules"
REVERT_ROUTE = "/v1/feedback/fraud-rules/revert"
MERCHANT = uuid.UUID("0b1c6a52-3f1e-4c55-9d3a-6f7e2a1b9c01")
OTHER_MERCHANT = uuid.UUID("0b1c6a52-3f1e-4c55-9d3a-6f7e2a1b9c02")
USER = uuid.UUID("a1a1a1a1-0000-4000-8000-000000000001")
INVALID_TOKEN_BODY = {"detail": "invalid token"}
FORBIDDEN_BODY = {"detail": "forbidden"}
FEEDBACK_KEYS = {"feedback_id", "target_kind", "target_id", "verdict", "rule_version", "incident_score"}
RULE_VERSION_KEYS = {
    "version",
    "steps",
    "min_micro_payments",
    "min_micro_share",
    "min_distinct_cards",
    "reason",
    "feedback_id",
    "created_at",
}
BASELINE_OUT: dict[str, object] = {
    "version": 0,
    "steps": 0,
    "min_micro_payments": 10,
    "min_micro_share": 0.8,
    "min_distinct_cards": 3,
    "reason": "baseline",
    "feedback_id": None,
    "created_at": None,
}
STARTED_AT = datetime(2026, 11, 3, 10, 0, tzinfo=UTC)

INSERT_ALERT = text(
    "INSERT INTO ops_ai.fraud_alerts (alert_id, window_key, merchant_id, kind, first_payment_id, started_at, "
    "last_seen_at, payment_count, features, created_at) VALUES (:alert_id, :window_key, :merchant_id, "
    "'card_testing', :first_payment_id, :started_at, :started_at, 12, '{}'::jsonb, :started_at)"
)
INSERT_INCIDENT = text(
    "INSERT INTO ops_ai.incidents (incident_id, merchant_id, title, root_cause, fix_applied, "
    "resolution_notes, started_at, resolved_at, recurrence_within_7d) VALUES "
    "(:incident_id, :merchant_id, 'Issuer outage', 'Issuer timeouts', 'Rerouted', 'notes', :started_at, "
    ":started_at, false)"
)


@dataclass(frozen=True)
class KeyPair:
    private_pem: str
    public_pem: str


def _generate_key_pair() -> KeyPair:
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


@pytest.fixture(scope="module")
def key_pair() -> KeyPair:
    return _generate_key_pair()


@pytest.fixture(scope="module")
def other_key_pair() -> KeyPair:
    return _generate_key_pair()


@dataclass
class IdleLlm:
    """The feedback routes never narrate; any call is a bug."""

    calls: list[LlmRequest] = field(default_factory=list)

    def complete(self, request: LlmRequest) -> LlmResponse:
        self.calls.append(request)
        return LlmResponse(text="unused", model="idle-model")


def _client(session_factory: sessionmaker[Session], public_key_pem: str) -> TestClient:
    deps = OpsAiAppDeps(
        session_factory=session_factory,
        llm=IdleLlm(),
        jwt_public_key_pem=public_key_pem,
        clock=lambda: TEST_NOW,
    )
    return TestClient(create_app(deps))


def _claims(subject: str, extra: dict[str, object], roles: list[str]) -> dict[str, object]:
    issued_at = int(TEST_NOW.timestamp())
    return {
        "iss": "gateway",
        "aud": "ops-ai",
        "sub": subject,
        "roles": roles,
        "iat": issued_at,
        "exp": issued_at + 900,
        "jti": str(uuid.uuid4()),
        **extra,
    }


def _merchant_token(private_pem: str, merchant_id: uuid.UUID = MERCHANT) -> str:
    claims = _claims(f"merchant:{merchant_id}", {"merchant_id": str(merchant_id)}, ["merchant_admin"])
    return jwt.encode(claims, private_pem, algorithm="RS256")


def _user_token(private_pem: str) -> str:
    claims = _claims(f"user:{USER}", {"user_id": str(USER)}, ["consumer"])
    return jwt.encode(claims, private_pem, algorithm="RS256")


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _as_dict(value: object) -> dict[str, object]:
    assert isinstance(value, dict)
    return value


def _as_list(value: object) -> list[object]:
    assert isinstance(value, list)
    return value


def _json(response: Response) -> dict[str, object]:
    return _as_dict(response.json())


def _insert_alert(session_factory: sessionmaker[Session], merchant_id: uuid.UUID) -> str:
    alert_id = uuid.uuid4()
    with session_factory.begin() as session:
        session.execute(
            INSERT_ALERT,
            {
                "alert_id": alert_id,
                "window_key": f"card_testing:{merchant_id}:{alert_id}",
                "merchant_id": merchant_id,
                "first_payment_id": uuid.uuid4(),
                "started_at": STARTED_AT,
            },
        )
    return str(alert_id)


def _insert_incident(
    session_factory: sessionmaker[Session], incident_id: str, merchant_id: uuid.UUID | None
) -> None:
    with session_factory.begin() as session:
        session.execute(
            INSERT_INCIDENT,
            {"incident_id": incident_id, "merchant_id": merchant_id, "started_at": STARTED_AT},
        )


def _post_feedback(
    client: TestClient, key_pair: KeyPair, body: Mapping[str, object], merchant_id: uuid.UUID = MERCHANT
) -> Response:
    response: Response = client.post(
        FEEDBACK_ROUTE, json=body, headers=_bearer(_merchant_token(key_pair.private_pem, merchant_id))
    )
    return response


def _get_rules(client: TestClient, key_pair: KeyPair, merchant_id: uuid.UUID = MERCHANT) -> dict[str, object]:
    response = client.get(RULES_ROUTE, headers=_bearer(_merchant_token(key_pair.private_pem, merchant_id)))
    assert response.status_code == 200
    return _json(response)


def _versions(rules: dict[str, object]) -> list[object]:
    return [_as_dict(item)["version"] for item in _as_list(rules["history"])]


def _false_positive(alert_id: str) -> dict[str, object]:
    return {"target_kind": "alert", "target_id": alert_id, "verdict": "false_positive"}


# --- POST /v1/feedback: success -------------------------------------------------------------------------


def test_a_false_positive_on_an_alert_returns_201_with_exactly_the_specified_keys(
    session_factory: sessionmaker[Session], key_pair: KeyPair
) -> None:
    # Arrange
    client = _client(session_factory, key_pair.public_pem)
    alert_id = _insert_alert(session_factory, MERCHANT)

    # Act
    response = _post_feedback(client, key_pair, {**_false_positive(alert_id), "comment": "load test"})

    # Assert
    assert response.status_code == 201
    body = _json(response)
    assert set(body) == FEEDBACK_KEYS
    assert (body["target_kind"], body["target_id"], body["verdict"]) == ("alert", alert_id, "false_positive")
    assert body["incident_score"] is None
    assert str(uuid.UUID(str(body["feedback_id"]))) == body["feedback_id"]


def test_the_201_rule_version_carries_the_new_thresholds_and_links_the_feedback(
    session_factory: sessionmaker[Session], key_pair: KeyPair
) -> None:
    # Arrange
    client = _client(session_factory, key_pair.public_pem)
    alert_id = _insert_alert(session_factory, MERCHANT)

    # Act
    response = _post_feedback(client, key_pair, _false_positive(alert_id))

    # Assert
    body = _json(response)
    rule_version = _as_dict(body["rule_version"])
    assert set(rule_version) == RULE_VERSION_KEYS
    assert {key: value for key, value in rule_version.items() if key != "created_at"} == {
        "version": 1,
        "steps": 1,
        "min_micro_payments": 12,
        "min_micro_share": 0.83,
        "min_distinct_cards": 4,
        "reason": "feedback:false_positive",
        "feedback_id": body["feedback_id"],
    }
    assert datetime.fromisoformat(str(rule_version["created_at"])) == TEST_NOW


def test_feedback_responses_are_not_cacheable(
    session_factory: sessionmaker[Session], key_pair: KeyPair
) -> None:
    # Arrange
    client = _client(session_factory, key_pair.public_pem)
    alert_id = _insert_alert(session_factory, MERCHANT)

    # Act
    response = _post_feedback(client, key_pair, _false_positive(alert_id))

    # Assert
    assert response.headers["cache-control"] == "no-store"


def test_a_true_positive_at_the_baseline_returns_a_null_rule_version(
    session_factory: sessionmaker[Session], key_pair: KeyPair
) -> None:
    # Arrange
    client = _client(session_factory, key_pair.public_pem)
    alert_id = _insert_alert(session_factory, MERCHANT)
    body = {"target_kind": "alert", "target_id": alert_id, "verdict": "true_positive"}

    # Act
    response = _post_feedback(client, key_pair, body)

    # Assert
    assert response.status_code == 201
    assert _json(response)["rule_version"] is None
    assert _get_rules(client, key_pair) == {"active": BASELINE_OUT, "history": []}


def test_helpful_on_an_incident_returns_the_updated_incident_score(
    session_factory: sessionmaker[Session], key_pair: KeyPair
) -> None:
    # Arrange
    client = _client(session_factory, key_pair.public_pem)
    _insert_incident(session_factory, "INC-1", MERCHANT)
    body = {"target_kind": "incident", "target_id": "INC-1", "verdict": "helpful"}

    # Act
    response = _post_feedback(client, key_pair, body)

    # Assert
    assert response.status_code == 201
    parsed = _json(response)
    assert parsed["incident_score"] == {"incident_id": "INC-1", "helpful": 1, "not_helpful": 0}
    assert parsed["rule_version"] is None


def test_not_helpful_on_a_platform_incident_is_accepted(
    session_factory: sessionmaker[Session], key_pair: KeyPair
) -> None:
    # Arrange
    client = _client(session_factory, key_pair.public_pem)
    _insert_incident(session_factory, "INC-PLATFORM", None)
    body = {"target_kind": "incident", "target_id": "INC-PLATFORM", "verdict": "not_helpful"}

    # Act
    response = _post_feedback(client, key_pair, body)

    # Assert
    assert response.status_code == 201
    assert _json(response)["incident_score"] == {
        "incident_id": "INC-PLATFORM",
        "helpful": 0,
        "not_helpful": 1,
    }


def test_answer_feedback_returns_201_with_null_rule_version_and_score(
    session_factory: sessionmaker[Session], key_pair: KeyPair
) -> None:
    # Arrange
    client = _client(session_factory, key_pair.public_pem)
    correlation_id = str(uuid.uuid4())
    body = {"target_kind": "answer", "target_id": correlation_id, "verdict": "not_helpful"}

    # Act
    response = _post_feedback(client, key_pair, body)

    # Assert
    assert response.status_code == 201
    parsed = _json(response)
    assert (parsed["target_kind"], parsed["target_id"], parsed["verdict"]) == (
        "answer",
        correlation_id,
        "not_helpful",
    )
    assert (parsed["rule_version"], parsed["incident_score"]) == (None, None)


def test_the_comment_is_stored_and_a_500_character_comment_is_accepted(
    session_factory: sessionmaker[Session], key_pair: KeyPair
) -> None:
    # Arrange
    client = _client(session_factory, key_pair.public_pem)
    comment = "c" * 500
    body = {"target_kind": "answer", "target_id": "corr-1", "verdict": "helpful", "comment": comment}

    # Act
    response = _post_feedback(client, key_pair, body)

    # Assert
    assert response.status_code == 201
    with session_factory() as session:
        stored = session.execute(text("SELECT comment FROM ops_ai.feedback")).scalars().all()
    assert stored == [comment]


def test_a_128_character_target_id_is_accepted(
    session_factory: sessionmaker[Session], key_pair: KeyPair
) -> None:
    # Arrange
    client = _client(session_factory, key_pair.public_pem)
    target_id = "t" * 128

    # Act
    response = _post_feedback(
        client, key_pair, {"target_kind": "answer", "target_id": target_id, "verdict": "helpful"}
    )

    # Assert
    assert response.status_code == 201
    assert _json(response)["target_id"] == target_id


# --- POST /v1/feedback: rejections ----------------------------------------------------------------------


def test_a_verdict_that_does_not_match_the_target_kind_is_a_422_with_the_specified_detail(
    session_factory: sessionmaker[Session], key_pair: KeyPair
) -> None:
    # Arrange
    client = _client(session_factory, key_pair.public_pem)
    alert_id = _insert_alert(session_factory, MERCHANT)
    body = {"target_kind": "alert", "target_id": alert_id, "verdict": "helpful"}

    # Act
    response = _post_feedback(client, key_pair, body)

    # Assert
    assert response.status_code == 422
    assert response.json() == {"detail": "verdict does not match target"}


def test_an_unknown_alert_is_a_404_with_the_specified_detail(
    session_factory: sessionmaker[Session], key_pair: KeyPair
) -> None:
    # Arrange
    client = _client(session_factory, key_pair.public_pem)

    # Act
    response = _post_feedback(client, key_pair, _false_positive(str(uuid.uuid4())))

    # Assert
    assert response.status_code == 404
    assert response.json() == {"detail": "target not found"}


def test_another_merchants_alert_is_the_same_404_and_changes_nothing(
    session_factory: sessionmaker[Session], key_pair: KeyPair
) -> None:
    # Arrange
    client = _client(session_factory, key_pair.public_pem)
    others_alert = _insert_alert(session_factory, OTHER_MERCHANT)

    # Act
    response = _post_feedback(client, key_pair, _false_positive(others_alert))

    # Assert
    assert response.status_code == 404
    assert response.json() == {"detail": "target not found"}
    assert _get_rules(client, key_pair, OTHER_MERCHANT) == {"active": BASELINE_OUT, "history": []}


def test_an_alert_target_id_that_is_not_a_uuid_is_a_404_not_a_server_error(
    session_factory: sessionmaker[Session], key_pair: KeyPair
) -> None:
    # Arrange
    client = _client(session_factory, key_pair.public_pem)

    # Act
    response = _post_feedback(client, key_pair, _false_positive("not-a-uuid"))

    # Assert
    assert response.status_code == 404
    assert response.json() == {"detail": "target not found"}


def test_another_merchants_incident_is_a_404(
    session_factory: sessionmaker[Session], key_pair: KeyPair
) -> None:
    # Arrange
    client = _client(session_factory, key_pair.public_pem)
    _insert_incident(session_factory, "INC-OTHER", OTHER_MERCHANT)
    body = {"target_kind": "incident", "target_id": "INC-OTHER", "verdict": "helpful"}

    # Act
    response = _post_feedback(client, key_pair, body)

    # Assert
    assert response.status_code == 404
    assert response.json() == {"detail": "target not found"}


def test_a_second_feedback_on_the_same_target_is_a_409_with_the_specified_detail(
    session_factory: sessionmaker[Session], key_pair: KeyPair
) -> None:
    # Arrange
    client = _client(session_factory, key_pair.public_pem)
    alert_id = _insert_alert(session_factory, MERCHANT)
    first = _post_feedback(client, key_pair, _false_positive(alert_id))

    # Act
    second = _post_feedback(client, key_pair, _false_positive(alert_id))

    # Assert
    assert first.status_code == 201
    assert second.status_code == 409
    assert second.json() == {"detail": "feedback already recorded"}
    assert _versions(_get_rules(client, key_pair)) == [1]


@pytest.mark.parametrize(
    "body",
    [
        {"target_kind": "alert", "target_id": "", "verdict": "false_positive"},
        {"target_kind": "answer", "target_id": "t" * 129, "verdict": "helpful"},
        {"target_kind": "answer", "target_id": "corr-1", "verdict": "helpful", "comment": "c" * 501},
        {"target_kind": "ticket", "target_id": "corr-1", "verdict": "helpful"},
        {"target_kind": "answer", "target_id": "corr-1", "verdict": "maybe"},
        {"target_kind": "answer", "target_id": "corr-1"},
        {"target_id": "corr-1", "verdict": "helpful"},
        {"target_kind": "answer", "target_id": 42, "verdict": "helpful"},
    ],
    ids=[
        "empty_target_id",
        "target_id_over_128",
        "comment_over_500",
        "unknown_kind",
        "unknown_verdict",
        "missing_verdict",
        "missing_kind",
        "numeric_target_id",
    ],
)
def test_an_invalid_body_is_a_422_validation_failure(
    session_factory: sessionmaker[Session], key_pair: KeyPair, body: dict[str, object]
) -> None:
    # Arrange
    client = _client(session_factory, key_pair.public_pem)

    # Act
    response = _post_feedback(client, key_pair, body)

    # Assert
    assert response.status_code == 422
    assert _json(response)["error"] == "validation_failed"
    with session_factory() as session:
        assert session.execute(text("SELECT count(*) FROM ops_ai.feedback")).scalar_one() == 0


def test_an_extra_field_in_the_body_is_a_422_and_is_not_echoed_back(
    session_factory: sessionmaker[Session], key_pair: KeyPair
) -> None:
    # Arrange
    client = _client(session_factory, key_pair.public_pem)
    body = {
        "target_kind": "answer",
        "target_id": "corr-1",
        "verdict": "helpful",
        "merchant_id": str(OTHER_MERCHANT),
    }

    # Act
    response = _post_feedback(client, key_pair, body)

    # Assert
    assert response.status_code == 422
    assert _json(response)["error"] == "validation_failed"
    assert str(OTHER_MERCHANT) not in response.text
    with session_factory() as session:
        assert session.execute(text("SELECT count(*) FROM ops_ai.feedback")).scalar_one() == 0


# --- authentication and authorisation (all three routes) ------------------------------------------------

ROUTES: list[tuple[str, str, dict[str, object] | None]] = [
    ("POST", FEEDBACK_ROUTE, {"target_kind": "answer", "target_id": "corr-1", "verdict": "helpful"}),
    ("GET", RULES_ROUTE, None),
    ("POST", REVERT_ROUTE, {"version": 0}),
]
ROUTE_IDS = ["post_feedback", "get_fraud_rules", "post_revert"]


@pytest.mark.parametrize(("method", "path", "body"), ROUTES, ids=ROUTE_IDS)
def test_a_request_without_a_token_is_a_401(
    session_factory: sessionmaker[Session],
    key_pair: KeyPair,
    method: str,
    path: str,
    body: dict[str, object] | None,
) -> None:
    # Arrange
    client = _client(session_factory, key_pair.public_pem)

    # Act
    response = client.request(method, path, json=body)

    # Assert
    assert response.status_code == 401
    assert response.json() == INVALID_TOKEN_BODY


@pytest.mark.parametrize(("method", "path", "body"), ROUTES, ids=ROUTE_IDS)
def test_a_token_signed_by_another_key_is_a_401(
    session_factory: sessionmaker[Session],
    key_pair: KeyPair,
    other_key_pair: KeyPair,
    method: str,
    path: str,
    body: dict[str, object] | None,
) -> None:
    # Arrange
    client = _client(session_factory, key_pair.public_pem)
    headers = _bearer(_merchant_token(other_key_pair.private_pem))

    # Act
    response = client.request(method, path, json=body, headers=headers)

    # Assert
    assert response.status_code == 401
    assert response.json() == INVALID_TOKEN_BODY


@pytest.mark.parametrize(("method", "path", "body"), ROUTES, ids=ROUTE_IDS)
def test_a_user_token_is_a_403(
    session_factory: sessionmaker[Session],
    key_pair: KeyPair,
    method: str,
    path: str,
    body: dict[str, object] | None,
) -> None:
    # Arrange
    client = _client(session_factory, key_pair.public_pem)
    headers = _bearer(_user_token(key_pair.private_pem))

    # Act
    response = client.request(method, path, json=body, headers=headers)

    # Assert
    assert response.status_code == 403
    assert response.json() == FORBIDDEN_BODY


def test_a_user_token_cannot_record_feedback(
    session_factory: sessionmaker[Session], key_pair: KeyPair
) -> None:
    # Arrange
    client = _client(session_factory, key_pair.public_pem)
    headers = _bearer(_user_token(key_pair.private_pem))
    body = {"target_kind": "answer", "target_id": "corr-1", "verdict": "helpful"}

    # Act
    client.post(FEEDBACK_ROUTE, json=body, headers=headers)

    # Assert
    with session_factory() as session:
        assert session.execute(text("SELECT count(*) FROM ops_ai.feedback")).scalar_one() == 0


# --- GET /v1/feedback/fraud-rules -----------------------------------------------------------------------


def test_the_fraud_rules_of_a_merchant_without_feedback_are_the_baseline_and_an_empty_history(
    session_factory: sessionmaker[Session], key_pair: KeyPair
) -> None:
    # Arrange
    client = _client(session_factory, key_pair.public_pem)

    # Act
    response = client.get(RULES_ROUTE, headers=_bearer(_merchant_token(key_pair.private_pem)))

    # Assert
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {"active": BASELINE_OUT, "history": []}


def test_the_loop_closes_after_a_false_positive_the_rules_show_version_one_as_active(
    session_factory: sessionmaker[Session], key_pair: KeyPair
) -> None:
    # Arrange
    client = _client(session_factory, key_pair.public_pem)
    alert_id = _insert_alert(session_factory, MERCHANT)
    posted = _json(_post_feedback(client, key_pair, _false_positive(alert_id)))

    # Act
    rules = _get_rules(client, key_pair)

    # Assert
    active = _as_dict(rules["active"])
    assert set(active) == RULE_VERSION_KEYS
    assert {key: value for key, value in active.items() if key != "created_at"} == {
        "version": 1,
        "steps": 1,
        "min_micro_payments": 12,
        "min_micro_share": 0.83,
        "min_distinct_cards": 4,
        "reason": "feedback:false_positive",
        "feedback_id": posted["feedback_id"],
    }
    assert datetime.fromisoformat(str(active["created_at"])) == TEST_NOW
    assert rules["history"] == [active]


def test_the_history_lists_every_version_newest_first_and_active_is_the_newest(
    session_factory: sessionmaker[Session], key_pair: KeyPair
) -> None:
    # Arrange
    client = _client(session_factory, key_pair.public_pem)
    for _ in range(3):
        _post_feedback(client, key_pair, _false_positive(_insert_alert(session_factory, MERCHANT)))

    # Act
    rules = _get_rules(client, key_pair)

    # Assert
    history = [_as_dict(item) for item in _as_list(rules["history"])]
    assert [(item["version"], item["steps"]) for item in history] == [(3, 3), (2, 2), (1, 1)]
    assert _as_dict(rules["active"]) == history[0]
    assert history[0]["min_micro_payments"] == 16


def test_the_fraud_rules_never_show_another_merchants_versions(
    session_factory: sessionmaker[Session], key_pair: KeyPair
) -> None:
    # Arrange
    client = _client(session_factory, key_pair.public_pem)
    _post_feedback(client, key_pair, _false_positive(_insert_alert(session_factory, MERCHANT)))

    # Act
    others = _get_rules(client, key_pair, OTHER_MERCHANT)

    # Assert
    assert others == {"active": BASELINE_OUT, "history": []}


# --- POST /v1/feedback/fraud-rules/revert ---------------------------------------------------------------


def test_revert_to_version_zero_returns_201_and_a_new_baseline_valued_version(
    session_factory: sessionmaker[Session], key_pair: KeyPair
) -> None:
    # Arrange
    client = _client(session_factory, key_pair.public_pem)
    _post_feedback(client, key_pair, _false_positive(_insert_alert(session_factory, MERCHANT)))

    # Act
    response = client.post(
        REVERT_ROUTE, json={"version": 0}, headers=_bearer(_merchant_token(key_pair.private_pem))
    )

    # Assert
    assert response.status_code == 201
    assert response.headers["cache-control"] == "no-store"
    body = _json(response)
    assert set(body) == RULE_VERSION_KEYS
    assert {key: value for key, value in body.items() if key != "created_at"} == {
        "version": 2,
        "steps": 0,
        "min_micro_payments": 10,
        "min_micro_share": 0.8,
        "min_distinct_cards": 3,
        "reason": "revert:v0",
        "feedback_id": None,
    }
    assert datetime.fromisoformat(str(body["created_at"])) == TEST_NOW


def test_after_a_revert_the_new_version_is_the_active_one(
    session_factory: sessionmaker[Session], key_pair: KeyPair
) -> None:
    # Arrange
    client = _client(session_factory, key_pair.public_pem)
    for _ in range(2):
        _post_feedback(client, key_pair, _false_positive(_insert_alert(session_factory, MERCHANT)))
    headers = _bearer(_merchant_token(key_pair.private_pem))

    # Act
    reverted = _json(client.post(REVERT_ROUTE, json={"version": 1}, headers=headers))
    rules = _get_rules(client, key_pair)

    # Assert
    assert (reverted["version"], reverted["steps"], reverted["reason"]) == (3, 1, "revert:v1")
    assert _as_dict(rules["active"]) == reverted
    assert _versions(rules) == [3, 2, 1]


def test_revert_to_an_unknown_version_is_a_404_with_the_specified_detail(
    session_factory: sessionmaker[Session], key_pair: KeyPair
) -> None:
    # Arrange
    client = _client(session_factory, key_pair.public_pem)
    _post_feedback(client, key_pair, _false_positive(_insert_alert(session_factory, MERCHANT)))

    # Act
    response = client.post(
        REVERT_ROUTE, json={"version": 9}, headers=_bearer(_merchant_token(key_pair.private_pem))
    )

    # Assert
    assert response.status_code == 404
    assert response.json() == {"detail": "rule version not found"}
    assert _versions(_get_rules(client, key_pair)) == [1]


def test_revert_cannot_target_another_merchants_version(
    session_factory: sessionmaker[Session], key_pair: KeyPair
) -> None:
    # Arrange
    client = _client(session_factory, key_pair.public_pem)
    _post_feedback(
        client, key_pair, _false_positive(_insert_alert(session_factory, OTHER_MERCHANT)), OTHER_MERCHANT
    )

    # Act
    response = client.post(
        REVERT_ROUTE, json={"version": 1}, headers=_bearer(_merchant_token(key_pair.private_pem))
    )

    # Assert
    assert response.status_code == 404
    assert response.json() == {"detail": "rule version not found"}


@pytest.mark.parametrize(
    "body",
    [{"version": -1}, {"version": "latest"}, {}, {"version": 1.5}],
    ids=["negative", "not_a_number", "missing", "fractional"],
)
def test_an_invalid_revert_body_is_a_422_validation_failure(
    session_factory: sessionmaker[Session], key_pair: KeyPair, body: dict[str, object]
) -> None:
    # Arrange
    client = _client(session_factory, key_pair.public_pem)

    # Act
    response = client.post(REVERT_ROUTE, json=body, headers=_bearer(_merchant_token(key_pair.private_pem)))

    # Assert
    assert response.status_code == 422
    assert _json(response)["error"] == "validation_failed"
