"""cron 表达式的校验与解析。

用户会自己填 cron 表达式，填错了不能让它进库——
一个非法表达式会让调度器在运行时炸掉，
而调度器是后台任务，炸了用户根本不知道。

所以这里做两件事：
1. 校验表达式合法性，给出**人话**的错误提示
2. 提供常用预设，用户不需要理解 cron 语法

支持的语法是标准 5 段 cron：
    分 时 日 月 周
    0 3 * * *      每天 3 点
    0 */2 * * *    每 2 小时
    30 8,20 * * *  每天 8:30 和 20:30
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from apscheduler.triggers.cron import CronTrigger

# 常用预设。前端直接用这个列表渲染下拉框。
# 值为 None 表示「用平台默认」。
CRON_PRESETS: list[tuple[str, str, str]] = [
    ("每 30 分钟", "*/30 * * * *", "适合刚添加友链、想快点看到结果"),
    ("每小时", "0 * * * *", "更新频繁的博客"),
    ("每 3 小时", "0 */3 * * *", "省资源但更新不算慢"),
    ("每 6 小时", "0 */6 * * *", "大多数博客的合适频率"),
    ("每 12 小时", "0 */12 * * *", "每天两次"),
    ("每天 凌晨 3 点", "0 3 * * *", "最省资源，延迟一天"),
    ("每天 早 8 点", "0 8 * * *", "早上看更新"),
    ("每天 早 6 点 & 晚 6 点", "0 6,18 * * *", "一天两次"),
    ("每周一 早 8 点", "0 8 * * 1", "周更博客"),
]


@dataclass
class CronValidation:
    """校验结果。"""

    valid: bool
    error: str | None = None
    description: str | None = None


def validate_cron(expression: str | None) -> CronValidation:
    """校验 cron 表达式。

    空字符串或 None 是合法的——表示「用平台默认」。
    """
    if expression is None or not expression.strip():
        return CronValidation(valid=True, description="使用平台默认频率")

    expr = expression.strip()

    # 长度检查——先给个明显的提示，比让 APScheduler 报错友好
    if len(expr.split()) != 5:
        return CronValidation(
            valid=False,
            error=f"cron 表达式应该是 5 段（分 时 日 月 周），现在有 {len(expr.split())} 段",
        )

    try:
        trigger = CronTrigger.from_crontab(expr)
    except (ValueError, TypeError) as exc:
        return CronValidation(valid=False, error=f"cron 表达式无效：{exc}")

    return CronValidation(valid=True, description=describe_cron(expr))


def describe_cron(expression: str) -> str:
    """把 cron 表达式翻译成人话。

    直接解析原始表达式，不依赖 APScheduler 的 fields 结构——
    那个是倒序的（year 在前 minute 在后），
    按正序索引取会全取错。
    """
    parts = expression.split()
    if len(parts) != 5:
        return ""

    minute, hour, day, month, week = parts

    # 每 N 分钟
    if minute.startswith("*/") and hour == "*":
        step = _leading_number(minute)
        if step:
            return f"每 {step} 分钟"

    # 每 N 小时
    if minute == "0" and hour.startswith("*/"):
        step = _leading_number(hour)
        if step:
            return f"每 {step} 小时"

    # 每天的几个钟点
    if "," in hour and minute == "0" and day == "*" and month == "*" and week == "*":
        hours = hour.split(",")
        if len(hours) == 2:
            return f"每天 {hours[0]} 点和 {hours[1]} 点"

    # 每周几
    if week != "*" and day == "*" and month == "*":
        names = {"1": "一", "2": "二", "3": "三", "4": "四",
                 "5": "五", "6": "六", "0": "日", "7": "日"}
        week_name = names.get(week, week)
        if minute == "0":
            return f"每周{week_name} {hour} 点"
        return f"每周{week_name} {hour}:{minute.zfill(2)}"

    # 每天
    if day == "*" and month == "*" and week == "*":
        if hour == "*":
            return "每小时"
        if minute == "0":
            return f"每天 {hour} 点"
        return f"每天 {hour}:{minute.zfill(2)}"

    return ""


def _leading_number(text: str) -> int | None:
    """从 "*/6" 里取出 6。"""
    if text.startswith("*/"):
        try:
            return int(text[2:])
        except ValueError:
            return None
    try:
        return int(text)
    except ValueError:
        return None


def user_cron_or_default(expression: str | None, default: str) -> str:
    """取用户的 cron，没配或非法就用默认。

    调度器里用这个——保证非法表达式不会让调度炸掉。
    """
    if not expression or not expression.strip():
        return default
    result = validate_cron(expression)
    return expression.strip() if result.valid else default


# 各字段的取值范围。*/N 的计算基准就是这个范围的起点。
# 分 -> 0, 时 -> 0, 日 -> 1, 月 -> 1, 周 -> 0
_FIELD_RANGES = {
    0: (0, 59),    # 分
    1: (0, 23),    # 时
    2: (1, 31),    # 日
    3: (1, 12),    # 月
    4: (0, 6),     # 周（0=周日）
}


def _match_field(field: str, value: int, index: int) -> bool:
    """判断一个 cron 字段是否匹配某个取值。

    支持的写法：
        *       任意
        5       恰好 5
        1-10    1 到 10
        */6     每 6 个（时字段是 0,6,12,18；分字段是 0,30）
        1-20/3  1 到 20 里每 3 个
        1,5,9   枚举

    index 是字段序号，用来取取值范围——
    */N 的基准必须是「该字段的最小值」，
    不是表达式里能推断出来的东西（"*/6" 里推不出起点）。
    """
    low_default, high_default = _FIELD_RANGES.get(index, (0, 0))

    # 枚举字段（"6,18"）里只要有一项匹配就算命中，
    # 所以不能遇到不匹配的就 return False——要继续试下一项。
    for part in field.split(","):
        part = part.strip()
        if not part:
            continue

        step = 1
        if "/" in part:
            part, step_text = part.split("/", 1)
            try:
                step = int(step_text)
            except ValueError:
                continue
            if step <= 0:
                continue

        if part == "*":
            # */N 从该字段的最小值开始每 N 个
            if low_default <= value <= high_default and (
                value - low_default
            ) % step == 0:
                return True
            continue

        if "-" in part:
            try:
                low, high = part.split("-", 1)
                low_i, high_i = int(low), int(high)
            except ValueError:
                continue
            if low_i <= value <= high_i and (value - low_i) % step == 0:
                return True
            continue

        try:
            if int(part) == value:
                return True
        except ValueError:
            continue

    return False


def cron_matches(expression: str, moment: datetime) -> bool:
    """判断某个时刻是否匹配 cron 表达式。

    自己实现而不是用 APScheduler 的 get_next_fire_time——
    那个方法的语义（返回之后的触发点、从 now 往前找）
    和直觉相反，混用时区后很难推理。

    匹配逻辑很直白：分、时、日、月、周五个字段逐一比对。
    """
    parts = expression.split()
    if len(parts) != 5:
        return False

    minute, hour, day, month, week = parts

    # cron 的星期：0 和 7 都是周日
    weekday = (moment.weekday() + 1) % 7  # 周一=0 -> 1；周日=6 -> 0
    if weekday == 0 and "7" in week:
        weekday = 7

    if not _match_field(minute, moment.minute, 0):
        return False
    if not _match_field(hour, moment.hour, 1):
        return False
    if not _match_field(month, moment.month, 3):
        return False

    # 日和周的组合规则（cron 标准）：
    #   两者都是 *      -> 每天都匹配
    #   只有 day 是 *  -> 只看周
    #   只有 week 是 * -> 只看日
    #   两者都不是 *   -> 「或」关系（任一匹配即可）
    #
    # 「或」是 cron 的标准行为，比如 "0 8 15 * 6" 表示
    # 「每月 15 号 或 每个周六 的 8 点」。
    day_star = day == "*"
    week_star = week == "*"

    if day_star and week_star:
        return True
    if day_star:
        return _match_field(week, weekday, 4)
    if week_star:
        return _match_field(day, moment.day, 2)

    return _match_field(day, moment.day, 2) or _match_field(week, weekday, 4)


def last_fire_time(
    expression: str | None,
    now: datetime,
    default: str | None = None,
    lookback_minutes: int = 1440,
) -> datetime | None:
    """找到 now 之前（含 now）最近的一个 cron 触发点。

    从 now 往回逐分钟找，最多重看 lookback_minutes 分钟（默认一天）。
    找不到返回 None——比如 cron 是「每周一 8 点」而今天是周二，
    往回看一天看不到（正常情况下应该能看到上周一，
    但那太久了，用 24 小时足够覆盖绝大多数场景，
    真要更长的回溯留给调用方传入更大的值）。
    """
    expr = (expression or default or "").strip()
    if not expr:
        return None

    moment = now.replace(second=0, microsecond=0)
    for _ in range(lookback_minutes + 1):
        if cron_matches(expr, moment):
            return moment
        moment -= timedelta(minutes=1)
    return None


def should_run_now(
    expression: str | None,
    last_run: datetime | None,
    now: datetime,
    default: str | None = None,
) -> bool:
    """判断这个用户现在该不该执行任务。

    规则：**最近一个触发点是否晚于上次执行时间**。

    这个语义不会漏也不会重：
    - 不漏：调度器延迟到 12:08 才跑，12:00 的触发点仍然会被识别
    - 不重：已经跑过（last_run >= 触发点）就不会再跑

    last_run 是 None（第一次执行）时，只看最近触发点是不是
    「不太久以前」——默认 10 分钟内才算，
    否则新用户一上线就会把过去一天的活全补跑一遍。
    """
    fire_time = last_fire_time(expression, now, default)
    if fire_time is None:
        return False

    if last_run is None:
        # 第一次：只在最近 10 分钟内有过触发点才执行
        return (now - fire_time) <= timedelta(minutes=10)

    return fire_time > last_run


def should_fire_in_window(
    expression: str | None,
    window_start: datetime,
    window_end: datetime,
    default: str | None = None,
) -> bool:
    """判断 cron 表达式在给定时间窗口内是否应该触发。

    这是用户级频率的实现基础：
    调度器每 N 分钟跑一次（比如 5 分钟），
    每次检查每个用户的 cron 在这个窗口内有没有触发点，
    有就执行这个用户的任务。

    实现方式是**按分钟扫描窗口**，找第一个匹配的时刻。
    窗口通常只有 5 分钟，扫描代价可以忽略，
    而且完全避免了 APScheduler 的时区转换问题。

    窗口判断而不是「当前时间是否匹配」：
    窗口能容忍调度器晚几秒触发——比如 cron 是 12:00，
    调度器 12:00:03 才跑，仍然算命中。
    """
    expr = (expression or default or "").strip()
    if not expr:
        return False

    # 逐分钟扫描窗口
    moment = window_start.replace(second=0, microsecond=0)
    # 从窗口起点所在的这一分钟开始
    if moment < window_start:
        moment = moment + timedelta(minutes=1)

    limit = window_end + timedelta(minutes=1)
    while moment < limit:
        if cron_matches(expr, moment):
            return True
        moment += timedelta(minutes=1)

    return False
