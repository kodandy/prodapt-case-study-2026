import uuid
from datetime import timedelta

from sqlalchemy import Text, cast, func, select
from sqlalchemy.orm import Session

from ops_ai.clock import Clock
from ops_ai.ingest.models import PaymentFact
from ops_ai.tickets.models import Ticket
from ops_ai.tickets.service import NewTicket, TicketOpened, open_ticket

UNKNOWN_GRACE = timedelta(minutes=15)
UNKNOWN_SWEEP_LIMIT = 500
DISPUTE_TITLE = "Chargeback received"
UNKNOWN_TITLE = "Payment outcome unknown"


def open_dispute_ticket(
    session: Session,
    clock: Clock,
    *,
    payment_id: uuid.UUID,
    merchant_id: uuid.UUID,
    amount_minor: int,
    currency: str,
) -> TicketOpened:
    """One ticket per charged-back payment. The acquirer settlement report's chargeback flag is the dispute
    signal ops-ai ingests (no gateway dispute event reaches ops-ai). Does not commit."""
    return open_ticket(
        session,
        clock,
        NewTicket(
            source="dispute",
            source_key=f"dispute:{payment_id}",
            merchant_id=merchant_id,
            title=DISPUTE_TITLE,
            reason=(
                f"Chargeback on payment {payment_id}: {amount_minor} {currency} (minor units) "
                "per the acquirer settlement report"
            ),
        ),
    )


def sweep_unknown_payments(
    session: Session, clock: Clock, grace: timedelta = UNKNOWN_GRACE, limit: int = UNKNOWN_SWEEP_LIMIT
) -> int:
    """Opens a ticket for each payment still `unknown` after `grace` (oldest first, at most `limit`) that has
    none yet. Returns the number of tickets created. Does not commit."""
    cutoff = clock() - grace
    ticketed = select(Ticket.ticket_id).where(
        Ticket.source_key == func.concat("unknown_payment:", cast(PaymentFact.payment_id, Text))
    )
    rows = session.execute(
        select(
            PaymentFact.payment_id,
            PaymentFact.merchant_id,
            PaymentFact.amount_minor,
            PaymentFact.currency,
            PaymentFact.provider,
            PaymentFact.occurred_at,
        )
        .where(PaymentFact.state == "unknown", PaymentFact.occurred_at <= cutoff, ~ticketed.exists())
        .order_by(PaymentFact.occurred_at, PaymentFact.payment_id)
        .limit(limit)
    ).all()
    created = 0
    for row in rows:
        opened = open_ticket(
            session,
            clock,
            NewTicket(
                source="unknown_payment",
                source_key=f"unknown_payment:{row.payment_id}",
                merchant_id=row.merchant_id,
                title=UNKNOWN_TITLE,
                reason=(
                    f"Payment {row.payment_id} ({row.amount_minor} {row.currency} minor units, "
                    f"provider {row.provider or 'none'}) has been in state unknown since "
                    f"{row.occurred_at.isoformat()}"
                ),
            ),
        )
        created += int(opened.created)
    return created
