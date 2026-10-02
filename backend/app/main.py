"""FastAPI 入口：仅监听本地，CORS 限定本地前端。"""
from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.config import settings
from app.routers import (
    bundle,
    dashboard,
    diagnostic,
    execute,
    followups,
    insights,
    plan,
    profile,
    quality,
    questions,
    sessions,
    validate,
)
from app.services.llm.errors import (
    ContractError,
    LLMConfigError,
    LLMNetworkError,
)
from app.services.llm.fixtures import FixtureMissingError

app = FastAPI(title="AI Data Analyst MVP", version="0.1.0")

app.include_router(sessions.router)
app.include_router(profile.router)
app.include_router(quality.router)
app.include_router(questions.router)
app.include_router(bundle.router)
app.include_router(dashboard.router)
app.include_router(diagnostic.router)
app.include_router(plan.router)
app.include_router(execute.router)
app.include_router(validate.router)
app.include_router(insights.router)
app.include_router(followups.router)


@app.exception_handler(ContractError)
async def contract_error_handler(_: Request, exc: ContractError) -> JSONResponse:
    # 契约耗尽：显式报错，前端提供「重试 / 编辑输入」，严禁兜底编造
    return JSONResponse(
        status_code=422,
        content={
            "detail": f"AI 输出未通过校验，已达自动修复上限：{'；'.join(exc.reasons)}",
            "stage": exc.stage,
            "reasons": exc.reasons,
            "retryable": True,
        },
    )


@app.exception_handler(LLMConfigError)
async def config_error_handler(_: Request, exc: LLMConfigError) -> JSONResponse:
    return JSONResponse(status_code=503, content={"detail": str(exc), "retryable": False})


@app.exception_handler(LLMNetworkError)
async def network_error_handler(_: Request, exc: LLMNetworkError) -> JSONResponse:
    return JSONResponse(status_code=503, content={"detail": str(exc), "retryable": True})


@app.exception_handler(FixtureMissingError)
async def fixture_missing_handler(_: Request, exc: FixtureMissingError) -> JSONResponse:
    # 兜底：理论上各阶段均有确定性规则降级，不会冒泡到此
    return JSONResponse(
        status_code=503,
        content={
            "detail": "当前为离线演示模式且该数据无回放固件，同时未配置 LLM_API_KEY，"
                      "无法生成该阶段内容。请使用内置示例数据集，或在 backend/.env 配置 API Key。",
            "retryable": False,
        },
    )

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://127.0.0.1:3000"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
def health() -> dict:
    return {
        "status": "ok",
        "fixture_mode": settings.fixture_mode,
        "llm_configured": bool(settings.llm_api_key) or settings.fixture_mode == "replay",
        "llm_key_present": bool(settings.llm_api_key),
        "offline_fallback": settings.fixture_mode == "replay",
    }
