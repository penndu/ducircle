"""解析器公共类型。

四层策略各自产出一个 ParsedSite，编排器按优先级挑选第一个非空结果。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class ParsedPost:
    """一篇文章的最小信息。"""

    title: str
    link: str
    created: datetime | None = None
    updated: datetime | None = None
    strategy: str = ""
    # RSS 的 guid（WordPress 是 ?p=12345 这种文章 ID）。
    # 比 link 稳定：永久链接结构变了（/2026/10/2281/ → /post/2281/）、
    # 换域名了（.com → .cn），guid 都不变——去重靠它。
    guid: str | None = None
    # RSS 里带的全文（content:encoded）。有些站点文章页是 SPA 骨架页，
    # 静态抓取拿不到正文，但 feed 里带着完整内容——这时用它。
    content: str | None = None

    def __post_init__(self) -> None:
        self.title = (self.title or "").strip()
        self.link = (self.link or "").strip()
        if not self.title:
            self.title = "（无标题）"


@dataclass
class ParsedSite:
    """一个站点的解析结果。"""

    name: str = ""
    url: str = ""
    feed_url: str | None = None
    avatar: str | None = None
    description: str | None = None
    framework: str | None = None
    strategy: str = ""
    posts: list[ParsedPost] = field(default_factory=list)

    @property
    def is_valid(self) -> bool:
        """是否有可用内容。name 和 url 至少要有一个，文章流可以为空。"""
        return bool(self.url or self.name)

    @property
    def is_empty(self) -> bool:
        return not self.posts


@dataclass
class CrawlResult:
    """一次抓取的完整结果，同时用于写日志。"""

    url: str
    status: str  # success / failed / skipped
    strategy: str | None = None
    http_status: int | None = None
    error: str | None = None
    duration_ms: int = 0
    post_count: int = 0
    site: ParsedSite | None = None
    # 本次真正**新增**的文章数。
    # 和 post_count（「本次抓到几篇」）不是一回事——
    # 重复抓取时抓到 10 篇但新增 0 篇，两者混用会误导用户。
    new_posts: int = 0