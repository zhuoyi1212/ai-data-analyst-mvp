# 后端镜像：FastAPI + 确定性分析引擎（pandas / pyarrow）
# 用于 Railway / Render / Fly.io 等支持常驻进程与持久卷的平台。
# 构建上下文 = 仓库根目录（需要同时拿到 backend/ 与 sample_data/）。
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_LINK_MODE=copy \
    LLM_FIXTURE_MODE=replay

# 与本地一致的依赖管理工具
RUN pip install --no-cache-dir uv

WORKDIR /app/backend

# 先只装依赖，利用镜像层缓存（代码改动不必重装依赖）
COPY backend/pyproject.toml backend/uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# 再拷入代码、回放固件（replay 模式需要 backend/tests/fixtures/llm 与样例数据）
COPY backend/ ./
COPY sample_data/ /app/sample_data/

EXPOSE 8000

# 平台通常注入 $PORT；本地默认 8000
CMD ["sh", "-c", ".venv/bin/uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]