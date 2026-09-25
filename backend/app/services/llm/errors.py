"""LLM 相关的统一异常类型。"""
from __future__ import annotations


class LLMError(Exception):
    """LLM 层异常基类。"""


class LLMConfigError(LLMError):
    """缺少 API key / base_url 等配置问题。"""


class LLMNetworkError(LLMError):
    """网络、超时、上游 5xx 等可重试/可重试耗尽的传输问题。"""


class ContractError(LLMError):
    """模型输出无法解析为约定的 JSON 契约（修复次数耗尽）。

    上层必须将其转化为显式的用户可见错误，严禁兜底编造。
    """

    def __init__(self, stage: str, reasons: list[str], raw: str | None = None):
        self.stage = stage
        self.reasons = reasons
        self.raw = raw
        super().__init__(f"[{stage}] LLM 输出未通过契约校验: {'; '.join(reasons)}")
