"""Contract tests: StripeProvider against recorded Stripe-shaped JSON served through httpx.MockTransport."""

from collections.abc import Callable
from pathlib import Path
from urllib.parse import parse_qsl

import httpx
import pytest
from gateway.payments.providers.port import CardRef, ProviderResult, ProviderTimeoutError
from gateway.payments.providers.stripe.provider import (
    STRIPE_API_BASE,
    STRIPE_DECLINE_CODES,
    TEST_PAYMENT_METHODS,
    LiveKeyRefusedError,
    StripeProvider,
    StripeRequestError,
)

FIXTURES = Path(__file__).parent / "fixtures"
SECRET_KEY = "sk_test_FAKEunitTESTkeyNOTreal0000"
PROVIDER_KEY = "gw-charge-7f3a9c1e"
CARD_TOKEN = "tok_gateway_vault_9f8e7d6c"
PAN = "4242424242424242"
VISA = CardRef(token=CARD_TOKEN, bin="424242", last4="4242")
MASTERCARD = CardRef(token=CARD_TOKEN, bin="555555", last4="4444")
INSUFFICIENT = CardRef(token=CARD_TOKEN, bin="400000", last4="9995")
DECLINED = CardRef(token=CARD_TOKEN, bin="400000", last4="0002")
PROCESSING_ERROR = CardRef(token=CARD_TOKEN, bin="400000", last4="0119")

SUCCEEDED_REF = "pi_3PqRxX2eZvKYlo2C1aBcDeFg"
INSUFFICIENT_FUNDS_REF = "pi_3PqRxX2eZvKYlo2C4VwXyZaB"
GENERIC_DECLINE_REF = "pi_3PqRxX2eZvKYlo2C5CdEfGhI"
PROCESSING_ERROR_REF = "pi_3PqRxX2eZvKYlo2C6JkLmNoP"
FRAUDULENT_REF = "pi_3PqRxX2eZvKYlo2C7QrStUvW"

Handler = Callable[[httpx.Request], httpx.Response]


def _fixture_text(name: str) -> str:
    return (FIXTURES / f"{name}.json").read_text()


def _json_response(status: int, body: str) -> httpx.Response:
    return httpx.Response(status, content=body, headers={"content-type": "application/json"})


def _provider(handler: Handler) -> StripeProvider:
    return StripeProvider(SECRET_KEY, http=httpx.Client(transport=httpx.MockTransport(handler)))


def _serving(status: int, body: str) -> tuple[StripeProvider, list[httpx.Request]]:
    recorded: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        recorded.append(request)
        return _json_response(status, body)

    return _provider(handler), recorded


def _serving_fixture(status: int, name: str) -> tuple[StripeProvider, list[httpx.Request]]:
    return _serving(status, _fixture_text(name))


def _raising(error: Exception) -> tuple[StripeProvider, list[httpx.Request]]:
    recorded: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        recorded.append(request)
        raise error

    return _provider(handler), recorded


def _form_pairs(request: httpx.Request) -> list[tuple[str, str]]:
    return parse_qsl(request.content.decode(), keep_blank_values=True)


def _wire_text(request: httpx.Request) -> str:
    headers = "\n".join(f"{name}: {value}" for name, value in request.headers.items())
    return f"{request.method} {request.url}\n{headers}\n{request.content.decode()}"


class TestChargeRequest:
    def test_posts_a_form_encoded_payment_intent_to_the_stripe_api(self) -> None:
        # Arrange
        provider, recorded = _serving_fixture(200, "payment_intent_succeeded")

        # Act
        provider.charge(PROVIDER_KEY, 1999, "EUR", VISA)

        # Assert
        assert len(recorded) == 1
        request = recorded[0]
        assert request.method == "POST"
        assert str(request.url) == f"{STRIPE_API_BASE}/v1/payment_intents"
        assert request.headers["content-type"] == "application/x-www-form-urlencoded"

    def test_sends_exactly_the_documented_form_fields(self) -> None:
        # Arrange
        provider, recorded = _serving_fixture(200, "payment_intent_succeeded")

        # Act
        provider.charge(PROVIDER_KEY, 1999, "EUR", VISA)

        # Assert
        assert sorted(_form_pairs(recorded[0])) == sorted(
            [
                ("amount", "1999"),
                ("currency", "eur"),
                ("payment_method", "pm_card_visa"),
                ("confirm", "true"),
                ("payment_method_types[]", "card"),
                ("metadata[provider_key]", PROVIDER_KEY),
            ]
        )

    def test_authenticates_with_the_bearer_key_and_the_provider_key_as_idempotency_key(self) -> None:
        # Arrange
        provider, recorded = _serving_fixture(200, "payment_intent_succeeded")

        # Act
        provider.charge(PROVIDER_KEY, 1999, "eur", VISA)

        # Assert
        assert recorded[0].headers["authorization"] == f"Bearer {SECRET_KEY}"
        assert recorded[0].headers["idempotency-key"] == PROVIDER_KEY

    def test_never_puts_the_pan_or_the_vault_token_on_the_wire(self) -> None:
        # Arrange
        provider, recorded = _serving_fixture(200, "payment_intent_succeeded")

        # Act
        provider.charge(PROVIDER_KEY, 1999, "EUR", VISA)

        # Assert
        wire = _wire_text(recorded[0])
        assert PAN not in wire
        assert CARD_TOKEN not in wire
        assert "424242424242" not in wire

    def test_lowercases_the_currency(self) -> None:
        # Arrange
        provider, recorded = _serving_fixture(200, "payment_intent_succeeded")

        # Act
        provider.charge(PROVIDER_KEY, 250, "USD", VISA)

        # Assert
        assert ("currency", "usd") in _form_pairs(recorded[0])
        assert ("amount", "250") in _form_pairs(recorded[0])

    @pytest.mark.parametrize(
        ("card", "payment_method"),
        [
            (VISA, "pm_card_visa"),
            (MASTERCARD, "pm_card_mastercard"),
            (INSUFFICIENT, "pm_card_visa_chargeDeclinedInsufficientFunds"),
            (DECLINED, "pm_card_chargeDeclined"),
            (PROCESSING_ERROR, "pm_card_chargeDeclinedProcessingError"),
        ],
    )
    def test_maps_each_test_card_to_its_stripe_test_payment_method(
        self, card: CardRef, payment_method: str
    ) -> None:
        # Arrange
        provider, recorded = _serving_fixture(200, "payment_intent_succeeded")

        # Act
        provider.charge(PROVIDER_KEY, 1999, "eur", card)

        # Assert
        assert ("payment_method", payment_method) in _form_pairs(recorded[0])

    def test_test_payment_method_table_is_exactly_the_five_test_cards(self) -> None:
        # Arrange
        expected = {
            ("424242", "4242"): "pm_card_visa",
            ("555555", "4444"): "pm_card_mastercard",
            ("400000", "9995"): "pm_card_visa_chargeDeclinedInsufficientFunds",
            ("400000", "0002"): "pm_card_chargeDeclined",
            ("400000", "0119"): "pm_card_chargeDeclinedProcessingError",
        }

        # Act
        actual = dict(TEST_PAYMENT_METHODS)

        # Assert
        assert actual == expected

    def test_uses_the_configured_base_url(self) -> None:
        # Arrange
        recorded: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            recorded.append(request)
            return _json_response(200, _fixture_text("payment_intent_succeeded"))

        provider = StripeProvider(
            SECRET_KEY,
            http=httpx.Client(transport=httpx.MockTransport(handler)),
            base_url="https://stripe.example.test",
        )

        # Act
        provider.charge(PROVIDER_KEY, 1999, "eur", VISA)

        # Assert
        assert str(recorded[0].url) == "https://stripe.example.test/v1/payment_intents"


class TestChargeSuccess:
    def test_a_succeeded_payment_intent_yields_a_succeeded_result_with_its_id(self) -> None:
        # Arrange
        provider, _ = _serving_fixture(200, "payment_intent_succeeded")

        # Act
        result = provider.charge(PROVIDER_KEY, 1999, "eur", VISA)

        # Assert
        assert result == ProviderResult(status="succeeded", provider_ref=SUCCEEDED_REF, decline_code=None)

    def test_the_provider_is_named_stripe(self) -> None:
        # Arrange
        provider, _ = _serving_fixture(200, "payment_intent_succeeded")

        # Act
        name = provider.name

        # Assert
        assert name == "stripe"


class TestChargeDeclines:
    @pytest.mark.parametrize(
        ("fixture", "payment_intent_id", "decline_code"),
        [
            ("error_card_declined_insufficient_funds", INSUFFICIENT_FUNDS_REF, "insufficient_funds"),
            ("error_card_declined_generic", GENERIC_DECLINE_REF, "card_declined"),
            ("error_card_declined_processing_error", PROCESSING_ERROR_REF, "issuer_unavailable"),
            ("error_card_declined_fraudulent", FRAUDULENT_REF, "fraud_flagged"),
        ],
    )
    def test_a_402_card_error_yields_a_declined_result_with_the_mapped_code(
        self, fixture: str, payment_intent_id: str, decline_code: str
    ) -> None:
        # Arrange
        provider, _ = _serving_fixture(402, fixture)

        # Act
        result = provider.charge(PROVIDER_KEY, 1999, "eur", DECLINED)

        # Assert
        assert result == ProviderResult(
            status="declined", provider_ref=payment_intent_id, decline_code=decline_code
        )

    def test_the_decline_code_wins_over_the_error_code(self) -> None:
        # Arrange: error.code is "card_declined" but decline_code says insufficient_funds
        provider, _ = _serving_fixture(402, "error_card_declined_insufficient_funds")

        # Act
        result = provider.charge(PROVIDER_KEY, 1999, "eur", INSUFFICIENT)

        # Assert
        assert result.decline_code == "insufficient_funds"

    def test_the_error_code_is_used_when_there_is_no_decline_code(self) -> None:
        # Arrange: the processing_error fixture carries error.code only
        body = _fixture_text("error_card_declined_processing_error")
        assert '"decline_code"' not in body
        provider, _ = _serving(402, body)

        # Act
        result = provider.charge(PROVIDER_KEY, 1999, "eur", PROCESSING_ERROR)

        # Assert
        assert result.decline_code == "issuer_unavailable"

    def test_an_unknown_decline_code_falls_back_to_card_declined(self) -> None:
        # Arrange
        body = _fixture_text("error_card_declined_generic")
        replaced = body.replace('"decline_code": "generic_decline"', '"decline_code": "brand_new_code"')
        assert replaced != body
        provider, _ = _serving(402, replaced)

        # Act
        result = provider.charge(PROVIDER_KEY, 1999, "eur", DECLINED)

        # Assert
        assert result == ProviderResult(
            status="declined", provider_ref=GENERIC_DECLINE_REF, decline_code="card_declined"
        )

    def test_a_decline_without_any_code_falls_back_to_card_declined(self) -> None:
        # Arrange
        body = _fixture_text("error_card_declined_generic")
        stripped = body.replace('"decline_code": "generic_decline"', '"decline_code": null').replace(
            '"code": "card_declined"', '"code": null'
        )
        assert stripped != body
        provider, _ = _serving(402, stripped)

        # Act
        result = provider.charge(PROVIDER_KEY, 1999, "eur", DECLINED)

        # Assert
        assert result.status == "declined"
        assert result.decline_code == "card_declined"

    @pytest.mark.parametrize(
        ("stripe_code", "our_code"),
        [
            ("insufficient_funds", "insufficient_funds"),
            ("do_not_honor", "do_not_honor"),
            ("expired_card", "expired_card"),
            ("incorrect_cvc", "incorrect_cvc"),
            ("fraudulent", "fraud_flagged"),
            ("stolen_card", "fraud_flagged"),
            ("lost_card", "fraud_flagged"),
            ("pickup_card", "fraud_flagged"),
            ("merchant_blacklist", "fraud_flagged"),
            ("processing_error", "issuer_unavailable"),
            ("issuer_not_available", "issuer_unavailable"),
            ("try_again_later", "issuer_unavailable"),
            ("reenter_transaction", "issuer_unavailable"),
            ("generic_decline", "card_declined"),
            ("card_declined", "card_declined"),
        ],
    )
    def test_decline_code_table_maps_stripe_codes_to_the_gateway_taxonomy(
        self, stripe_code: str, our_code: str
    ) -> None:
        # Arrange
        table = dict(STRIPE_DECLINE_CODES)

        # Act
        mapped = table[stripe_code]

        # Assert
        assert mapped == our_code

    def test_decline_code_table_has_no_other_entries(self) -> None:
        # Arrange
        expected_keys = {
            "insufficient_funds",
            "do_not_honor",
            "expired_card",
            "incorrect_cvc",
            "fraudulent",
            "stolen_card",
            "lost_card",
            "pickup_card",
            "merchant_blacklist",
            "processing_error",
            "issuer_not_available",
            "try_again_later",
            "reenter_transaction",
            "generic_decline",
            "card_declined",
        }

        # Act
        keys = set(STRIPE_DECLINE_CODES)

        # Assert
        assert keys == expected_keys


class TestChargeOutcomeUnknown:
    @pytest.mark.parametrize(
        "error",
        [
            httpx.ReadTimeout("read timed out"),
            httpx.ConnectTimeout("connect timed out"),
            httpx.WriteTimeout("write timed out"),
            httpx.PoolTimeout("pool exhausted"),
            httpx.ConnectError("connection refused"),
            httpx.ReadError("connection reset"),
            httpx.RemoteProtocolError("server disconnected"),
        ],
        ids=lambda error: type(error).__name__,
    )
    def test_timeouts_and_transport_errors_raise_provider_timeout(self, error: httpx.TransportError) -> None:
        # Arrange
        provider, recorded = _raising(error)

        # Act
        with pytest.raises(ProviderTimeoutError):
            provider.charge(PROVIDER_KEY, 1999, "eur", VISA)

        # Assert
        assert len(recorded) == 1

    @pytest.mark.parametrize("status", [500, 502, 503, 504])
    def test_5xx_raises_provider_timeout(self, status: int) -> None:
        # Arrange
        provider, _ = _serving_fixture(status, "error_server")

        # Act / Assert
        with pytest.raises(ProviderTimeoutError):
            provider.charge(PROVIDER_KEY, 1999, "eur", VISA)

    def test_429_raises_provider_timeout(self) -> None:
        # Arrange
        provider, _ = _serving_fixture(429, "error_rate_limit")

        # Act / Assert
        with pytest.raises(ProviderTimeoutError):
            provider.charge(PROVIDER_KEY, 1999, "eur", VISA)

    @pytest.mark.parametrize("fixture", ["payment_intent_processing", "payment_intent_requires_action"])
    def test_a_200_that_is_not_succeeded_raises_provider_timeout(self, fixture: str) -> None:
        # Arrange
        provider, _ = _serving_fixture(200, fixture)

        # Act / Assert
        with pytest.raises(ProviderTimeoutError):
            provider.charge(PROVIDER_KEY, 1999, "eur", VISA)


class TestChargeRequestRejected:
    @pytest.mark.parametrize(
        ("status", "fixture"),
        [
            (400, "error_invalid_request"),
            (401, "error_unauthorized"),
            (403, "error_unauthorized"),
            (404, "error_invalid_request"),
        ],
    )
    def test_other_4xx_raise_stripe_request_error(self, status: int, fixture: str) -> None:
        # Arrange
        provider, _ = _serving_fixture(status, fixture)

        # Act / Assert
        with pytest.raises(StripeRequestError):
            provider.charge(PROVIDER_KEY, 1999, "eur", VISA)

    def test_a_402_that_is_not_a_card_error_raises_stripe_request_error(self) -> None:
        # Arrange
        provider, _ = _serving_fixture(402, "error_invalid_request")

        # Act / Assert
        with pytest.raises(StripeRequestError):
            provider.charge(PROVIDER_KEY, 1999, "eur", VISA)

    def test_a_request_error_is_not_confused_with_an_unknown_outcome(self) -> None:
        # Arrange
        provider, _ = _serving_fixture(401, "error_unauthorized")

        # Act
        with pytest.raises(StripeRequestError) as raised:
            provider.charge(PROVIDER_KEY, 1999, "eur", VISA)

        # Assert
        assert not isinstance(raised.value, ProviderTimeoutError)
        assert SECRET_KEY not in str(raised.value)
        assert SECRET_KEY not in repr(raised.value)

    @pytest.mark.parametrize(
        "card",
        [
            CardRef(token=CARD_TOKEN, bin="411111", last4="1111"),
            CardRef(token=CARD_TOKEN, bin="424242", last4="4243"),
            CardRef(token=CARD_TOKEN, bin="", last4=""),
        ],
    )
    def test_a_card_without_a_stripe_test_payment_method_is_rejected_before_any_request(
        self, card: CardRef
    ) -> None:
        # Arrange
        provider, recorded = _serving_fixture(200, "payment_intent_succeeded")

        # Act
        with pytest.raises(ValueError):
            provider.charge(PROVIDER_KEY, 1999, "eur", card)

        # Assert
        assert recorded == []


class TestLiveKeyRefusal:
    @pytest.mark.parametrize(
        "key",
        [
            "sk_live_51HqLyjWDarjtT1zdp7dcSECRETSECRET",
            "rk_live_51HqLyjWDarjtT1zdp7dcSECRETSECRET",
            "pk_test_51HqLyjWDarjtT1zdp7dcSECRETSECRET",
            " sk_test_51HqLyjWDarjtT1zdp7dc",
            "",
        ],
    )
    def test_only_sk_test_keys_are_accepted(self, key: str) -> None:
        # Arrange
        recorded: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            recorded.append(request)
            return _json_response(200, _fixture_text("payment_intent_succeeded"))

        http = httpx.Client(transport=httpx.MockTransport(handler))

        # Act
        with pytest.raises(LiveKeyRefusedError):
            StripeProvider(key, http=http)

        # Assert
        assert recorded == []

    @pytest.mark.parametrize(
        "key",
        [
            "sk_live_51HqLyjWDarjtT1zdp7dcSECRETSECRET",
            "rk_live_51HqLyjWDarjtT1zdp7dcSECRETSECRET",
            "pk_test_51HqLyjWDarjtT1zdp7dcSECRETSECRET",
        ],
    )
    def test_the_refusal_never_contains_the_key(self, key: str) -> None:
        # Arrange / Act
        with pytest.raises(LiveKeyRefusedError) as raised:
            StripeProvider(key)

        # Assert
        assert key not in str(raised.value)
        assert key not in repr(raised.value)
        assert all(key not in str(arg) for arg in raised.value.args)
        assert key[8:] not in str(raised.value)

    def test_the_refusal_is_a_value_error(self) -> None:
        # Arrange / Act
        with pytest.raises(ValueError) as raised:
            StripeProvider("sk_live_51HqLyjWDarjtT1zdp7dcSECRETSECRET")

        # Assert
        assert isinstance(raised.value, LiveKeyRefusedError)

    def test_a_test_key_is_accepted_and_sends_nothing_at_construction(self) -> None:
        # Arrange
        recorded: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            recorded.append(request)
            return _json_response(200, _fixture_text("payment_intent_succeeded"))

        # Act
        provider = StripeProvider(SECRET_KEY, http=httpx.Client(transport=httpx.MockTransport(handler)))

        # Assert
        assert provider.name == "stripe"
        assert recorded == []


class TestLookup:
    def test_searches_payment_intents_by_the_provider_key_metadata(self) -> None:
        # Arrange
        provider, recorded = _serving_fixture(200, "search_empty")

        # Act
        provider.lookup(PROVIDER_KEY)

        # Assert
        assert len(recorded) == 1
        request = recorded[0]
        assert request.method == "GET"
        assert request.url.path == "/v1/payment_intents/search"
        assert request.url.host == "api.stripe.com"
        assert request.url.params["query"] == f"metadata['provider_key']:'{PROVIDER_KEY}'"
        assert request.headers["authorization"] == f"Bearer {SECRET_KEY}"

    def test_returns_none_when_no_payment_intent_matches(self) -> None:
        # Arrange
        provider, _ = _serving_fixture(200, "search_empty")

        # Act
        result = provider.lookup(PROVIDER_KEY)

        # Assert
        assert result is None

    def test_returns_a_succeeded_result_for_a_succeeded_payment_intent(self) -> None:
        # Arrange
        provider, _ = _serving_fixture(200, "search_succeeded")

        # Act
        result = provider.lookup(PROVIDER_KEY)

        # Assert
        assert result == ProviderResult(status="succeeded", provider_ref=SUCCEEDED_REF, decline_code=None)

    def test_returns_a_declined_result_with_the_mapped_code_for_a_failed_attempt(self) -> None:
        # Arrange
        provider, _ = _serving_fixture(200, "search_declined")

        # Act
        result = provider.lookup(PROVIDER_KEY)

        # Assert
        assert result == ProviderResult(
            status="declined", provider_ref=INSUFFICIENT_FUNDS_REF, decline_code="insufficient_funds"
        )

    @pytest.mark.parametrize("fixture", ["search_processing", "search_requires_payment_method"])
    def test_returns_none_for_any_other_status(self, fixture: str) -> None:
        # Arrange
        provider, _ = _serving_fixture(200, fixture)

        # Act
        result = provider.lookup(PROVIDER_KEY)

        # Assert
        assert result is None
