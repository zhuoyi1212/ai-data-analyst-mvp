"""LLM 访问层：OpenAI 兼容 JSON 调用 + 录制/回放固件 + 契约校验。

边界约定：本包只负责「取模型输出并校验结构」，不包含任何业务数值计算。
"""
from app.services.llm.client import generate_json
from app.services.llm.errors import (
    ContractError,
    LLMConfigError,
    LLMError,
    LLMNetworkError,
)

__all__ = [
    "generate_json",
    "ContractError",
    "LLMConfigError",
    "LLMError",
    "LLMNetworkError",
]
