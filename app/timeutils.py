"""时间工具：GTFS 时刻（HH:MM:SS，小时可 >=24）与日期、时区换算。"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

GTFS_TIME_RE = re.compile(r"^(\d{1,3}):([0-5]\d):([0-5]\d)$")
GTFS_DATE_RE = re.compile(r"^\d{8}$")


class GTFSError(ValueError):
    """数据错误：触发整包拒绝。"""


def parse_gtfs_time(value: str) -> int:
    """``HH:MM:SS`` -> 相对服务日零点的秒数；小时允许超过 23。"""
    m = GTFS_TIME_RE.match(value.strip())
    if not m:
        raise GTFSError(f"非法时刻 {value!r}（应为 HH:MM:SS，小时可 >=24）")
    h, minute, sec = (int(x) for x in m.groups())
    return h * 3600 + minute * 60 + sec


def parse_gtfs_date(value: str) -> date:
    if not GTFS_DATE_RE.match(value.strip()):
        raise GTFSError(f"非法日期 {value!r}（应为 YYYYMMDD）")
    value = value.strip()
    return date(int(value[0:4]), int(value[4:6]), int(value[6:8]))


def gtfs_date_str(d: date) -> str:
    return d.strftime("%Y%m%d")


def get_zone(tz_name: str) -> ZoneInfo:
    try:
        return ZoneInfo(tz_name)
    except Exception as exc:  # noqa: BLE001
        raise GTFSError(f"非法时区 {tz_name!r}: {exc}") from exc


def local_midnight(d: date, tz: ZoneInfo) -> datetime:
    """指定 agency 时区下某日零点（naive 表示，与时区算术解耦）。"""
    return datetime(d.year, d.month, d.day)


def parse_local_datetime(d: date, hhmmss: str) -> datetime:
    sec = parse_gtfs_time(hhmmss)
    return datetime(d.year, d.month, d.day) + timedelta(seconds=sec)


def fmt_local_dt(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def fmt_local_time(dt: datetime) -> str:
    return dt.strftime("%H:%M:%S")


def secs_to_hms(sec: int) -> str:
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"
