"""在真实 MySQL 上验证注册与时间字段行为。

这个文件**不会**在常规 pytest 运行里执行（需要真实 MySQL），
但它是本项目最有价值的测试之一——因为它抓到过一个只在
MySQL 上出现的 500。

背景：
`TimestampMixin` 最早只用 `server_default`。那样的话，
flush 之后要拿到 created_at 必须回数据库查一次。
SQLite 支持 RETURNING，能直接拿到；MySQL 不支持，
只能 SELECT 回读，而那个隐式 SELECT 在 async 上下文里抛
MissingGreenlet —— 注册接口直接 500。

本地 SQLite 测试永远发现不了这个问题。

运行方式：
    ./scripts/verify_mysql.sh
"""

from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select, text  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def record(name: str, passed: bool, detail: str = "") -> None:
    RESULTS.append((name, passed, detail))
    print(f"  [{'OK  ' if passed else 'FAIL'}] {name}" + (f" -- {detail}" if detail else ""))


async def main() -> None:
    url = (
        f"mysql+asyncmy://{os.environ.get('MYSQL_USER', 'root')}"
        f":{os.environ.get('MYSQL_PASSWORD', '')}"
        f"@{os.environ.get('MYSQL_HOST', '127.0.0.1')}"
        f":{os.environ.get('MYSQL_PORT', '3399')}"
        f"/{os.environ.get('MYSQL_DATABASE', 'fcircle_mig')}"
        "?charset=utf8mb4"
    )

    engine = create_async_engine(url, echo=False)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    from app.core.security import hash_password
    from app.models import User

    async with factory() as db:
        await db.execute(User.__table__.delete())
        await db.commit()
        print(f"  MySQL: {(await db.execute(text('SELECT VERSION()'))).scalar_one()}")

    # ---- 1. flush 后能直接拿到 created_at（不触发隐式 SELECT）----
    async with factory() as db:
        user = User(
            username="flush_test",
            password_hash=hash_password("x" * 12),
        )
        db.add(user)
        await db.flush()

        # 关键断言：这里如果触发隐式 SELECT，就会抛 MissingGreenlet
        try:
            created = user.created_at
            ok = isinstance(created, datetime)
            record(
                "flush 后直接读 created_at 不触发隐式查询",
                ok,
                f"值={created}",
            )
        except Exception as exc:  # noqa: BLE001
            record("flush 后直接读 created_at 不触发隐式查询", False, f"{type(exc).__name__}: {exc}")

        await db.commit()

    # ---- 2. flush 后能读 updated_at ----
    async with factory() as db:
        user = User(
            username="flush_test2",
            password_hash=hash_password("x" * 12),
        )
        db.add(user)
        await db.flush()
        try:
            updated = user.updated_at
            record("flush 后直接读 updated_at", isinstance(updated, datetime), f"值={updated}")
        except Exception as exc:  # noqa: BLE001
            record("flush 后直接读 updated_at", False, f"{type(exc).__name__}: {exc}")
        await db.commit()

    # ---- 3. 库里的值和 Python 侧一致 ----
    async with factory() as db:
        row = (await db.execute(
            select(User).where(User.username == "flush_test")
        )).scalar_one()
        record(
            "库里的 created_at 已填充",
            isinstance(row.created_at, datetime) and row.created_at is not None,
            f"值={row.created_at}",
        )

    # ---- 4. UPDATE 时 updated_at 会变 ----
    # 注意：before 必须存成副本。
    # expire_on_commit=False 时 session 里的对象会被就地更新，
    # 持有引用再比较，永远相等——测试会假失败。
    async with factory() as db:
        row = (await db.execute(
            select(User).where(User.username == "flush_test")
        )).scalar_one()
        before = row.updated_at
        row.email = "changed@example.com"
        await db.commit()

    async with factory() as db:
        row = (await db.execute(
            select(User).where(User.username == "flush_test")
        )).scalar_one()
        # 不能用 != 判断：MySQL DATETIME 精度是秒，
        # 同一秒内做的更新，前后值本来就相等。
        # 正确做法是等一秒再更新，或者检查它不早于 before。
        not_older = row.updated_at >= before
        record(
            "UPDATE 时 updated_at 会刷新（不早于原值）",
            not_older,
            f"前={before} 后={row.updated_at}",
        )

    # 明确验证「跨秒更新后确实变了」
    async with factory() as db:
        row = (await db.execute(
            select(User).where(User.username == "flush_test")
        )).scalar_one()
        before = row.updated_at
        # MySQL DATETIME 精度是秒，等过去再更新才能看出差别
        await asyncio.sleep(1.1)
        row.email = "changed2@example.com"
        await db.commit()

    async with factory() as db:
        row = (await db.execute(
            select(User).where(User.username == "flush_test")
        )).scalar_one()
        record(
            "跨秒 UPDATE 后 updated_at 确实变化",
            row.updated_at > before,
            f"前={before} 后={row.updated_at}",
        )

    # ---- 5. 直接 INSERT（绕过 ORM）也能填时间（server_default 生效）----
    from sqlalchemy import insert

    async with factory() as db:
        await db.execute(
            insert(User.__table__).values(
                username="raw_insert",
                password_hash="raw",
            )
        )
        await db.commit()

    async with factory() as db:
        row = (await db.execute(
            select(User).where(User.username == "raw_insert")
        )).scalar_one()
        record(
            "绕过 ORM 直接 INSERT 时 server_default 生效",
            isinstance(row.created_at, datetime),
            f"值={row.created_at}",
        )

    # ---- 6. 注册接口完整流程（模拟 auth.register 的核心）----
    async with factory() as db:
        from app.api.auth import RegisterRequest

        payload = RegisterRequest(username="full_register", password="testpass123")
        user = User(
            username=payload.username,
            password_hash=hash_password(payload.password),
        )
        db.add(user)
        await db.flush()
        try:
            result = {
                "id": user.id,
                "username": user.username,
                "created_at": user.created_at,
                "is_admin": user.is_admin,
            }
            record(
                "注册流程完整走通",
                result["id"] is not None and result["created_at"] is not None,
                f"id={result['id']} created_at={result['created_at']}",
            )
        except Exception as exc:  # noqa: BLE001
            record("注册流程完整走通", False, f"{type(exc).__name__}: {exc}")
        await db.commit()

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    total = len(RESULTS)
    print("")
    print(f"  通过 {passed}/{total}")
    if passed < total:
        print("")
        for name, ok, detail in RESULTS:
            if not ok:
                print(f"    - {name}: {detail}")

    await engine.dispose()
    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    asyncio.run(main())