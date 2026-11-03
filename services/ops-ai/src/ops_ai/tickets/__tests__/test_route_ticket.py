"""Tests for route_ticket, written from docs/specs/sq3.md section 4 (ops_ai/tickets)."""

from ops_ai.tickets import TicketSource, route_ticket


def test_route_ticket_sends_card_testing_to_the_fraud_queue_with_high_priority() -> None:
    # Arrange
    source: TicketSource = "card_testing"

    # Act
    routed = route_ticket(source)

    # Assert
    assert routed == ("fraud", "high")


def test_route_ticket_is_deterministic() -> None:
    # Arrange / Act
    first = route_ticket("card_testing")
    second = route_ticket("card_testing")

    # Assert
    assert first == second
