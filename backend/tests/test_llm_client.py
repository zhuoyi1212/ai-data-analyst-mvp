"""TR-2.1 / TR-2.2 / TR-2.3：回放、缺 key、契约修复与耗尽。"""
from __future__ import annotations

import dataclasses

import pydantic
import pytest

import app.services.llm.client as client_mod
from app.config import settings
from app.services.llm import generate_json
from app.services.llm.errors import ContractError, LLMConfigError
from app.services.llm.fixtures import FixtureMissingError


class Sample(pydantic.BaseModel):
    name: str
    n: int


SYSTEM = "你只输出 JSON。"
USER = "输出示例。"


def test_replay_returns_validated_object_without_network(llm_env, monkeypatch):
    llm_env("demo", "ok", {"response": {"name": "alpha", "n": 3}})

    def _forbidden(*a, **k):
        raise AssertionError("replay 模式下不允许发起真实调用")

    monkeypatch.setattr(client_mod, "_live_chat", _forbidden)

    obj = generate_json(
        stage="demo", fixture_name="ok", system=SYSTEM, user=USER, schema=Sample
    )
    assert obj.name == "alpha" and obj.n == 3


def test_missing_key_in_live_mode_raises(monkeypatch, llm_env):
    live_settings = dataclasses.replace(
        settings, fixture_mode="live", llm_api_key="", fixture_dir=settings.fixture_dir
    )
    monkeypatch.setattr(client_mod, "settings", live_settings)
    with pytest.raises(LLMConfigError):
        generate_json(
            stage="demo", fixture_name="x", system=SYSTEM, user=USER, schema=Sample
        )


def test_repair_succeeds_on_second_attempt(llm_env):
    llm_env(
        "demo",
        "repair",
        {"responses": ["这不是JSON", {"name": "beta", "n": 7}]},
    )
    obj = generate_json(
        stage="demo", fixture_name="repair", system=SYSTEM, user=USER, schema=Sample
    )
    assert obj == Sample(name="beta", n=7)


def test_contract_error_after_repair_exhausted(llm_env):
    llm_env(
        "demo",
        "bad",
        {"responses": ["not json", "{oops", {"name": "missing n"}]},
    )
    with pytest.raises(ContractError) as exc:
        generate_json(
            stage="demo",
            fixture_name="bad",
            system=SYSTEM,
            user=USER,
            schema=Sample,
        )
    assert exc.value.stage == "demo"
    assert exc.value.reasons  # 附带结构化错误原因
    assert exc.value.raw is not None


def test_missing_fixture_raises(llm_env):
    with pytest.raises(FixtureMissingError):
        generate_json(
            stage="demo", fixture_name="absent", system=SYSTEM, user=USER, schema=Sample
        )
