"""Imports every model so Alembic sees the full metadata."""

from gateway.accounts import models as accounts_models
from gateway.audit import models as audit_models
from gateway.compliance import models as compliance_models
from gateway.db import GatewayBase, SimulatorBase
from gateway.notifications import models as notifications_models
from gateway.outbox import models as outbox_models
from gateway.payments import models as payments_models
from gateway.payments.fx import models as fx_models
from gateway.payments.idempotency import models as idempotency_models
from gateway.payments.ledger import models as ledger_models
from gateway.payments.providers.simulator import models as simulator_models
from gateway.payments.tokenization import models as tokenization_models
from gateway.wallets import models as wallets_models

ALL_MODEL_MODULES = (
    accounts_models,
    audit_models,
    compliance_models,
    notifications_models,
    outbox_models,
    payments_models,
    fx_models,
    idempotency_models,
    ledger_models,
    simulator_models,
    tokenization_models,
    wallets_models,
)
TARGET_METADATA = (GatewayBase.metadata, SimulatorBase.metadata)
