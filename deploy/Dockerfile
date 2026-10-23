# 多阶段构建：编译依赖留在 builder，运行镜像只留运行时。
#
# 构建在 GitHub Actions 上完成（服务器只有 1.7G 内存，
# 上面直接 build 容易 OOM），产物推到 ghcr.io。

# ---------- builder ----------
FROM python:3.12-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_ROOT_USER_ACTION=ignore

WORKDIR /build

# gcc 编译 asyncmy 的 C 扩展；不装 mysql 客户端头文件，
# 因为 asyncmy 是纯 C 实现，不需要 MySQL 开发包。
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# 先只拷依赖声明，让 pip 层的缓存能命中——
# 只改业务代码时不必重装依赖。
COPY pyproject.toml ./
COPY app ./app

RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --upgrade pip setuptools wheel \
    && /opt/venv/bin/pip install .

# ---------- runtime ----------
FROM python:3.12-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH" \
    TZ=Asia/Shanghai

# curl 供 HEALTHCHECK 用
RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app
COPY --chown=app:app app ./app
COPY --chown=app:app alembic.ini ./
COPY --chown=app:app alembic ./alembic
COPY --chown=app:app web ./web

# 非 root 运行。容器内除临时目录外不需要写权限。
RUN useradd --create-home --uid 1000 app \
    && mkdir -p /app/logs \
    && chown -R app:app /app
USER app

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD curl -fsS http://localhost:8000/health || exit 1

# 启动时先跑迁移。迁移失败就让容器起不来——
# 宁可启动失败也不要「服务跑着但表结构是旧的」。
CMD ["sh", "-c", "alembic upgrade head && exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --proxy-headers --forwarded-allow-ips='*' --workers 1"]