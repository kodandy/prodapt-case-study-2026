"""Imports every model so Alembic sees the full metadata."""

from ops_ai.db import OpsAiBase
from ops_ai.ingest import models as ingest_models
from ops_ai.search import models as search_models

ALL_MODEL_MODULES = (ingest_models, search_models)
TARGET_METADATA = OpsAiBase.metadata
