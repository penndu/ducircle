"""兑换码：管理员批量生成，用户兑换成平台 AI 摘要次数。

**为什么是「预付次数」而不是「按天额度」**

按天额度是平台白送的东西，今天不用就作废，用户没有损失感。
兑换码不一样——用户为它付过钱，换来的必须是**能累加的余额**：
今天没用完的明天还在，买两张就叠加。所以余额落在 ``users`` 上
（``ai_summary_credits``），而不是落在某个带日期的表里。

**两张表各管一件事**

- ``redeem_batches``：一次「批量生成」动作的台账。发票、对账、
  「这批 100 张卖出去多少张」都靠它。
- ``redeem_codes``：每张码的个体状态。一张码只能被一个账号用一次，
  状态由 ``used_by`` / ``revoked_at`` 两个字段表达。

**删除用户不能丢码**

``used_by`` / ``created_by`` 都是 ``ondelete=SET NULL`` 而不是 CASCADE：
码是**钱换来的凭证**，用户注销、管理员离职都不该让兑换记录蒸发。
同时冗余保存 ``used_by_name`` / ``created_by_name``，行没了也能追溯。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class RedeemBatch(Base, TimestampMixin):
    """一次批量生成动作。"""

    __tablename__ = "redeem_batches"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # 人可读的批次号，形如 20261116-4F7K。对账时比 id 好用得多。
    batch_no: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)

    # trial / standard，取值见 app.services.redeem.PLANS
    plan: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    # 每张码能兑换的摘要次数（生成时快照，之后改价不影响已生成的码）
    credits: Mapped[int] = mapped_column(Integer, nullable=False)
    # 每张码的定价（单位：分）。用整数分而不是浮点元。
    price_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    # 这一批生成了几张
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)

    note: Mapped[str | None] = mapped_column(String(255), default=None)

    # 生成者。管理员注销后置 NULL，靠 created_by_name 追溯。
    created_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), default=None
    )
    created_by_name: Mapped[str | None] = mapped_column(String(32), default=None)

    # 导出过没有。仅用于界面提示，不参与业务判断。
    exported: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class RedeemCode(Base, TimestampMixin):
    """单个兑换码。"""

    __tablename__ = "redeem_codes"
    __table_args__ = (
        # 码的查询永远带着唯一值或批次，两条索引够用
        Index("ix_redeem_codes_batch", "batch_id"),
        Index("ix_redeem_codes_used_by", "used_by"),
        Index("ix_redeem_codes_state", "used_by", "revoked_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # 兑换码本体。固定长度、只含大写字母与数字（去掉了 0/O/1/I/L 等易混字符），
    # 索引长度 32*4=128 字节，远低于 MySQL utf8mb4 的 3072 上限。
    code: Mapped[str] = mapped_column(
        String(32), unique=True, nullable=False, index=True
    )

    batch_id: Mapped[int | None] = mapped_column(
        ForeignKey("redeem_batches.id", ondelete="SET NULL"), default=None
    )

    # 同样在生成时快照：以后调价，已发出去的码还是它当初承诺的面值。
    plan: Mapped[str] = mapped_column(String(16), nullable=False)
    credits: Mapped[int] = mapped_column(Integer, nullable=False)
    price_cents: Mapped[int] = mapped_column(Integer, nullable=False)

    # 谁用的。已使用与已作废互斥，靠业务逻辑保证。
    used_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), default=None
    )
    used_by_name: Mapped[str | None] = mapped_column(String(32), default=None)
    used_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)

    revoked_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)
    revoked_by_name: Mapped[str | None] = mapped_column(String(32), default=None)
    revoke_reason: Mapped[str | None] = mapped_column(String(255), default=None)

    note: Mapped[str | None] = mapped_column(String(255), default=None)

    @property
    def status(self) -> str:
        """unused / used / revoked。界面按它上色。"""
        if self.revoked_at is not None:
            return "revoked"
        if self.used_by is not None:
            return "used"
        return "unused"
