"""TR-4.1 / TR-4.2：方案契约校验与「无自由代码执行」审计。"""
from __future__ import annotations

import re
from pathlib import Path

import pydantic
import pytest

from app.schemas.common import ChartType, FieldInfo, SemanticType
from app.schemas.plan import AnalysisPlan
from app.services.engine.catalog import terminal_chart, validate_plan_against_fields

FIELDS = [
    FieldInfo(name="区域", semantic_type=SemanticType.dimension),
    FieldInfo(name="品类", semantic_type=SemanticType.dimension),
    FieldInfo(name="日期", semantic_type=SemanticType.date),
    FieldInfo(name="金额", semantic_type=SemanticType.metric),
    FieldInfo(name="数量", semantic_type=SemanticType.metric),
    FieldInfo(name="订单号", semantic_type=SemanticType.id, ignored=True),
]

VALID_PLAN = {
    "question": "华北区域各品类销售额对比",
    "data_scope": "全部数据",
    "steps": [
        {"step_id": "s1", "op": "filter", "description": "仅华北",
         "params": {"column": "区域", "operator": "==", "value": "华北"}},
        {"step_id": "s2", "op": "group_by", "description": "按品类汇总金额",
         "params": {"dimension": "品类", "metric": "金额", "func": "sum", "order": "desc"}},
    ],
}


def test_valid_plan_passes():
    plan = AnalysisPlan.model_validate(VALID_PLAN)
    assert validate_plan_against_fields(plan, FIELDS) == []
    assert terminal_chart(plan) == ChartType.bar


def test_unknown_op_rejected():
    raw = {"question": "q", "steps": [{"step_id": "x", "op": "run_sql", "params": {}}]}
    with pytest.raises(pydantic.ValidationError):
        AnalysisPlan.model_validate(raw)


def test_extra_param_rejected():
    raw = {
        "question": "q",
        "steps": [{"step_id": "x", "op": "aggregate",
                   "params": {"column": "金额", "func": "sum", "danger": 1}}],
    }
    with pytest.raises(pydantic.ValidationError):
        AnalysisPlan.model_validate(raw)


def test_ghost_column_rejected():
    raw = {
        "question": "q",
        "steps": [{"step_id": "x", "op": "group_by",
                   "params": {"dimension": "品类", "metric": "不存在", "func": "sum"}}],
    }
    plan = AnalysisPlan.model_validate(raw)
    errors = validate_plan_against_fields(plan, FIELDS)
    assert any("不存在" in e for e in errors)


def test_time_op_requires_date_field():
    raw = {
        "question": "q",
        "steps": [{"step_id": "x", "op": "time_series",
                   "params": {"date_column": "区域", "granularity": "month",
                              "metric": "金额", "func": "sum"}}],
    }
    plan = AnalysisPlan.model_validate(raw)
    errors = validate_plan_against_fields(plan, FIELDS)
    assert any("日期字段" in e for e in errors)


def test_ignored_field_unusable():
    raw = {
        "question": "q",
        "steps": [{"step_id": "x", "op": "aggregate",
                   "params": {"column": "订单号", "func": "count_distinct"}}],
    }
    plan = AnalysisPlan.model_validate(raw)
    errors = validate_plan_against_fields(plan, FIELDS)
    assert any("已被忽略" in e for e in errors)


def test_bad_param_type_rejected():
    raw = {
        "question": "q",
        "steps": [{"step_id": "x", "op": "top_n",
                   "params": {"dimension": "品类", "metric": "金额", "func": "sum",
                              "n": "三个"}}],
    }
    with pytest.raises(pydantic.ValidationError):
        AnalysisPlan.model_validate(raw)


FORBIDDEN = [
    r"\beval\s*\(",
    r"\bexec\s*\(",
    r"\bos\.system\s*\(",
    r"\bsubprocess\b",
    r"read_sql\s*\(",
    r"pickle\.loads\s*\(",
]


def test_no_arbitrary_code_execution_paths():
    app_dir = Path(__file__).resolve().parents[1] / "app"
    offenders = []
    for path in app_dir.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for pat in FORBIDDEN:
            if re.search(pat, text):
                offenders.append(f"{path.relative_to(app_dir)}: {pat}")
    assert not offenders, f"发现潜在的自由代码执行入口：{offenders}"


def test_engine_has_no_llm_imports():
    engine_dir = Path(__file__).resolve().parents[1] / "app" / "services" / "engine"
    offenders = []
    for path in engine_dir.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if re.search(r"^\s*(from|import)\s+app\.services\.llm", text, re.MULTILINE):
            offenders.append(str(path))
    assert not offenders, f"引擎包禁止依赖 LLM 层：{offenders}"
