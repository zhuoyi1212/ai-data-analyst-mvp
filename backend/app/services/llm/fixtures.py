"""录制/回放固件：按 (stage, fixture_name) 组织的 JSON 文件。

固件格式（二选一）：

    {"response": "<模型输出的 JSON 字符串或对象>"}
    {"responses": ["第 1 次调用输出", "第 2 次调用输出（修复）", ...]}

多轮形态用于确定性地复现「输出非法 → 注入修复反馈 → 重试」序列；
调用序号超出列表长度时取最后一个元素。
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Sequence

from app.config import settings


class FixtureMissingError(Exception):
    pass


def fixture_path(stage: str, name: str) -> Path:
    safe_name = name.replace("/", "_").replace("..", "_")
    return settings.fixture_dir / stage / f"{safe_name}.json"


def question_variant(base: str, stage_suffix: str, question: str) -> str:
    """按问题文本生成稳定的固件变体名，用于多轮追问的离线回放。

    例如 ``question_variant("sales_orders_plan", "plan", "...")`` →
    ``sales_orders_plan__a1b2c3d4e5``。
    """
    digest = hashlib.md5(question.encode("utf-8")).hexdigest()[:10]
    return f"{base}__{digest}"


def load_fixture(stage: str, name: str) -> dict[str, Any]:
    path = fixture_path(stage, name)
    if not path.exists():
        raise FixtureMissingError(f"回放固件不存在: {path.relative_to(settings.fixture_dir)}")
    return json.loads(path.read_text(encoding="utf-8"))


def load_fixture_candidates(stage: str, names: Sequence[str]) -> dict[str, Any]:
    """按顺序尝试候选固件名，返回第一个存在的；全部缺失时抛出。

    用于多轮追问：优先使用与当前问题绑定的变体固件，缺失则回退到 canonical 固件。
    """
    for name in names:
        path = fixture_path(stage, name)
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    tried = ", ".join(names)
    raise FixtureMissingError(
        f"回放固件不存在（已尝试: {tried}）"
    )


def response_at(data: dict[str, Any], attempt: int) -> str:
    """返回第 attempt 次（0-based）调用应得到的输出字符串。"""
    if "responses" in data:
        seq = data["responses"]
        item = seq[min(attempt, len(seq) - 1)]
    else:
        item = data["response"]
    if isinstance(item, str):
        return item
    return json.dumps(item, ensure_ascii=False)


def record_response(stage: str, name: str, content: str) -> None:
    """录制模式：把一次真实调用的输出追加进固件。"""
    path = fixture_path(stage, name)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        data = json.loads(path.read_text(encoding="utf-8"))
        if "response" in data:
            data = {"responses": [data["response"]]}
        data.setdefault("responses", []).append(content)
    else:
        data = {"response": content}
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
