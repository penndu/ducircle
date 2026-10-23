"""端到端验证：真实站点 → 抓取 → 落库 → 读 API。

用 SQLite 做临时库，不依赖 MySQL。验证的是完整链路，
包括 ORM 强制过滤、去重、日志写入在真实网络响应下是否成立。

对应需求文档 §8.2 的验收标准：抽取真实博客站点，抓取准确率验证。
"""

import asyncio
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

# 覆盖不同框架与不同主题——尤其是旧工具抓不了的
SITES = [
    ("akilar.top", "https://akilar.top/"),
    ("elizen.me", "https://elizen.me/"),
    ("noionion.top", "https://noionion.top/"),
]


async def main() -> None:
    from app.core.ratelimit import RedisManager
    from app.core.security import hash_api_key, hash_password, site_hash
    from app.core.tenant import as_tenant_async, set_tenant
    from app.db import session_scope
    from app.models import Base, Post, Site, User
    from app.services.crawler import Crawler, save_crawl_result, should_crawl

    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    factory = async_sessionmaker(engine, expire_on_commit=False)

    print("=" * 74)
    print("端到端验证：真实站点 → 抓取 → 落库 → 查询")
    print("=" * 74)

    # 建两个用户，验证隔离
    async with factory() as db:
        db.add_all([
            User(id=1, username="alice", password_hash=hash_password("x" * 12),
                 api_key_hash=hash_api_key("fc_alice_test")),
            User(id=2, username="bob", password_hash=hash_password("x" * 12),
                 api_key_hash=hash_api_key("fc_bob_test")),
        ])
        await db.commit()

    settings_crawler = None
    from app.core.config import get_settings

    settings_crawler = get_settings()
    settings_crawler.respect_robots = False  # 冒烟测试不因单个 robots 阻断
    settings_crawler.crawl_retries = 1
    settings_crawler.crawl_timeout = 25

    total_posts = 0
    async with Crawler(settings_crawler, redis=None) as crawler:
        async with as_tenant_async(1):
            for name, url in SITES:
                async with factory() as db:
                    site = Site(
                        user_id=1, name=name, url=url,
                        url_hash=site_hash(url), source="manual",
                    )
                    db.add(site)
                    await db.commit()
                    await db.refresh(site)

                    print(f"\n[{name}] {url}")
                    print(f"  should_crawl: {should_crawl(site)}")

                    started = datetime.now()
                    result = await crawler.crawl_site_with_rules(db, site)
                    new_posts = await save_crawl_result(db, site, result)
                    await db.commit()

                    elapsed = (datetime.now() - started).total_seconds()
                    status = "OK  " if result.status == "success" else "FAIL"
                    print(f"  [{status}] 策略={result.strategy} "
                          f"文章={result.post_count} 新增={new_posts} 耗时={elapsed:.1f}s")
                    if result.error:
                        print(f"         错误: {result.error}")

                    site = await db.get(Site, site.id)
                    print(f"         站点状态: error={site.error} "
                          f"strategy={site.strategy} 连续失败={site.consecutive_failures}")

                    if result.site and result.site.posts:
                        print("         样例文章:")
                        for p in result.site.posts[:3]:
                            date = p.created.strftime("%Y-%m-%d") if p.created else "无日期"
                            print(f"           · {p.title[:36]}  [{date}]")

                    total_posts += new_posts

    # 二次抓取：验证去重
    print("\n" + "=" * 74)
    print("去重验证：重复抓取同一批站点")
    async with Crawler(settings_crawler, redis=None) as crawler:
        async with as_tenant_async(1):
            for name, url in SITES:
                async with factory() as db:
                    from sqlalchemy import select

                    site = (
                        await db.execute(
                            select(Site).where(Site.url_hash == site_hash(url))
                        )
                    ).scalar_one()
                    result = await crawler.crawl_site_with_rules(db, site)
                    added = await save_crawl_result(db, site, result)
                    await db.commit()
                    print(f"  {name}: 抓到 {result.post_count} 篇，新增 {added} 篇 "
                          f"{'（去重生效）' if added == 0 else '（有新增）'}")

    # 租户隔离验证
    print("\n" + "=" * 74)
    print("租户隔离验证")
    async with as_tenant_async(1):
        async with factory() as db:
            from sqlalchemy import select

            alice_sites = (await db.execute(select(Site))).scalars().all()
            alice_posts = (await db.execute(select(Post))).scalars().all()
            print(f"  Alice: {len(alice_sites)} 个站点, {len(alice_posts)} 篇文章")

    async with as_tenant_async(2):
        async with factory() as db:
            from sqlalchemy import select

            bob_sites = (await db.execute(select(Site))).scalars().all()
            bob_posts = (await db.execute(select(Post))).scalars().all()
            print(f"  Bob:   {len(bob_sites)} 个站点, {len(bob_posts)} 篇文章")
            if not bob_sites and not bob_posts:
                print("  [OK  ] Bob 看不到 Alice 的任何数据 —— 隔离生效")
            else:
                print("  [FAIL] 隔离失效！")

    # 日志与统计需要跨用户视角，用显式豁免
    print("\n" + "=" * 74)
    print("日志与统计")
    from app.core.tenant import no_tenant_filter

    with no_tenant_filter():
        async with factory() as db:
            from sqlalchemy import func, select

            from app.models import CrawlLog

            logs = (await db.execute(select(CrawlLog))).scalars().all()
            success = sum(1 for log in logs if log.status == "success")
            print(f"  抓取日志: {len(logs)} 条（成功 {success}，失败 {len(logs)-success}）")

            strategy_rows = (
                await db.execute(
                    select(Post.strategy, func.count(Post.id)).group_by(Post.strategy)
                )
            ).all()
            print("  策略命中分布:")
            for strategy, count in strategy_rows:
                print(f"    {strategy or 'unknown'}: {count} 篇")

    print("\n" + "=" * 74)
    print(f"总计新增 {total_posts} 篇文章")
    print("=" * 74)

    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())