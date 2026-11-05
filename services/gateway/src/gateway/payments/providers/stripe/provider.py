"""Stripe test-mode acquirer behind ProviderPort (R2-9).

Only Stripe's `pm_card_*` test payment methods are sent, chosen from the BIN/last4 of our own test PANs: the
PAN and the vault token never leave the tokenisation boundary. Live keys are refused at construction.
"""

import json
import re

import httpx

from gateway.payments.providers.port import (
    CardRef,
    ProviderRequestError,
    ProviderResult,
    ProviderTimeoutError,
)

STRIPE_API_BASE = "https://api.stripe.com"
TEST_KEY_PREFIX = "sk_test_"
PROVIDER_KEY_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,255}", re.ASCII)

TEST_PAYMENT_METHODS: dict[tuple[str, str], str] = {
    ("424242", "4242"): "pm_card_visa",
    ("555555", "4444"): "pm_card_mastercard",
    ("400000", "9995"): "pm_card_visa_chargeDeclinedInsufficientFunds",
    ("400000", "0002"): "pm_card_chargeDeclined",
    ("400000", "0119"): "pm_card_chargeDeclinedProcessingError",
}

FALLBACK_DECLINE_CODE = "card_declined"
STRIPE_DECLINE_CODES: dict[str, str] = {
    "insufficient_funds": "insufficient_funds",
    "do_not_honor": "do_not_honor",
    "expired_card": "expired_card",
    "incorrect_cvc": "incorrect_cvc",
    "fraudulent": "fraud_flagged",
    "stolen_card": "fraud_flagged",
    "lost_card": "fraud_flagged",
    "pickup_card": "fraud_flagged",
    "merchant_blacklist": "fraud_flagged",
    "processing_error": "issuer_unavailable",
    "issuer_not_available": "issuer_unavailable",
    "try_again_later": "issuer_unavailable",
    "reenter_transaction": "issuer_unavailable",
    "generic_decline": "card_declined",
    "card_declined": "card_declined",
}


class LiveKeyRefusedError(ValueError):
    """The configured key is not a Stripe test-mode secret key. Never carries the key."""

    def __init__(self) -> None:
        super().__init__("STRIPE_SECRET_KEY must be a test-mode secret key (sk_test_...)")


class StripeRequestError(ProviderRequestError):
    """Stripe rejected the request itself (bad request, auth): a bug or misconfiguration, not a decline."""


def _object(value: object) -> dict[str, object]:
    return {str(k): v for k, v in value.items()} if isinstance(value, dict) else {}


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _decline_code(error: dict[str, object]) -> str:
    stripe_code = _text(error.get("decline_code")) or _text(error.get("code"))
    if stripe_code is None:
        return FALLBACK_DECLINE_CODE
    return STRIPE_DECLINE_CODES.get(stripe_code, FALLBACK_DECLINE_CODE)


def _json(response: httpx.Response) -> dict[str, object]:
    try:
        parsed: object = json.loads(response.content)
    except ValueError:
        return {}
    return _object(parsed)


class StripeProvider:
    name = "stripe"

    def __init__(
        self,
        secret_key: str,
        http: httpx.Client | None = None,
        *,
        base_url: str = STRIPE_API_BASE,
        timeout_seconds: float = 10.0,
    ) -> None:
        if not secret_key.startswith(TEST_KEY_PREFIX):
            raise LiveKeyRefusedError
        if not base_url.startswith("https://"):
            raise ValueError("the Stripe base URL must use https")
        self._secret_key = secret_key
        self._http = http or httpx.Client(timeout=timeout_seconds, trust_env=False)
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout_seconds

    def __repr__(self) -> str:
        return f"StripeProvider(base_url={self._base_url!r})"

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._secret_key}"}

    def charge(self, provider_key: str, amount_minor: int, currency: str, card: CardRef) -> ProviderResult:
        payment_method = TEST_PAYMENT_METHODS.get((card.bin, card.last4))
        if payment_method is None:
            raise ValueError("card has no Stripe test payment method")
        form = {
            "amount": str(amount_minor),
            "currency": currency.lower(),
            "payment_method": payment_method,
            "confirm": "true",
            "payment_method_types[]": "card",
            "metadata[provider_key]": provider_key,
        }
        try:
            response = self._http.post(
                f"{self._base_url}/v1/payment_intents",
                data=form,
                headers={**self._headers(), "Idempotency-Key": provider_key},
                timeout=self._timeout,
            )
        except httpx.RequestError:
            raise ProviderTimeoutError from None
        return self._charge_result(response)

    def _charge_result(self, response: httpx.Response) -> ProviderResult:
        status = response.status_code
        if status >= 500 or status == 429:
            raise ProviderTimeoutError
        body = _json(response)
        if status == 200:
            intent_id = _text(body.get("id"))
            if body.get("status") == "succeeded" and intent_id is not None:
                return ProviderResult("succeeded", intent_id, None)
            raise ProviderTimeoutError
        error = _object(body.get("error"))
        if status == 402 and error.get("type") == "card_error":
            intent_id = _text(_object(error.get("payment_intent")).get("id")) or ""
            return ProviderResult("declined", intent_id, _decline_code(error))
        raise StripeRequestError(f"stripe rejected the request with HTTP {status}")

    def lookup(self, provider_key: str) -> ProviderResult | None:
        """Raises ValueError for a key outside PROVIDER_KEY_PATTERN: it is quoted into Stripe search."""
        if PROVIDER_KEY_PATTERN.fullmatch(provider_key) is None:
            raise ValueError("provider_key has unsupported characters")
        try:
            response = self._http.get(
                f"{self._base_url}/v1/payment_intents/search",
                params={"query": f"metadata['provider_key']:'{provider_key}'"},
                headers=self._headers(),
                timeout=self._timeout,
            )
        except httpx.RequestError:
            raise ProviderTimeoutError from None
        if response.status_code != 200:
            raise ProviderTimeoutError
        data = _json(response).get("data")
        if not isinstance(data, list) or not data:
            return None
        intent = _object(data[0])
        intent_id = _text(intent.get("id")) or ""
        if intent.get("status") == "succeeded":
            return ProviderResult("succeeded", intent_id, None)
        last_error = _object(intent.get("last_payment_error"))
        if intent.get("status") == "requires_payment_method" and last_error:
            return ProviderResult("declined", intent_id, _decline_code(last_error))
        return None
