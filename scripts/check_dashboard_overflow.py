"""概览页「最新文章」摘要出框的复现与验证。

问题：宽屏（≥1400px）时概览页是双列 grid，每列只有 650~750px。
摘要用 white-space:nowrap + text-overflow:ellipsis，
64 个中文字（12px 字号）需要约 770px，放不下就横向溢出、压到相邻内容上。

这个脚本用无头浏览器测量「摘要元素的宽度 vs 容器宽度」，
验证修复后不再溢出。

**这是手动工具，不进 CI**——CI 里不装 Playwright。
跑法：
    npm i playwright && npx playwright install chromium
    python scripts/check_dashboard_overflow.py

浏览器查找顺序：
1. PLAYWRIGHT_MODULE 环境变量指定的模块路径
2. 项目本地 node_modules
3. 系统已装的 playwright（全局 npm 目录）

浏览器用系统 Chrome（channel:'chrome'），不用等 Playwright 自带浏览器下载。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path


def _playwright_module_dir() -> str | None:
    """找到 playwright 的 node_modules 目录。"""
    env = os.environ.get("PLAYWRIGHT_MODULE")
    if env:
        return env

    # 项目本地
    local = Path(__file__).resolve().parents[1] / "node_modules"
    if (local / "playwright").exists():
        return str(local)

    # 全局 npm
    global_root = subprocess.run(
        ["npm", "root", "-g"], capture_output=True, text=True, timeout=30
    ).stdout.strip()
    if global_root and (Path(global_root) / "playwright").exists():
        return global_root

    return None

# 概览页的关键 CSS（修复前 / 修复后两版）
CSS_BROKEN = """
.ds-posts{display:grid;grid-template-columns:1fr;gap:0 1.5rem}
@media(min-width:1400px){.ds-posts{grid-template-columns:repeat(2,minmax(0,1fr))}}
.ds-post{display:flex;gap:12px;padding:12px 0}
.ds-avatar{width:34px;height:34px;border-radius:10px;flex-shrink:0}
.ds-post-body{flex:1;min-width:0}
.ds-post-title{font-size:13.5px;display:block;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;line-height:1.5}
.ds-post-sum{font-size:12px;color:#555;margin-top:5px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
"""

CSS_FIXED = """
.ds-posts{display:grid;grid-template-columns:1fr;gap:0 1.5rem}
@media(min-width:1400px){.ds-posts{grid-template-columns:repeat(2,minmax(0,1fr))}}
.ds-post{display:flex;gap:12px;padding:12px 0}
.ds-avatar{width:34px;height:34px;border-radius:10px;flex-shrink:0}
.ds-post-body{flex:1;min-width:0}
.ds-post-title{font-size:13.5px;display:block;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;line-height:1.5}
.ds-post-sum{font-size:12px;color:#555;margin-top:5px;line-height:1.65;
  display:-webkit-box;-webkit-box-orient:vertical;-webkit-line-clamp:2;
  overflow:hidden;word-break:break-word;overflow-wrap:anywhere}
"""

# 真实数据里最长的摘要（226 字，截断到 64 字）
SUMMARY = (
    "作者认为，AI真正进入大众生产力的方向不是短剧、PPT或作业批改，而是代码生成与审查；"
    "罗永浩直播推广 Trae、GitHub 项目被 AI 扫描出大量漏洞，以及 WordPress 站点被迫迁移，都是"
    "值得注意的信号…"
)

HTML = """<!doctype html>
<html><head><meta charset="utf-8"><style>
body{{margin:0;font-family:-apple-system,"PingFang SC",sans-serif}}
.wrap{{padding:16px;max-width:1560px;margin:0 auto}}
{css}
</style></head>
<body><div class="wrap"><div class="ds-posts">
<div class="ds-post"><div class="ds-avatar"></div><div class="ds-post-body">
<a class="ds-post-title">这一届的双十一，已经非常具有AI味了</a>
<div class="ds-post-sum">{summary}</div>
</div></div>
<div class="ds-post"><div class="ds-avatar"></div><div class="ds-post-body">
<a class="ds-post-title">11月13日，星期五, 每天60秒读懂全世界！</a>
<div class="ds-post-sum">{summary}</div>
</div></div>
</div></div></body></html>"""

# 各视口宽度：覆盖单列区间与双列区间
WIDTHS = [1024, 1280, 1366, 1400, 1440, 1512, 1600, 1920]


def measure(css: str) -> list[dict]:
    """用无头 Chrome 测量每个视口宽度下的溢出情况。"""
    html = HTML.format(css=css, summary=SUMMARY)
    path = Path("/tmp/_overflow_probe.html")
    path.write_text(html, encoding="utf-8")

    module_dir = _playwright_module_dir()
    if not module_dir:
        raise RuntimeError(
            "找不到 playwright。跑 `npm i playwright && npx playwright install chromium`，"
            "或设 PLAYWRIGHT_MODULE 环境变量指向 node_modules 目录"
        )

    script = """
    const out = [];
    const widths = %(widths)s;
    for (const w of widths) {
      await page.setViewportSize({width: w, height: 900});
      await page.goto('file:///tmp/_overflow_probe.html');
      await page.waitForTimeout(120);
      const r = await page.evaluate(() => {
        const sums = [...document.querySelectorAll('.ds-post-sum')];
        const bodies = [...document.querySelectorAll('.ds-post-body')];
        const posts = [...document.querySelectorAll('.ds-post')];
        let worst = 0;
        sums.forEach((s, i) => {
          // scrollWidth > clientWidth 说明内容撑破了元素
          const over = s.scrollWidth - s.clientWidth;
          if (over > worst) worst = over;
        });
        // 元素右边界是否越过了所属卡片的右边界
        let escape = 0;
        sums.forEach((s, i) => {
          const sr = s.getBoundingClientRect();
          const pr = posts[i].getBoundingClientRect();
          const d = sr.right - pr.right;
          if (d > escape) escape = d;
        });
        return {sumW: sums[0].clientWidth, bodyW: bodies[0].clientWidth,
                worstOverflow: worst, rightEscape: escape};
      });
      out.push({width: w, ...r});
    }
    console.log(JSON.stringify(out));
    """ % {"widths": json.dumps(WIDTHS)}

    # 把测量脚本写到独立文件，node 直接读它
    probe = Path("/tmp/_probe.mjs")
    probe.write_text(
        f"""
import {{ chromium }} from '{module_dir}/playwright/index.mjs';
// 用系统 Chrome（channel:'chrome'）——不用等 Playwright 自带浏览器下载
const browser = await chromium.launch({{channel: 'chrome'}});
const page = await browser.newPage();
"""
        + script
        + """
console.log(JSON.stringify(out));
await browser.close();
""",
        encoding="utf-8",
    )

    result = subprocess.run(
        ["node", str(probe)],
        capture_output=True,
        text=True,
        timeout=180,
    )
    if result.returncode != 0:
        raise RuntimeError(f"测量失败: {result.stderr[:500]}")
    return json.loads(result.stdout.strip().splitlines()[-1])


def main() -> int:
    print("=== 修复前（nowrap + ellipsis）===")
    before = measure(CSS_BROKEN)
    for r in before:
        flag = "溢出" if r["worstOverflow"] > 1 or r["rightEscape"] > 1 else "ok"
        print(
            f"  {r['width']:5}px  列宽 {r['bodyW']:4.0f}px  "
            f"内容溢出 {r['worstOverflow']:4.0f}px  越界 {r['rightEscape']:4.0f}px  {flag}"
        )

    print("\n=== 修复后（line-clamp 2 行）===")
    after = measure(CSS_FIXED)
    for r in after:
        flag = "溢出" if r["worstOverflow"] > 1 or r["rightEscape"] > 1 else "ok"
        print(
            f"  {r['width']:5}px  列宽 {r['bodyW']:4.0f}px  "
            f"内容溢出 {r['worstOverflow']:4.0f}px  越界 {r['rightEscape']:4.0f}px  {flag}"
        )

    bad_before = [r for r in before if r["worstOverflow"] > 1 or r["rightEscape"] > 1]
    bad_after = [r for r in after if r["worstOverflow"] > 1 or r["rightEscape"] > 1]

    print(f"\n修复前溢出视口: {[r['width'] for r in bad_before]}")
    print(f"修复后溢出视口: {[r['width'] for r in bad_after]}")

    if not bad_before:
        print("\n⚠️  修复前没复现出溢出——CSS 或数据可能与线上不符，检查一下")
        return 1
    if bad_after:
        print("\n❌ 修复后仍有溢出")
        return 1
    print("\n✅ 修复生效：所有视口宽度下都不再溢出")
    return 0


if __name__ == "__main__":
    sys.exit(main())
