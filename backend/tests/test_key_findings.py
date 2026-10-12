from app.schemas.bundle import ViewType
from app.schemas.dashboard import ChartSpec, ColumnSchema, DashboardArtifact, Finding, ViewCard, ViewDataEnvelope
from app.services.key_findings import _Output, review_key_findings, validate_findings


def _artifact():
    card = ViewCard(view_id="v1", title="利润对比", question="q", type=ViewType.comparison,
                    status="success", consumable=True, metric_label="Profit",
                    chart_spec=ChartSpec(type="bar", title="利润", dimension="Category", x_field="Category", metric="Profit", y_fields=["value"]),
                    data=ViewDataEnvelope(kind="aggregate", columns=[ColumnSchema(name="Category", dtype="string"), ColumnSchema(name="value", dtype="number")], rows=[{"Category":"A", "value":10}, {"Category":"B", "value":40}]))
    anomaly = card.model_copy(update={"view_id":"a1", "type":ViewType.anomaly})
    return DashboardArtifact(bundle_id="b", run_id="r", title="demo", state="ready", views={"v1":card,"a1":anomaly})


def _output(title="A的利润较低", summary="A的利润为10，B为40。", refs=None):
    return _Output.model_validate({"findings":[{"title":title,"summary":summary,
        "type":"群体差异", "importance":"medium", "evidence_view_ids":refs or ["v1"]}]})


def test_open_topics_accept_grounded_new_observation():
    assert validate_findings(_output(), _artifact()) == []


def test_unknown_numbers_and_evidence_are_rejected():
    assert validate_findings(_output(summary="利润为9999。"), _artifact())
    assert validate_findings(_output(refs=["made_up"]), _artifact())


def test_raw_iqr_and_causal_claims_do_not_become_key_findings():
    assert validate_findings(_output(refs=["a1"]), _artifact())
    assert validate_findings(_output(summary="产品差异导致利润较低。"), _artifact())


def test_model_path_uses_open_prompt_and_preserves_result_data(monkeypatch):
    import app.services.key_findings as service
    artifact = _artifact()
    original = artifact.model_dump()
    def generate(**args):
        assert "分析方向开放" in args["system"]
        assert "不同筛选范围" in args["system"]
        assert args["business_validator"](_output()) == []
        return _output()
    monkeypatch.setattr(service, "generate_json", generate)
    reviewed = review_key_findings(artifact)
    assert reviewed.findings_source == "model"
    assert reviewed.findings[0].type == "群体差异"
    assert reviewed.views == artifact.views
    assert artifact.model_dump() == original


def test_offline_fallback_keeps_computed_findings_but_promotes_nothing(monkeypatch):
    import app.services.key_findings as service
    from app.services.llm.fixtures import FixtureMissingError
    from app.schemas.insight_candidate import InsightCandidate
    artifact = _artifact()
    # 已计算的确定性异常发现按 T12 契约保留
    artifact.findings = [Finding(finding_id="old", title="异常信号", summary="已计算的离群发现", type="anomaly", importance="high", evidence_view_ids=["a1"])]
    # 计算候选即使引用合法证据，离线兜底时也不得被提升为关键发现
    artifact.insights = [InsightCandidate(insight_id="i1", type="anomaly", title="统计离群", summary="待核查", importance="high", evidence_view_ids=["a1"])]
    def missing(**_):
        raise FixtureMissingError("missing")
    monkeypatch.setattr(service, "generate_json", missing)
    reviewed = review_key_findings(artifact)
    assert [f.title for f in reviewed.findings] == ["异常信号"]
    assert reviewed.views["a1"].consumable
    assert reviewed.findings_source == "computed"


def test_signed_change_percent_uses_computed_candidate_not_guesswork():
    from app.schemas.insight_candidate import InsightCandidate
    artifact = _artifact()
    artifact.insights = [InsightCandidate(insight_id="i1", type="performance_change",
        title="利润下滑28.1%", summary="利润下滑28.1%。", delta_pct=-.281,
        evidence_view_ids=["v1"])]
    assert validate_findings(_output(summary="利润变化为-28.1%。"), artifact) == []
    assert validate_findings(_output(summary="利润变化为-80%。"), artifact)


def test_parent_total_needs_parent_evidence_reference():
    artifact = _artifact()
    parent = artifact.views["v1"].model_copy(update={"view_id":"parent",
        "data":ViewDataEnvelope(kind="aggregate",columns=[ColumnSchema(name="value",dtype="number")],rows=[{"value":1000}])})
    artifact.views["parent"] = parent
    assert validate_findings(_output(summary="整体利润1000。"), artifact)
    assert validate_findings(_output(summary="整体利润1000。", refs=["v1","parent"]), artifact) == []


def test_head_tail_ranking_is_not_concentration_evidence():
    artifact = _artifact()
    artifact.views["v1"].type = ViewType.ranking
    assert validate_findings(_output(title="客户利润高度集中", summary="A的利润为10，B为40。"), artifact)


def test_rank_member_count_is_grounded_metadata():
    artifact = _artifact()
    artifact.views["v1"].type = ViewType.ranking
    assert validate_findings(_output(title="前2名客户利润", summary="B的利润为40，A为10。"), artifact) == []
    assert validate_findings(_output(title="前9名客户利润", summary="B的利润为40，A为10。"), artifact)


def test_verbal_ratios_and_unobserved_sign_changes_are_rejected():
    assert validate_findings(_output(title="A利润为B的四分之一"), _artifact())
    assert validate_findings(_output(summary="A利润转负。"), _artifact())
