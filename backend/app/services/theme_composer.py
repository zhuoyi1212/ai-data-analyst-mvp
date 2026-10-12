"""业务主题归集（AI 版）：把可见图表按「用户关心的业务问题」分组并命名。

边界：LLM 只做「归类 + 命名」，不产数字、不改数字；无 key / 调用失败 / 契约失败时
回退到「按主指标中文名分组」的确定性规则。数值全部来自确定性引擎，本模块只组织展示。
"""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from app.schemas.dashboard import DashboardTheme
from app.services.llm import generate_json
from app.services.llm.errors import LLMError
from app.services.llm.fixtures import FixtureMissingError

_MAX_THEMES = 5

_SYSTEM = (
    "你是一名资深数据分析师，擅长用业务语言组织分析结论。"
    "你的任务是把一组分析视图按「业务主题」分组，让业务用户能快速找到想看的内容。"
    "主题名要站在业务用户角度，回答「他关心什么」，"
    "例如「销售表现」「利润与盈利」「风险与异常」「客户与市场」。"
    "禁止用「趋势/走势/结构/分布/诊断/对比/排名」这类描述图表类型的词。只输出 JSON。"
)


class _ThemeDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    views: list[int]


class _ThemeSetDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    themes: list[_ThemeDraft]


def _build_user(views: list[dict[str, str]]) -> str:
    """把可见图表列表转成给 LLM 的 user prompt（编号 + 问题 + 指标/维度）。"""
    lines = ["请把下面这些分析视图按业务主题分组。", ""]
    for i, v in enumerate(views, start=1):
        metric = v.get("metric") or ""
        dimension = v.get("dimension") or ""
        head = f"{i}. {v.get('question') or v.get('title') or '（无问题）'}"
        if metric:
            head += f"（指标：{metric}"
            head += f"；维度：{dimension}）" if dimension else "）"
        lines.append(head)
    lines += [
        "",
        "请输出业务主题，每个主题包含：",
        "- name：简短的中文业务主题名（2–6 字），如「销售表现」「利润与盈利」，禁止「趋势/结构/走势/诊断」这类词",
        "- views：属于该主题的视图编号列表",
        "要求：每个视图必须且只能归属一个主题；所有主题的 views 合起来恰好覆盖全部视图编号，不能遗漏、不能重复。",
    ]
    return "\n".join(lines)


def _validate(obj: _ThemeSetDraft, n: int) -> list[str]:
    """业务校验：主题数、名称、编号覆盖与去重。"""
    if not obj.themes:
        return ["至少需要 1 个主题"]
    errors: list[str] = []
    if len(obj.themes) > _MAX_THEMES:
        errors.append(f"主题数超过 {_MAX_THEMES}")
    names = [t.name.strip() for t in obj.themes]
    if any(not nm for nm in names):
        errors.append("主题名不能为空")
    if len(set(names)) != len(names):
        errors.append("主题名不能重复")
    seen: set[int] = set()
    for t in obj.themes:
        for idx in t.views:
            if idx < 1 or idx > n:
                errors.append(f"视图编号 {idx} 越界")
            elif idx in seen:
                errors.append(f"视图编号 {idx} 重复归属")
            else:
                seen.add(idx)
    missing = [i for i in range(1, n + 1) if i not in seen]
    if missing:
        errors.append(f"视图编号未覆盖：{missing}")
    return errors


def _fallback(views: list[dict[str, str]]) -> list[DashboardTheme]:
    """确定性回退：按主指标中文名分组，覆盖全部视图。"""
    groups: dict[str, list[str]] = {}
    order: list[str] = []
    for v in views:
        metric = v.get("metric") or "其他"
        if metric not in groups:
            groups[metric] = []
            order.append(metric)
        groups[metric].append(v["view_id"])
    return [
        DashboardTheme(
            theme_id=f"theme_{i:02d}",
            name=metric,
            view_ids=groups[metric],
        )
        for i, metric in enumerate(order, start=1)
    ]


def derive_themes(
    views: list[dict[str, str]],
    *,
    base: str = "dashboard",
) -> list[DashboardTheme]:
    """由可见图表生成业务主题。views 每项含 view_id/question/metric/dimension。"""
    if not views:
        return []
    try:
        draft = generate_json(
            stage="themes",
            fixture_name=f"{base}_themes",
            system=_SYSTEM,
            user=_build_user(views),
            schema=_ThemeSetDraft,
            business_validator=lambda obj: _validate(obj, len(views)),
        )
    except (FixtureMissingError, LLMError):
        return _fallback(views)

    return [
        DashboardTheme(
            theme_id=f"theme_{i:02d}",
            name=t.name.strip(),
            view_ids=[views[idx - 1]["view_id"] for idx in t.views],
        )
        for i, t in enumerate(draft.themes, start=1)
    ]
