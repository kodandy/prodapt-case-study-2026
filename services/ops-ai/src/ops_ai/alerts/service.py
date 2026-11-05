import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from redis import Redis
from redis.exceptions import RedisError
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, sessionmaker

from ops_ai.alerts.models import OpsAlert, OpsAlertDelivery
from ops_ai.clock import Clock

OPS_ALERTS_STREAM = "ops.alerts.v1"
OPS_ALERT_CHANNEL = "redis_stream"
MAX_DELIVERY_ATTEMPTS = 5
AlertStatus = Literal["open", "acknowledged", "all"]


@dataclass(frozen=True)
class OpsAlertView:
    alert_id: uuid.UUID
    kind: str
    severity: str
    merchant_id: uuid.UUID
    fraud_alert_id: uuid.UUID | None
    title: str
    body: str
    created_at: datetime
    acknowledged_at: datetime | None
    acknowledged_by: uuid.UUID | None
    delivery_status: str


def raise_ops_alert(
    session: Session,
    clock: Clock,
    *,
    source_key: str,
    kind: str,
    severity: str,
    merchant_id: uuid.UUID,
    fraud_alert_id: uuid.UUID | None,
    title: str,
    body: str,
) -> uuid.UUID | None:
    now = clock()
    alert_id = session.execute(
        insert(OpsAlert)
        .values(
            alert_id=uuid.uuid4(),
            source_key=source_key,
            kind=kind,
            severity=severity,
            merchant_id=merchant_id,
            fraud_alert_id=fraud_alert_id,
            title=title,
            body=body,
            created_at=now,
        )
        .on_conflict_do_nothing(index_elements=["source_key"])
        .returning(OpsAlert.alert_id)
    ).scalar_one_or_none()
    if alert_id is not None:
        session.execute(
            insert(OpsAlertDelivery).values(
                delivery_id=uuid.uuid4(),
                alert_id=alert_id,
                channel=OPS_ALERT_CHANNEL,
                status="pending",
                attempts=0,
                created_at=now,
            )
        )
    return alert_id


def deliver_pending(
    session_factory: sessionmaker[Session], redis: Redis, clock: Clock, limit: int = 100
) -> int:
    delivered = 0
    with session_factory.begin() as session:
        deliveries = session.scalars(
            select(OpsAlertDelivery)
            .where(OpsAlertDelivery.status == "pending", OpsAlertDelivery.channel == OPS_ALERT_CHANNEL)
            .order_by(OpsAlertDelivery.created_at, OpsAlertDelivery.delivery_id)
            .limit(limit)
            .with_for_update(skip_locked=True)
        ).all()
        for delivery in deliveries:
            alert = session.get(OpsAlert, delivery.alert_id)
            if alert is None:
                continue
            try:
                entry_id = redis.xadd(
                    OPS_ALERTS_STREAM,
                    {
                        "alert_id": str(alert.alert_id),
                        "kind": alert.kind,
                        "severity": alert.severity,
                        "merchant_id": str(alert.merchant_id),
                        "title": alert.title,
                        "created_at": alert.created_at.isoformat(),
                    },
                    maxlen=10_000,
                    approximate=True,
                )
            except RedisError as exc:
                delivery.attempts += 1
                delivery.last_error = type(exc).__name__[:64]
                if delivery.attempts >= MAX_DELIVERY_ATTEMPTS:
                    delivery.status = "failed"
                continue
            delivery.status = "delivered"
            delivery.stream_entry_id = entry_id.decode() if isinstance(entry_id, bytes) else str(entry_id)
            delivery.delivered_at = clock()
            delivery.last_error = None
            delivered += 1
    return delivered


def _view(alert: OpsAlert, delivery_status: str) -> OpsAlertView:
    return OpsAlertView(
        alert.alert_id,
        alert.kind,
        alert.severity,
        alert.merchant_id,
        alert.fraud_alert_id,
        alert.title,
        alert.body,
        alert.created_at,
        alert.acknowledged_at,
        alert.acknowledged_by,
        delivery_status,
    )


def get_ops_alert(session: Session, alert_id: uuid.UUID) -> OpsAlertView | None:
    row = session.execute(
        select(OpsAlert, OpsAlertDelivery.status)
        .join(
            OpsAlertDelivery,
            (OpsAlertDelivery.alert_id == OpsAlert.alert_id)
            & (OpsAlertDelivery.channel == OPS_ALERT_CHANNEL),
        )
        .where(OpsAlert.alert_id == alert_id)
    ).one_or_none()
    return None if row is None else _view(row[0], row[1])


def list_ops_alerts(session: Session, *, status: AlertStatus, limit: int) -> list[OpsAlertView]:
    query = select(OpsAlert, OpsAlertDelivery.status).join(
        OpsAlertDelivery,
        (OpsAlertDelivery.alert_id == OpsAlert.alert_id) & (OpsAlertDelivery.channel == OPS_ALERT_CHANNEL),
    )
    if status == "open":
        query = query.where(OpsAlert.acknowledged_at.is_(None))
    elif status == "acknowledged":
        query = query.where(OpsAlert.acknowledged_at.is_not(None))
    rows = session.execute(query.order_by(OpsAlert.created_at.desc(), OpsAlert.alert_id.desc()).limit(limit))
    return [_view(row[0], row[1]) for row in rows]


def acknowledge_ops_alert(
    session: Session, clock: Clock, alert_id: uuid.UUID, staff_id: uuid.UUID
) -> OpsAlertView | None:
    session.execute(
        update(OpsAlert)
        .where(
            OpsAlert.alert_id == alert_id,
            OpsAlert.acknowledged_at.is_(None),
        )
        .values(acknowledged_at=clock(), acknowledged_by=staff_id)
    )
    return get_ops_alert(session, alert_id)
