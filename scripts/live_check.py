"""真实站点冒烟测试。

不依赖数据库，直接跑完整抓取流程，验证四层策略在真实站点上的真实命中率。
这是决定「自动识别能不能替掉手写CSS规则」的关键验证。
"""

import asyncio
import sys
import time

sys.path.insert(0, ".")

from app.core.config import Settings  # noqa: E402
from app.parser.feed import discover_feed_url  # noqa: E402
from app.parser.fingerprint import detect_framework  # noqa: E402
from app.services.crawler import Crawler  # noqa: E402

# 覆盖不同框架的公开博客
SITES = [
    "https://akilar.top/",          # Hexo + ark 主题（旧工具抓不了的站）
    "https://elizen.me/",           # Hugo
    "https://www.yyyzyyyz.cn/",     # Hexo + butterfly
    "https://noionion.top/",        # Hexo
]


async def check(url: str) -> dict:
    settings = Settings()
    # 冒烟测试关掉 robots 检查，避免因单个站点的 robots 阻断整个验证
    settings.respect_robots = False
    settings.crawl_retries = 1
    settings.crawl_timeout = 20

    async with Crawler(settings) as crawler:
        started = time.monotonic()
        result = await crawler.crawl_site(url)
        elapsed = int((time.monotonic() - started) * 1000)

    info = {
        "url": url,
        "status": result.status,
        "strategy": result.strategy,
        "posts": result.post_count,
        "ms": elapsed,
        "error": result.error,
    }
    if result.site and result.site.posts:
        first = result.site.posts[0]
        info["sample_title"] = first.title[:40]
        info["sample_link"] = first.link[:70]
        info["sample_date"] = str(first.created)[:19] if first.created else None
    return info


async def main() -> None:
    print("=" * 72)
    print("真实站点抓取验证")
    print("=" * 72)

    results = []
    for url in SITES:
        try:
            info = await check(url)
        except Exception as exc:  # noqa: BLE001
            info = {"url": url, "status": "error", "error": f"{type(exc).__name__}: {exc}"}
        results.append(info)

        status = info["status"]
        mark = {"success": "OK  ", "failed": "FAIL", "skipped": "SKIP"}.get(status, "ERR ")
        print(f"\n[{mark}] {url}")
        print(f"       策略: {info.get('strategy')}  文章数: {info.get('posts')}  "
              f"耗时: {info.get('ms')}ms")
        if info.get("error"):
            print(f"       错误: {info['error']}")
        if info.get("sample_title"):
            print(f"       样例: {info['sample_title']}")
            print(f"       链接: {info['sample_link']}")
            print(f"       时间: {info['sample_date']}")

    ok = sum(1 for r in results if r["status"] == "success")
    print("\n" + "=" * 72)
    print(f"成功率: {ok}/{len(results)}")

    strategies: dict[str, int] = {}
    for r in results:
        if r["status"] == "success" and r.get("strategy"):
            key = r["strategy"].split(":")[0]
            strategies[key] = strategies.get(key, 0) + 1
    if strategies:
        print("策略命中分布: " + ", ".join(f"{k}={v}" for k, v in strategies.items()))
    print("=" * 72)


if __name__ == "__main__":
    asyncio.run(main())