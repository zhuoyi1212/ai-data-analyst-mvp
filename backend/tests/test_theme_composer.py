"""业务主题归集：LLM 归类 + 规则回退。"""
from __future__ import annotations

from app.schemas.dashboard import DashboardArtifact, DashboardTheme
from app.services.llm.fixtures import FixtureMissingError
from app.services.theme_composer import derive_themes


def _views() -> list[dict[str, str]]:
    return [
        {"view_id": "v1", "title": "t1", "question": "销售额按月趋势", "metric": "销售额", "dimension": "订单日期"},
        {"view_id": "v2", "title": "t2", "question": "各区域销售额", "metric": "销售额", "dimension": "区域"},
        {"view_id": "v3", "title": "t3", "question": "各品类利润", "metric": "利润", "dimension": "类别"},
        {"view_id": "v4", "title": "t4", "question": "利润率", "metric": "利润率", "dimension": "客户细分"},
    ]


def test_derive_themes_falls_back_to_metric_grouping(monkeypatch):
    def _boom(**kwargs):
        raise FixtureMissingError()

    monkeypatch.setattr("app.services.theme_composer.generate_json", _boom)
    themes = derive_themes(_views(), base="unknown")

    assert [t.name for t in themes] == ["销售额", "利润", "利润率"]
    assert [t.theme_id for t in themes] == ["theme_01", "theme_02", "theme_03"]
    # 覆盖全部视图、无遗漏无重复
    all_ids = [vid for t in themes for vid in t.view_ids]
    assert sorted(all_ids) == ["v1", "v2", "v3", "v4"]


def test_derive_themes_empty():
    assert derive_themes([]) == []


def test_dashboard_artifact_carries_themes():
    theme = DashboardTheme(theme_id="theme_01", name="销售表现", view_ids=["v1"])
    artifact = DashboardArtifact(bundle_id="b", title="t", themes=[theme])
    assert artifact.themes[0].name == "销售表现"
    assert artifact.themes[0].view_ids == ["v1"]
