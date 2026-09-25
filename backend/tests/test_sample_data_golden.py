"""TR-16.1：示例数据字节稳定，黄金值与真实质量/计算管线复算一致。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.schemas.dictionary import DataDictionary
from app.services.quality_actions import apply_decisions
from app.services.quality_checker import run_quality_checks
from app.services.storage import SessionStore
from scripts.generate_sample_data import (
    DATASETS_CONFIG,
    GOLDEN_PATH,
    SAMPLE_DIR,
    _dictionary_for,
    _golden_values,
)


def test_sample_files_exist():
    for name in DATASETS_CONFIG:
        assert (SAMPLE_DIR / f"{name}.csv").exists()
    assert GOLDEN_PATH.exists()


@pytest.mark.parametrize("name", list(DATASETS_CONFIG))
def test_builder_byte_stable(name):
    cfg = DATASETS_CONFIG[name]
    a = cfg["builder"]().to_csv(index=False).encode("utf-8")
    b = cfg["builder"]().to_csv(index=False).encode("utf-8")
    assert a == b
    # 与已落盘文件字节一致
    assert a == (SAMPLE_DIR / f"{name}.csv").read_bytes()


@pytest.mark.parametrize("name", list(DATASETS_CONFIG))
def test_golden_pipeline_reproduces(name, tmp_path):
    golden = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))["datasets"][name]
    cfg = DATASETS_CONFIG[name]
    df = cfg["builder"]()

    store = SessionStore(tmp_path / "store")
    sid = store.create_from_bytes(f"{name}.csv",
                                  df.to_csv(index=False).encode("utf-8"))["session_id"]
    dictionary: DataDictionary = _dictionary_for(df, cfg["semantics"])
    dictionary.session_id = sid
    store.write_artifact(sid, "dictionary", dictionary.model_dump(mode="json"))

    report = run_quality_checks(sid, store)
    actual_counts: dict[str, int] = {}
    for i in report.issues:
        actual_counts[i.type.value] = actual_counts.get(i.type.value, 0) + 1
    assert actual_counts == golden["issue_counts"]

    # 使用黄金固件中记录的用户决策复现快照
    report = apply_decisions(sid, golden["decisions"], store)
    assert report.snapshot_rows == golden["snapshot_rows"]
    assert report.snapshot_hash == golden["snapshot_hash"]

    snapshot = store.load_snapshot(sid)
    values = _golden_values(name, snapshot)
    assert values == golden["golden_values"]
