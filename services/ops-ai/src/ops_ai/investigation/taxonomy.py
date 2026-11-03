FAILURE_RULES: tuple[tuple[tuple[str, ...], str], ...] = (
    (("issuer unavailable", "timeout"), "issuer_unavailable"),
    (("do_not_honor",), "do_not_honor"),
    (("insufficient funds",), "insufficient_funds"),
    (("expired",), "expired_card"),
    (("cvc",), "incorrect_cvc"),
    (("fraud",), "fraud_flagged"),
)
OTHER_FAILURE_CODE = "other"


def failure_code(failure_reason: str) -> str:
    """Maps a free-text failure reason to a decline code: case-insensitive, first matching rule wins."""
    reason = failure_reason.lower()
    for needles, code in FAILURE_RULES:
        if any(needle in reason for needle in needles):
            return code
    return OTHER_FAILURE_CODE
