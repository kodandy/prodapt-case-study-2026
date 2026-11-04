"""`make web-merchant-key`: issue a fresh API key for the SQ1 demo merchant and print it once, for pasting
into the web client's merchant sign-in (docs/specs/web.md decision 5).

The key is written to stdout exactly once and nowhere else: not to a log, a file or the database in plain
text (the gateway stores only its peppered hash). Every run issues a new key; earlier keys keep working.
"""

import sys

from gateway.admin import bootstrap_merchant
from gateway.clock import system_clock
from gateway.config import load_gateway_settings
from gateway.db import make_engine, make_session_factory
from ops_ai.seed import merchant_uuid

WEB_DEMO_MERCHANT = "mch_0001"


def main() -> int:
    settings = load_gateway_settings()
    session_factory = make_session_factory(make_engine(settings.database_url))
    credentials = bootstrap_merchant(
        session_factory,
        system_clock,
        "demo-sq1",
        settings.api_key_pepper.get_secret_value(),
        merchant_id=merchant_uuid(WEB_DEMO_MERCHANT),
    )
    print(
        f"Demo merchant {WEB_DEMO_MERCHANT} ({credentials.merchant_id}). Local demo key, shown once:",
        file=sys.stderr,
    )
    sys.stdout.write(credentials.api_key + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
