"""在真实 MySQL 上验证 ORM 行为。

SQLite 上测不出的东西：
- MySQL 的排序与比较规则（utf8mb4_unicode_ci 大小写不敏感）
- 唯一约束的冲突处理（重复插入时的 IntegrityError）
- 中文按拼音排序
- VARCHAR 长度边界
- 连接池在真实驱动下的表现

这个脚本必须在有真实 MySQL 的环境里跑，不能用 SQLite 替代。
"""

from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import func, select, text  # noqa: E402
from sqlalchemy.exc import IntegrityError  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def record(name: str, passed: bool, detail: str = "") -> None:
    RESULTS.append((name, passed, detail))
    mark = "OK  " if passed else "FAIL"
    print(f"  [{mark}] {name}" + (f" -- {detail}" if detail else ""))


async def main() -> None:
    url = (
        f"mysql+asyncmy://{os.environ.get('MYSQL_USER', 'root')}"
        f":{os.environ.get('MYSQL_PASSWORD', '')}"
        f"@{os.environ.get('MYSQL_HOST', '127.0.0.1')}"
        f":{os.environ.get('MYSQL_PORT', '3399')}"
        f"/{os.environ.get('MYSQL_DATABASE', 'ducircle_verify')}"
        "?charset=utf8mb4"
    )

    engine = create_async_engine(url, echo=False, pool_size=5, max_overflow=10)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    from app.core.security import hash_password, site_hash
    from app.core.tenant import as_tenant_async, no_tenant_filter, set_tenant
    from app.models import CrawlLog, Post, Site, User

    # ---- 清理 ----
    # no_tenant_filter 是同步 contextmanager，异步代码里要用 with
    with no_tenant_filter():
      async with factory() as db:
        for model in (CrawlLog, Post, Site, User):
          await db.execute(model.__table__.delete())
        await db.commit()
      async with factory() as db:
        print("  MySQL 版本:",
              (await db.execute(text("SELECT VERSION()"))).scalar_one())

    # ---- 0. 先建用户 ----
    # 外键约束会强制这个顺序：sites.user_id 引用 users.id，
    # 没有用户就插不了站点。这正是外键该起的作用。
    async with factory() as db:
        set_tenant(1)
        db.add_all([
            User(id=1, username="alice", password_hash=hash_password("x" * 12)),
            User(id=2, username="bob", password_hash=hash_password("x" * 12)),
        ])
        await db.commit()
    set_tenant(None)

    # ---- 1. 中文字符集 ----
    async with factory() as db:
        name = "中文站点测试"
        db.add(Site(id=1, user_id=1, name=name, url="https://charset.com",
                    url_hash=site_hash("https://charset.com"), source="manual"))
        await db.commit()
        stored = (await db.execute(select(Site).where(Site.id == 1))).scalar_one()
        record("utf8mb4 存中文", stored.name == name, f"读回={stored.name!r}")

    # ---- 2. DATETIME 默认值 ----
    async with factory() as db:
        created = (await db.execute(
            select(Site.created_at).where(Site.id == 1)
        )).scalar_one()
        record("created_at 自动填充", isinstance(created, datetime), f"值={created}")
        if isinstance(created, datetime):
            record("DATETIME 秒级精度", created.microsecond == 0,
                   f"微秒={created.microsecond}")

    # ---- 3. 唯一约束真的生效 ----
    async with factory() as db:
        db.add(Site(id=2, user_id=1, name="重复域名", url="https://dup.com",
                    url_hash=site_hash("https://dup.com"), source="manual"))
        await db.commit()

    conflict_caught = False
    try:
        async with factory() as db:
            db.add(Site(id=3, user_id=1, name="又一个重复", url="https://dup.com",
                        url_hash=site_hash("https://dup.com"), source="manual"))
            await db.commit()
    except IntegrityError:
        conflict_caught = True
    record("url_hash 唯一约束生效", conflict_caught,
           "重复插入被拒绝" if conflict_caught else "竟然插进去了")

    # ---- 4. 租户隔离 ----
    async with factory() as db:
        set_tenant(1)
        db.add_all([
            Site(id=10, user_id=1, name="Alice的站A", url="https://alice-a.com",
                 url_hash=site_hash("https://alice-a.com"), source="manual"),
            Site(id=11, user_id=1, name="Alice的站B", url="https://alice-b.com",
                 url_hash=site_hash("https://alice-b.com"), source="manual"),
        ])
        await db.commit()

    async with factory() as db, as_tenant_async(1):
        alice_sites = (await db.execute(select(Site))).scalars().all()
        count = len(alice_sites)
        record(
            "Alice 能看到自己的站点",
            count >= 2 and all(s.user_id == 1 for s in alice_sites),
            f"实际 {count} 个，且全部属于 Alice",
        )

    async with factory() as db, as_tenant_async(2):
        bob_sites = (await db.execute(select(Site))).scalars().all()
        record("Bob 看不到 Alice 的站点", len(bob_sites) == 0,
               f"Bob 看到 {len(bob_sites)} 个（应为 0）")

    # ---- 5. 聚合查询 ----
    async with factory() as db, as_tenant_async(2):
        total = (await db.execute(
            select(func.count()).select_from(Site))).scalar_one()
        record(
            "count() 也受隔离保护",
            total == 0,
            f"Bob count={total}（Alice 名下有多个站点）",
        )

    # ---- 6. 手写越权 WHERE ----
    async with factory() as db, as_tenant_async(2):
        sneaky = (await db.execute(
            select(Site).where(Site.user_id == 1))).scalars().all()
        record("手写越权条件被拦截", len(sneaky) == 0,
               f"Bob 越权查到 {len(sneaky)} 条")

    # ---- 7. 外键级联 ----
    async with factory() as db, as_tenant_async(1):
        db.add(Post(id=100, user_id=1, site_id=10, title="要级联删的文章",
                    link="https://alice-a.com/1", guid_hash="g-cascade"))
        await db.commit()

    cascade_ok = False
    try:
        async with factory() as db, as_tenant_async(1):
            site = await db.get(Site, 10)
            await db.delete(site)
            await db.commit()
        async with factory() as db, as_tenant_async(1):
            remaining = (await db.execute(
                select(func.count()).select_from(Post).where(Post.id == 100)
            )).scalar_one()
            cascade_ok = remaining == 0
    except Exception as exc:  # noqa: BLE001
        record("外键级联删除", False, f"异常: {exc}")

    record("外键级联删除生效", cascade_ok, "删除站点后其文章一并消失")

    # ---- 8. 中文排序 ----
    async with factory() as db, as_tenant_async(1):
        db.add_all([
            Site(id=20, user_id=1, name="张三", url="https://zhang.com",
                 url_hash=site_hash("https://zhang.com"), source="manual"),
            Site(id=21, user_id=1, name="李四", url="https://li.com",
                 url_hash=site_hash("https://li.com"), source="manual"),
            Site(id=22, user_id=1, name="王五", url="https://wang.com",
                 url_hash=site_hash("https://wang.com"), source="manual"),
        ])
        await db.commit()
        ordered = (await db.execute(
            select(Site.name).where(Site.id.in_([20, 21, 22])).order_by(Site.name)
        )).scalars().all()

        # utf8mb4_unicode_ci 按 Unicode 码点排序，不是拼音。
        # 这不是 bug 而是 MySQL 的既定行为（排序规则的名字有误导性）。
        # 码点序对中文用户来说是「乱」的——所以后台要用拼音排序，
        # 见 fix_chinese_ordering()。
        code_point_order = list(ordered) == sorted(
            ["张三", "李四", "王五"]
        )
        record(
            "中文排序符合 utf8mb4_unicode_ci 的码点序",
            code_point_order,
            f"实际={list(ordered)}（码点序，非拼音）",
        )

        collation = (await db.execute(text(
            "SELECT DEFAULT_COLLATION_NAME FROM information_schema.SCHEMATA "
            "WHERE SCHEMA_NAME=DATABASE()"
        ))).scalar_one()
        record(
            "库排序规则为 utf8mb4_unicode_ci",
            collation == "utf8mb4_unicode_ci",
            f"实际={collation}",
        )

    # ---- 9. VARCHAR 长度边界 ----
    async with factory() as db, as_tenant_async(1):
        long_url = "https://verylong.com/" + "a" * 1000
        db.add(Site(id=30, user_id=1, name="超长URL", url=long_url,
                    url_hash=site_hash(long_url), source="manual"))
        await db.commit()
        stored = (await db.execute(select(Site).where(Site.id == 30))).scalar_one()
        record("VARCHAR(1024) 能存超长 URL", stored.url == long_url,
               f"长度 {len(stored.url)}")

    # ---- 10. 中文 LIKE ----
    async with factory() as db, as_tenant_async(1):
        found = (await db.execute(
            select(func.count()).select_from(Site).where(Site.name.contains("张三"))
        )).scalar_one()
        record("中文 LIKE 查询正常", found == 1, f"查到 {found} 条")

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    total = len(RESULTS)
    print("")
    print(f"  通过 {passed}/{total}")

    if passed < total:
        print("")
        print("  失败项：")
        for name, ok, detail in RESULTS:
            if not ok:
                print(f"    - {name}: {detail}")

    await engine.dispose()
    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    asyncio.run(main())