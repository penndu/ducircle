"""验证正文抽取质量。

摘要质量的天花板是正文质量。这里用真实文章页面验证：
- 能不能拿到正文
- 有没有混入导航/页脚噪声
- 长度是否合理（决定模型能不能吃下）
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402

from app.core.config import get_settings  # noqa: E402
from app.services.extractor import content_hash, extract_content  # noqa: E402

# 挑有代表性的：中文技术博客、不同主题
# 注意 URL 必须是**文章页**而不是站点首页——传首页会抽到
# 站点聚合列表而非正文，这是实测发现的真实陷阱。
ARTICLES = [
    ("akilar 短文", "https://akilar.top/posts/c215d440/"),
    ("elizen 长文", "https://elizen.me/posts/2026/07/thinking-is-like-a-wormhole"),
    ("akilar 另一篇", "https://akilar.top/posts/fb6a1a78/"),
]


async def fetch(url: str) -> str | None:
    settings = get_settings()
    try:
        async with httpx.AsyncClient(
            timeout=30,
            follow_redirects=True,
            headers={
                "User-Agent": settings.user_agent,
                "Accept-Language": "zh-CN,zh;q=0.9",
            },
        ) as client:
            resp = await client.get(url)
            if resp.status_code >= 400:
                print(f"    HTTP {resp.status_code}")
                return None
            return resp.text
    except Exception as exc:  # noqa: BLE001
        print(f"    抓取失败: {type(exc).__name__}")
        return None


# 常见噪声关键词，出现在正文里说明抽取不干净
NOISE_MARKERS = [
    "友链", "订阅", "归档", "上一篇", "下一篇", "版权所有",
    "Powered by", "Copyright", "Subscribe", "RSS",
]


async def main() -> None:
    print("=" * 72)
    print("正文抽取质量验证（真实文章页面）")
    print("=" * 72)

    ok = 0
    for label, url in ARTICLES:
        print(f"\n[{label}]")
        print(f"  {url}")

        html = await fetch(url)
        if html is None:
            continue

        content = extract_content(html, url)
        if not content:
            print("  [FAIL] 未抽到正文")
            continue

        ok += 1
        print(f"  [OK  ] 正文 {len(content)} 字符（原页面 {len(html)} 字符，"
              f"压缩比 {len(content)/len(html)*100:.1f}%）")
        print(f"         哈希: {content_hash(content)[:12]}")

        noise = [m for m in NOISE_MARKERS if m in content]
        if noise:
            print(f"  [注意] 正文含疑似导航噪声: {noise}")
        else:
            print("         无导航噪声")

        print(f"         开头: {content[:100]}")
        print(f"         结尾: {content[-60:]}")

    print("\n" + "=" * 72)
    print(f"抽取成功 {ok}/{len(ARTICLES)}")
    print("=" * 72)
    print("\n结论：正文是摘要的输入。抽取失败或噪声太多，")
    print("      再好的模型也生成不出有价值的摘要。")


if __name__ == "__main__":
    asyncio.run(main())