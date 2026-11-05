"""RED tests for gRPC transport security: server credentials come from env, plaintext only by explicit opt-in.

`resolve_server_credentials` decides between TLS, an explicit insecure opt-in and refusing to start;
`main()` must consult it before building the server. One end-to-end test proves a real TLS handshake.
"""

import datetime
import ipaddress
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path

import grpc
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from gateway.clock import FixedClock
from gateway.merchant_api import grpc_server
from gateway.merchant_api.grpc_server import (
    GrpcDeps,
    GrpcTlsConfigError,
    build_grpc_server,
    resolve_server_credentials,
)
from gateway.merchant_api.pb import payments_pb2, payments_pb2_grpc
from gateway.payments.charge import ChargeDeps
from gateway.payments.providers.port import CardRef, ProviderResult
from pydantic import SecretStr
from sqlalchemy.orm import Session, sessionmaker

from conftest import TEST_API_KEY_PEPPER, TEST_CARD_FINGERPRINT_KEY

CERT_ENV = "GATEWAY_GRPC_TLS_CERT_PATH"
KEY_ENV = "GATEWAY_GRPC_TLS_KEY_PATH"
INSECURE_ENV = "GATEWAY_GRPC_INSECURE"
PORT_ENV = "GATEWAY_GRPC_PORT"
RPC_TIMEOUT_SECONDS = 10.0
MISSING_API_KEY = "missing api key"
KEY_MARKER = "KEY-MATERIAL-MARKER-d41d8cd98f00b204e980"
CERT_MARKER = "CERT-MATERIAL-MARKER-9e107d9d372bb6826bd8"


class NullProvider:
    """A provider that is never reached: the RPCs under test stop at authentication."""

    name = "null"

    def charge(self, provider_key: str, amount_minor: int, currency: str, card: CardRef) -> ProviderResult:
        raise AssertionError("the provider must not be called")

    def lookup(self, provider_key: str) -> ProviderResult | None:
        raise AssertionError("the provider must not be called")


def _deps(session_factory: sessionmaker[Session], clock: FixedClock) -> GrpcDeps:
    charge = ChargeDeps(
        session_factory=session_factory,
        redis=None,
        provider=NullProvider(),
        clock=clock,
        card_fingerprint_key=TEST_CARD_FINGERPRINT_KEY,
    )
    return GrpcDeps(charge=charge, api_key_pepper=TEST_API_KEY_PEPPER)


def _write_pem_files(
    directory: Path, key_text: str = KEY_MARKER, cert_text: str = CERT_MARKER
) -> Mapping[str, str]:
    cert_path = directory / "server.crt"
    key_path = directory / "server.key"
    cert_path.write_text(cert_text)
    key_path.write_text(key_text)
    return {CERT_ENV: str(cert_path), KEY_ENV: str(key_path)}


@dataclass(frozen=True)
class SelfSigned:
    cert_pem: bytes
    key_pem: bytes


def _self_signed_localhost() -> SelfSigned:
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    issued = datetime.datetime.now(datetime.UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(issued - datetime.timedelta(days=1))
        .not_valid_after(issued + datetime.timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName(
                [x509.DNSName("localhost"), x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]
            ),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    return SelfSigned(
        cert_pem=certificate.public_bytes(serialization.Encoding.PEM),
        key_pem=key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ),
    )


@pytest.fixture
def started_servers() -> Iterator[list[grpc.Server]]:
    servers: list[grpc.Server] = []
    try:
        yield servers
    finally:
        for server in servers:
            server.stop(None)


# --- resolve_server_credentials: TLS configured --------------------------------------------------------


def test_grpc_tls_config_error_is_a_runtime_error() -> None:
    # Arrange / Act
    is_runtime_error = issubclass(GrpcTlsConfigError, RuntimeError)

    # Assert
    assert is_runtime_error is True


def test_cert_and_key_paths_build_ssl_server_credentials_from_the_file_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Arrange
    environ = _write_pem_files(tmp_path)
    received: list[list[tuple[bytes, bytes]]] = []
    produced: list[grpc.ServerCredentials] = []
    original = grpc.ssl_server_credentials

    def recording(
        private_key_certificate_chain_pairs: list[tuple[bytes, bytes]],
        root_certificates: bytes | None = None,
        require_client_auth: bool = False,
    ) -> grpc.ServerCredentials:
        received.append(list(private_key_certificate_chain_pairs))
        credentials = original(private_key_certificate_chain_pairs, root_certificates, require_client_auth)
        produced.append(credentials)
        return credentials

    monkeypatch.setattr(grpc, "ssl_server_credentials", recording)

    # Act
    credentials = resolve_server_credentials(environ)

    # Assert
    assert received == [[(KEY_MARKER.encode(), CERT_MARKER.encode())]]
    assert credentials is produced[0]


def test_tls_wins_when_insecure_is_also_opted_in(tmp_path: Path) -> None:
    # Arrange
    environ = {**_write_pem_files(tmp_path), INSECURE_ENV: "true"}

    # Act
    credentials = resolve_server_credentials(environ)

    # Assert
    assert type(credentials) is grpc.ServerCredentials


# --- resolve_server_credentials: partial / missing / unreadable config ---------------------------------


@pytest.mark.parametrize("missing_env", [CERT_ENV, KEY_ENV])
def test_exactly_one_path_set_is_refused_without_leaking_file_contents(
    tmp_path: Path, missing_env: str
) -> None:
    # Arrange
    environ = {name: path for name, path in _write_pem_files(tmp_path).items() if name != missing_env}

    # Act
    with pytest.raises(GrpcTlsConfigError) as info:
        resolve_server_credentials(environ)

    # Assert
    assert type(info.value) is GrpcTlsConfigError
    assert KEY_MARKER not in str(info.value)
    assert CERT_MARKER not in str(info.value)


@pytest.mark.parametrize("empty_env", [CERT_ENV, KEY_ENV])
def test_an_empty_path_counts_as_unset_so_the_other_one_alone_is_refused(
    tmp_path: Path, empty_env: str
) -> None:
    # Arrange
    environ = {**_write_pem_files(tmp_path), empty_env: ""}

    # Act
    with pytest.raises(GrpcTlsConfigError) as info:
        resolve_server_credentials(environ)

    # Assert
    assert type(info.value) is GrpcTlsConfigError


@pytest.mark.parametrize("empty_env", [CERT_ENV, KEY_ENV])
def test_exactly_one_path_set_is_refused_even_when_insecure_is_opted_in(
    tmp_path: Path, empty_env: str
) -> None:
    # Arrange
    environ = {**_write_pem_files(tmp_path), empty_env: "", INSECURE_ENV: "true"}

    # Act
    with pytest.raises(GrpcTlsConfigError) as info:
        resolve_server_credentials(environ)

    # Assert
    assert type(info.value) is GrpcTlsConfigError


@pytest.mark.parametrize("missing_env", [CERT_ENV, KEY_ENV])
def test_a_configured_path_that_does_not_exist_is_refused_without_leaking_file_contents(
    tmp_path: Path, missing_env: str
) -> None:
    # Arrange
    environ = {**_write_pem_files(tmp_path), missing_env: str(tmp_path / "does-not-exist.pem")}

    # Act
    with pytest.raises(GrpcTlsConfigError) as info:
        resolve_server_credentials(environ)

    # Assert
    assert type(info.value) is GrpcTlsConfigError
    assert KEY_MARKER not in str(info.value)
    assert CERT_MARKER not in str(info.value)


# --- resolve_server_credentials: no TLS ----------------------------------------------------------------


def test_neither_path_set_and_insecure_true_returns_no_credentials() -> None:
    # Arrange
    environ = {INSECURE_ENV: "true"}

    # Act
    credentials = resolve_server_credentials(environ)

    # Assert
    assert credentials is None


def test_empty_paths_with_insecure_true_return_no_credentials() -> None:
    # Arrange
    environ = {CERT_ENV: "", KEY_ENV: "", INSECURE_ENV: "true"}

    # Act
    credentials = resolve_server_credentials(environ)

    # Assert
    assert credentials is None


def test_neither_path_set_and_insecure_absent_is_refused() -> None:
    # Arrange
    environ: dict[str, str] = {}

    # Act
    with pytest.raises(GrpcTlsConfigError) as info:
        resolve_server_credentials(environ)

    # Assert
    assert type(info.value) is GrpcTlsConfigError


@pytest.mark.parametrize("insecure_value", ["false", "1", "TRUE", "True", "yes", "", " true", "true "])
def test_neither_path_set_and_insecure_other_than_exactly_true_is_refused(insecure_value: str) -> None:
    # Arrange
    environ = {INSECURE_ENV: insecure_value}

    # Act
    with pytest.raises(GrpcTlsConfigError) as info:
        resolve_server_credentials(environ)

    # Assert
    assert type(info.value) is GrpcTlsConfigError


# --- build_grpc_server ---------------------------------------------------------------------------------


def test_build_grpc_server_without_credentials_binds_a_plaintext_port(
    session_factory: sessionmaker[Session],
    clock: FixedClock,
    started_servers: list[grpc.Server],
) -> None:
    # Arrange
    server, port = build_grpc_server(_deps(session_factory, clock), "127.0.0.1:0", server_credentials=None)
    server.start()
    started_servers.append(server)
    channel = grpc.insecure_channel(f"127.0.0.1:{port}")
    stub = payments_pb2_grpc.MerchantPaymentsStub(channel)

    # Act
    with pytest.raises(grpc.RpcError) as info:
        stub.GetPayment(payments_pb2.GetPaymentRequest(payment_id="x"), timeout=RPC_TIMEOUT_SECONDS)
    channel.close()

    # Assert
    error = info.value
    assert isinstance(error, grpc.Call)
    assert (error.code(), error.details()) == (grpc.StatusCode.UNAUTHENTICATED, MISSING_API_KEY)


# --- end to end: a real TLS handshake ------------------------------------------------------------------


@pytest.fixture
def tls_server(
    tmp_path: Path,
    session_factory: sessionmaker[Session],
    clock: FixedClock,
    started_servers: list[grpc.Server],
) -> tuple[int, bytes]:
    material = _self_signed_localhost()
    cert_path = tmp_path / "localhost.crt"
    key_path = tmp_path / "localhost.key"
    cert_path.write_bytes(material.cert_pem)
    key_path.write_bytes(material.key_pem)
    credentials = resolve_server_credentials({CERT_ENV: str(cert_path), KEY_ENV: str(key_path)})
    assert type(credentials) is grpc.ServerCredentials
    server, port = build_grpc_server(
        _deps(session_factory, clock), "localhost:0", server_credentials=credentials
    )
    server.start()
    started_servers.append(server)
    return port, material.cert_pem


def test_tls_client_trusting_the_certificate_reaches_the_servicer(tls_server: tuple[int, bytes]) -> None:
    # Arrange
    port, cert_pem = tls_server
    channel = grpc.secure_channel(
        f"localhost:{port}", grpc.ssl_channel_credentials(root_certificates=cert_pem)
    )
    stub = payments_pb2_grpc.MerchantPaymentsStub(channel)

    # Act
    with pytest.raises(grpc.RpcError) as info:
        stub.GetPayment(payments_pb2.GetPaymentRequest(payment_id="x"), timeout=RPC_TIMEOUT_SECONDS)
    channel.close()

    # Assert
    error = info.value
    assert isinstance(error, grpc.Call)
    assert (error.code(), error.details()) == (grpc.StatusCode.UNAUTHENTICATED, MISSING_API_KEY)


def test_plaintext_client_cannot_reach_a_tls_port(tls_server: tuple[int, bytes]) -> None:
    # Arrange
    port, _cert_pem = tls_server
    channel = grpc.insecure_channel(f"localhost:{port}")
    stub = payments_pb2_grpc.MerchantPaymentsStub(channel)

    # Act
    with pytest.raises(grpc.RpcError) as info:
        stub.GetPayment(payments_pb2.GetPaymentRequest(payment_id="x"), timeout=RPC_TIMEOUT_SECONDS)
    channel.close()

    # Assert
    error = info.value
    assert isinstance(error, grpc.Call)
    assert error.code() == grpc.StatusCode.UNAVAILABLE


# --- main() consults resolve_server_credentials --------------------------------------------------------


class _FakeServer:
    def __init__(self) -> None:
        self.started = False

    def start(self) -> None:
        self.started = True

    def wait_for_termination(self, timeout: float | None = None) -> bool:
        return True


@dataclass(frozen=True)
class _FakeSettings:
    api_key_pepper: SecretStr


def _patch_main_collaborators(
    monkeypatch: pytest.MonkeyPatch,
    charge: ChargeDeps,
    build: Callable[..., tuple[_FakeServer, int]],
) -> None:
    monkeypatch.setattr(
        grpc_server, "load_gateway_settings", lambda: _FakeSettings(SecretStr(TEST_API_KEY_PEPPER))
    )
    monkeypatch.setattr(grpc_server, "build_charge_deps", lambda settings: charge)
    monkeypatch.setattr(grpc_server, "build_grpc_server", build)


def test_main_passes_the_resolved_credentials_to_build_grpc_server(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    charge = _deps(session_factory, clock).charge
    material = _self_signed_localhost()
    sentinel = grpc.ssl_server_credentials([(material.key_pem, material.cert_pem)])
    calls: list[tuple[str, grpc.ServerCredentials | None]] = []

    def fake_build(
        deps: GrpcDeps,
        address: str = "[::]:50051",
        max_workers: int = 16,
        server_credentials: grpc.ServerCredentials | None = None,
    ) -> tuple[_FakeServer, int]:
        calls.append((address, server_credentials))
        return _FakeServer(), 50051

    seen_environ: list[Mapping[str, str]] = []

    def fake_resolve(environ: Mapping[str, str]) -> grpc.ServerCredentials | None:
        seen_environ.append(environ)
        return sentinel

    _patch_main_collaborators(monkeypatch, charge, fake_build)
    monkeypatch.setattr(grpc_server, "resolve_server_credentials", fake_resolve)
    monkeypatch.setenv(PORT_ENV, "50555")

    # Act
    grpc_server.main()

    # Assert
    assert len(seen_environ) == 1
    assert seen_environ[0]["GATEWAY_GRPC_PORT"] == "50555"
    assert calls == [("[::]:50555", sentinel)]


def test_main_refuses_to_start_without_credentials_or_insecure_opt_in(
    monkeypatch: pytest.MonkeyPatch, session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    charge = _deps(session_factory, clock).charge
    built: list[str] = []

    def fake_build(
        deps: GrpcDeps,
        address: str = "[::]:50051",
        max_workers: int = 16,
        server_credentials: grpc.ServerCredentials | None = None,
    ) -> tuple[_FakeServer, int]:
        built.append(address)
        return _FakeServer(), 50051

    _patch_main_collaborators(monkeypatch, charge, fake_build)
    for name in (CERT_ENV, KEY_ENV, INSECURE_ENV):
        monkeypatch.delenv(name, raising=False)

    # Act
    with pytest.raises(GrpcTlsConfigError) as info:
        grpc_server.main()

    # Assert
    assert type(info.value) is GrpcTlsConfigError
    assert built == []
