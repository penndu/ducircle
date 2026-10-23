"""HTML 解析器兼容层。

selectolax 1.0 移除默认的 modest 后端，`from selectolax.parser import HTMLParser`
会直接抛 ImportError。1.x 的正确入口是 lexbor 模块，且类名为 LexborHTMLParser；
0.x 才是 HTMLParser。API 基本一致，这里统一封装，上层代码无需关心版本。
"""

try:  # selectolax >= 1.0
    from selectolax.lexbor import LexborHTMLParser as _HTMLParser
except ImportError:  # pragma: no cover - selectolax 0.x 回退
    from selectolax.parser import HTMLParser as _HTMLParser

HTMLParser = _HTMLParser

__all__ = ["HTMLParser"]