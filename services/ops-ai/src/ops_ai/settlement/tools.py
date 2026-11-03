from sqlalchemy import select
from sqlalchemy.orm import Session

from ops_ai.ingest.models import PaymentFact
from ops_ai.scope import Scope
from ops_ai.settlement.contract import FX_SNAPSHOT, STANDARD_CONTRACT
from ops_ai.settlement.models import SettlementEntry
from ops_ai.settlement.statement import (
    EntryRow,
    SettlementStatement,
    build_lines,
    make_statement,
    recompute_lines,
    verify_lines,
)

MAX_SETTLEMENT_ENTRIES = 5000
PROJECTION_STATE = "succeeded"
PROJECTION_METHOD = "card"
PERIOD_PATTERN = "^[0-9]{4}-(0[1-9]|1[0-2])$"


def explain_settlement(session: Session, scope: Scope, period: str | None) -> SettlementStatement | None:
    """The scope merchant's statement for the period (the newest one without a period), built with the
    standard contract and FX snapshot, verified, and cross-checked against the payment projection. None when
    the merchant has no entries in that period.
    """
    resolved = period if period is not None else _latest_period(session, scope)
    if resolved is None:
        return None
    rows = session.execute(
        select(
            SettlementEntry.payment_id,
            SettlementEntry.amount_minor,
            SettlementEntry.currency,
            SettlementEntry.refunded,
            SettlementEntry.chargeback,
        )
        .where(SettlementEntry.merchant_id == scope.merchant_id, SettlementEntry.statement_period == resolved)
        .order_by(SettlementEntry.captured_at, SettlementEntry.payment_id)
        .limit(MAX_SETTLEMENT_ENTRIES + 1)
    ).all()
    if not rows:
        return None
    entries = tuple(
        EntryRow(
            payment_id=row.payment_id,
            amount_minor=row.amount_minor,
            currency=row.currency,
            refunded=row.refunded,
            chargeback=row.chargeback,
        )
        for row in rows[:MAX_SETTLEMENT_ENTRIES]
    )
    lines = build_lines(entries, STANDARD_CONTRACT, FX_SNAPSHOT)
    mismatches = [
        *verify_lines(lines, entries, STANDARD_CONTRACT, FX_SNAPSHOT),
        *recompute_lines(lines, entries, STANDARD_CONTRACT, FX_SNAPSHOT),
    ]
    if len(rows) > MAX_SETTLEMENT_ENTRIES:
        mismatches.append("too_many_entries")
    if not _projection_agrees(session, scope, entries):
        mismatches.append("projection_mismatch")
    return make_statement(
        resolved, STANDARD_CONTRACT, FX_SNAPSHOT, lines, tuple(mismatches), projection_checked=len(entries)
    )


def _latest_period(session: Session, scope: Scope) -> str | None:
    newest: str | None = session.scalar(
        select(SettlementEntry.statement_period)
        .where(
            SettlementEntry.merchant_id == scope.merchant_id,
            SettlementEntry.statement_period.regexp_match(PERIOD_PATTERN),
        )
        .order_by(SettlementEntry.statement_period.desc())
        .limit(1)
    )
    return newest


def _projection_agrees(session: Session, scope: Scope, entries: tuple[EntryRow, ...]) -> bool:
    """Every entry has a succeeded card payment_facts row of the same merchant, amount and currency."""
    facts = {
        row.payment_id: (row.amount_minor, row.currency)
        for row in session.execute(
            select(PaymentFact.payment_id, PaymentFact.amount_minor, PaymentFact.currency).where(
                PaymentFact.merchant_id == scope.merchant_id,
                PaymentFact.state == PROJECTION_STATE,
                PaymentFact.method == PROJECTION_METHOD,
                PaymentFact.payment_id.in_([entry.payment_id for entry in entries]),
            )
        )
    }
    return all(facts.get(entry.payment_id) == (entry.amount_minor, entry.currency) for entry in entries)
