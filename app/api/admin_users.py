"""平台管理：用户列表、账号状态、角色、额度、密码重置、删除。

这是 v2.0.0 多用户平台的核心。设计上有三条硬约束：

1. **一律走 ``AdminUser`` 依赖**
   普通登录用户拿到 403，未登录拿到 401。管理接口不能只靠「前端不显示入口」。

2. **跨租户查询必须显式豁免**
   认证依赖会把当前管理员 ID 写进租户上下文，之后所有 ``sites`` /
   ``posts`` 查询都会被自动追加 ``user_id = 管理员ID``。管理员要看的
   恰恰是**别人的**数据，所以每个跨用户查询都要包
   ``async with no_tenant_filter_async():`` 并自己写 ``user_id == ?``。
   漏掉的话，管理页会把「全站只有管理员自己」当成事实展示出来。

3. **不许把自己锁在门外**
   不能停用自己、不能撤销自己的管理员权限、不能删除自己。
   管理员只有一个时尤其危险——一次误点，平台就再也没有人能进管理页了。
"""

from __future__ import annotations

import logging
import time
from datetime import datetime

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, or_, select

from app.api.deps import AdminUser, DbSession
from app.core.security import hash_password
from app.core.tenant import no_tenant_filter_async
from app.models import (
    AIQuotaLog,
    Blacklist,
    CrawlLog,
    LinkPage,
    Post,
    Site,
    SiteRule,
    SiteTag,
    Tag,
    User,
)

router = APIRouter(prefix="/api/admin/users", tags=["平台管理"])
logger = logging.getLogger("ducircle.admin_users")


# ---- 输出模型 ----


class AdminUserOut(BaseModel):
    id: int
    username: str
    email: str | None
    is_active: bool
    is_admin: bool
    admin_note: str | None

    ai_enabled: bool
    ai_provider: str | None
    has_own_ai_key: bool
    ai_daily_quota_override: int | None
    max_concurrent_override: int | None

    # 资源占用。管理页用它判断「谁在用、谁只是占了个号」。
    site_count: int = 0
    post_count: int = 0
    summarized_count: int = 0

    last_login_at: datetime | None
    last_login_ip: str | None
    created_at: datetime
    password_changed_at: int | None


class AdminUserListOut(BaseModel):
    items: list[AdminUserOut]
    total: int
    limit: int
    offset: int


class AdminUserUpdate(BaseModel):
    is_active: bool | None = None
    is_admin: bool | None = None
    admin_note: str | None = Field(default=None, max_length=255)
    ai_enabled: bool | None = None
    ai_provider: str | None = Field(default=None, max_length=32)
    ai_daily_quota_override: int | None = Field(default=None, ge=0, le=1000)
    max_concurrent_override: int | None = Field(default=None, ge=1, le=50)


class PasswordResetIn(BaseModel):
    new_password: str = Field(min_length=8, max_length=128)


class PlatformOverviewOut(BaseModel):
    users_total: int
    users_active: int
    users_admin: int
    users_new_today: int
    sites_total: int
    posts_total: int
    posts_summarized: int
    sites_paused: int


# ---- 辅助 ----


async def _resource_usage(db, user_ids: list[int]) -> dict[int, tuple[int, int, int]]:
    """批量取每个用户的 (站点数, 文章数, 已摘要数)。

    一次 group by 拿完，不做 N+1——管理页一屏 50 行，
    逐行查三次就是 150 次往返。
    """
    if not user_ids:
        return {}

    usage: dict[int, list[int]] = {}
    async with no_tenant_filter_async():
        site_rows = (
            await db.execute(
                select(Site.user_id, func.count(Site.id))
                .where(Site.user_id.in_(user_ids), Site.deleted_at.is_(None))
                .group_by(Site.user_id)
            )
        ).all()
        post_rows = (
            await db.execute(
                select(Post.user_id, func.count(Post.id))
                .where(Post.user_id.in_(user_ids), Post.deleted_at.is_(None))
                .group_by(Post.user_id)
            )
        ).all()
        summarized_rows = (
            await db.execute(
                select(Post.user_id, func.count(Post.id))
                .where(
                    Post.user_id.in_(user_ids),
                    Post.deleted_at.is_(None),
                    Post.summary.is_not(None),
                )
                .group_by(Post.user_id)
            )
        ).all()

    for uid, count in site_rows:
        usage.setdefault(uid, [0, 0, 0])[0] = int(count)
    for uid, count in post_rows:
        usage.setdefault(uid, [0, 0, 0])[1] = int(count)
    for uid, count in summarized_rows:
        usage.setdefault(uid, [0, 0, 0])[2] = int(count)
    return {uid: tuple(values) for uid, values in usage.items()}  # type: ignore[misc]


def _to_out(user: User, usage: tuple[int, int, int]) -> AdminUserOut:
    return AdminUserOut(
        id=user.id,
        username=user.username,
        email=user.email,
        is_active=user.is_active,
        is_admin=user.is_admin,
        admin_note=user.admin_note,
        ai_enabled=user.ai_enabled,
        ai_provider=user.ai_provider,
        has_own_ai_key=user.has_own_ai_key,
        ai_daily_quota_override=user.ai_daily_quota_override,
        max_concurrent_override=user.max_concurrent_override,
        site_count=usage[0],
        post_count=usage[1],
        summarized_count=usage[2],
        last_login_at=user.last_login_at,
        last_login_ip=user.last_login_ip,
        created_at=user.created_at,
        password_changed_at=user.password_changed_at,
    )


async def _get_user_or_404(db, user_id: int) -> User:
    user = (
        await db.execute(select(User).where(User.id == user_id))
    ).scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=404, detail="用户不存在")
    return user


# ---- 接口 ----


@router.get("/overview", response_model=PlatformOverviewOut, summary="平台总览")
async def platform_overview(user: AdminUser, db: DbSession) -> PlatformOverviewOut:
    """全站数字。管理页顶部的统计卡片用它。"""
    today_start = datetime.combine(datetime.now().date(), datetime.min.time())

    users_total = (await db.execute(select(func.count(User.id)))).scalar_one()
    users_active = (
        await db.execute(
            select(func.count(User.id)).where(User.is_active.is_(True))
        )
    ).scalar_one()
    users_admin = (
        await db.execute(
            select(func.count(User.id)).where(User.is_admin.is_(True))
        )
    ).scalar_one()
    users_new_today = (
        await db.execute(
            select(func.count(User.id)).where(User.created_at >= today_start)
        )
    ).scalar_one()

    # 下面的统计跨租户，必须豁免过滤，否则只会统计到管理员自己
    async with no_tenant_filter_async():
        sites_total = (
            await db.execute(
                select(func.count(Site.id)).where(Site.deleted_at.is_(None))
            )
        ).scalar_one()
        sites_paused = (
            await db.execute(
                select(func.count(Site.id)).where(
                    Site.deleted_at.is_(None), Site.paused.is_(True)
                )
            )
        ).scalar_one()
        posts_total = (
            await db.execute(
                select(func.count(Post.id)).where(Post.deleted_at.is_(None))
            )
        ).scalar_one()
        posts_summarized = (
            await db.execute(
                select(func.count(Post.id)).where(
                    Post.deleted_at.is_(None), Post.summary.is_not(None)
                )
            )
        ).scalar_one()

    return PlatformOverviewOut(
        users_total=int(users_total),
        users_active=int(users_active),
        users_admin=int(users_admin),
        users_new_today=int(users_new_today),
        sites_total=int(sites_total),
        posts_total=int(posts_total),
        posts_summarized=int(posts_summarized),
        sites_paused=int(sites_paused),
    )


@router.get("", response_model=AdminUserListOut, summary="用户列表")
async def list_users(
    user: AdminUser,
    db: DbSession,
    search: str | None = Query(default=None, description="按用户名或邮箱搜索"),
    status_filter: str = Query(
        default="all",
        alias="status",
        pattern="^(all|active|disabled)$",
        description="账号状态",
    ),
    role: str = Query(
        default="all", pattern="^(all|admin|user)$", description="角色"
    ),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> AdminUserListOut:
    """分页列出所有用户。

    ``users`` 表本身不在租户隔离范围内（否则连登录都做不到），
    所以这里的查询天然是跨用户的，不需要额外豁免。
    """
    return await query_users(
        db,
        search=search,
        status_filter=status_filter,
        role=role,
        limit=limit,
        offset=offset,
    )


async def query_users(
    db,
    *,
    search: str | None = None,
    status_filter: str = "all",
    role: str = "all",
    limit: int = 50,
    offset: int = 0,
) -> AdminUserListOut:
    """用户列表的查询本体。

    与路由分离，供后台页面直接调用——页面渲染和 JSON 接口
    必须用同一套筛选逻辑，否则「搜出来的」和「导出/翻页的」会不一致。
    """
    conditions = []
    if search:
        keyword = f"%{search.strip()}%"
        conditions.append(
            or_(User.username.like(keyword), User.email.like(keyword))
        )
    if status_filter == "active":
        conditions.append(User.is_active.is_(True))
    elif status_filter == "disabled":
        conditions.append(User.is_active.is_(False))
    if role == "admin":
        conditions.append(User.is_admin.is_(True))
    elif role == "user":
        conditions.append(User.is_admin.is_(False))

    count_stmt = select(func.count(User.id))
    if conditions:
        count_stmt = count_stmt.where(*conditions)
    total = (await db.execute(count_stmt)).scalar_one()

    stmt = select(User)
    if conditions:
        stmt = stmt.where(*conditions)
    # 管理员排前面，其余按注册时间倒序——新号在顶部，
    # 批量注册的异常账号一眼就能看到。
    rows = (
        await db.execute(
            stmt.order_by(User.is_admin.desc(), User.id.desc())
            .limit(limit)
            .offset(offset)
        )
    ).scalars().all()

    usage = await _resource_usage(db, [u.id for u in rows])
    return AdminUserListOut(
        items=[_to_out(u, usage.get(u.id, (0, 0, 0))) for u in rows],
        total=int(total),
        limit=limit,
        offset=offset,
    )


@router.get("/{user_id}", response_model=AdminUserOut, summary="用户详情")
async def get_user(
    user_id: int, user: AdminUser, db: DbSession
) -> AdminUserOut:
    target = await _get_user_or_404(db, user_id)
    usage = await _resource_usage(db, [target.id])
    return _to_out(target, usage.get(target.id, (0, 0, 0)))


@router.patch("/{user_id}", response_model=AdminUserOut, summary="修改用户")
async def update_user(
    user_id: int,
    payload: AdminUserUpdate,
    user: AdminUser,
    db: DbSession,
) -> AdminUserOut:
    """改账号状态、角色、额度、备注。

    对自己操作时**禁止**停用或撤销管理员——这是唯一可能让平台
    「再也进不去管理页」的路径，必须在服务端拦住，不能只靠前端隐藏按钮。
    """
    target = await _get_user_or_404(db, user_id)
    data = payload.model_dump(exclude_unset=True)

    if target.id == user.id:
        if data.get("is_active") is False:
            raise HTTPException(status_code=400, detail="不能停用自己的账号")
        if data.get("is_admin") is False:
            raise HTTPException(status_code=400, detail="不能撤销自己的管理员权限")

    # 空字符串等价于「清空备注」
    if "admin_note" in data and data["admin_note"] is not None:
        data["admin_note"] = data["admin_note"].strip() or None

    for field, value in data.items():
        setattr(target, field, value)

    await db.flush()
    await db.commit()

    logger.info(
        "管理员 %s 修改了用户 %s：%s",
        user.username,
        target.username,
        ", ".join(f"{k}={v}" for k, v in data.items()) or "无变更",
    )

    usage = await _resource_usage(db, [target.id])
    return _to_out(target, usage.get(target.id, (0, 0, 0)))


@router.post(
    "/{user_id}/password", status_code=status.HTTP_200_OK, summary="重置用户密码"
)
async def reset_user_password(
    user_id: int,
    payload: PasswordResetIn,
    user: AdminUser,
    db: DbSession,
) -> dict:
    """管理员替用户重设密码。

    改完把 ``password_changed_at`` 推到当前时刻——
    该用户在所有设备上的旧会话立即失效，必须用新密码重新登录。
    不做这一步的话，「重置密码」只是改了个字符串，
    别人手里的旧 Cookie 还能继续用一周。
    """
    target = await _get_user_or_404(db, user_id)

    target.password_hash = hash_password(payload.new_password)
    target.password_changed_at = int(time.time())
    await db.flush()
    await db.commit()

    logger.info(
        "管理员 %s 重置了用户 %s 的密码", user.username, target.username
    )
    return {
        "ok": True,
        "message": f"已重置 {target.username} 的密码，该用户需要重新登录",
    }


@router.delete("/{user_id}", summary="删除用户")
async def delete_user(
    user_id: int,
    user: AdminUser,
    db: DbSession,
    confirm: bool = Query(default=False, description="必须传 true，防误触"),
) -> dict:
    """彻底删除账号及其全部数据。

    删除是**物理删除**，不可恢复。按依赖顺序手工清理：
    ``User.sites`` 虽然配了 ``cascade="all, delete-orphan"``，
    但在 async 会话里 ``await db.delete(user)`` 会触发惰性加载，
    抛 ``MissingGreenlet``——所以这里显式逐表删。
    """
    if not confirm:
        raise HTTPException(status_code=400, detail="需要 confirm=true 参数确认")

    target = await _get_user_or_404(db, user_id)
    if target.id == user.id:
        raise HTTPException(status_code=400, detail="不能删除自己的账号")

    username = target.username
    deleted_posts = 0

    async with no_tenant_filter_async():
        deleted_posts = (
            await db.execute(
                select(func.count(Post.id)).where(Post.user_id == user_id)
            )
        ).scalar_one()

        # 子表先删：site_tags 引用 tags 与 sites，site_rules 引用 sites
        await db.execute(delete(SiteTag).where(SiteTag.user_id == user_id))
        await db.execute(delete(SiteRule).where(SiteRule.user_id == user_id))
        await db.execute(delete(AIQuotaLog).where(AIQuotaLog.user_id == user_id))
        await db.execute(delete(CrawlLog).where(CrawlLog.user_id == user_id))
        await db.execute(delete(Post).where(Post.user_id == user_id))
        await db.execute(delete(Tag).where(Tag.user_id == user_id))
        await db.execute(delete(Blacklist).where(Blacklist.user_id == user_id))
        await db.execute(delete(LinkPage).where(LinkPage.user_id == user_id))
        await db.execute(delete(Site).where(Site.user_id == user_id))
        await db.execute(delete(User).where(User.id == user_id))

    await db.commit()

    logger.warning(
        "管理员 %s 删除了用户 %s（连带 %d 篇文章）",
        user.username,
        username,
        int(deleted_posts),
    )
    return {
        "ok": True,
        "deleted_user": username,
        "deleted_posts": int(deleted_posts),
    }
