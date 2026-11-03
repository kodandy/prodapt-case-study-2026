from typing import Literal

TicketSource = Literal["card_testing"]
Queue = Literal["fraud", "payments_ops", "settlement", "compliance"]
Priority = Literal["high", "normal", "low"]

_ROUTES: dict[TicketSource, tuple[Queue, Priority]] = {"card_testing": ("fraud", "high")}


def route_ticket(source: TicketSource) -> tuple[Queue, Priority]:
    """Deterministic routing table: the queue and priority a ticket of this source starts in."""
    return _ROUTES[source]
