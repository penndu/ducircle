"""中文按拼音排序。

MySQL 的 utf8mb4_unicode_ci 按 Unicode 码点排序，不是拼音。
对中文用户来说这是「乱」的（张三排到最后），但改 collation 会
影响索引行为且需要修改数据库结构，成本过高。

更轻的方案是查询时显式按拼音排——这个函数把字符串转成
pypinyin 风格的可比较形式。
"""

from __future__ import annotations

# 简单场景：导入失败时降级为原字符串，不能让排序报错
try:
    from pypinyin import lazy_pinyin

    def to_pinyin(text: str) -> str:
        """把中文转成拼音串，便于排序。

        「张三」→「zhang san」
        返回的字符串可直接参与 SQL ORDER BY（UTF-8 排序 = 拼音序）。
        """
        if not text:
            return ""
        # 拉丁字符直接保留，转小写让大小写不影响顺序
        return "".join(p.lower() for p in lazy_pinyin(text))
except ImportError:

    def to_pinyin(text: str) -> str:
        # 没装 pypinyin 时降级为原字符串：排序退化为码点序，
        # 但不会让请求失败。
        return text or ""
