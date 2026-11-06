"""RED HTTP tests for M3, from docs/specs/compliance-rbac.md section 5: a `rejected` user moves no money at
all. POST /v1/me/transfers (even a small domestic transfer) and POST /v1/me/payments answer 403
`{"error": "kyc_reverification_required"}` (Cache-Control: no-store), move no money, call no provider and
audit one `kyc.operation_blocked` row. The payment route checks the Idempotency-Key first (400 wins). A
`pending` or never-screened user keeps transferring small amounts and paying merchants."""

import threading
import uuid
from dataclasses import dataclass

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from gateway.accounts.service import create_merchant, sign_up
from gateway.accounts.sessions import issue_session_token
from gateway.app import GatewayAppDeps, create_app
from gateway.clock import FixedClock
from gateway.payments.charge import ChargeDeps
from gateway.payments.providers.port import CardRef, ProviderPort, ProviderResult
from gateway.payments.providers.simulator.provider import SimulatorProvider
from gateway.wallets.service import list_wallets, top_up_wallet
from httpx import Response
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from conftest import TEST_API_KEY_PEPPER, TEST_CARD_FINGERPRINT_KEY

TRANSFERS = "/v1/me/transfers"
PAYMENTS = "/v1/me/payments"
ALICE = "alice@example.com"
BOB = "bob@example.com"
PASSWORD = "rejected-user-password-7Q"
SAME_COUNTRY = "US"  # sender and recipient share a country, so P2P transfers stay domestic
APPROVE_PAN = "4242424242424242"
NO_STORE = "no-store"
KYC_REFUSAL = {"error": "kyc_reverification_required"}
FUNDS_MINOR = 10_000
SMALL_AMOUNT = 1_000
PAYMENT_AMOUNT = 1_500
Balances = list[tuple[str, int, int]]


class CountingProvider:
    """Wraps a provider and counts `charge` calls (thread-safe)."""

    def __init__(self, inner: ProviderPort) -> None:
        self.name = inner.name
        self._inner = inner
        self._lock = threading.Lock()
        self._calls = 0

    @property
    def calls(self) -> int:
        with self._lock:
            return self._calls

    def charge(self, provider_key: str, amount_minor: int, currency: str, card: CardRef) -> ProviderResult:
        with self._lock:
            self._calls += 1
        return self._inner.charge(provider_key, amount_minor, currency, card)

    def lookup(self, provider_key: str) -> ProviderResult | None:
        return self._inner.lookup(provider_key)


@dataclass(frozen=True)
class Env:
    client: TestClient
    provider: CountingProvider
    private_pem: str
    session_factory: sessionmaker[Session]
    clock: FixedClock
    merchant_id: uuid.UUID


@pytest.fixture(scope="module")
def private_pem() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()


# --- arrange helpers -------------------------------------------------------------------------------------


def _env(session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str) -> Env:
    provider = CountingProvider(SimulatorProvider(session_factory, clock))
    charge = ChargeDeps(
        session_factory=session_factory,
        redis=None,
        provider=provider,
        clock=clock,
        card_fingerprint_key=TEST_CARD_FINGERPRINT_KEY,
    )
    with session_factory() as session:
        credentials = create_merchant(session, clock, "Acme", TEST_API_KEY_PEPPER)
        session.commit()
    deps = GatewayAppDeps(charge=charge, api_key_pepper=TEST_API_KEY_PEPPER, jwt_private_key_pem=private_pem)
    return Env(
        TestClient(create_app(deps)), provider, private_pem, session_factory, clock, credentials.merchant_id
    )


def _user(env: Env, email: str, kyc_status: str | None = None) -> uuid.UUID:
    """A funded user; `kyc_status` None keeps the sign-up default."""
    with env.session_factory() as session:
        result = sign_up(session, env.clock, email, PASSWORD)
        session.commit()
    with env.session_factory.begin() as session:
        session.execute(
            text("UPDATE gateway.users SET country = :country WHERE id = :id"),
            {"country": SAME_COUNTRY, "id": result.user_id},
        )
    with env.session_factory.begin() as session:
        top_up_wallet(session, env.clock, result.user_id, "USD", FUNDS_MINOR)
        if kyc_status is not None:
            session.execute(
                text("UPDATE gateway.users SET kyc_status = :kyc_status WHERE id = :id"),
                {"kyc_status": kyc_status, "id": result.user_id},
            )
    return result.user_id


def _auth(env: Env, user_id: uuid.UUID) -> dict[str, str]:
    token = issue_session_token(env.private_pem, user_id, env.clock(), ("pwd",))
    return {"Authorization": f"Bearer {token}"}


def _post_transfer(env: Env, user_id: uuid.UUID, amount_minor: int = SMALL_AMOUNT) -> Response:
    response: Response = env.client.post(
        TRANSFERS,
        headers={**_auth(env, user_id), "Idempotency-Key": "k-1"},
        json={"recipient_email": BOB, "amount_minor": amount_minor, "currency": "USD"},
    )
    return response


def _post_payment(env: Env, user_id: uuid.UUID, key: str | None = "k-1") -> Response:
    headers = _auth(env, user_id)
    if key is not None:
        headers["Idempotency-Key"] = key
    response: Response = env.client.post(
        PAYMENTS,
        headers=headers,
        json={
            "merchant_id": str(env.merchant_id),
            "amount_minor": PAYMENT_AMOUNT,
            "currency": "USD",
            "card": {"number": APPROVE_PAN, "exp_month": 12, "exp_year": 2030, "cvc": "123"},
        },
    )
    return response


# --- assert helpers --------------------------------------------------------------------------------------


def _rows(env: Env, query: str, **params: object) -> list[dict[str, object]]:
    with env.session_factory() as session:
        return [dict(row) for row in session.execute(text(query), params).mappings()]


def _count(env: Env, table: str) -> int:
    assert table in {"transfers", "journal_entries", "payments"}
    query = f"SELECT count(*) AS n FROM gateway.{table}"  # noqa: S608
    return int(str(_rows(env, query)[0]["n"]))


def _balances(env: Env, user_id: uuid.UUID) -> Balances:
    with env.session_factory() as session:
        return [(v.currency, v.available_minor, v.held_minor) for v in list_wallets(session, user_id)]


def _blocked_audit(env: Env) -> list[dict[str, object]]:
    return _rows(
        env,
        "SELECT actor_id, actor_role, action, summary FROM gateway.audit_log "
        "WHERE action = 'kyc.operation_blocked' ORDER BY id",
    )


# --- POST /v1/me/transfers -------------------------------------------------------------------------------


def test_a_small_domestic_transfer_by_a_rejected_user_returns_403_with_no_store(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    env = _env(session_factory, clock, private_pem)
    alice = _user(env, ALICE, "rejected")
    _user(env, BOB)

    # Act
    response = _post_transfer(env, alice)

    # Assert
    assert response.status_code == 403
    assert response.json() == KYC_REFUSAL
    assert response.headers["Cache-Control"] == NO_STORE


def test_a_refused_transfer_by_a_rejected_user_moves_no_money_and_creates_no_transfer(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    env = _env(session_factory, clock, private_pem)
    alice = _user(env, ALICE, "rejected")
    bob = _user(env, BOB)
    journal_before = _count(env, "journal_entries")

    # Act
    _post_transfer(env, alice)

    # Assert
    assert _count(env, "transfers") == 0
    assert _count(env, "journal_entries") == journal_before
    assert _balances(env, alice) == [("USD", FUNDS_MINOR, 0)]
    assert _balances(env, bob) == [("USD", FUNDS_MINOR, 0)]


def test_a_refused_transfer_by_a_rejected_user_audits_one_operation_blocked_row(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    env = _env(session_factory, clock, private_pem)
    alice = _user(env, ALICE, "rejected")
    _user(env, BOB)

    # Act
    _post_transfer(env, alice)

    # Assert
    [row] = _blocked_audit(env)
    summary = row["summary"]
    assert isinstance(summary, dict)
    assert row["actor_id"] == f"user:{alice}"
    assert row["action"] == "kyc.operation_blocked"
    assert summary["operation"] == "transfer"
    assert summary["kyc_status"] == "rejected"


def test_a_small_domestic_transfer_by_a_pending_user_is_still_allowed(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    env = _env(session_factory, clock, private_pem)
    alice = _user(env, ALICE, "pending")
    bob = _user(env, BOB)

    # Act
    response = _post_transfer(env, alice)

    # Assert
    assert response.status_code == 201
    assert _count(env, "transfers") == 1
    assert _balances(env, alice) == [("USD", FUNDS_MINOR - SMALL_AMOUNT, 0)]
    assert _balances(env, bob) == [("USD", FUNDS_MINOR + SMALL_AMOUNT, 0)]
    assert _blocked_audit(env) == []


# --- POST /v1/me/payments --------------------------------------------------------------------------------


def test_paying_a_merchant_as_a_rejected_user_returns_403_with_no_store(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    env = _env(session_factory, clock, private_pem)
    alice = _user(env, ALICE, "rejected")

    # Act
    response = _post_payment(env, alice)

    # Assert
    assert response.status_code == 403
    assert response.json() == KYC_REFUSAL
    assert response.headers["Cache-Control"] == NO_STORE


def test_a_refused_payment_by_a_rejected_user_creates_no_payment_and_never_calls_the_provider(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    env = _env(session_factory, clock, private_pem)
    alice = _user(env, ALICE, "rejected")

    # Act
    _post_payment(env, alice)

    # Assert
    assert _count(env, "payments") == 0
    assert env.provider.calls == 0


def test_a_refused_payment_by_a_rejected_user_audits_one_operation_blocked_row(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    env = _env(session_factory, clock, private_pem)
    alice = _user(env, ALICE, "rejected")

    # Act
    _post_payment(env, alice)

    # Assert
    [row] = _blocked_audit(env)
    assert row["actor_id"] == f"user:{alice}"
    assert row["actor_role"] == "consumer"
    assert row["summary"] == {"operation": "payment", "kyc_status": "rejected"}


@pytest.mark.parametrize("key", [None, "has space"], ids=["missing", "invalid"])
def test_a_rejected_user_with_a_missing_or_invalid_idempotency_key_still_gets_400_and_no_audit(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str, key: str | None
) -> None:
    # Arrange
    env = _env(session_factory, clock, private_pem)
    alice = _user(env, ALICE, "rejected")

    # Act
    response = _post_payment(env, alice, key)

    # Assert
    assert response.status_code == 400
    assert _blocked_audit(env) == []
    assert _count(env, "payments") == 0
    assert env.provider.calls == 0


@pytest.mark.parametrize("kyc_status", ["pending", None], ids=["pending", "never-screened"])
def test_a_pending_or_never_screened_user_still_pays_a_merchant(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str, kyc_status: str | None
) -> None:
    # Arrange
    env = _env(session_factory, clock, private_pem)
    alice = _user(env, ALICE, kyc_status)

    # Act
    response = _post_payment(env, alice)

    # Assert
    body = response.json()
    assert response.status_code == 201
    assert (body["state"], body["amount_minor"], body["currency"]) == ("succeeded", PAYMENT_AMOUNT, "USD")
    assert _count(env, "payments") == 1
    assert env.provider.calls == 1
    assert _blocked_audit(env) == []
