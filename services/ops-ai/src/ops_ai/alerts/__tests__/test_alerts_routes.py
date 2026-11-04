"""Tests for the /v1/ops/alerts routes, written from docs/specs/failure-tools.md section 12 (Router).

The router is mounted on a bare FastAPI app; `staff_id` is a plain dependency that returns a fixed staff id.
"""

import uuid
from collections.abc import Callable
from datetime import datetime, timedelta

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from gateway.clock import FixedClock
from ops_ai.alerts import (
    acknowledge_ops_alert,
    deliver_pending,
    list_ops_alerts,
    raise_ops_alert,
)
from ops_ai.alerts.routes import create_alerts_router
from redis import Redis
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from conftest import TEST_NOW

STAFF = uuid.UUID("dddddddd-0000-4000-8000-000000000004")
OTHER_STAFF = uuid.UUID("dddddddd-0000-4000-8000-000000000005")
MERCHANT = uuid.UUID("aaaaaaaa-0000-4000-8000-000000000001")
FRAUD_ALERT = uuid.UUID("cccccccc-0000-4000-8000-000000000003")
ALERTS = "/v1/ops/alerts"
FORBIDDEN_BODY = {"detail": "forbidden"}
ITEM_KEYS = {
    "alert_id",
    "kind",
    "severity",
    "merchant_id",
    "fraud_alert_id",
    "title",
    "body",
    "created_at",
    "acknowledged_at",
    "acknowledged_by",
    "delivery_status",
}


def _staff() -> uuid.UUID:
    return STAFF


def _other_staff() -> uuid.UUID:
    return OTHER_STAFF


def _forbidden() -> uuid.UUID:
    raise HTTPException(status_code=403, detail="forbidden")


def _client(
    session_factory: sessionmaker[Session],
    clock: FixedClock,
    staff: Callable[[], uuid.UUID] = _staff,
) -> TestClient:
    app = FastAPI()
    app.include_router(create_alerts_router(session_factory, clock, staff))
    return TestClient(app)


def _seed(
    session_factory: sessionmaker[Session],
    key: str,
    *,
    minutes: int = 0,
    fraud_alert_id: uuid.UUID | None = None,
) -> uuid.UUID:
    seeded_at = FixedClock(TEST_NOW + timedelta(minutes=minutes))
    with session_factory.begin() as session:
        alert_id = raise_ops_alert(
            session,
            seeded_at,
            source_key=f"card_testing:{key}",
            kind="card_testing",
            severity="high",
            merchant_id=MERCHANT,
            fraud_alert_id=fraud_alert_id,
            title=f"Card testing suspected {key}",
            body=f"body {key}",
        )
    assert alert_id is not None
    return alert_id


def _acknowledge(
    session_factory: sessionmaker[Session], alert_id: uuid.UUID, *, minutes: int = 0, staff: uuid.UUID = STAFF
) -> None:
    with session_factory.begin() as session:
        acknowledge_ops_alert(session, FixedClock(TEST_NOW + timedelta(minutes=minutes)), alert_id, staff)


def _ids(response_json: dict[str, list[dict[str, object]]]) -> list[object]:
    return [item["alert_id"] for item in response_json["alerts"]]


def _stored_ack(session_factory: sessionmaker[Session], alert_id: uuid.UUID) -> dict[str, object]:
    with session_factory() as session:
        row = session.execute(
            text("SELECT acknowledged_at, acknowledged_by FROM ops_ai.ops_alerts WHERE alert_id = :id"),
            {"id": alert_id},
        ).mappings().one()
    return dict(row)


# --- GET /v1/ops/alerts ----------------------------------------------------------------------------------


def test_list_returns_open_alerts_by_default_newest_first(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    oldest = _seed(session_factory, "a", minutes=0)
    acknowledged = _seed(session_factory, "b", minutes=1)
    newest = _seed(session_factory, "c", minutes=2)
    _acknowledge(session_factory, acknowledged)
    client = _client(session_factory, clock)

    # Act
    response = client.get(ALERTS)

    # Assert
    assert response.status_code == 200
    assert set(response.json()) == {"alerts"}
    assert _ids(response.json()) == [str(newest), str(oldest)]


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("?status=open", ["c", "a"]),
        ("?status=acknowledged", ["b"]),
        ("?status=all", ["c", "b", "a"]),
    ],
)
def test_list_filters_by_status(
    session_factory: sessionmaker[Session], clock: FixedClock, query: str, expected: list[str]
) -> None:
    # Arrange
    ids = {
        "a": _seed(session_factory, "a", minutes=0),
        "b": _seed(session_factory, "b", minutes=1),
        "c": _seed(session_factory, "c", minutes=2),
    }
    _acknowledge(session_factory, ids["b"])
    client = _client(session_factory, clock)

    # Act
    response = client.get(f"{ALERTS}{query}")

    # Assert
    assert response.status_code == 200
    assert _ids(response.json()) == [str(ids[key]) for key in expected]


def test_list_returns_an_empty_list_when_there_are_no_alerts(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    client = _client(session_factory, clock)

    # Act
    response = client.get(ALERTS)

    # Assert
    assert response.status_code == 200
    assert response.json() == {"alerts": []}


def test_list_items_have_exactly_the_specified_keys_and_values(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    alert_id = _seed(session_factory, "a", fraud_alert_id=FRAUD_ALERT)
    client = _client(session_factory, clock)

    # Act
    response = client.get(ALERTS)

    # Assert
    (item,) = response.json()["alerts"]
    assert set(item) == ITEM_KEYS
    assert datetime.fromisoformat(item["created_at"]) == TEST_NOW
    assert {key: value for key, value in item.items() if key != "created_at"} == {
        "alert_id": str(alert_id),
        "kind": "card_testing",
        "severity": "high",
        "merchant_id": str(MERCHANT),
        "fraud_alert_id": str(FRAUD_ALERT),
        "title": "Card testing suspected a",
        "body": "body a",
        "acknowledged_at": None,
        "acknowledged_by": None,
        "delivery_status": "pending",
    }


def test_list_item_delivery_status_is_delivered_once_the_alert_was_streamed(
    session_factory: sessionmaker[Session], redis_client: Redis, clock: FixedClock
) -> None:
    # Arrange
    _seed(session_factory, "a")
    deliver_pending(session_factory, redis_client, clock)
    client = _client(session_factory, clock)

    # Act
    response = client.get(ALERTS)

    # Assert
    (item,) = response.json()["alerts"]
    assert item["delivery_status"] == "delivered"


def test_list_item_of_an_acknowledged_alert_carries_the_acknowledgement(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    alert_id = _seed(session_factory, "a")
    _acknowledge(session_factory, alert_id, minutes=30)
    client = _client(session_factory, clock)

    # Act
    response = client.get(f"{ALERTS}?status=acknowledged")

    # Assert
    (item,) = response.json()["alerts"]
    assert datetime.fromisoformat(item["acknowledged_at"]) == TEST_NOW + timedelta(minutes=30)
    assert item["acknowledged_by"] == str(STAFF)


def test_list_defaults_to_50_alerts(session_factory: sessionmaker[Session], clock: FixedClock) -> None:
    # Arrange
    for index in range(51):
        _seed(session_factory, f"k{index}", minutes=index)
    client = _client(session_factory, clock)

    # Act
    response = client.get(ALERTS)

    # Assert
    assert response.status_code == 200
    assert len(response.json()["alerts"]) == 50


def test_list_honours_the_limit_and_keeps_the_newest(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    _seed(session_factory, "a", minutes=0)
    middle = _seed(session_factory, "b", minutes=1)
    newest = _seed(session_factory, "c", minutes=2)
    client = _client(session_factory, clock)

    # Act
    response = client.get(f"{ALERTS}?limit=2")

    # Assert
    assert _ids(response.json()) == [str(newest), str(middle)]


@pytest.mark.parametrize("limit", [1, 200])
def test_list_accepts_the_limit_bounds(
    session_factory: sessionmaker[Session], clock: FixedClock, limit: int
) -> None:
    # Arrange
    _seed(session_factory, "a")
    client = _client(session_factory, clock)

    # Act
    response = client.get(f"{ALERTS}?limit={limit}")

    # Assert
    assert response.status_code == 200
    assert len(response.json()["alerts"]) == 1


@pytest.mark.parametrize("limit", ["0", "201", "-1", "abc"])
def test_list_rejects_a_limit_outside_1_to_200(
    session_factory: sessionmaker[Session], clock: FixedClock, limit: str
) -> None:
    # Arrange
    client = _client(session_factory, clock)

    # Act
    response = client.get(f"{ALERTS}?limit={limit}")

    # Assert
    assert response.status_code == 422


def test_list_rejects_an_unknown_status(session_factory: sessionmaker[Session], clock: FixedClock) -> None:
    # Arrange
    client = _client(session_factory, clock)

    # Act
    response = client.get(f"{ALERTS}?status=bogus")

    # Assert
    assert response.status_code == 422


# --- GET /v1/ops/alerts/{alert_id} -----------------------------------------------------------------------


def test_detail_returns_the_alert_with_exactly_the_specified_keys(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    alert_id = _seed(session_factory, "a", fraud_alert_id=FRAUD_ALERT)
    _seed(session_factory, "other", minutes=1)
    client = _client(session_factory, clock)

    # Act
    response = client.get(f"{ALERTS}/{alert_id}")

    # Assert
    body = response.json()
    assert response.status_code == 200
    assert set(body) == ITEM_KEYS
    assert (body["alert_id"], body["title"], body["fraud_alert_id"], body["delivery_status"]) == (
        str(alert_id),
        "Card testing suspected a",
        str(FRAUD_ALERT),
        "pending",
    )


def test_detail_of_an_acknowledged_alert_is_found_regardless_of_status(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    alert_id = _seed(session_factory, "a")
    _acknowledge(session_factory, alert_id)
    client = _client(session_factory, clock)

    # Act
    response = client.get(f"{ALERTS}/{alert_id}")

    # Assert
    assert response.status_code == 200
    assert response.json()["acknowledged_by"] == str(STAFF)


def test_detail_of_an_unknown_alert_is_404(session_factory: sessionmaker[Session], clock: FixedClock) -> None:
    # Arrange
    _seed(session_factory, "a")
    client = _client(session_factory, clock)

    # Act
    response = client.get(f"{ALERTS}/{uuid.uuid4()}")

    # Assert
    assert response.status_code == 404


def test_detail_with_a_malformed_alert_id_is_422(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    client = _client(session_factory, clock)

    # Act
    response = client.get(f"{ALERTS}/not-a-uuid")

    # Assert
    assert response.status_code == 422


# --- POST /v1/ops/alerts/{alert_id}/ack ------------------------------------------------------------------


def test_ack_sets_acknowledged_at_and_by_and_returns_the_alert(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    alert_id = _seed(session_factory, "a")
    clock.advance(timedelta(minutes=10))
    client = _client(session_factory, clock)

    # Act
    response = client.post(f"{ALERTS}/{alert_id}/ack")

    # Assert
    body = response.json()
    assert response.status_code == 200
    assert set(body) == ITEM_KEYS
    assert body["alert_id"] == str(alert_id)
    assert datetime.fromisoformat(body["acknowledged_at"]) == clock()
    assert body["acknowledged_by"] == str(STAFF)
    assert _stored_ack(session_factory, alert_id) == {"acknowledged_at": clock(), "acknowledged_by": STAFF}


def test_ack_moves_the_alert_from_the_open_list_to_the_acknowledged_list(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    alert_id = _seed(session_factory, "a")
    client = _client(session_factory, clock)

    # Act
    client.post(f"{ALERTS}/{alert_id}/ack")

    # Assert
    assert client.get(f"{ALERTS}?status=open").json() == {"alerts": []}
    assert _ids(client.get(f"{ALERTS}?status=acknowledged").json()) == [str(alert_id)]


def test_ack_leaves_other_alerts_open(session_factory: sessionmaker[Session], clock: FixedClock) -> None:
    # Arrange
    target = _seed(session_factory, "a", minutes=0)
    other = _seed(session_factory, "b", minutes=1)
    client = _client(session_factory, clock)

    # Act
    client.post(f"{ALERTS}/{target}/ack")

    # Assert
    assert _ids(client.get(ALERTS).json()) == [str(other)]
    assert _stored_ack(session_factory, other) == {"acknowledged_at": None, "acknowledged_by": None}


def test_a_second_ack_is_idempotent_and_keeps_the_first_values_even_for_another_staff_member(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    alert_id = _seed(session_factory, "a")
    first_ack_at = clock()
    first = _client(session_factory, clock, _staff).post(f"{ALERTS}/{alert_id}/ack")
    clock.advance(timedelta(hours=1))

    # Act
    second = _client(session_factory, clock, _other_staff).post(f"{ALERTS}/{alert_id}/ack")

    # Assert
    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json() == first.json()
    assert datetime.fromisoformat(second.json()["acknowledged_at"]) == first_ack_at
    assert second.json()["acknowledged_by"] == str(STAFF)
    stored = _stored_ack(session_factory, alert_id)
    assert stored == {"acknowledged_at": first_ack_at, "acknowledged_by": STAFF}


def test_ack_of_an_unknown_alert_is_404_and_changes_nothing(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    alert_id = _seed(session_factory, "a")
    client = _client(session_factory, clock)

    # Act
    response = client.post(f"{ALERTS}/{uuid.uuid4()}/ack")

    # Assert
    assert response.status_code == 404
    assert _stored_ack(session_factory, alert_id) == {"acknowledged_at": None, "acknowledged_by": None}


# --- headers and authorization ---------------------------------------------------------------------------


def test_every_successful_response_sets_cache_control_no_store(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    alert_id = _seed(session_factory, "a")
    client = _client(session_factory, clock)

    # Act
    responses = [
        client.get(ALERTS),
        client.get(f"{ALERTS}/{alert_id}"),
        client.post(f"{ALERTS}/{alert_id}/ack"),
    ]

    # Assert
    assert [r.status_code for r in responses] == [200, 200, 200]
    assert [r.headers["Cache-Control"] for r in responses] == ["no-store"] * 3


def test_an_empty_list_response_sets_cache_control_no_store(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    client = _client(session_factory, clock)

    # Act
    response = client.get(ALERTS)

    # Assert
    assert response.headers["Cache-Control"] == "no-store"


@pytest.mark.parametrize("method", ["list", "detail", "ack"])
def test_a_403_from_the_staff_dependency_propagates_on_every_route(
    session_factory: sessionmaker[Session], clock: FixedClock, method: str
) -> None:
    # Arrange
    alert_id = _seed(session_factory, "a")
    client = _client(session_factory, clock, _forbidden)

    # Act
    if method == "list":
        response = client.get(ALERTS)
    elif method == "detail":
        response = client.get(f"{ALERTS}/{alert_id}")
    else:
        response = client.post(f"{ALERTS}/{alert_id}/ack")

    # Assert
    assert (response.status_code, response.json()) == (403, FORBIDDEN_BODY)
    assert str(alert_id) not in response.text


def test_a_403_on_ack_leaves_the_alert_unacknowledged(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    alert_id = _seed(session_factory, "a")
    client = _client(session_factory, clock, _forbidden)

    # Act
    client.post(f"{ALERTS}/{alert_id}/ack")

    # Assert
    assert _stored_ack(session_factory, alert_id) == {"acknowledged_at": None, "acknowledged_by": None}


# --- service functions -----------------------------------------------------------------------------------


def test_list_ops_alerts_returns_views_newest_first_filtered_by_status_and_limit(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    oldest = _seed(session_factory, "a", minutes=0)
    middle = _seed(session_factory, "b", minutes=1)
    newest = _seed(session_factory, "c", minutes=2)
    _acknowledge(session_factory, middle)

    # Act
    with session_factory() as session:
        open_views = list_ops_alerts(session, status="open", limit=50)
        acknowledged_views = list_ops_alerts(session, status="acknowledged", limit=50)
        all_views = list_ops_alerts(session, status="all", limit=2)

    # Assert
    assert [v.alert_id for v in open_views] == [newest, oldest]
    assert [v.alert_id for v in acknowledged_views] == [middle]
    assert [v.alert_id for v in all_views] == [newest, middle]


def test_acknowledge_ops_alert_returns_none_for_an_unknown_alert(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    _seed(session_factory, "a")

    # Act
    with session_factory.begin() as session:
        view = acknowledge_ops_alert(session, clock, uuid.uuid4(), STAFF)

    # Assert
    assert view is None


def test_acknowledge_ops_alert_returns_the_view_with_the_acknowledgement(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    alert_id = _seed(session_factory, "a")

    # Act
    with session_factory.begin() as session:
        view = acknowledge_ops_alert(session, clock, alert_id, STAFF)

    # Assert
    assert view is not None
    assert (view.alert_id, view.acknowledged_at, view.acknowledged_by) == (alert_id, clock(), STAFF)
