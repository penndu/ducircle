"""Tailwind 加载源的回归测试。

背景：后台页面原先引 ``https://cdn.tailwindcss.com``。这个地址在国内
反复出现不可达，一旦加载失败，整个后台直接掉样式——而它是**所有**
后台页面的样式来源，属于单点故障。

所以这里把两件事钉住：

1. 不能再引 ``cdn.tailwindcss.com``（那是被动等它再一次挂）；
2. 必须走国内可访问的镜像，并且有回退，不能只有一个来源。

另外还要守住公开页面的现状：落地页 / 文档 / 状态页是自带样式表的，
**不引 Tailwind**，别被顺手加回去。
"""

from __future__ import annotations

import re
from pathlib import Path

BASE = Path("web/templates/base.html")
PUBLIC_PAGES = [
    Path("web/templates/landing.html"),
    Path("web/templates/docs.html"),
    Path("web/templates/status.html"),
]
TAILWIND_SCRIPT = re.compile(r'<script[^>]+src="([^"]+)"[^>]*>')


def _tailwind_urls(html: str) -> list[str]:
    return [url for url in TAILWIND_SCRIPT.findall(html) if "tailwind" in url]


class TestBackendTailwindSource:
    def test_no_longer_uses_unreachable_cdn(self):
        html = BASE.read_text(encoding="utf-8")
        # 注释里提到旧地址是可以的，但不能再有真实的 script src
        scripts = TAILWIND_SCRIPT.findall(html)
        assert not any("cdn.tailwindcss.com" in url for url in scripts), (
            "又用回了国内访问不稳定的 cdn.tailwindcss.com"
        )

    def test_uses_china_reachable_mirror(self):
        html = BASE.read_text(encoding="utf-8")
        urls = _tailwind_urls(html)
        assert urls, "base.html 没有加载 Tailwind"
        assert any("registry.npmmirror.com" in url for url in urls), (
            "首选镜像应是国内可访问的 npmmirror"
        )
        # 版本要锁定，否则镜像上的静默升级会让样式无预兆地变
        assert any(re.search(r"@tailwindcss/browser/\d+\.\d+\.\d+", u) for u in urls)

    def test_has_fallback(self):
        """单一来源就是单点故障——加载失败要有第二条链路。"""
        html = BASE.read_text(encoding="utf-8")
        assert "onerror" in html
        assert "unpkg.com/@tailwindcss/browser" in html

    def test_v4_compat_layer_present(self):
        """v4 把默认边框色改成 currentColor，模板里大量裸 border 依赖旧值。"""
        html = BASE.read_text(encoding="utf-8")
        assert "body *" in html and "border-color" in html
        # v4 preflight 把按钮 cursor 改成了 default
        assert "cursor: pointer" in html


class TestPublicPagesKeepOwnStyles:
    def test_public_pages_do_not_load_tailwind(self):
        for path in PUBLIC_PAGES:
            html = path.read_text(encoding="utf-8")
            assert not _tailwind_urls(html), f"{path} 不该引 Tailwind"
            assert "cdn.tailwindcss.com" not in html

    def test_admin_templates_extend_base(self):
        """后台模板必须继承 base.html —— 否则它拿不到 Tailwind 和兼容层。"""
        for name in ("layout.html", "auth.html"):
            html = Path(f"web/templates/{name}").read_text(encoding="utf-8")
            assert 'extends "base.html"' in html
