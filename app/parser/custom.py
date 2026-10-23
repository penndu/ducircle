"""第 0 层：用户自定义规则。

优先级最高。用户配了就完全信任，不再走自动推断——
这是需求确认时明确的要求（Q13=D）。

规则结构与旧工具的 css_rules.yaml 兼容，但改为数据库存储 + 后台可视化编辑。
"""

from __future__ import annotations

from urllib.parse import urljoin

import html as html_module

from app.core.security import normalize_url
from app.parser.base import ParsedPost, ParsedSite
from app.parser.fingerprint import parse_datetime_flexible


class CustomRuleSet:
    """一个站点的规则集合，按 priority 升序尝试。"""

    __slots__ = ("fields",)

    def __init__(self, rules) -> None:
        # rules: 形如 SiteRule 的 ORM 对象列表
        by_field: dict[str, list] = {}
        for rule in rules:
            if not rule.enabled:
                continue
            by_field.setdefault(rule.field, []).append(rule)
        for items in by_field.values():
            items.sort(key=lambda r: (-r.priority, r.id))
        self.fields = by_field

    def has_rules(self) -> bool:
        return bool(self.fields)

    def _select(self, tree, field: str) -> list[str]:
        """按 priority 顺序尝试，命中即返回，不再试后续规则。"""
        from app.parser.html import HTMLParser

        for rule in self.fields.get(field, []):
            values: list[str] = []
            for node in tree.css(rule.selector):
                if rule.attr == "text":
                    value = node.text(strip=True)
                else:
                    value = node.attributes.get(rule.attr)
                if value:
                    values.append(_unescape(value.strip()))
            if values:
                return values
        return []

    def to_selector(self, field: str) -> str | None:
        rules = self.fields.get(field) or []
        return rules[0].selector if rules else None

    def has_field(self, field: str) -> bool:
        return bool(self.fields.get(field))


def _unescape(text: str) -> str:
    """还原 HTML 实体，避免昵称出现 &quot; 这类转义残留。"""
    return html_module.unescape(text)


def parse_with_custom(
    html: str, base_url: str, rules: CustomRuleSet
) -> ParsedSite | None:
    """按用户自定义规则解析。

    返回 None 表示规则集不完整或未命中，由上层决定是否降级——
    需求是「用户配了就完全信任」，所以这里不做静默降级，
    失败会明确记录在抓取日志里，用户能看到自己的规则哪里不对。
    """
    from app.parser.html import HTMLParser

    tree = HTMLParser(html)

    site = ParsedSite(
        url=base_url,
        strategy="custom",
        framework=rules.to_selector("author"),
    )

    # 站点级字段（友链页解析时用）
    titles = rules._select(tree, "author")
    links = rules._select(tree, "link")
    avatars = rules._select(tree, "avatar")

    if titles and links and len(titles) == len(links):
        site.name = titles[0]
        site.url = normalize_url(urljoin(base_url, links[0]))
        if avatars:
            site.avatar = urljoin(base_url, avatars[0])
        return site

    # 文章级字段
    titles = rules._select(tree, "title")
    links = rules._select(tree, "link")
    if not titles or not links or len(titles) != len(links):
        return None

    created_values = rules._select(tree, "created")
    updated_values = rules._select(tree, "updated")

    for i in range(len(titles)):
        link = normalize_url(urljoin(base_url, links[i]))
        if not link:
            continue

        created = (
            parse_datetime_flexible(created_values[i])
            if i < len(created_values)
            else None
        )
        updated = (
            parse_datetime_flexible(updated_values[i])
            if i < len(updated_values)
            else None
        )
        if created is None and updated is None:
            continue

        site.posts.append(
            ParsedPost(
                title=titles[i],
                link=link,
                created=created or updated,
                updated=updated or created,
                strategy="custom",
            )
        )

    return site if site.posts else None


def validate_selector(html: str, selector: str, attr: str) -> tuple[bool, int]:
    """后台规则编辑器实时预览用：返回该选择器是否有效、命中多少元素。"""
    from app.parser.html import HTMLParser

    try:
        tree = HTMLParser(html)
        nodes = tree.css(selector)
    except Exception:
        return False, 0

    if attr == "text":
        return len(nodes) > 0, len(nodes)

    hits = sum(1 for n in nodes if n.attributes.get(attr))
    return hits > 0, hits


def summarize_mismatches(
    rules: CustomRuleSet, html: str, base_url: str
) -> list[str]:
    """给用户看的规则问题提示，比只说"抓取失败"有用。"""
    problems: list[str] = []
    for field in ("title", "link"):
        if not rules.has_field(field):
            problems.append(f"缺少「{field}」规则")
            continue
        values = rules._select(_parse(html), field)
        if not values:
            problems.append(f"「{field}」规则未命中任何元素")

    if rules.has_field("title") and rules.has_field("link"):
        tree = _parse(html)
        titles = rules._select(tree, "title")
        links = rules._select(tree, "link")
        if titles and links and len(titles) != len(links):
            problems.append(
                f"title 命中 {len(titles)} 个，link 命中 {len(links)} 个，两者需对应"
            )
    return problems


def _parse(html: str):
    from app.parser.html import HTMLParser

    return HTMLParser(html)