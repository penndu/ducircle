"""兑换码业务：定价目录、批量生成、兑换、作废。

**码的形态**

``FC2-XXXX-XXXX-XXXX-XXXX``（体验版）与 ``FC10-...``（标准版），
四组四位、连字符分隔——方便口头念、方便手抄，也方便一眼看出面值。

随机部分的字母表刻意剔除了 ``0 O 1 I L`` 这类易混字符：
码是要发给人、贴进聊天窗口、手抄到别处的，抄错的代价是用户以为码是假的。
每组 4 位、共 16 位，来自 30 个字符 → 约 78 bit 熵，
暴力枚举不可行，不需要额外的猜测次数限制。

**为什么兑换要做成条件更新**

「查一下有没有被用过，然后写 used_by」在并发下是典型的 TOCTOU：
两个请求同时查到「没用过」，两张都发出去，一份钱发两份货。
所以真正把码标记为已用的动作是一句**带条件的 UPDATE**，
靠 ``rowcount == 1`` 判断谁抢到了——这个判断由数据库保证原子性。
"""

from __future__ import annotations

import logging
import secrets
import string
from datetime import datetime

from sqlalchemy import case, func, select, update

from app.models import RedeemBatch, RedeemCode, User

logger = logging.getLogger("ducircle.redeem")

# 单次批量生成的上限。够用（一批 500 张），也拦得住手滑多打一个 0。
MAX_BATCH_QUANTITY = 500

# 定价目录。改价只影响**之后**生成的码——码上的面值在生成时已快照，
# 已发出去的码承诺多少就是多少。
PLANS: dict[str, dict] = {
    "trial": {
        "key": "trial",
        "label": "体验版",
        "credits": 100,
        "price_cents": 200,
        "summary": "2 元 · 100 次 AI 摘要",
    },
    "standard": {
        "key": "standard",
        "label": "标准版",
        "credits": 1000,
        "price_cents": 1000,
        "summary": "10 元 · 1000 次 AI 摘要",
    },
}

PLAN_ORDER: tuple[str, ...] = ("trial", "standard")

# 前缀带上价格，肉眼就能认出面值
_CODE_PREFIX: dict[str, str] = {"trial": "FC2", "standard": "FC10"}

# 去掉 0 O 1 I L：手抄、口述、截图辨认都不容易错
CODE_ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"


class RedeemError(Exception):
    """兑换失败。消息直接给用户看，所以措辞要说人话。"""


def plan_catalog() -> list[dict]:
    """给模板和接口用的定价目录（按固定顺序）。"""
    return [dict(PLANS[key]) for key in PLAN_ORDER]


def price_yuan(cents: int) -> str:
    """分 → 元的展示串。整数元不显示小数点。"""
    if cents % 100 == 0:
        return f"{cents // 100}"
    return f"{cents / 100:.2f}"


def _random_group(length: int = 4) -> str:
    return "".join(secrets.choice(CODE_ALPHABET) for _ in range(length))


def generate_code(plan: str) -> str:
    prefix = _CODE_PREFIX.get(plan)
    if prefix is None:
        raise ValueError(f"未知的兑换码类型：{plan}")
    return prefix + "-" + "-".join(_random_group() for _ in range(4))


def normalize_code(raw: str) -> str:
    """把用户输入的码规范化。

    用户会：全小写粘贴、带前后空格、把连字符漏掉、从截图里连字符识别成空格。
    这些都认，别让格式问题变成「你的码是假的」。
    """
    text = (raw or "").strip().upper().replace(" ", "").replace("\u3000", "")
    if not text:
        return ""
    if "-" in text:
        return text
    # 整段粘贴（没连字符）：按前缀长度把连字符重新插回去
    for prefix in ("FC10", "FC2"):
        body_len = len(text) - len(prefix)
        if text.startswith(prefix) and body_len == 16:
            body = text[len(prefix):]
            return prefix + "-" + "-".join(
                body[i:i + 4] for i in range(0, 16, 4)
            )
    return text


def _new_batch_no() -> str:
    """批次号：日期 + 4 位随机。对账时人眼比 id 好认。"""
    return datetime.now().strftime("%Y%m%d") + "-" + _random_group(4)


async def create_batch(
    db,
    *,
    admin: User,
    plan: str,
    quantity: int,
    note: str | None = None,
) -> tuple[RedeemBatch, list[RedeemCode]]:
    """批量生成兑换码。返回 (批次, 码列表)。

    面值在这里**快照**进每一张码：以后调价，已生成的码不受影响。
    """
    spec = PLANS.get(plan)
    if spec is None:
        raise ValueError(f"未知的兑换码类型：{plan}")
    if not 1 <= quantity <= MAX_BATCH_QUANTITY:
        raise ValueError(f"一次最多生成 {MAX_BATCH_QUANTITY} 张")

    # 批次号撞车概率极小，但撞了就重试，不值得为此让管理员看到报错
    batch_no = _new_batch_no()
    for _ in range(5):
        exists = (
            await db.execute(
                select(RedeemBatch.id).where(RedeemBatch.batch_no == batch_no)
            )
        ).first()
        if not exists:
            break
        batch_no = _new_batch_no()

    batch = RedeemBatch(
        batch_no=batch_no,
        plan=plan,
        credits=spec["credits"],
        price_cents=spec["price_cents"],
        quantity=quantity,
        note=note,
        created_by=admin.id,
        created_by_name=admin.username,
    )
    db.add(batch)
    await db.flush()

    seen: set[str] = set()
    codes: list[RedeemCode] = []
    while len(codes) < quantity:
        code = generate_code(plan)
        if code in seen:
            continue
        seen.add(code)
        codes.append(
            RedeemCode(
                code=code,
                batch_id=batch.id,
                plan=plan,
                credits=spec["credits"],
                price_cents=spec["price_cents"],
                note=note,
            )
        )
    db.add_all(codes)
    await db.flush()

    logger.info(
        "管理员 %s 生成兑换码批次 %s：%s ×%d（面值 %s 元）",
        admin.username,
        batch_no,
        spec["label"],
        quantity,
        price_yuan(spec["price_cents"] * quantity),
    )
    return batch, codes


async def redeem_code(db, *, user: User, raw_code: str) -> dict:
    """兑换一张码，次数直接加到用户余额上。

    失败一律抛 ``RedeemError``，消息面向用户。
    """
    code = normalize_code(raw_code)
    if not code:
        raise RedeemError("请输入兑换码")

    row = (
        await db.execute(select(RedeemCode).where(RedeemCode.code == code))
    ).scalar_one_or_none()

    if row is None:
        # 不区分「格式不对」和「码不存在」，避免被拿来探测有效码
        raise RedeemError("兑换码无效，请核对后重试")
    if row.revoked_at is not None:
        raise RedeemError("这个兑换码已被作废，请联系管理员")
    if row.used_by is not None:
        raise RedeemError("这个兑换码已经被使用过了")

    # 先取出需要的标量，后面要用条件 UPDATE 抢占，ORM 对象会失同步
    code_id = row.id
    plan = row.plan
    credits = row.credits
    used_at = datetime.now()

    result = await db.execute(
        update(RedeemCode)
        .where(
            RedeemCode.id == code_id,
            RedeemCode.used_by.is_(None),
            RedeemCode.revoked_at.is_(None),
        )
        .values(used_by=user.id, used_by_name=user.username, used_at=used_at)
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        # 有人抢先一步——同一个码只能被一个账号用一次
        raise RedeemError("这个兑换码已经被使用过了")

    user.ai_summary_credits = (user.ai_summary_credits or 0) + credits
    await db.flush()

    logger.info(
        "用户 %s 兑换码 %s 成功，+%d 次（余额 %d）",
        user.username,
        code,
        credits,
        user.ai_summary_credits,
    )
    return {
        "ok": True,
        "code": code,
        "plan": plan,
        "plan_label": PLANS.get(plan, {}).get("label", plan),
        "credits_added": credits,
        "credits": user.ai_summary_credits,
    }


async def revoke_code(
    db, *, code_id: int, admin: User, reason: str | None = None
) -> RedeemCode:
    """作废一张还没被使用的码。

    已使用的码不能作废——那等于用户付了钱又被收回权益。
    要处理这类情况得走退款流程（人工），不是点一下按钮。
    """
    row = await db.get(RedeemCode, code_id)
    if row is None:
        raise RedeemError("兑换码不存在")
    if row.used_by is not None:
        raise RedeemError("这张码已经被使用，不能作废")
    if row.revoked_at is not None:
        raise RedeemError("这张码已经作废过了")

    result = await db.execute(
        update(RedeemCode)
        .where(
            RedeemCode.id == code_id,
            RedeemCode.used_by.is_(None),
            RedeemCode.revoked_at.is_(None),
        )
        .values(
            revoked_at=datetime.now(),
            revoked_by_name=admin.username,
            revoke_reason=reason,
        )
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        raise RedeemError("这张码已经被使用，不能作废")

    await db.flush()
    # row 是条件 UPDATE **之前**载入的对象，字段还是旧的；
    # 不 refresh 的话接口会把「未使用」返回给管理员，界面上看着像作废没生效。
    await db.refresh(row)
    logger.info("管理员 %s 作废兑换码 id=%d", admin.username, code_id)
    return row


async def batch_stats(db, batch_ids: list[int]) -> dict[int, dict]:
    """批量统计每个批次的使用情况。一次 group by 拿完，不做 N+1。"""
    if not batch_ids:
        return {}

    rows = (
        await db.execute(
            select(
                RedeemCode.batch_id,
                func.count(RedeemCode.id),
                func.sum(case((RedeemCode.used_by.is_not(None), 1), else_=0)),
                func.sum(case((RedeemCode.revoked_at.is_not(None), 1), else_=0)),
            )
            .where(RedeemCode.batch_id.in_(batch_ids))
            .group_by(RedeemCode.batch_id)
        )
    ).all()

    stats: dict[int, dict] = {}
    for batch_id, total, used, revoked in rows:
        stats[int(batch_id)] = {
            "total": int(total or 0),
            "used": int(used or 0),
            "revoked": int(revoked or 0),
            "unused": int(total or 0) - int(used or 0) - int(revoked or 0),
        }
    return stats


async def my_codes(db, user_id: int, limit: int = 50) -> list[RedeemCode]:
    """我兑换过的码。设置页/兑换页展示用。"""
    return (
        await db.execute(
            select(RedeemCode)
            .where(RedeemCode.used_by == user_id)
            .order_by(RedeemCode.used_at.desc())
            .limit(limit)
        )
    ).scalars().all()


__all__ = [
    "CODE_ALPHABET",
    "MAX_BATCH_QUANTITY",
    "PLANS",
    "PLAN_ORDER",
    "RedeemError",
    "batch_stats",
    "create_batch",
    "generate_code",
    "my_codes",
    "normalize_code",
    "plan_catalog",
    "price_yuan",
    "redeem_code",
    "revoke_code",
]
