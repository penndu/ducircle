"""用户模型。"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin


class User(Base, TimestampMixin):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(32), unique=True, nullable=False,
                                          index=True)
    password_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    # 密码最后修改时间（Unix 时间戳，秒）。
    #
    # Session 是无状态签名（itsdangerous），本来就无法主动踢人下线。
    # 有了这个时间戳，改密码后就能让**所有旧会话失效**——
    # 校验会话时比一下签发时间，早于这个时刻的一律拒绝。
    # 没有它的话，用户改完密码，旧浏览器上的 Cookie 还能继续用一周。
    #
    # 为什么存 Unix 时间戳而不是 DateTime：
    # DateTime 方案要处理 naive/aware 混用、容器时区与数据库时区不一致、
    # SQLite 存储精度截断等问题——实测就踩到过「存取后差了 1 秒」的坑，
    # 导致同一秒签发的旧会话没被作废。整数没有这些问题。
    password_changed_at: Mapped[int | None] = mapped_column(
        BigInteger, default=None
    )
    email: Mapped[str | None] = mapped_column(String(128), default=None)

    # 只读接口用的 Key。鉴权比对的是 api_key_hash，明文不参与校验。
    api_key_hash: Mapped[str | None] = mapped_column(String(64), default=None,
                                                      unique=True)

    # API Key 的明文副本，只为「设置页能查看」而存在。
    #
    # 为什么要把明文也存下来（v2.0.1 起）：
    # 只存哈希时，用户一旦没在注册那一屏复制走，明文就永久丢失——
    # 设置页只能显示「（暂无）」。用户看到这个提示会以为 Key 丢了，
    # 于是去点「重新生成」，而重新生成必然改变 Key，
    # 博客前端立刻全线 401。**这个设计会把「看不到」逼成「Key 变了」**。
    #
    # 安全权衡：Key 是只读的（只能读到该用户自己的公开文章数据），
    # 泄露的影响面远小于写权限凭证。相比「用户被迫重置导致服务中断」，
    # 这个取舍更划算。真要更严，可改为用独立的 KEY_ENCRYPTION_SECRET
    # 做可逆加密——但不能复用 SECRET_KEY，否则换密钥时所有 Key 一起解不开。
    #
    # 存量用户该字段为 NULL：明文无从还原，需用「导入已有 Key」认领。
    api_key_plain: Mapped[str | None] = mapped_column(String(64), default=None)

    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # ---- 平台管理 ----
    # 管理员给这个账号写的备注（为什么禁用、工单号等）。
    # 只有管理员能看到，普通用户在设置页看不到这一列。
    admin_note: Mapped[str | None] = mapped_column(String(255), default=None)

    # 最后一次登录的来源 IP。
    #
    # 为什么要存：账号被批量注册、被撞库时，管理员需要看到「同一个 IP
    # 注册了几十个号」。只存最后一条，不建日志表——审计需求由一个
    # 独立的管理操作日志承担（见 2.1.0），这里只做快速定位。
    last_login_ip: Mapped[str | None] = mapped_column(String(64), default=None)

    # AI 摘要配置：用户可自带 Key 以摆脱平台额度限制
    ai_provider: Mapped[str | None] = mapped_column(String(32), default=None)
    ai_api_key: Mapped[str | None] = mapped_column(String(256), default=None)
    ai_model: Mapped[str | None] = mapped_column(String(128), default=None)
    ai_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    ai_daily_quota_override: Mapped[int | None] = mapped_column(
        Integer, default=None, comment="为空则用平台默认值"
    )

    # 每用户抓取并发上限，NULL 表示用平台默认值
    max_concurrent_override: Mapped[int | None] = mapped_column(Integer, default=None)

    # ---- 采集频率 ----
    # cron 表达式（5 段）。空表示用平台默认。
    #
    # 为什么存 cron 而不是「每隔 N 小时」：
    # 「每天 3 点」这种需求 cron 能表达，「每隔 90 分钟」不能。
    # 用户是博客主，让他说人话比让他做算术强。
    #
    # 文章采集：控制这个用户的站点多久抓一次
    crawl_cron: Mapped[str | None] = mapped_column(String(64), default=None)
    # 友链采集：控制这个用户的友链页多久检测一次
    link_page_cron: Mapped[str | None] = mapped_column(String(64), default=None)

    # 上次执行采集任务的时间。用来判断「到点了没」——
    # 规则是「最近一个 cron 触发点是否晚于这个时间」。
    last_crawl_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)
    last_link_page_scan_at: Mapped[datetime | None] = mapped_column(
        DateTime, default=None
    )

    last_login_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)

    sites: Mapped[list["Site"]] = relationship(  # noqa: F821
        back_populates="user", cascade="all, delete-orphan"
    )

    @property
    def has_own_ai_key(self) -> bool:
        return bool(self.ai_api_key)

    def quota_limit(self, default: int) -> int:
        # 显式判 None：写成 `override or default` 的话，
        # override=0 会被当成「没设置」而回落到 default。
        # 现在平台默认就是 0（无免费额度），这个区别很关键。
        if self.ai_daily_quota_override is None:
            return default
        return self.ai_daily_quota_override