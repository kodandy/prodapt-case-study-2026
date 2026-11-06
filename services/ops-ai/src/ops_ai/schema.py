"""Imports every model so Alembic sees the full metadata."""

from ops_ai.agents import models as agent_models
from ops_ai.alerts import models as alert_models
from ops_ai.analytics import models as analytics_models
from ops_ai.compliance import models as compliance_models
from ops_ai.db import OpsAiBase
from ops_ai.feedback import models as feedback_models
from ops_ai.fraud_watch import models as fraud_watch_models
from ops_ai.ingest import models as ingest_models
from ops_ai.investigation import models as investigation_models
from ops_ai.memory import models as memory_models
from ops_ai.notify import models as notify_models
from ops_ai.search import models as search_models
from ops_ai.settlement import models as settlement_models
from ops_ai.tickets import models as ticket_models
from ops_ai.transfers import models as transfer_models

ALL_MODEL_MODULES = (
    analytics_models,
    compliance_models,
    agent_models,
    feedback_models,
    alert_models,
    fraud_watch_models,
    ingest_models,
    investigation_models,
    memory_models,
    notify_models,
    search_models,
    settlement_models,
    ticket_models,
    transfer_models,
)
TARGET_METADATA = OpsAiBase.metadata
