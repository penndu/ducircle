"""API 路由包。

这里刻意不在导入时加载子模块。子模块之间存在循环依赖：
app.web 需要 app.api.deps，而 app.api.web_pages 又需要 app.web。
若在此处 import 各子模块，导入顺序稍有偏差就会出现
「partially initialized module」，或更隐蔽地拿到未定义的名字
（表现为运行时拿到协程对象，报 'coroutine' has no attribute ...）。

各路由在 app/main.py 里显式注册，导入顺序可控。
"""

__all__ = ["admin", "ai", "auth", "export", "posts", "sites", "users", "web_pages"]