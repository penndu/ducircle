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

# 换源。基础镜像默认用 deb.debian.org，实测在国内云主机上
# 容器内解析该域名会拿到 IPv6 地址，但没有 IPv6 路由，
# apt 会卡住直到超时。换成腾讯云镜像（同机房，稳定且快）。
RUN sed -i 's|http://deb.debian.org/debian|https://mirrors.cloud.tencent.com/debian|g; \
            s|http://deb.debian.org/debian-security|https://mirrors.cloud.tencent.com/debian-security|g; \
            s|http://security.debian.org/debian-security|https://mirrors.cloud.tencent.com/debian-security|g' \
        /etc/apt/sources.list.d/debian.sources 2>/dev/null || \
    sed -i 's|http://deb.debian.org/debian|https://mirrors.cloud.tencent.com/debian|g' /etc/apt/sources.list

# gcc 编译 asyncmy 的 C 扩展；不装 mysql 客户端头文件，
# 因为 asyncmy 是纯 C 实现，不需要 MySQL 开发包。
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# 先只拷依赖声明，让 pip 层的缓存能命中——
# 只改业务代码时不必重装依赖。
#
# README.md 必须一起拷：pyproject.toml 声明了 readme = "README.md"，
# hatchling 构建时会校验该文件在构建目录中存在，缺了就报
#   OSError: Readme file does not exist: README.md
# 这个坑卡了 CI 五轮才定位到——报错被 BuildKit 吞成了
# metadata-generation-failed，看不到真正原因。
COPY pyproject.toml README.md ./
COPY app ./app

# 先装构建后端再用 --no-build-isolation安装本项目。
# 否则 pip 会在隔离环境里临时下载 hatchling，
# 那一步要访问 PyPI——容器网络不稳时会失败，
# 而且 BuildKit 会把真实报错吞掉只留 metadata-generation-failed。
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --upgrade pip setuptools wheel \
    && /opt/venv/bin/pip install hatchling \
    && /opt/venv/bin/pip install --no-build-isolation .

# ---------- runtime ----------
FROM python:3.12-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH" \
    TZ=Asia/Shanghai

# curl 供 HEALTHCHECK 用。源替换同 builder 阶段，理由见上。
RUN sed -i 's|http://deb.debian.org/debian|https://mirrors.cloud.tencent.com/debian|g; \
            s|http://deb.debian.org/debian-security|https://mirrors.cloud.tencent.com/debian-security|g; \
            s|http://security.debian.org/debian-security|https://mirrors.cloud.tencent.com/debian-security|g' \
        /etc/apt/sources.list.d/debian.sources 2>/dev/null || \
    sed -i 's|http://deb.debian.org/debian|https://mirrors.cloud.tencent.com/debian|g' /etc/apt/sources.list

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