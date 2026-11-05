"""RED tests for the Stripe adapter hardening: https-only base URL, provider_key allowlist, no proxy env."""

import httpx
import pytest
from gateway.payments.providers.port import ProviderResult
from gateway.payments.providers.stripe.provider import LiveKeyRefusedError, StripeProvider

SECRET_KEY = "sk_test_FAKEunitTESTkeyNOTreal0000"
PROVIDER_KEY = "gw-charge-7f3a9c1e"
MAX_PROVIDER_KEY_LENGTH = 255
EMPTY_SEARCH_BODY = '{"object": "search_result", "data": [], "has_more": false}'


def _recording_provider() -> tuple[StripeProvider, list[httpx.Request]]:
    recorded: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        recorded.append(request)
        return httpx.Response(200, content=EMPTY_SEARCH_BODY, headers={"content-type": "application/json"})

    provider = StripeProvider(SECRET_KEY, http=httpx.Client(transport=httpx.MockTransport(handler)))
    return provider, recorded


# --- base URL scheme -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "base_url",
    [
        "http://api.stripe.com",
        "HTTP://api.stripe.com",
        "ftp://api.stripe.com",
        "//api.stripe.com",
        "api.stripe.com",
    ],
)
def test_non_https_base_url_is_refused_at_construction(base_url: str) -> None:
    # Arrange
    http = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200)))

    # Act
    with pytest.raises(ValueError) as info:
        StripeProvider(SECRET_KEY, http=http, base_url=base_url)

    # Assert
    assert not isinstance(info.value, LiveKeyRefusedError)
    assert SECRET_KEY not in str(info.value)


def test_https_base_url_is_accepted() -> None:
    # Arrange
    http = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200)))

    # Act
    provider = StripeProvider(SECRET_KEY, http=http, base_url="https://stripe.test")

    # Assert
    assert repr(provider) == "StripeProvider(base_url='https://stripe.test')"


# --- provider_key allowlist on lookup ------------------------------------------------------------------


@pytest.mark.parametrize(
    "provider_key",
    [
        "x' OR status:'succeeded",
        "",
        "a" * (MAX_PROVIDER_KEY_LENGTH + 1),
        "key with space",
        "gw-charge-7f3a9c1e\n",
        "gw-charge'7f3a9c1e",
        "gw.charge.7f3a9c1e",
        "gw-charge-٣",
    ],
)
def test_lookup_rejects_a_provider_key_outside_the_allowlist_without_sending_a_request(
    provider_key: str,
) -> None:
    # Arrange
    provider, recorded = _recording_provider()

    # Act
    with pytest.raises(ValueError):
        provider.lookup(provider_key)

    # Assert
    assert recorded == []


def test_lookup_with_a_valid_provider_key_sends_exactly_one_request() -> None:
    # Arrange
    provider, recorded = _recording_provider()

    # Act
    result = provider.lookup(PROVIDER_KEY)

    # Assert
    assert result is None
    assert [request.url.path for request in recorded] == ["/v1/payment_intents/search"]
    assert recorded[0].url.params["query"] == f"metadata['provider_key']:'{PROVIDER_KEY}'"


@pytest.mark.parametrize("provider_key", ["a", "A_z-9", "a" * MAX_PROVIDER_KEY_LENGTH])
def test_lookup_accepts_allowlist_boundary_provider_keys(provider_key: str) -> None:
    # Arrange
    provider, recorded = _recording_provider()

    # Act
    result: ProviderResult | None = provider.lookup(provider_key)

    # Assert
    assert result is None
    assert len(recorded) == 1


# --- proxy environment ---------------------------------------------------------------------------------


def test_default_http_client_does_not_trust_proxy_environment() -> None:
    # Arrange
    provider = StripeProvider(SECRET_KEY)

    # Act
    trusts_env = provider._http.trust_env

    # Assert
    assert trusts_env is False
    provider._http.close()
