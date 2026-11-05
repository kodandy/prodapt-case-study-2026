import logging
import uuid
from datetime import timedelta

from contracts.events import PaymentEventV1
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from ops_ai.alerts import raise_ops_alert
from ops_ai.clock import Clock
from ops_ai.feedback import thresholds
from ops_ai.fraud_watch.analysis import CardTestingEpisode, analyze_fraud
from ops_ai.fraud_watch.models import FraudAlert, FraudAlertRuleVersion
from ops_ai.fraud_watch.rules import LIVE_LOOKBACK_MINUTES, is_micro
from ops_ai.scope import Scope
from ops_ai.tickets import TicketSource, route_ticket
from ops_ai.tickets.models import Ticket

logger = logging.getLogger(__name__)

CARD_TESTING_KIND: TicketSource = "card_testing"
TICKET_TITLE = "Card testing suspected"


def watch_payment(session: Session, clock: Clock, event: PaymentEventV1) -> uuid.UUID | None:
    """Raises or refreshes the card-testing alert for the episode that holds a live micro payment.

    Returns the alert id, or None when the event is not live, not micro, or its episode is not flagged.
    The merchant's active rule version can only tighten the baseline flag; the version applied is recorded
    per alert. The ticket is created only when the alert is first inserted. Does not commit.
    """
    if event.origin != "live" or not is_micro(event.amount_minor, event.currency):
        return None
    scope = Scope(merchant_id=event.merchant_id)
    analysis = analyze_fraud(
        session,
        scope,
        event.occurred_at - timedelta(minutes=LIVE_LOOKBACK_MINUTES),
        event.occurred_at + timedelta(microseconds=1),
    )
    episode = next((e for e in analysis.episodes if event.payment_id in e.payment_ids), None)
    if episode is None or not episode.flagged:
        return None
    rule = thresholds.active_rule_version(session, scope)
    if not thresholds.is_flagged(episode, rule):
        return None
    alert_id = _upsert_alert(session, clock, event, episode)
    _record_applied_version(session, clock, alert_id, event.merchant_id, rule.version)
    return alert_id


def applied_rule_version(session: Session, scope: Scope, alert_id: uuid.UUID) -> int | None:
    return session.execute(
        select(FraudAlertRuleVersion.rule_version).where(
            FraudAlertRuleVersion.alert_id == alert_id, FraudAlertRuleVersion.merchant_id == scope.merchant_id
        )
    ).scalar_one_or_none()


def _record_applied_version(
    session: Session, clock: Clock, alert_id: uuid.UUID, merchant_id: uuid.UUID, rule_version: int
) -> None:
    applied_at = clock()
    session.execute(
        insert(FraudAlertRuleVersion)
        .values(alert_id=alert_id, merchant_id=merchant_id, rule_version=rule_version, applied_at=applied_at)
        .on_conflict_do_update(
            index_elements=[FraudAlertRuleVersion.alert_id],
            set_={"rule_version": rule_version, "applied_at": applied_at},
        )
    )


def _upsert_alert(
    session: Session, clock: Clock, event: PaymentEventV1, episode: CardTestingEpisode
) -> uuid.UUID:
    window_key = f"{CARD_TESTING_KIND}:{event.merchant_id}:{episode.first_payment_id}"
    features = _features(episode)
    inserted = session.execute(
        insert(FraudAlert)
        .values(
            alert_id=uuid.uuid4(),
            window_key=window_key,
            merchant_id=event.merchant_id,
            kind=CARD_TESTING_KIND,
            first_payment_id=episode.first_payment_id,
            started_at=episode.start,
            last_seen_at=episode.end,
            payment_count=episode.micro_payments,
            features=features,
            created_at=clock(),
        )
        .on_conflict_do_nothing(index_elements=["window_key"])
        .returning(FraudAlert.alert_id)
    ).scalar_one_or_none()
    if inserted is None:
        return session.execute(
            update(FraudAlert)
            .where(FraudAlert.window_key == window_key)
            .values(last_seen_at=episode.end, payment_count=episode.micro_payments, features=features)
            .returning(FraudAlert.alert_id)
        ).scalar_one()
    queue, priority = route_ticket(CARD_TESTING_KIND)
    session.execute(
        insert(Ticket)
        .values(
            ticket_id=uuid.uuid4(),
            source_key=window_key,
            source=CARD_TESTING_KIND,
            queue=queue,
            priority=priority,
            merchant_id=event.merchant_id,
            title=TICKET_TITLE,
            reason=_ticket_reason(episode),
            alert_id=inserted,
            created_at=clock(),
        )
        .on_conflict_do_nothing(index_elements=["source_key"])
    )
    raise_ops_alert(
        session,
        clock,
        source_key=window_key,
        kind=CARD_TESTING_KIND,
        severity="high",
        merchant_id=event.merchant_id,
        fraud_alert_id=inserted,
        title=TICKET_TITLE,
        body=_ticket_reason(episode),
    )
    logger.info(
        "fraud alert raised alert_id=%s merchant_id=%s payment_count=%d",
        inserted,
        event.merchant_id,
        episode.micro_payments,
    )
    return inserted


def _features(episode: CardTestingEpisode) -> dict[str, object]:
    return {
        "micro_payments": episode.micro_payments,
        "card_payments": episode.card_payments,
        "micro_share": episode.micro_share,
        "distinct_cards": episode.distinct_cards,
        "declined": episode.declined,
        "decline_ratio": episode.decline_ratio,
        "card_bins": list(episode.card_bins),
        "currencies": list(episode.currencies),
        "reasons": list(episode.reasons),
    }


def format_span(episode: CardTestingEpisode) -> str:
    minutes, seconds = divmod(int((episode.end - episode.start).total_seconds()), 60)
    return f"{minutes} min {seconds} s"


def describe_cards(distinct_cards: int | None) -> str:
    return "card identity unknown" if distinct_cards is None else f"{distinct_cards} distinct cards"


def _ticket_reason(episode: CardTestingEpisode) -> str:
    return (
        f"{episode.micro_payments} micro card payments on {describe_cards(episode.distinct_cards)} "
        f"over {format_span(episode)}"
    )
