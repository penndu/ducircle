"""抓取编排。

四层策略的调度者：
    第 0 层  用户自定义规则   —— 配了就完全信任，不做静默降级
    第 1 层  RSS / Atom 自动发现
    第 2 层  框架指纹识别
    第 3 层  通用结构推断
    失败     标记失败，写 crawl_logs，等后台人工介入

每层的结果与命中策略都记入日志，这是持续优化规则覆盖的唯一依据——
上线后可以据此统计各层实际贡献率，知道该继续加框架规则还是加通用规则。
"""

from __future__ import annotations

import asyncio
import random
import re
import time
from datetime import datetime, timedelta
from urllib.parse import urljoin

import httpx
from sqlalchemy import select

from app.core.config import Settings, get_settings
from app.core.ratelimit import Semaphore
from app.core.security import normalize_url, request_url, site_hash
from app.models import CrawlLog, Post, Site, SiteRule
from app.parser.base import CrawlResult, ParsedSite
from app.parser.custom import CustomRuleSet, parse_with_custom
from app.parser.feed import (
    COMMON_FEED_PATHS,
    discover_feed_url,
    parse_feed,
)
from app.parser.fingerprint import detect_framework, parse_with_framework
from app.parser.heuristic import parse_with_heuristic, looks_like_article

# 连续失败达到该次数后停止高频重试
FAILURE_THRESHOLD = 5


def _with_www(url: str) -> str | None:
    """给裸域地址加上 www. 前缀。

    只处理 http/https 的根域名，不动子域名（blog.x.com 加 www 没意义）。
    已经带 www 的返回 None（没有别的可试）。
    """
    from urllib.parse import urlsplit, urlunsplit

    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        return None
    netloc = parts.netloc
    if netloc.startswith("www.") or "." not in netloc:
        return None
    # 带端口的先摘出来
    port = ""
    if ":" in netloc:
        host, port_text = netloc.rsplit(":", 1)
        if port_text.isdigit():
            netloc = host
            port = f":{port_text}"
    if netloc.count(".") != 1:
        # blog.x.com 这种子域名，不加 www
        return None
    return urlunsplit(
        (parts.scheme, f"www.{netloc}{port}", parts.path, parts.query, "")
    )


def _explain_failure(html: str) -> str:
    """给「解析不出内容」一个能指导下一步的说明。

    光说「四层策略都没解析出内容」用户完全不知道该怎么办。
    最常见的原因是这是个纯前端 SPA——HTML 里没有正文，
    文章要靠 JS 渲染或调私有 API 才能拿到。
    """
    # SPA 的典型特征：页面里有空的挂载点，脚本才是内容的来源
    if re.search(
        r'<div[^>]+id=["\'](app|root|__nuxt|__next)["\'][^>]*>\s*</div>',
        html,
        re.I,
    ):
        return (
            "页面内容由 JavaScript 渲染，静态抓取拿不到文章；"
            "该站需要自己提供 RSS 地址才能收录"
        )
    if "<script" in html and len(html) < 8000:
        return (
            "页面内容疑似由 JavaScript 渲染（响应体很小），"
            "该站需要自己提供 RSS 地址才能收录"
        )
    return "四层策略均未解析出内容（可尝试为该站配置自定义规则）"


class Crawler:
    def __init__(self, settings: Settings | None = None, redis=None) -> None:
        self.settings = settings or get_settings()
        self._client: httpx.AsyncClient | None = None
        self._redis = redis
        self._global_sem = Semaphore(
            "crawl", self.settings.max_concurrent_global
        )

    async def __aenter__(self) -> "Crawler":
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(self.settings.crawl_timeout),
            headers={
                "User-Agent": self.settings.user_agent,
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            },
            follow_redirects=True,
            limits=httpx.Limits(max_connections=100, max_keepalive_connections=20),
        )
        return self

    async def __aexit__(self, *exc) -> None:
        if self._client:
            await self._client.aclose()

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            raise RuntimeError("Crawler 必须用 async with 打开")
        return self._client

    async def _fetch(
        self, url: str, *, stream: bool = False
    ) -> tuple[int, str | bytes]:
        """带重试的请求。指数退避 + 抖动，避免多个任务同时重试撞在一起。"""
        last_error: str = "unknown"
        for attempt in range(self.settings.crawl_retries):
            try:
                if stream:
                    resp = await self.client.get(url)
                    return resp.status_code, resp.text
                resp = await self.client.get(url)
                return resp.status_code, resp.text
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_error = type(exc).__name__
                if attempt < self.settings.crawl_retries - 1:
                    backoff = (2**attempt) + random.uniform(0, 1)
                    await asyncio.sleep(backoff)
            except Exception as exc:  # noqa: BLE001
                last_error = str(exc)
                break
        raise CrawlError(f"{last_error}: {url}")

    async def _fetch_final_url(self, url: str) -> tuple[int, str, str]:
        """请求并返回 (状态码, 正文, 重定向后的最终 URL)。

        有些站点会把域名整个换掉（vjo.cc → liuz.cn）。
        后续拼相对路径时必须用最终地址，否则 /feed 会拼到旧域名上，
        被重定向回首页——表现为「feed 找到了但 0 篇文章」。
        """
        resp = await self.client.get(url)
        return resp.status_code, resp.text, str(resp.url)

    async def crawl_site(self, url: str, feed_url: str | None = None) -> CrawlResult:
        """抓取一个站点首页/文章列表页。不涉及数据库，可独立测试。

        feed_url 是用户手动指定的 RSS 地址。填了就只用它，
        不再自动发现——用户既然明确指定了，自动猜出来的结果
        反而可能不是他想要的。
        """
        started = time.monotonic()
        # 发请求用 request_url（保留 www.），不能用 normalize_url——
        # 后者会剥掉 www.，很多站点裸域没有 DNS，抓取直接 ConnectError。
        url = request_url(url)

        if not await self._respect_robots(url):
            return CrawlResult(
                url=url,
                status="skipped",
                error="robots.txt 禁止抓取",
                duration_ms=int((time.monotonic() - started) * 1000),
            )

        try:
            status, html, final_url = await self._fetch_final_url(url)
        except (httpx.TimeoutException, httpx.TransportError, httpx.HTTPError) as exc:
            # 裸域连不上就试 www.。
            # 很多国内博客只在 www 上配了 DNS——存量数据里存的就是裸域，
            # 不做这个回退就永远连不上。
            fallback = _with_www(url)
            if fallback and fallback != url:
                try:
                    status, html, final_url = await self._fetch_final_url(fallback)
                    url = fallback
                except Exception:  # noqa: BLE001
                    return CrawlResult(
                        url=url,
                        status="failed",
                        error=f"{type(exc).__name__}: {url}",
                        duration_ms=int((time.monotonic() - started) * 1000),
                    )
            else:
                return CrawlResult(
                    url=url,
                    status="failed",
                    error=f"{type(exc).__name__}: {url}",
                    duration_ms=int((time.monotonic() - started) * 1000),
                )
        except Exception as exc:  # noqa: BLE001
            return CrawlResult(
                url=url,
                status="failed",
                error=str(exc),
                duration_ms=int((time.monotonic() - started) * 1000),
            )

        if status >= 400:
            return CrawlResult(
                url=url,
                status="failed",
                http_status=status,
                error=f"HTTP {status}",
                duration_ms=int((time.monotonic() - started) * 1000),
            )

        # 后续拼相对路径用重定向后的最终地址。
        # 有些站点整个换域名（vjo.cc → liuz.cn），
        # 用原地址拼出来的 /feed 会被重定向回首页，表现为「0 篇文章」。
        base_url = request_url(final_url)

        site = await self._apply_strategies(base_url, html, feed_url=feed_url)

        if site is None:
            return CrawlResult(
                url=url,
                status="failed",
                http_status=status,
                error=_explain_failure(html),
                duration_ms=int((time.monotonic() - started) * 1000),
            )

        # 只保留站内文章
        site.posts = [p for p in site.posts if looks_like_article(p.link)][
            : self.settings.max_posts_per_site
        ]

        return CrawlResult(
            url=url,
            status="success",
            strategy=site.strategy,
            http_status=status,
            duration_ms=int((time.monotonic() - started) * 1000),
            post_count=len(site.posts),
            site=site,
        )

    async def _apply_strategies(
        self,
        url: str,
        html: str,
        rules: CustomRuleSet | None = None,
        feed_url: str | None = None,
    ) -> ParsedSite | None:
        """按优先级依次尝试各层策略。"""
        # 第 1 层：RSS 自动发现。
        # 注意只调一次——之前写成 `if await self._try_feed(...) is not None:
        # return await self._try_feed(...)`，等于把整个探测流程跑两遍，
        # 每个候选路径都发两次请求。
        feed = await self._try_feed(url, html, feed_url=feed_url)
        if feed is not None:
            return feed

        # 第 2 层：框架指纹
        framework = detect_framework(html)
        if framework:
            parsed = parse_with_framework(html, url, framework)
            if parsed:
                return parsed

        # 第 3 层：通用结构推断
        return parse_with_heuristic(html, url)

    async def _try_feed(
        self, url: str, html: str, feed_url: str | None = None
    ) -> ParsedSite | None:
        """尝试 RSS/Atom。

        候选来源有三个：
        1. 用户手动填的 feed_url——填了就只用它，不再猜
        2. 页面里声明的 <link rel="alternate">（最准）
        3. 一组常见路径（兜底）

        三者都保留 www. 前缀。
        """
        candidates: list[str] = []

        if feed_url:
            # 用户指定的地址优先，且**只用**它。
            # 试不通就明确报错，而不是悄悄换成别的源——
            # 静默降级会让用户以为自己的配置生效了。
            candidates.append(request_url(feed_url))
        else:
            declared = discover_feed_url(html, url)
            if declared:
                # 声明的地址最可信，优先试
                candidates.append(request_url(declared))
            candidates.extend(
                request_url(urljoin(url, path)) for path in COMMON_FEED_PATHS
            )

        seen: set[str] = set()
        for candidate in candidates:
            if candidate in seen:
                continue
            seen.add(candidate)
            try:
                status, content = await self._fetch(candidate)
                if status >= 400:
                    continue
                parsed = parse_feed(
                    content.encode("utf-8") if isinstance(content, str) else content,
                    candidate,
                    url,
                )
                if parsed:
                    parsed.name = parsed.name or url
                    parsed.feed_url = candidate
                    return parsed
            except CrawlError:
                continue
        return None

    async def load_rules(self, session, site: Site) -> CustomRuleSet | None:
        """加载站点的自定义规则。需要数据库会话。"""
        if not self.settings.enable_custom_rules:
            return None
        stmt = (
            select(SiteRule)
            .where(SiteRule.site_id == site.id, SiteRule.enabled.is_(True))
        )
        rules = (await session.execute(stmt)).scalars().all()
        return CustomRuleSet(rules) if rules else None

    async def crawl_site_with_rules(
        self, session, site: Site
    ) -> CrawlResult:
        """针对数据库中的站点执行抓取，含自定义规则优先与结果落库。"""
        rules = await self.load_rules(session, site)
        return await self.crawl_site_with_loaded_rules(site, rules)

    async def crawl_site_with_loaded_rules(
        self, site: Site, rules: CustomRuleSet | None
    ) -> CrawlResult:
        """用**已加载好**的规则抓取站点。

        与 crawl_site_with_rules 的区别是不需要数据库会话——
        这样抓取（网络 IO，可能几十秒）期间不会占着数据库连接。
        批量抓取几十个站点时，这是连接池不被撑爆的关键。
        """
        started = time.monotonic()

        if not await self._acquire():
            return CrawlResult(
                url=site.url, status="skipped", error="超出全局并发上限"
            )

        try:
            # 第 0 层：用户规则优先。配了就完全信任，失败也不静默降级——
            # 明确记日志，让用户看到自己的规则哪里不对。
            if rules is not None and rules.has_rules():
                return await self._crawl_with_custom(site, rules, started)

            return await self.crawl_site(
                site.url, feed_url=getattr(site, "feed_url", None)
            )
        finally:
            await self._release()

    async def _crawl_with_custom(
        self, site: Site, rules: CustomRuleSet, started: float
    ) -> CrawlResult:
        try:
            status, html = await self._fetch(site.url)
        except CrawlError as exc:
            return CrawlResult(
                url=site.url,
                status="failed",
                error=str(exc),
                duration_ms=int((time.monotonic() - started) * 1000),
            )

        if status >= 400:
            return CrawlResult(
                url=site.url,
                status="failed",
                http_status=status,
                error=f"HTTP {status}",
                duration_ms=int((time.monotonic() - started) * 1000),
            )

        parsed = parse_with_custom(html, site.url, rules)
        if parsed is None:
            return CrawlResult(
                url=site.url,
                status="failed",
                http_status=status,
                error="自定义规则未命中任何内容（规则已配，不会自动降级）",
                duration_ms=int((time.monotonic() - started) * 1000),
            )

        return CrawlResult(
            url=site.url,
            status="success",
            strategy="custom",
            http_status=status,
            duration_ms=int((time.monotonic() - started) * 1000),
            post_count=len(parsed.posts),
            site=parsed,
        )

    async def _load_rules(self, session, site: Site) -> CustomRuleSet | None:
        """兼容旧调用点。"""
        return await self.load_rules(session, site)

    async def _acquire(self) -> bool:
        if self._redis is None:
            return True
        return await self._global_sem.acquire(self._redis)

    async def _release(self) -> None:
        if self._redis is not None:
            await self._global_sem.release(self._redis)

    async def _respect_robots(self, url: str) -> bool:
        if not self.settings.respect_robots:
            return True
        from urllib.parse import urlsplit

        parts = urlsplit(url)
        robots_url = f"{parts.scheme}://{parts.netloc}/robots.txt"
        try:
            from urllib.robotparser import RobotFileParser

            status, text = await self._fetch(robots_url)
            if status >= 400:
                return True  # 没有 robots.txt 即视为允许
            parser = RobotFileParser()
            parser.parse(text.splitlines())
            return parser.can_fetch(self.settings.user_agent, url)
        except Exception:  # noqa: BLE001
            # robots.txt 拉取失败时不阻塞主流程
            return True


class CrawlError(Exception):
    pass


async def save_crawl_result(session, site: Site, result: CrawlResult) -> int:
    """把抓取结果落库，返回新增文章数。"""
    now = datetime.now()
    new_posts = 0

    if result.status == "success" and result.site is not None:
        for parsed in result.site.posts:
            link = parsed.link
            if not link:
                continue

            from app.core.security import guid_hash

            gh = guid_hash(link)
            # 用 scalars().first() 而不是 scalar_one_or_none()——
            # 历史数据里可能存在重复的 guid_hash（唯一约束是后补的），
            # scalar_one_or_none() 遇到多行会直接抛 MultipleResultsFound，
            # 整个站点的抓取结果全丢。
            existing = (
                (
                    await session.execute(
                        select(Post)
                        .where(Post.user_id == site.user_id, Post.guid_hash == gh)
                        .order_by(Post.id)
                    )
                )
                .scalars()
                .first()
            )

            if existing is not None:
                # 已存在：只刷新可能变化的字段
                if parsed.title and existing.title != parsed.title:
                    existing.title = parsed.title
                existing.strategy = parsed.strategy or existing.strategy
                continue

            site_id = await _resolve_site_id(session, parsed.link, site)
            if site_id is None:
                continue

            session.add(
                Post(
                    user_id=site.user_id,
                    site_id=site_id,
                    title=parsed.title,
                    link=link,
                    guid_hash=gh,
                    created=parsed.created,
                    updated=parsed.updated,
                    crawled_at=now,
                    strategy=parsed.strategy,
                )
            )
            new_posts += 1

        site.strategy = result.strategy
        site.framework = result.site.framework
        site.error = False
        site.error_message = None
        site.consecutive_failures = 0
        site.last_crawled_at = now
        site.last_success_at = now
        site.crawl_interval_hours = 12
    else:
        site.error = result.status == "failed"
        site.error_message = result.error
        site.consecutive_failures += 1
        site.last_crawled_at = now
        # 连续失败则逐步降频，最多每 7 天一次，避免反复骚扰已下线站点
        if site.consecutive_failures >= FAILURE_THRESHOLD:
            site.crawl_interval_hours = min(24 * 7, 12 * site.consecutive_failures)

    session.add(
        CrawlLog(
            user_id=site.user_id,
            site_id=site.id,
            site_url=site.url,
            status=result.status,
            strategy=result.strategy,
            error=result.error,
            duration_ms=result.duration_ms,
            post_count=new_posts,
            http_status=result.http_status,
        )
    )
    return new_posts


async def _resolve_site_id(session, article_url: str, current_site: Site) -> int | None:
    """文章可能属于另一个友链站点。按 host 匹配，找不到就归到当前站点。"""
    from urllib.parse import urlsplit

    from app.models import Site as SiteModel

    host = urlsplit(article_url).netloc
    if not host:
        return None

    target_hash = site_hash(article_url)
    stmt = (
        select(SiteModel)
        .where(
            SiteModel.user_id == current_site.user_id,
            SiteModel.url_hash == target_hash,
        )
        .order_by(SiteModel.id)
    )
    # 同样用 first() 而非 scalar_one_or_none()：同一 host 可能有多条历史记录
    matched = (await session.execute(stmt)).scalars().first()
    if matched is not None:
        return matched.id
    return current_site.id


def should_crawl(site: Site, now: datetime | None = None) -> bool:
    """按上次抓取时间与失败次数判断是否该抓这个站点。"""
    now = now or datetime.now()
    # 注意用 `is False` 而非 `not` —— SQLAlchemy 的 Python 侧默认值只在
    # INSERT 时生效，构造出的对象属性可能是 None。用 `not` 判断会把
    # 「没设过 active」误判成「已停用」，导致站点永远不被抓取。
    if site.active is False:
        return False
    if site.last_crawled_at is None:
        return True
    interval = timedelta(hours=site.crawl_interval_hours or 12)
    return now - site.last_crawled_at >= interval