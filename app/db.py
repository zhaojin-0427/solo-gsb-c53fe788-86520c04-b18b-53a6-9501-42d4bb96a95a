"""SQLite 连接与表结构。

数据库分为两组表：
- 正式表（agencies / stops / routes / ...）：保存最近一次成功导入的完整 GTFS；
- 导入临时表（stg_*）：仅在单次导入事务中存在，导入失败时随事务回滚，
  正式表中的旧数据因此仍可查询。
"""

from __future__ import annotations

import os

DB_PATH = os.environ.get("DB_PATH", "/data/transit.db")

# ---------------------------------------------------------------------------
# 正式表
# ---------------------------------------------------------------------------

PROD_DDL = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS agencies (
    agency_id   TEXT PRIMARY KEY,
    agency_name TEXT NOT NULL,
    agency_timezone TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS stops (
    stop_id   TEXT PRIMARY KEY,
    stop_name TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS routes (
    route_id        TEXT PRIMARY KEY,
    agency_id       TEXT,
    route_short_name TEXT,
    route_long_name  TEXT
);

CREATE TABLE IF NOT EXISTS calendar (
    service_id TEXT PRIMARY KEY,
    start_date TEXT NOT NULL,
    end_date   TEXT NOT NULL,
    -- bit0=周一 ... bit6=周日
    weekdays   INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS calendar_dates (
    service_id TEXT NOT NULL,
    date       TEXT NOT NULL,
    exception_type INTEGER NOT NULL, -- 1=加开 2=停运
    PRIMARY KEY (service_id, date)
);

CREATE TABLE IF NOT EXISTS patterns (
    pattern_id INTEGER PRIMARY KEY,
    route_id   TEXT NOT NULL,
    stop_ids   TEXT NOT NULL  -- 以 %00 连接的有序 stop_id 序列
);

CREATE TABLE IF NOT EXISTS trips (
    trip_id     TEXT PRIMARY KEY,
    service_id  TEXT NOT NULL,
    route_id    TEXT NOT NULL,
    pattern_id  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_trips_service ON trips(service_id);

CREATE TABLE IF NOT EXISTS stop_times (
    trip_id        TEXT NOT NULL,
    stop_sequence  INTEGER NOT NULL,
    stop_id        TEXT NOT NULL,
    arrival_sec    INTEGER NOT NULL,   -- 相对服务日零点的秒数，可 >= 86400
    departure_sec  INTEGER NOT NULL,
    PRIMARY KEY (trip_id, stop_sequence)
);
CREATE INDEX IF NOT EXISTS idx_stoptimes_stop_dep ON stop_times(stop_id, departure_sec);
CREATE INDEX IF NOT EXISTS idx_stoptimes_trip ON stop_times(trip_id, stop_sequence);

-- 显式有向换乘边（GTFS transfers 中 transfer_type=0/2，多规则聚合后）
CREATE TABLE IF NOT EXISTS transfers (
    from_stop_id      TEXT NOT NULL,
    to_stop_id        TEXT NOT NULL,
    min_transfer_sec  INTEGER NOT NULL,
    PRIMARY KEY (from_stop_id, to_stop_id)
);

-- 不可用的换乘方向（transfer_type=3）
CREATE TABLE IF NOT EXISTS transfer_forbidden (
    from_stop_id TEXT NOT NULL,
    to_stop_id   TEXT NOT NULL,
    PRIMARY KEY (from_stop_id, to_stop_id)
);
"""

# ---------------------------------------------------------------------------
# 导入临时表（结构与正式表一致，外加数据处理所需列）
# ---------------------------------------------------------------------------

STG_DDL = """
CREATE TABLE stg_agencies (
    agency_id TEXT PRIMARY KEY,
    agency_name TEXT NOT NULL,
    agency_timezone TEXT NOT NULL
);
CREATE TABLE stg_stops (
    stop_id TEXT PRIMARY KEY,
    stop_name TEXT NOT NULL,
    parent_station TEXT
);
CREATE TABLE stg_routes (
    route_id TEXT PRIMARY KEY,
    agency_id TEXT,
    route_short_name TEXT,
    route_long_name TEXT
);
CREATE TABLE stg_calendar (
    service_id TEXT PRIMARY KEY,
    start_date TEXT NOT NULL,
    end_date TEXT NOT NULL,
    weekdays INTEGER NOT NULL
);
CREATE TABLE stg_calendar_dates (
    service_id TEXT NOT NULL,
    date TEXT NOT NULL,
    exception_type INTEGER NOT NULL,
    PRIMARY KEY (service_id, date)
);
CREATE TABLE stg_trips (
    trip_id TEXT PRIMARY KEY,
    service_id TEXT NOT NULL,
    route_id TEXT NOT NULL
);
CREATE TABLE stg_stop_times (
    trip_id TEXT NOT NULL,
    stop_sequence INTEGER NOT NULL,
    stop_id TEXT NOT NULL,
    arrival_sec INTEGER NOT NULL,
    departure_sec INTEGER NOT NULL,
    PRIMARY KEY (trip_id, stop_sequence)
);
CREATE TABLE stg_transfers (
    from_stop_id TEXT NOT NULL,
    to_stop_id TEXT NOT NULL,
    transfer_type INTEGER NOT NULL,
    min_transfer_sec INTEGER NOT NULL,
    PRIMARY KEY (from_stop_id, to_stop_id, transfer_type)
);
CREATE TABLE stg_patterns (
    pattern_id INTEGER PRIMARY KEY,
    route_id   TEXT NOT NULL,
    stop_ids   TEXT NOT NULL
);
"""


def connect(db_path: str | None = None) -> "sqlite3.Connection":
    import sqlite3

    path = db_path or DB_PATH
    folder = os.path.dirname(os.path.abspath(path))
    os.makedirs(folder, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA temp_store = MEMORY")
    conn.execute("PRAGMA cache_size = -65536")
    return conn


def init_db(conn) -> None:
    conn.executescript(PROD_DDL)
    conn.commit()
