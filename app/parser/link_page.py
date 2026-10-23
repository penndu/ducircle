"""友链页解析：从友情链接页面提取好友列表。

输入一个友链页 URL（如 https://dusays.com/friends/），
输出页面上所有友情链接的 url / 名称 / 头像 / 描述。

难点是**区分友链和页面里其它外链**：
真实友链页除了友链，还有导航、页脚推荐位、社交链接、评论区。
直接抓所有外链会混进一堆噪音。

策略是分层降级：
    第 1 层  容器识别 —— 找到专门的友链区块，只取里面的链接
    第 2 层  结构启发 —— 找「一组结构相似的卡片」
    第 3 层  全页外链 —— 兜底，靠启发式排除噪音

第 1 层最准但依赖页面结构，第 3 层最通用但最容易出错。
实际用第 1 层（认得出来就用），认不出才往下退。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urljoin, urlparse

from app.parser.html import HTMLParser

# 常见友链区块的 class / id 关键词。
# 命中任意一个就优先把范围限定在这个容器里。
LINK_CONTAINER_HINTS = (
    "friend", "friends", "friend-list", "friendlist",
    "link-list", "linklist", "links", "link-box",
    "blogger", "blogroll", "roll", "links-page",
)

# 卡片容器的关键词——页面用卡片网格展示友链时命中。
CARD_HINTS = (
    "friend", "link", "blogger", "card", "item", "user", "site",
)

# 明确的噪音：这些链接不是友链。
NOISE_TEXT = re.compile(
    r"^(首页|主页|关于|关于我|联系|留言|归档|分类|标签|搜索|"
    r"上一页|下一页|更多|展开|收起|返回|订阅|广告|"
    r"推荐|友链申请|申请友链|加入我们|"
    r"rss|atom|feed|github|twitter|x\.com|telegram|微博|推特)$",
    re.IGNORECASE,
)

# 社交/工具站——友链页页脚常见，不算好友。
#
# 必须是完整域名或子域名，不能用子串匹配：
# "x.com" 用子串匹配会把 "example.com"、"box.com" 全误伤。
# 所以这里存的是「主域名」，匹配时判断 host 等于它或以 "." + 它结尾。
NOISE_HOSTS = (
    "github.com", "gitee.com", "twitter.com", "x.com", "t.me",
    "telegram.me", "weibo.com", "facebook.com", "instagram.com",
    "youtube.com", "linkedin.com", "discord.gg", "discord.com",
    "google.com", "baidu.com", "bing.com", "douban.com",
    "gravatar.com", "gstatic.com", "jsdelivr.net", "cloudflare.com",
    "creativecommons.org", "w3.org",
    # 备案与统计——页脚常见，不是友链
    "beian.miit.gov.cn", "beian.gov.cn", "icp.gov.moe", "miit.gov.cn",
    # 各种小工具与翻页服务
    "travellings.cn", "foreverblog.cn", "travel.moe", "shields.io",
    "badge.fury.io", "img.shields.io", "badges.ubuntu.com",
)

# 头像图片常见的占位图/默认图
PLACEHOLDER_HINTS = ("placeholder", "default", "avatar", "logo", "qrcode")

# 链接文本里全是标点或为空 —— 多半是图标链接，不是友链名
_MEANINGLESS = re.compile(r"^[\W_]*$")


@dataclass
class LinkEntry:
    """从友链页提取的一条友链。"""

    url: str
    name: str = ""
    avatar: str | None = None
    description: str | None = None

    @property
    def host(self) -> str:
        return urlparse(self.url).netloc


def _clean_name(text: str) -> str:
    """清理链接文本里的图标字符和空白。

    很多主题会在站名前放 Font Awesome 图标（<i class="fas fa-comment">），
    selectolax 的 text() 会把图标内容也带出来，得去掉。
    """
    if not text:
        return ""
    # 去掉 HTML 实体和私用区字符（图标字体常用）
    text = re.sub(r"[-]", "", text)
    text = re.sub(r"&[a-zA-Z]+;", " ", text)
    # 去掉常见的图标字符
    text = re.sub(r"[\U0001F000-\U0001FAFF←-⇿⬀-⯿️]", "", text)
    return re.sub(r"\s+", " ", text).strip(" -–—|·,，。;；:：")


def _is_noise(url: str, name: str, page_host: str) -> bool:
    """判断一个链接是否该排除。"""
    parsed = urlparse(url)
    host = parsed.netloc.lower()

    # 站内链接——不是友链
    if host and page_host and (host == page_host or host.endswith("." + page_host)):
        return True
    if not host:
        return True

    # 明确的社交/工具站。
    # 只匹配主域名本身或它的子域——子串匹配会误伤
    # example.com、box.com 这类正常站点。
    if any(host == n or host.endswith("." + n) for n in NOISE_HOSTS):
        return True

    # 文本是导航词
    if name and NOISE_TEXT.match(name):
        return True

    # 无意义的链接文本（纯图标）
    if not name or _MEANINGLESS.match(name):
        # 但域名本身有意义的仍然算（比如 <a href="https://x.com"><img></a>）
        # 这种情况由调用方通过 avatar 判断，这里保守放行
        pass

    return False


def _extract_avatar(node) -> str | None:
    """从卡片里找头像图。"""
    for img in node.css("img"):
        src = (
            img.attributes.get("data-src")
            or img.attributes.get("data-original")
            or img.attributes.get("src")
        )
        if not src:
            continue
        if any(h in src.lower() for h in PLACEHOLDER_HINTS):
            continue
        return src
    return None


def _entry_from_anchor(anchor, page_url: str, page_host: str) -> LinkEntry | None:
    """从一个 <a> 提取友链条目。"""
    href = (anchor.attributes.get("href") or "").strip()
    if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
        return None

    url = urljoin(page_url, href)
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return None

    name = _clean_name(anchor.text())
    if _is_noise(url, name, page_host):
        return None

    # 找最近的卡片祖先，���头像和描述都从那里取
    container = anchor
    for _ in range(4):
        parent = container.parent
        if parent is None:
            break
        # 往上走到根节点时，parent 是 HTMLParser 实例，没有 attributes
        if not hasattr(parent, "attributes"):
            break
        cls = (parent.attributes.get("class") or "") + " " + (
            parent.attributes.get("id") or ""
        )
        low = cls.lower()
        if any(h in low for h in CARD_HINTS):
            container = parent
            break
        container = parent

    avatar = _extract_avatar(container)

    # 描述：卡片里除名称外的文本，或 title 属性
    description = None
    title = (anchor.attributes.get("title") or "").strip()
    if title:
        description = title

    return LinkEntry(url=url, name=name, avatar=avatar, description=description)


def _find_link_containers(tree) -> list:
    """找可能包含友链的区块。"""
    found = []
    for el in tree.css("div, section, ul, ol, main, article, article, aside"):
        cls = (el.attributes.get("class") or "") + " " + (
            el.attributes.get("id") or ""
        )
        low = cls.lower()
        if any(h in low for h in LINK_CONTAINER_HINTS):
            found.append(el)
    return found


def _find_card_groups(tree) -> list[list]:
    """找「一组结构相似的卡片」。

    做法：统计所有 class 组合出现的频率，
    出现多次且数量可观的，很可能就是友链列表。
    """
    from collections import Counter

    counter: Counter = Counter()
    elements: dict[str, list] = {}

    for el in tree.css("div, li, article, section"):
        cls = el.attributes.get("class")
        if not cls:
            continue
        # 用 class 组合的前两个作为指纹（更粗但更稳）
        key = " ".join(sorted(cls.split()[:2]))
        counter[key] += 1
        elements.setdefault(key, []).append(el)

    groups = []
    for key, count in counter.most_common(20):
        if count < 3:
            continue
        low = key.lower()
        if not any(h in low for h in CARD_HINTS):
            continue
        # 卡片里得有链接，否则不是友链
        group = elements[key]
        if sum(len(g.css("a[href]")) for g in group[:5]) < 3:
            continue
        groups.append(group)
        if len(groups) >= 3:
            break

    return groups


def parse_link_page(html: str, page_url: str) -> list[LinkEntry]:
    """解析友链页，返回友链列表。

    分层降级：先找专门的友链区块，找不到再找卡片组，
    都没有才全页扫描外链。
    """
    tree = HTMLParser(html)
    page_host = urlparse(page_url).netloc.lower()

    entries: dict[str, LinkEntry] = {}

    def collect(nodes) -> int:
        added = 0
        for node in nodes:
            # 根节点（HTMLParser 实例）没有 .attributes，
            # css_matches("a") 也不能用来判断「自己是不是 <a>」——
            # 它的语义是「子树里有没有 <a>」，对任何含链接的节点都返回 True。
            # 可靠的办法是看有没有 attributes（只有元素节点才有）。
            if hasattr(node, "attributes") and node.tag == "a":
                anchors = [node]
            else:
                anchors = node.css("a[href]")
            for anchor in anchors:
                entry = _entry_from_anchor(anchor, page_url, page_host)
                if entry is None:
                    continue
                key = entry.host.lower()
                if key in entries:
                    # 同域名保留信息更全的那条
                    old = entries[key]
                    if not old.name and entry.name:
                        old.name = entry.name
                    if not old.avatar and entry.avatar:
                        old.avatar = entry.avatar
                    continue
                entries[key] = entry
                added += 1
        return added

    # 第 1 层：专门的友链区块
    containers = _find_link_containers(tree)
    # 取外链最多的那个容器（友链多的那个区块）
    best = None
    best_count = 0
    for container in containers:
        count = 0
        for anchor in container.css("a[href]"):
            href = anchor.attributes.get("href") or ""
            if href.startswith("http"):
                host = urlparse(href).netloc.lower()
                if host and page_host and host != page_host:
                    count += 1
        if count > best_count:
            best, best_count = container, count

    if best is not None and best_count >= 2:
        collect([best])
        if len(entries) >= 2:
            return _finalize(entries)

    # 第 2 层：卡片组
    for group in _find_card_groups(tree):
        collect(group)
        if len(entries) >= 2:
            return _finalize(entries)

    # 第 3 层：全页外链
    collect([tree])

    return _finalize(entries)


def _finalize(entries: dict[str, LinkEntry]) -> list[LinkEntry]:
    """收尾：补名称、排序。

    名称缺失时用域名兜底——总比空着好，
    用户在列表里至少能认出是哪个站。
    """
    result = []
    for entry in entries.values():
        if not entry.name:
            host = entry.host
            entry.name = host[4:] if host.startswith("www.") else host
        result.append(entry)

    # 有名称的排前面（更可能是真正的友链），然后按名称排
    result.sort(key=lambda e: (not e.name, e.name.lower()))
    return result
