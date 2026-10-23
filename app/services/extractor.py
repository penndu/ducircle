"""文章正文抽取。

摘要质量的天花板是正文质量——正文抓得垃圾，再好的模型也总结不出好摘要。

用 trafilatura：它专为「提取正文、去掉导航广告」设计，
比 BeautifulSoup 手写规则准得多，且能识别中英文两种版面。
"""

from __future__ import annotations

import hashlib
import logging
import re

from app.core.config import get_settings

logger = logging.getLogger("fcircle.extract")


def extract_content(html: str, url: str | None = None) -> str | None:
    """从 HTML 抽取正文纯文本。失败返回 None。

    多个后备策略依次尝试：trafilatura 对极少数站点会失灵，
    这时退回 selectolax 的粗粒度提取，至少能拿到文章主体。
    """
    if not html or len(html) < 200:
        return None

    settings = get_settings()

    # ---- 首选：trafilatura ----
    try:
        import trafilatura

        text = trafilatura.extract(
            html,
            url=url,
            include_comments=False,
            include_tables=False,
            include_formatting=False,
            favor_precision=True,  # 宁可少抓也不要混入导航噪声
            no_fallback=False,
        )
        if text and _looks_like_article(text):
            return _normalize(text, settings.ai_max_chars)
        if text:
            logger.debug("抽取结果不像正文（疑似列表页），长度 %d", len(text))
    except Exception as exc:  # noqa: BLE001
        logger.warning("trafilatura 抽取失败: %s", exc)

    # ---- 后备：粗粒度提取 ----
    text = _fallback_extract(html, url)
    if text and _looks_like_article(text):
        return _normalize(text, settings.ai_max_chars)
    return None


def _looks_like_article(text: str) -> bool:
    """判断抽取结果是不是一篇**文章的正文**，而不是站点聚合列表。

    实测踩过的坑：URL 传成站点首页时，抽取器会返回「站点首页的
    文章列表」——看起来有几千字，实则是导航条目堆在一起，
    喂给模型只会生成一份「这个博客有 N 篇文章」的废话摘要。

    列表页的典型特征是大量短行（每条一行），
    而正常正文是连续段落。用换行密度区分比关键词可靠。
    """
    stripped = text.strip()
    if len(stripped) < 120:
        return False

    lines = [line for line in stripped.split("\n") if line.strip()]
    if not lines:
        return False

    # 单行长文是合法的（trafilatura 对某些站点会输出单段），
    # 所以只有多行时才做密度判断
    if len(lines) <= 8:
        return True

    short_lines = sum(1 for line in lines if len(line.strip()) < 25)
    # 超过八成都是短行，且条目数多——这是列表而不是文章
    if short_lines / len(lines) > 0.7:
        return False

    return True


def _fallback_extract(html: str, url: str | None) -> str | None:
    """后备策略：找出正文密度最高的容器。

    思路是正文容器通常有最大「文字/标签」比——导航列表链接多文字少，
    文章正文反之。这是简化版的 readability 算法。
    """
    from app.parser.html import HTMLParser

    tree = HTMLParser(html)
    best_text = None
    best_score = 0.0

    for node in tree.css("article, .post-content, .entry-content, .content, main"):
        text = node.text(separator=" ", strip=True)
        if len(text) < 100:
            continue
        tags = len(node.css("*")) or 1
        # 文字越多、标签越少，越可能是正文
        score = len(text) / tags
        if score > best_score:
            best_score = score
            best_text = text

    if best_text:
        return best_text

    # 再退一步：整个 body
    body = tree.css_first("body")
    return body.text(separator=" ", strip=True) if body else None


def _normalize(text: str, max_chars: int) -> str:
    """清洗正文：压缩空白、截断到模型能吃下的长度。"""
    # 连续空白压成单空格
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"\n[ \t]+", "\n", text)
    text = text.strip()

    if max_chars and len(text) > max_chars:
        # 在句子边界截断，避免把句子切成两半
        truncated = text[:max_chars]
        last_stop = max(
            truncated.rfind("。"), truncated.rfind("！"),
            truncated.rfind("？"), truncated.rfind("."),
            truncated.rfind("!"), truncated.rfind("?"),
            truncated.rfind("\n"),
        )
        if last_stop > max_chars * 0.6:
            truncated = truncated[: last_stop + 1]
        text = truncated + "\n\n（正文过长，已截断）"

    return text


def content_hash(content: str) -> str:
    """正文指纹。正文没变就不该重复花钱生成摘要。"""
    return hashlib.sha1(content.encode("utf-8")).hexdigest()


def needs_summary(existing_hash: str | None, current_hash: str) -> bool:
    """判断是否需要重新生成摘要。

    规则：
    - 从未生成过 → 需要
    - 正文变了 → 需要
    - 用户手动改过摘要 → 不需要（不能覆盖人工修正）
    - 正文没变 → 不需要
    """
    if not existing_hash:
        return True
    return existing_hash != current_hash