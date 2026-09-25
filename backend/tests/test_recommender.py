"""TR-8.1 / TR-8.2：推荐数量/分类/依据接地、无日期禁趋势。"""
from __future__ import annotations

import pytest

from app.schemas.common import Confidence, SemanticType
from app.schemas.dictionary import DataDictionary, FieldProfile
from app.services.llm.errors import ContractError
from app.services.recommender import recommend
from app.services.storage import SessionStore


def _field(name, st):
    return FieldProfile(
        name=name, physical_type="string", semantic_type=st,
        confidence=Confidence.high, confirmed=True, confirmed_by_user=True,
    )


def _dictionary(no_date: bool = False) -> DataDictionary:
    fields = [_field("区域", SemanticType.dimension), _field("品类", SemanticType.dimension),
              _field("金额", SemanticType.metric), _field("数量", SemanticType.metric)]
    if not no_date:
        fields.insert(0, _field("日期", SemanticType.date))
    return DataDictionary(session_id="x", fields=fields, complete=True)


_VALID = {
    "questions": [
        {"text": "总销售额是多少", "category": "overview", "rationale": "基于「金额」字段可直接汇总总量",
         "fields": ["金额"], "target_op": "aggregate", "confidence": "high"},
        {"text": "销售额随时间的趋势如何", "category": "trend", "rationale": "存在「日期」字段，可按月聚合「金额」看趋势",
         "fields": ["日期", "金额"], "target_op": "time_series", "confidence": "high"},
        {"text": "各区域销售额对比", "category": "comparison", "rationale": "「区域」是枚举维度，可分组对比「金额」",
         "fields": ["区域", "金额"], "target_op": "group_by", "confidence": "high"},
        {"text": "各品类销售额占比", "category": "share", "rationale": "用「品类」拆分「金额」占比结构",
         "fields": ["品类", "金额"], "target_op": "share", "confidence": "medium"},
        {"text": "销售额 Top5 品类", "category": "ranking", "rationale": "按「品类」聚合「金额」即可排名",
         "fields": ["品类", "金额"], "target_op": "top_n", "confidence": "medium"},
        {"text": "金额是否存在异常值", "category": "anomaly", "rationale": "「金额」为核心指标，可做离群检测",
         "fields": ["金额"], "target_op": "outlier_flag", "confidence": "medium"},
        {"text": "数量与金额是否相关", "category": "correlation",
         "rationale": "两个指标「数量」「金额」可计算相关系数", "fields": ["数量", "金额"],
         "target_op": "correlation", "confidence": "low"},
    ]
}


def _session(tmp_path, dictionary, llm_env, payload, name="mini_questions"):
    llm_env("questions", name, payload)
    store = SessionStore(tmp_path / "storage")
    session_id = _create(store, dictionary)
    return store, session_id


def _create(store: SessionStore, dictionary: DataDictionary) -> str:
    import pandas as pd

    cols = {f.name: (["x"] if f.semantic_type != SemanticType.metric else [1.0])
            for f in dictionary.fields}
    meta = store.create_from_bytes("d.csv", pd.DataFrame(cols).to_csv(index=False).encode())
    store.write_artifact(meta["session_id"], "dictionary",
                         {**dictionary.model_dump(mode="json"), "session_id": meta["session_id"]})
    return meta["session_id"]


def test_valid_recommendations(tmp_path, llm_env):
    d = _dictionary()
    store, sid = _session(tmp_path, d, llm_env, _VALID)
    qs = recommend(sid, store, fixture_name="mini")
    assert len(qs.questions) == 7
    assert len({q.category for q in qs.questions}) >= 4
    assert store.read_artifact(sid, "questions")["questions"][0]["fields"] == ["金额"]


def test_no_date_dict_rejects_trend(tmp_path, llm_env):
    no_date = {
        "questions": [
            {"text": "总销售额", "category": "overview", "rationale": "直接汇总「金额」字段得到销售总量",
             "fields": ["金额"], "target_op": "aggregate", "confidence": "high"},
            {"text": "销售额趋势", "category": "trend", "rationale": "观察「金额」指标随时间的趋势变化",
             "fields": ["金额"], "target_op": "time_series", "confidence": "high"},
        ]
        + [
            {"text": f"分析问题编号{i}", "category": c, "rationale": f"基于「金额」字段的{c}分析",
             "fields": ["金额"], "target_op": op, "confidence": "medium"}
            for i, (c, op) in enumerate(
                [("comparison", "group_by"), ("share", "share"), ("ranking", "top_n"),
                 ("anomaly", "outlier_flag")]
            )
        ]
    }
    store, sid = _session(tmp_path, _dictionary(no_date=True), llm_env, no_date, "nodate_questions")
    with pytest.raises(ContractError) as exc:
        recommend(sid, store, fixture_name="nodate")
    assert any("趋势" in r for r in exc.value.reasons)


def test_ungrounded_rationale_rejected(tmp_path, llm_env):
    bad = {"questions": [{**q, "rationale": "这个问题可以随便先看看再说"} for q in _VALID["questions"]]}
    store, sid = _session(tmp_path, _dictionary(), llm_env, bad, "badrationale_questions")
    with pytest.raises(ContractError) as exc:
        recommend(sid, store, fixture_name="badrationale")
    assert any("推荐依据" in r for r in exc.value.reasons)
