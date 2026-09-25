"""全局配置：仅从后端环境 / .env 读取，API key 永不下发前端。"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

BACKEND_DIR = Path(__file__).resolve().parents[1]
ROOT_DIR = BACKEND_DIR.parent

load_dotenv(BACKEND_DIR / ".env")


@dataclass(frozen=True)
class Settings:
    llm_base_url: str
    llm_api_key: str
    llm_model: str
    fixture_mode: str  # replay | live | record
    fixture_dir: Path
    storage_dir: Path
    sample_data_dir: Path

    # 上传边界（FR-1）
    max_file_mb: int = 20
    max_rows: int = 200_000
    max_cols: int = 50
    preview_rows: int = 10
    table_display_rows: int = 1_000

    # LLM 行为
    llm_timeout_sec: int = 60
    llm_max_repair: int = 2  # 契约失败后的有限自动修复次数


def _build() -> Settings:
    mode = os.getenv("LLM_FIXTURE_MODE", "replay").strip().lower()
    if mode not in {"replay", "live", "record"}:
        mode = "replay"
    return Settings(
        llm_base_url=os.getenv("LLM_BASE_URL", "https://api.deepseek.com").strip(),
        llm_api_key=os.getenv("LLM_API_KEY", "").strip(),
        llm_model=os.getenv("LLM_MODEL", "deepseek-chat").strip(),
        fixture_mode=mode,
        fixture_dir=BACKEND_DIR / "tests" / "fixtures" / "llm",
        storage_dir=BACKEND_DIR / "storage",
        sample_data_dir=ROOT_DIR / "sample_data",
    )


settings = _build()
settings.storage_dir.mkdir(parents=True, exist_ok=True)
settings.fixture_dir.mkdir(parents=True, exist_ok=True)
