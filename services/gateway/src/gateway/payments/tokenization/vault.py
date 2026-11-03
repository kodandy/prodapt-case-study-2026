import secrets
from dataclasses import dataclass

from sqlalchemy.orm import Session

from gateway.clock import Clock
from gateway.payments.tokenization.cards import card_fingerprint
from gateway.payments.tokenization.models import CardToken


@dataclass(frozen=True)
class TokenizedCard:
    token: str
    fingerprint: str
    bin: str
    last4: str


def tokenize_card(
    session: Session, clock: Clock, pan: str, exp_month: int, exp_year: int, fingerprint_key: str
) -> TokenizedCard:
    """Stores token, BIN, last4 and the keyed fingerprint. The PAN itself is dropped here. No commit."""
    card = TokenizedCard(
        token=f"tok_{secrets.token_urlsafe(24)}",
        fingerprint=card_fingerprint(pan, fingerprint_key),
        bin=pan[:6],
        last4=pan[-4:],
    )
    session.add(
        CardToken(
            token=card.token,
            fingerprint=card.fingerprint,
            bin=card.bin,
            last4=card.last4,
            exp_month=exp_month,
            exp_year=exp_year,
            created_at=clock(),
        )
    )
    # payments.card_token has an FK but no relationship(), so the unit of work cannot order the inserts
    session.flush()
    return card
