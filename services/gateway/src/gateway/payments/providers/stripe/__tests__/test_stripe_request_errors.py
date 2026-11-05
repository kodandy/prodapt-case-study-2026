"""RED tests for provider error classification: a refused request is a ProviderRequestError, and any httpx
failure that is not a transport error (bad encoding, redirect loop) is an unknown outcome, not a crash."""

import httpx
import pytest
from gateway.payments.providers.port import CardRef, ProviderRequestError, ProviderTimeoutError
from gateway.payments.providers.stripe.provider import StripeProvider, StripeRequestError

SECRET_KEY = "sk_test_FAKEunitTESTkeyNOTreal0000"
PROVIDER_KEY = "gw-charge-7f3a9c1e"
VISA = CardRef(token="tok_gateway_vault_9f8e7d6c", bin="424242", last4="4242")
BAD_REQUEST_BODY = '{"error": {"type": "invalid_request_error", "message": "bad"}}'


def _raising_provider(error: Exception) -> tuple[StripeProvider, list[httpx.Request]]:
    recorded: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        recorded.append(request)
        raise error

    provider = StripeProvider(SECRET_KEY, http=httpx.Client(transport=httpx.MockTransport(handler)))
    return provider, recorded


NON_TRANSPORT_ERRORS = [
    pytest.param(httpx.DecodingError("bad content-encoding"), id="decoding_error"),
    pytest.param(httpx.TooManyRedirects("redirect loop"), id="too_many_redirects"),
]


# --- StripeRequestError sits under ProviderRequestError -------------------------------------------------


def test_stripe_request_error_is_a_provider_request_error() -> None:
    # Arrange / Act
    is_subclass = issubclass(StripeRequestError, ProviderRequestError)

    # Assert
    assert is_subclass is True


def test_provider_request_error_is_not_a_provider_timeout_error() -> None:
    # Arrange / Act
    is_subclass = issubclass(ProviderRequestError, ProviderTimeoutError)

    # Assert
    assert is_subclass is False


def test_a_stripe_400_raises_stripe_request_error_catchable_as_provider_request_error() -> None:
    # Arrange
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            400, content=BAD_REQUEST_BODY, headers={"content-type": "application/json"}
        )
    )
    provider = StripeProvider(SECRET_KEY, http=httpx.Client(transport=transport))

    # Act
    with pytest.raises(ProviderRequestError) as info:
        provider.charge(PROVIDER_KEY, 1999, "EUR", VISA)

    # Assert
    assert type(info.value) is StripeRequestError
    assert str(info.value) == "stripe rejected the request with HTTP 400"


# --- non-transport httpx errors are an unknown outcome ------------------------------------------------


@pytest.mark.parametrize("error", NON_TRANSPORT_ERRORS)
def test_charge_converts_a_non_transport_httpx_error_to_provider_timeout_error(
    error: httpx.RequestError,
) -> None:
    # Arrange
    provider, recorded = _raising_provider(error)

    # Act
    with pytest.raises(ProviderTimeoutError) as info:
        provider.charge(PROVIDER_KEY, 1999, "EUR", VISA)

    # Assert
    assert type(info.value) is ProviderTimeoutError
    assert [request.url.path for request in recorded] == ["/v1/payment_intents"]


@pytest.mark.parametrize("error", NON_TRANSPORT_ERRORS)
def test_lookup_converts_a_non_transport_httpx_error_to_provider_timeout_error(
    error: httpx.RequestError,
) -> None:
    # Arrange
    provider, recorded = _raising_provider(error)

    # Act
    with pytest.raises(ProviderTimeoutError) as info:
        provider.lookup(PROVIDER_KEY)

    # Assert
    assert type(info.value) is ProviderTimeoutError
    assert [request.url.path for request in recorded] == ["/v1/payment_intents/search"]


@pytest.mark.parametrize(
    "error",
    [httpx.ConnectError("refused"), httpx.ReadTimeout("slow")],
    ids=["connect_error", "read_timeout"],
)
def test_transport_errors_are_still_provider_timeout_errors(error: httpx.RequestError) -> None:
    # Arrange
    provider, _ = _raising_provider(error)

    # Act
    with pytest.raises(ProviderTimeoutError) as info:
        provider.charge(PROVIDER_KEY, 1999, "EUR", VISA)

    # Assert
    assert type(info.value) is ProviderTimeoutError
