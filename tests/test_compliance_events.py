"""Tests for COMPLIANCE_TOPIC and the compliance event schemas, written from docs/specs/compliance-rbac.md
section "Events"."""

import json
import uuid
from datetime import UTC, datetime

import pytest
from contracts.compliance import (
    COMPLIANCE_TOPIC,
    KycReviewRequiredV1,
    SubjectErasedV1,
    parse_compliance_event,
)
from contracts.events import NOTIFICATIONS_TOPIC, PAYMENTS_TOPIC
from pydantic import ValidationError

OCCURRED_AT = datetime(2026, 11, 3, 12, 0, tzinfo=UTC)
EVENT_ID = uuid.UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
REVIEW_ID = uuid.UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
USER_ID = uuid.UUID("cccccccc-cccc-4ccc-8ccc-cccccccccccc")
REQUEST_ID = uuid.UUID("dddddddd-dddd-4ddd-8ddd-dddddddddddd")
KYC_FIELDS = {"event_id", "event_type", "occurred_at", "origin", "review_id", "user_id", "rules"}
ERASED_FIELDS = {"event_id", "event_type", "occurred_at", "origin", "request_id", "user_id"}
PII_FRAGMENTS = ("email", "name", "country", "address", "phone", "birth")


def _kyc_payload(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "event_id": str(EVENT_ID),
        "event_type": "compliance.kyc_review_required",
        "occurred_at": OCCURRED_AT.isoformat(),
        "origin": "live",
        "review_id": str(REVIEW_ID),
        "user_id": str(USER_ID),
        "rules": ["amount_threshold", "velocity"],
    }
    return {**base, **overrides}


def _erased_payload(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "event_id": str(EVENT_ID),
        "event_type": "compliance.subject_erased",
        "occurred_at": OCCURRED_AT.isoformat(),
        "origin": "live",
        "request_id": str(REQUEST_ID),
        "user_id": str(USER_ID),
    }
    return {**base, **overrides}


def _without(payload: dict[str, object], field: str) -> dict[str, object]:
    return {key: value for key, value in payload.items() if key != field}


# --- topic ----------------------------------------------------------------------------------------------


def test_compliance_topic_is_the_versioned_stream_name_and_differs_from_the_other_topics() -> None:
    # Arrange / Act
    topics = (COMPLIANCE_TOPIC, PAYMENTS_TOPIC, NOTIFICATIONS_TOPIC)

    # Assert
    assert topics == ("gw.compliance.v1", "gw.payments.v1", "gw.notifications.v1")


# --- KycReviewRequiredV1 --------------------------------------------------------------------------------


def test_a_valid_kyc_review_required_event_keeps_every_field() -> None:
    # Arrange
    payload = _kyc_payload()

    # Act
    event = KycReviewRequiredV1.model_validate(payload)

    # Assert
    assert event.event_id == EVENT_ID
    assert event.event_type == "compliance.kyc_review_required"
    assert event.occurred_at == OCCURRED_AT
    assert event.origin == "live"
    assert event.review_id == REVIEW_ID
    assert event.user_id == USER_ID
    assert list(event.rules) == ["amount_threshold", "velocity"]


def test_kyc_review_required_declares_exactly_the_specified_fields() -> None:
    # Arrange / Act
    declared = set(KycReviewRequiredV1.model_fields)

    # Assert
    assert declared == KYC_FIELDS


def test_kyc_review_required_rejects_an_extra_field() -> None:
    # Arrange
    payload = _kyc_payload(email="alice@example.com")

    # Act
    with pytest.raises(ValidationError) as error:
        KycReviewRequiredV1.model_validate(payload)

    # Assert
    assert [(item["type"], item["loc"]) for item in error.value.errors()] == [("extra_forbidden", ("email",))]


@pytest.mark.parametrize("field", sorted(KYC_FIELDS - {"origin"}))
def test_kyc_review_required_rejects_a_missing_required_field(field: str) -> None:
    # Arrange
    payload = _without(_kyc_payload(), field)

    # Act
    with pytest.raises(ValidationError) as error:
        KycReviewRequiredV1.model_validate(payload)

    # Assert
    assert [(item["type"], item["loc"]) for item in error.value.errors()] == [("missing", (field,))]


def test_kyc_review_required_rejects_a_subject_erased_event_type() -> None:
    # Arrange
    payload = _kyc_payload(event_type="compliance.subject_erased")

    # Act
    with pytest.raises(ValidationError) as error:
        KycReviewRequiredV1.model_validate(payload)

    # Assert
    assert [item["loc"] for item in error.value.errors()] == [("event_type",)]


def test_kyc_review_required_rejects_a_user_id_that_is_not_a_uuid() -> None:
    # Arrange
    payload = _kyc_payload(user_id="not-a-uuid")

    # Act
    with pytest.raises(ValidationError) as error:
        KycReviewRequiredV1.model_validate(payload)

    # Assert
    assert [item["loc"] for item in error.value.errors()] == [("user_id",)]


# --- SubjectErasedV1 ------------------------------------------------------------------------------------


def test_a_valid_subject_erased_event_keeps_every_field() -> None:
    # Arrange
    payload = _erased_payload()

    # Act
    event = SubjectErasedV1.model_validate(payload)

    # Assert
    assert event.event_id == EVENT_ID
    assert event.event_type == "compliance.subject_erased"
    assert event.occurred_at == OCCURRED_AT
    assert event.origin == "live"
    assert event.request_id == REQUEST_ID
    assert event.user_id == USER_ID


def test_subject_erased_declares_exactly_the_specified_fields() -> None:
    # Arrange / Act
    declared = set(SubjectErasedV1.model_fields)

    # Assert
    assert declared == ERASED_FIELDS


def test_subject_erased_rejects_an_extra_field() -> None:
    # Arrange
    payload = _erased_payload(full_name="Alice Example")

    # Act
    with pytest.raises(ValidationError) as error:
        SubjectErasedV1.model_validate(payload)

    # Assert
    assert [(item["type"], item["loc"]) for item in error.value.errors()] == [
        ("extra_forbidden", ("full_name",))
    ]


@pytest.mark.parametrize("field", sorted(ERASED_FIELDS - {"origin"}))
def test_subject_erased_rejects_a_missing_required_field(field: str) -> None:
    # Arrange
    payload = _without(_erased_payload(), field)

    # Act
    with pytest.raises(ValidationError) as error:
        SubjectErasedV1.model_validate(payload)

    # Assert
    assert [(item["type"], item["loc"]) for item in error.value.errors()] == [("missing", (field,))]


# --- parse_compliance_event (discriminated on event_type) -----------------------------------------------


def test_parse_compliance_event_returns_a_kyc_review_required_event_for_its_event_type() -> None:
    # Arrange
    payload = _kyc_payload()

    # Act
    event = parse_compliance_event(payload)

    # Assert
    assert isinstance(event, KycReviewRequiredV1)
    assert (event.review_id, event.user_id) == (REVIEW_ID, USER_ID)


def test_parse_compliance_event_returns_a_subject_erased_event_for_its_event_type() -> None:
    # Arrange
    payload = _erased_payload()

    # Act
    event = parse_compliance_event(payload)

    # Assert
    assert isinstance(event, SubjectErasedV1)
    assert (event.request_id, event.user_id) == (REQUEST_ID, USER_ID)


def test_parse_compliance_event_round_trips_a_serialised_event() -> None:
    # Arrange
    original = SubjectErasedV1.model_validate(_erased_payload())
    wire = json.loads(original.model_dump_json())

    # Act
    parsed = parse_compliance_event(wire)

    # Assert
    assert parsed == original


def test_parse_compliance_event_rejects_an_unknown_event_type() -> None:
    # Arrange
    payload = _kyc_payload(event_type="compliance.something_else")

    # Act / Assert
    with pytest.raises(ValidationError):
        parse_compliance_event(payload)


def test_parse_compliance_event_rejects_a_payload_without_an_event_type() -> None:
    # Arrange
    payload = _without(_kyc_payload(), "event_type")

    # Act / Assert
    with pytest.raises(ValidationError):
        parse_compliance_event(payload)


def test_parse_compliance_event_rejects_kyc_fields_under_the_subject_erased_type() -> None:
    # Arrange
    payload = _kyc_payload(event_type="compliance.subject_erased")

    # Act / Assert
    with pytest.raises(ValidationError):
        parse_compliance_event(payload)


def test_parse_compliance_event_rejects_an_extra_field_on_either_type() -> None:
    # Arrange
    payloads = [_kyc_payload(pan="4242424242424242"), _erased_payload(email="alice@example.com")]

    # Act
    rejected = []
    for payload in payloads:
        with pytest.raises(ValidationError):
            parse_compliance_event(payload)
        rejected.append(payload["event_type"])

    # Assert
    assert rejected == ["compliance.kyc_review_required", "compliance.subject_erased"]


# --- pseudonymous ids only ------------------------------------------------------------------------------


@pytest.mark.parametrize("model", [KycReviewRequiredV1, SubjectErasedV1])
def test_event_schemas_declare_no_personal_data_field(
    model: type[KycReviewRequiredV1] | type[SubjectErasedV1],
) -> None:
    # Arrange
    names = list(model.model_fields)

    # Act
    personal = [name for name in names for fragment in PII_FRAGMENTS if fragment in name]

    # Assert
    assert personal == []
