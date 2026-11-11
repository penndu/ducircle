"""UA 降级机制。

背景：有些站点按 User-Agent 拦截非浏览器请求。
默认用自研 bot UA 是对站主的尊重（可识别、可联系），
但被**明确拒绝**（403/406/429）时，说明这个站不接受当前访问方式，
换浏览器 UA 重试一次是合理的。

边界很重要：
- 只在明确拒绝时降级，不对所有请求都伪装
- 每个请求最多降级一次，不轮换 UA（那等于 UA 攻击）
"""

import pytest


class TestBrowserHeaders:
    def test_has_full_browser_headers(self):
        """反爬不只看 UA——Accept、Sec-Fetch-* 缺了也会被拦。"""
        from app.services.crawler import _browser_headers

        h = _browser_headers()
        assert "Mozilla/5.0" in h["User-Agent"]
        assert "Chrome" in h["User-Agent"]
        assert h["Accept"].startswith("text/html")
        assert "Sec-Fetch-Mode" in h
        assert "Sec-Ch-Ua" in h
        assert "Upgrade-Insecure-Requests" in h

    def test_referer_is_same_origin(self):
        """Referer 要是同源的，跨源或缺失都可能被拦。"""
        from app.services.crawler import _browser_headers

        h = _browser_headers(referer="https://x.com")
        assert h["Referer"] == "https://x.com"

    def test_origin_extraction(self):
        from app.services.crawler import _origin_of

        assert _origin_of("https://vjo.cc/feed/") == "https://vjo.cc"
        assert _origin_of("https://www.x.cn/a/b") == "https://www.x.cn"
        assert _origin_of("not-a-url") is None

    def test_blocked_status_list(self):
        """403/406/429 才是「拒绝」，404 不是。"""
        from app.services.crawler import BLOCKED_STATUS

        assert 403 in BLOCKED_STATUS
        assert 406 in BLOCKED_STATUS
        assert 429 in BLOCKED_STATUS
        assert 404 not in BLOCKED_STATUS
        assert 200 not in BLOCKED_STATUS


class TestUaFallbackBehaviour:
    @pytest.mark.asyncio
    async def test_403_triggers_browser_retry(self, monkeypatch):
        """被 403 拒绝时要用浏览器 UA 重试一次。"""
        from app.services.crawler import Crawler

        calls: list[dict] = []

        class _Resp:
            def __init__(self, code, text):
                self.status_code = code
                self.text = text
                self.url = "https://x.com/"

        async def _fake_get(self, url, headers=None):
            calls.append({"url": url, "headers": headers})
            if headers is None:
                return _Resp(403, "forbidden")
            return _Resp(200, "<html>ok</html>")

        monkeypatch.setattr(
            "httpx.AsyncClient.get", _fake_get, raising=False
        )

        async with Crawler() as crawler:
            code, text, final = await crawler._fetch_final_url("https://x.com/")

        assert code == 200
        assert text == "<html>ok</html>"
        assert len(calls) == 2, f"应该重试一次，实际调用 {len(calls)} 次"
        assert calls[1]["headers"] is not None, "重试要带浏览器头"
        assert "Mozilla" in calls[1]["headers"]["User-Agent"]

    @pytest.mark.asyncio
    async def test_no_retry_when_not_blocked(self, monkeypatch):
        """正常响应不重试——避免无谓的双倍请求。"""
        from app.services.crawler import Crawler

        calls: list[dict] = []

        class _Resp:
            status_code = 200
            text = "<html>ok</html>"
            url = "https://x.com/"

        async def _fake_get(self, url, headers=None):
            calls.append({"url": url, "headers": headers})
            return _Resp()

        monkeypatch.setattr(
            "httpx.AsyncClient.get", _fake_get, raising=False
        )

        async with Crawler() as crawler:
            await crawler._fetch_final_url("https://x.com/")

        assert len(calls) == 1, f"不该重试，实际调用 {len(calls)} 次"

    @pytest.mark.asyncio
    async def test_retry_only_once(self, monkeypatch):
        """最多降级一次——不轮换 UA，那等于 UA 攻击。"""
        from app.services.crawler import Crawler

        calls: list[dict] = []

        class _Resp:
            def __init__(self, code):
                self.status_code = code
                self.text = "blocked"
                self.url = "https://x.com/"

        async def _fake_get(self, url, headers=None):
            calls.append({"url": url, "headers": headers})
            return _Resp(403)

        monkeypatch.setattr(
            "httpx.AsyncClient.get", _fake_get, raising=False
        )

        async with Crawler() as crawler:
            code, _, _ = await crawler._fetch_final_url("https://x.com/")

        assert code == 403
        assert len(calls) == 2, f"降级后不该再重试，实际调用 {len(calls)} 次"

    @pytest.mark.asyncio
    async def test_fetch_also_falls_back(self, monkeypatch):
        """feed 候选探测用的 _fetch 也要有降级。"""
        from app.services.crawler import Crawler

        calls: list[dict] = []

        class _Resp:
            def __init__(self, code):
                self.status_code = code
                self.text = "blocked"
                self.url = "https://x.com/feed/"

        async def _fake_get(self, url, headers=None):
            calls.append({"url": url, "headers": headers})
            if headers is None:
                return _Resp(403)
            return _Resp(200)

        monkeypatch.setattr(
            "httpx.AsyncClient.get", _fake_get, raising=False
        )

        async with Crawler() as crawler:
            code, _ = await crawler._fetch("https://x.com/feed/")

        assert code == 200
        assert len(calls) == 2


class TestHttpStatusExplanation:
    def test_403_explains_ip_block(self):
        from app.services.crawler import _explain_http_status

        msg = _explain_http_status(403, "https://x.com")
        assert "IP" in msg
        assert "User-Agent" in msg

    def test_429_explains_rate_limit(self):
        from app.services.crawler import _explain_http_status

        msg = _explain_http_status(429, "https://x.com")
        assert "限流" in msg

    def test_404_is_simple(self):
        from app.services.crawler import _explain_http_status

        msg = _explain_http_status(404, "https://x.com")
        assert "不存在" in msg
        assert "IP" not in msg

    def test_default_fallback(self):
        from app.services.crawler import _explain_http_status

        msg = _explain_http_status(500, "https://x.com")
        assert "500" in msg
        assert "https://x.com" in msg


class TestDefaultUaStaysTransparent:
    def test_default_user_agent_is_our_own(self):
        """默认必须是自研 bot UA——对站主可识别是底线。"""
        from app.core.config import get_settings

        ua = get_settings().user_agent
        assert "ducircleBot" in ua
        assert "dusays.com" in ua
        # 不该默认就是浏览器 UA
        assert "Mozilla" not in ua
