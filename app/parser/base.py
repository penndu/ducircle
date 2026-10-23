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