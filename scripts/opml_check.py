"""OPML 解析的真实场景验证。

单元测试用的是人工构造的样本，这里模拟各阅读器的真实导出格式，
确认解析器在实际数据上的表现——尤其是那些「只有 feed 没有 htmlUrl」
的条目，它们是最容易出错的地方。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services.opml_parser import dedupe_entries, parse_opml  # noqa: E402

# ---- 场景 1：FreshRSS 导出（嵌套分类，最常见） ----
FRESH_RSS = """<?xml version="1.0" encoding="UTF-8"?>
<opml version="2.0">
  <head>
    <title>FreshRSS 订阅列表</title>
    <dateCreated>Mon, 01 Jan 2024 10:00:00 +0800</dateCreated>
    <ownerName>penndu</ownerName>
  </head>
  <body>
    <outline text="技术">
      <outline type="rss" text="Akilar的糖果屋"
               xmlUrl="https://akilar.top/atom.xml" htmlUrl="https://akilar.top/"/>
      <outline type="rss" text="Elizen"
               xmlUrl="https://elizen.me/index.xml" htmlUrl="https://elizen.me/"/>
    </outline>
    <outline text="生活">
      <outline type="rss" text="noionion"
               xmlUrl="https://noionion.top/atom.xml" htmlUrl="https://noionion.top/"/>
    </outline>
  </body>
</opml>"""

# ---- 场景 2：只有 feed 没有 htmlUrl（很多移动端阅读器这样导） ----
FEED_ONLY = """<?xml version="1.0" encoding="UTF-8"?>
<opml version="2.0">
  <head><title>只有feed</title></head>
  <body>
    <outline type="rss" text="站点甲" xmlUrl="https://jia.example.com/atom.xml"/>
    <outline type="rss" text="站点乙" xmlUrl="https://yi.example.com/feed"/>
    <outline type="rss" text="站点丙" xmlUrl="https://bing.example.com/blog/rss.xml"/>
  </body>
</opml>"""

# ---- 场景 3：同一站点多个 feed（用户订阅全文+分类） ----
MULTI_FEED = """<?xml version="1.0" encoding="UTF-8"?>
<opml version="2.0">
  <head><title>重复订阅</title></head>
  <body>
    <outline type="rss" text="多订阅站" htmlUrl="https://multi.com/"
             xmlUrl="https://multi.com/full.xml"/>
    <outline type="rss" text="多订阅站-技术" htmlUrl="https://multi.com/"
             xmlUrl="https://multi.com/tech.xml"/>
    <outline type="rss" text="多订阅站-生活" htmlUrl="https://multi.com/"
             xmlUrl="https://multi.com/life.xml"/>
  </body>
</opml>"""

# ---- 场景 4：脏数据（混入空节点、无效 URL、HTML 实体） ----
DIRTY = """<?xml version="1.0" encoding="UTF-8"?>
<opml version="2.0">
  <head><title>脏数据</title></head>
  <body>
    <outline text="空分组">
      <outline text="没有网址的节点"/>
    </outline>
    <outline type="rss" text="&lt;b&gt;加粗&lt;/b&gt;标题 &amp; 符号"
             htmlUrl="https://dirty-ok.com/" xmlUrl="https://dirty-ok.com/f.xml"/>
    <outline type="rss" text="无协议的网址"
             htmlUrl="dirty-noproto.com" xmlUrl="https://dirty-noproto.com/f.xml"/>
    <outline type="rss" text="正常的" htmlUrl="https://dirty-normal.com/"/>
  </body>
</opml>"""

# ---- 场景 5：GBK 编码（国内阅读器常见） ----
GBK_OPML = """<?xml version="1.0" encoding="GBK"?>
<opml version="2.0">
  <head><title>中文标题</title></head>
  <body>
    <outline type="rss" text="中文站点" htmlUrl="https://gbk.com/"
             xmlUrl="https://gbk.com/atom.xml"/>
  </body>
</opml>"""


def show(title: str, content, is_bytes: bool = False) -> None:
    print("\n" + "=" * 70)
    print(f"场景：{title}")
    print("=" * 70)

    try:
        result = parse_opml(content)
    except ValueError as exc:
        print(f"  [解析失败] {exc}")
        return

    entries, duplicates = dedupe_entries(result.entries)

    print(f"  文档标题：{result.title or '(无)'}")
    print(f"  outline 总数：{result.total_outlines}")
    print(f"  解析出条目：{len(result.entries)} → 去重后 {len(entries)}"
          f"（合并了 {duplicates} 个重复）")
    if result.skipped:
        print(f"  无效条目：{result.skipped}")

    for e in entries:
        flags = []
        if e.inferred_site:
            flags.append("站点地址为推断")
        if e.category:
            flags.append(f"分类={e.category}")
        flag_str = f"  [{'; '.join(flags)}]" if flags else ""
        print(f"\n  · {e.name}{flag_str}")
        print(f"      站点: {e.site_url}")
        print(f"      feed: {e.feed_url or '(未提供)'}")


def main() -> None:
    print("OPML 解析真实场景验证")
    show("FreshRSS 嵌套分类", FRESH_RSS)
    show("只有 feed 地址", FEED_ONLY)
    show("同一站点多个 feed", MULTI_FEED)
    show("脏数据混入", DIRTY)
    show("GBK 编码", GBK_OPML.encode("gbk"), is_bytes=True)

    print("\n" + "=" * 70)
    print("关键检查点")
    print("=" * 70)
    print("""
  1. htmlUrl 才是站点地址，xmlUrl 是 feed
     —— 拿错会导致抓取必然失败（抓到 XML 不是 HTML）

  2. 只有 feed 时推断站点根目录，并标记 inferred_site
     —— 用户导入了才知道要去核对

  3. 同域名多个 feed 合并成一条，feed 地址不丢
     —— 丢了这个等于白导入

  4. text 里的 HTML 标记要清掉
     —— 否则前端渲染会出现乱码或 XSS

  5. GBK 编码要能读
     —— 国内阅读器导出常见
""")


if __name__ == "__main__":
    main()