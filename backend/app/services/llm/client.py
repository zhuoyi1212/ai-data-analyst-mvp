"""OpenAI 兼容 JSON 调用封装。

核心契约：
- 输出必须是 JSON，并通过调用方给定的 Pydantic 模型校验；
- 校验失败时只做有限次（≤2）带错误反馈的自动修复；
- 修复耗尽抛 ContractError，上层显式报错，绝不静默编造。
"""
from __future__ import annotations

import json
import time
from typing import Callable, Sequence, Type

import pydantic
from openai import (
    APIConnectionError,
    APIError,
    APITimeoutError,
    OpenAI,
)

from app.config import settings
from app.services.llm import fixtures
from app.services.llm.errors import (
    ContractError,
    LLMConfigError,
    LLMNetworkError,
)

_REPAIR_NOTE = (
    "\n\n【系统反馈】你上一次的输出没有通过结构校验，错误如下：\n{errors}\n"
    "请仅输出修正后的 JSON 对象，不要输出 JSON 以外的任何解释或标记。"
)

_client: OpenAI | None = None


def _get_client() -> OpenAI:
    global _client
    if not settings.llm_api_key:
        raise LLMConfigError(
            "未配置 LLM_API_KEY。请在 backend/.env 中填写 API Key（参考 .env.example）。"
        )
    if _client is None:
        _client = OpenAI(
            base_url=settings.llm_base_url,
            api_key=settings.llm_api_key,
            timeout=settings.llm_timeout_sec,
        )
    return _client


def _format_validation(exc: pydantic.ValidationError) -> list[str]:
    lines = []
    for err in exc.errors()[:10]:
        loc = ".".join(str(p) for p in err["loc"]) or "<root>"
        lines.append(f"- {loc}: {err['msg']}")
    return lines or ["结构不符合契约"]


def _live_chat(system: str, user: str) -> str:
    """真实调用，传输层错误有限重试（2 次额外尝试，指数退避）。"""
    last_exc: Exception | None = None
    for net_attempt in range(3):
        try:
            resp = _get_client().chat.completions.create(
                model=settings.llm_model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                response_format={"type": "json_object"},
                temperature=0.1,
            )
            return resp.choices[0].message.content or ""
        except (APITimeoutError, APIConnectionError) as exc:
            last_exc = exc
        except APIError as exc:
            if getattr(exc, "status_code", 0) and exc.status_code >= 500:
                last_exc = exc
            else:
                raise
        time.sleep(0.5 * (2**net_attempt))
    raise LLMNetworkError(f"模型服务暂时不可用（已重试 3 次）：{last_exc}")


def _resolve_fixture_name(fixture_name: str | Sequence[str]) -> str:
    if isinstance(fixture_name, str):
        return fixture_name
    # 录制模式取第一个候选名；回放模式由 _chat 内部尝试候选。
    return fixture_name[0]


def _chat(
    stage: str,
    fixture_name: str | Sequence[str],
    system: str,
    user: str,
    attempt: int,
) -> str:
    if settings.fixture_mode == "replay":
        names = [fixture_name] if isinstance(fixture_name, str) else list(fixture_name)
        try:
            data = fixtures.load_fixture_candidates(stage, names)
        except fixtures.FixtureMissingError:
            # 回放固件缺失（典型：用户上传的自有文件）：
            # 配置了真实 key 时回退实时调用；否则继续抛出，由各服务走确定性规则降级。
            if settings.llm_api_key:
                return _live_chat(system, user)
            raise
        return fixtures.response_at(data, attempt)
    content = _live_chat(system, user)
    if settings.fixture_mode == "record":
        fixtures.record_response(stage, _resolve_fixture_name(fixture_name), content)
    return content


def generate_json(
    *,
    stage: str,
    fixture_name: str | Sequence[str],
    system: str,
    user: str,
    schema: Type[pydantic.BaseModel],
    max_repair: int | None = None,
    business_validator: Callable[[pydantic.BaseModel], list[str]] | None = None,
) -> pydantic.BaseModel:
    """请求模型输出并强制通过 ``schema`` 校验。

    ``business_validator`` 可选：对通过结构校验的对象再做业务规则校验，
    返回非空错误列表时同样进入「带反馈的有限修复」流程。
    """
    if max_repair is None:
        max_repair = settings.llm_max_repair

    reasons: list[str] = []
    raw = ""
    for attempt in range(max_repair + 1):
        user_msg = user if attempt == 0 else user + _REPAIR_NOTE.format(
            errors="\n".join(reasons)
        )
        raw = _chat(stage, fixture_name, system, user_msg, attempt)
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            reasons = [f"JSON 解析失败：{exc.msg}（位置 {exc.pos}）"]
            continue
        try:
            obj = schema.model_validate(parsed)
        except pydantic.ValidationError as exc:
            reasons = _format_validation(exc)
            continue
        if business_validator is not None:
            biz_errors = business_validator(obj)
            if biz_errors:
                reasons = biz_errors
                continue
        return obj
    raise ContractError(stage, reasons, raw)
