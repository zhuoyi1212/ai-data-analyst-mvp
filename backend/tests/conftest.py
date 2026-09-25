"""测试公共夹具：在临时目录上回放 LLM 固件，默认离线、禁网。"""
from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from app.config import settings


@pytest.fixture
def llm_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """把固件目录指向临时目录，并固定 replay 模式。

    返回一个向固件目录写入文件的辅助函数：
        write_fixture(stage, name, payload)
    payload 可以是 dict/list（自动包成 {"response": payload}），
    也可以传入 {"responses": [...]} 以模拟多轮修复序列。
    """
    fixture_dir = tmp_path / "fixtures"
    fixture_dir.mkdir()
    test_settings = dataclasses.replace(settings, fixture_dir=fixture_dir, fixture_mode="replay")

    import app.services.llm.client as client_mod
    import app.services.llm.fixtures as fixtures_mod

    monkeypatch.setattr(client_mod, "settings", test_settings)
    monkeypatch.setattr(fixtures_mod, "settings", test_settings)

    def write_fixture(stage: str, name: str, payload) -> None:
        d = fixture_dir / stage
        d.mkdir(parents=True, exist_ok=True)
        import json

        wrapped = payload if isinstance(payload, dict) and (
            "response" in payload or "responses" in payload
        ) else {"response": payload}
        (d / f"{name}.json").write_text(
            json.dumps(wrapped, ensure_ascii=False), encoding="utf-8"
        )

    return write_fixture
