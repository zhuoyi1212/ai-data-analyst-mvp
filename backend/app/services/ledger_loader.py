"""执行后上下文加载（台账/方案/校验/全量结果），供洞察与追问复用。"""
from __future__ import annotations

import pandas as pd

from app.schemas.ledger import Ledger
from app.schemas.plan import PlanArtifact
from app.schemas.validation import ValidationReport
from app.services.storage import SessionStore


def load_execution_context(session_id: str, store: SessionStore):
    ledger = Ledger.model_validate(store.read_artifact(session_id, "ledger"))
    plan_artifact = PlanArtifact.model_validate(store.read_artifact(session_id, "plan"))
    validation = ValidationReport.model_validate(store.read_artifact(session_id, "validation"))
    result = pd.read_parquet(store.session_dir(session_id) / "result.parquet")
    return ledger, plan_artifact, validation, result
