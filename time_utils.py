#!/usr/bin/env python3
"""
time_utils.py — 统一时区工具 v1.0
==================================
整个项目统一使用 Asia/Shanghai (UTC+8) 时区。
替换所有 datetime.now() → now() 确保跨平台一致性。

用法:
    from time_utils import now       # 替代 datetime.now()
    from time_utils import today_str  # 替代 datetime.now().strftime("%Y-%m-%d")
    from time_utils import now_iso    # 替代 datetime.now().isoformat()
    from time_utils import now_str    # 替代 datetime.now().strftime("%Y-%m-%d %H:%M:%S")

设计原则:
    - datetime.now(ZoneInfo) 返回带时区的 aware datetime
    - .isoformat() 自动包含 +08:00 后缀
    - .strftime() 输出本地时间字符串
    - 所有缓存/间隔判断继续使用 time.time() (UTC epoch, 时区无关)
"""

import time
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

# ── 项目统一时区 ──
TZ = ZoneInfo("Asia/Shanghai")
TZ_NAME = "Asia/Shanghai"
UTC_OFFSET = "+08:00"

# ═══════════════════════════════════════════════
# 核心函数 (推荐使用)
# ═══════════════════════════════════════════════


def now() -> datetime:
    """返回带 Asia/Shanghai 时区的当前时间 (aware datetime)。
    替代 datetime.now() — .isoformat() 会输出 +08:00。
    """
    return datetime.now(TZ)


def now_ts() -> float:
    """返回当前 UTC epoch 时间戳 (秒)。
    等同于 time.time() — 用于间隔/缓存 TTL 计算, 时区无关。
    """
    return time.time()


def today_str() -> str:
    """返回今日日期字符串 "YYYY-MM-DD" (Asia/Shanghai)。"""
    return now().strftime("%Y-%m-%d")


def now_iso() -> str:
    """返回 ISO 格式时间字符串, 含时区偏移。
    示例: "2026-06-03T12:30:45.123456+08:00"
    """
    return now().isoformat()


def now_str(fmt: str = "%Y-%m-%d %H:%M:%S") -> str:
    """返回格式化时间字符串 (Asia/Shanghai 本地时间)。
    示例: "2026-06-03 12:30:45"
    """
    return now().strftime(fmt)


def now_str_compact() -> str:
    """返回紧凑格式 "YYYYMMDD_HHMMSS", 用于文件名。"""
    return now().strftime("%Y%m%d_%H%M%S")


# ═══════════════════════════════════════════════
# 工具
# ═══════════════════════════════════════════════


def datetime_from_iso(iso_string: str) -> datetime:
    """从 ISO 字符串解析为 aware datetime (Asia/Shanghai)。
    兼容带/不带时区的输入, 不带时区时假定为 Asia/Shanghai。
    """
    dt = datetime.fromisoformat(iso_string)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ)
    else:
        dt = dt.astimezone(TZ)
    return dt


def ts_to_datetime(timestamp: float) -> datetime:
    """将 epoch 时间戳转为 Asia/Shanghai aware datetime。"""
    return datetime.fromtimestamp(timestamp, tz=TZ)


# ═══════════════════════════════════════════════
# 自检
# ═══════════════════════════════════════════════

if __name__ == "__main__":
    print(f"时区: {TZ_NAME}")
    print(f"当前时间 (iso):  {now_iso()}")
    print(f"当前时间 (str):  {now_str()}")
    print(f"今日日期:        {today_str()}")
    print(f"紧凑格式:        {now_str_compact()}")
    print(f"Epoch时间戳:     {now_ts()}")
    print(f"UTC时间:         {datetime.now(timezone.utc).isoformat()}")

    # 验证时区一致性
    n = now()
    assert n.tzinfo is not None, "FAIL: datetime 无时区信息!"
    assert n.utcoffset() == timedelta(hours=8), f"FAIL: UTC偏移不是 +08:00, 实际 {n.utcoffset()}"
    assert "+08:00" in now_iso(), "FAIL: isoformat 不含 +08:00"
    print("\n[OK] All timezone checks passed")
