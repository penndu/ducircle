"""分层验证：单独测试第 2 层（框架指纹）与第 3 层（通用推断）。

live_check 会优先走 feed，导致指纹层永远没被验证到。
这里强制跳过 feed，直接拿 HTML 喂给后两层，才能确认兜底能力真实存在——
这是「能不能替掉手写 CSS 规则」的关键：feed 挂了之后还有没有救。
"""

import asyncio
import sys

sys.path.insert(0, ".")

import httpx  # noqa: E402

from app.core.config import Settings  # noqa: E402
from app.parser.fingerprint import detect_framework, parse_with_framework  # noqa: E402
from app.parser.heuristic import parse_with_heuristic  # noqa: E402

SITES = [
    "https://akilar.top/",
    "https://elizen.me/",
    "https://noionion.top/",
    "https://www.yyyzyyyz.cn/",
]


async def fetch(url: str) -> str | None:
    settings = Settings()
    try:
        async with httpx.AsyncClient(
            timeout=20,
            follow_redirects=True,
            headers={"User-Agent": settings.user_agent},
        ) as client:
            resp = await client.get(url)
            return resp.text if resp.status_code < 400 else None
    except Exception as exc:  # noqa: BLE001
        print(f"    抓取失败: {type(exc).__name__}")
        return None


async def main() -> None:
    print("=" * 74)
    print("第 2 层（框架指纹）与第 3 层（通用推断）独立验证")
    print("=" * 74)

    fp_hits = heuristic_hits = both_fail = 0

    for url in SITES:
        print(f"\n{url}")
        html = await fetch(url)
        if html is None:
            print("    跳过：无法抓取")
            continue

        framework = detect_framework(html)
        print(f"    框架指纹: {framework or '未识别'}")

        fp_site = None
        if framework:
            fp_site = parse_with_framework(html, url, framework)
        fp_count = len(fp_site.posts) if fp_site else 0
        print(f"    第2层 指纹: {fp_count} 篇" + (f"  ({fp_site.strategy})" if fp_site else ""))

        heur_site = parse_with_heuristic(html, url)
        h_count = len(heur_site.posts) if heur_site else 0
        print(f"    第3层 推断: {h_count} 篇")

        if fp_count >= 3:
            fp_hits += 1
            for p in fp_site.posts[:2]:
                print(f"      · {p.title[:38]}")
                print(f"        {p.link[:72]}")
        if h_count >= 3:
            heuristic_hits += 1

        if fp_count < 3 and h_count < 3:
            both_fail += 1
            print("    ⚠ 两层都不足 3 篇 —— 该站需依赖用户自定义规则")

    total = len(SITES)
    print("\n" + "=" * 74)
    print(f"第2层 指纹可用: {fp_hits}/{total}")
    print(f"第3 层 推断可用: {heuristic_hits}/{total}")
    print(f"两层均不足3篇: {both_fail}/{total}")
    print("=" * 74)
    print("\n结论：feed 不可用时，兜底层能否救回站点 —— 看上面两个数字。")


if __name__ == "__main__":
    asyncio.run(main())