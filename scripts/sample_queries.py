"""Prints the raw JSON answers to the four sample queries (SQ1-SQ4) for the README, one object per line.

Run after `make demo` (it seeds the data and runs the live SQ3 burst):
    docker compose --profile demo run --rm --no-deps -T demo python scripts/sample_queries.py

Makes the same requests as scripts/demo.py with the same principals, plus SQ1 on the failure assistant route.
Each merchant gets one more API key and the SQ2 sender a fresh random password. Never prints an API key, a
password or a token.
"""

import json
import os
import secrets

import httpx
from gateway.admin import bootstrap_merchant, bootstrap_user
from gateway.clock import system_clock
from gateway.config import load_gateway_settings
from gateway.db import make_engine, make_session_factory
from ops_ai.seed import merchant_uuid
from sqlalchemy.orm import Session, sessionmaker

from scripts.demo import (
    SQ1_MERCHANT,
    SQ1_QUESTION,
    SQ2_QUESTION,
    SQ2_SENDER_EMAIL,
    SQ3_MERCHANT,
    SQ3_QUESTION,
    SQ4_MERCHANT,
    SQ4_QUESTION,
    _sq2_sender_id,
)


def _merchant_token(
    gateway: httpx.Client, session_factory: sessionmaker[Session], pepper: str, dataset_id: str
) -> str:
    credentials = bootstrap_merchant(
        session_factory, system_clock, f"sample-{dataset_id}", pepper, merchant_id=merchant_uuid(dataset_id)
    )
    issued = gateway.post("/v1/merchant-tokens", headers={"Authorization": f"Bearer {credentials.api_key}"})
    issued.raise_for_status()
    return str(issued.json()["access_token"])


def _sq2_user_token(gateway: httpx.Client, session_factory: sessionmaker[Session]) -> str:
    password = secrets.token_urlsafe(18)
    bootstrap_user(session_factory, system_clock, SQ2_SENDER_EMAIL, password, _sq2_sender_id())
    login = gateway.post("/v1/user-tokens", json={"email": SQ2_SENDER_EMAIL, "password": password})
    login.raise_for_status()
    return str(login.json()["access_token"])


def _ask(ops_ai: httpx.Client, query_id: str, route: str, token: str, question: str) -> None:
    response = ops_ai.post(route, headers={"Authorization": f"Bearer {token}"}, json={"question": question})
    record = {
        "query": query_id,
        "route": route,
        "question": question,
        "status": response.status_code,
        "body": response.json(),
    }
    print(json.dumps(record), flush=True)


def run() -> None:
    settings = load_gateway_settings()
    session_factory = make_session_factory(make_engine(settings.database_url))
    pepper = settings.api_key_pepper.get_secret_value()
    gateway = httpx.Client(base_url=os.environ["GATEWAY_URL"], timeout=30.0)
    ops_ai = httpx.Client(base_url=os.environ["OPS_AI_URL"], timeout=30.0)

    sq1_token = _merchant_token(gateway, session_factory, pepper, SQ1_MERCHANT)
    _ask(ops_ai, "SQ1", "/v1/assistant/investigate", sq1_token, SQ1_QUESTION)
    _ask(ops_ai, "SQ1", "/v1/assistant/failures", sq1_token, SQ1_QUESTION)
    _ask(
        ops_ai,
        "SQ2",
        "/v1/assistant/investigate-transfer",
        _sq2_user_token(gateway, session_factory),
        SQ2_QUESTION,
    )
    sq3_token = _merchant_token(gateway, session_factory, pepper, SQ3_MERCHANT)
    _ask(ops_ai, "SQ3", "/v1/assistant/investigate-fraud", sq3_token, SQ3_QUESTION)
    sq4_token = _merchant_token(gateway, session_factory, pepper, SQ4_MERCHANT)
    _ask(ops_ai, "SQ4", "/v1/assistant/explain-settlement", sq4_token, SQ4_QUESTION)


if __name__ == "__main__":
    run()
