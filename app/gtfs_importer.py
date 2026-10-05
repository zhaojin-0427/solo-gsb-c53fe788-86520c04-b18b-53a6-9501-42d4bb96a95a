"""GTFS Schedule ZIP 导入。

流程（全部处于单个事务中）：
1. 解压并校验必需文件；
2. 逐文件解析、做引用完整性与站时顺序校验，写入 stg_* 临时表；
3. 推导 trip pattern（同线路 + 相同有序停站序列）；
4. 清空正式表并整批写入。

任何一步失败都回滚，正式表中的旧数据不受影响，仍可继续查询。
"""

from __future__ import annotations

import csv
import io
import zipfile
from datetime import datetime, timezone

from .db import STG_DDL
from .timeutils import GTFSError, get_zone, parse_gtfs_date, parse_gtfs_time

REQUIRED_FILES = {
    "agency.txt",
    "stops.txt",
    "routes.txt",
    "trips.txt",
    "stop_times.txt",
}
MAX_ERRORS = 20
BATCH = 2000

WEEKDAY_COLS = [
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
]


class ImportRejected(GTFSError):
    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__("；".join(errors[:5]))


def _open_csv(zf: zipfile.ZipFile, member: str):
    raw = zf.read(member)
    text = raw.decode("utf-8-sig")
    return csv.DictReader(io.StringIO(text))


def _find_member(zf: zipfile.ZipFile, name: str) -> str | None:
    for info in zf.infolist():
        if not info.is_dir() and info.filename.split("/")[-1].lower() == name:
            return info.filename
    return None


def _req(row: dict, col: str, what: str, line: int, errors: list[str]) -> str | None:
    val = (row.get(col) or "").strip()
    if not val:
        errors.append(f"{what} 第 {line} 行：缺少 {col}")
        return None
    return val


def _batched_insert(cur, sql: str, rows: list[tuple]) -> None:
    if rows:
        cur.executemany(sql, rows)
        rows.clear()


def import_gtfs_zip(conn, payload: bytes) -> dict:
    errors: list[str] = []

    def fail():
        raise ImportRejected(errors)

    try:
        zf = zipfile.ZipFile(io.BytesIO(payload))
    except zipfile.BadZipFile:
        raise ImportRejected(["上传文件不是合法的 ZIP 包"])

    members = {}
    for n in ("agency.txt", "stops.txt", "routes.txt", "trips.txt",
              "stop_times.txt", "calendar.txt", "calendar_dates.txt",
              "transfers.txt"):
        m = _find_member(zf, n)
        if m:
            members[n] = m
    missing = REQUIRED_FILES - set(members)
    if missing:
        raise ImportRejected([f"ZIP 缺少必需文件：{', '.join(sorted(missing))}"])
    if "calendar.txt" not in members and "calendar_dates.txt" not in members:
        raise ImportRejected(["ZIP 缺少服务日历：calendar.txt 与 calendar_dates.txt 至少要有一个"])

    try:
        bad = zf.testzip()
        if bad:
            raise ImportRejected([f"ZIP 内容损坏：{bad}"])
    except zipfile.BadZipFile:
        raise
    except Exception as exc:  # noqa: BLE001
        raise ImportRejected([f"ZIP 内容损坏：{exc}"]) from exc

    conn.execute("BEGIN IMMEDIATE")
    try:
        cur = conn.cursor()
        cur.executescript(
            "; ".join(f"DROP TABLE IF EXISTS {t}" for t in (
                "stg_agencies", "stg_stops", "stg_routes", "stg_calendar",
                "stg_calendar_dates", "stg_trips", "stg_stop_times",
                "stg_transfers", "stg_patterns")) + ";"
        )
        cur.executescript(STG_DDL)

        # ------------------------------------------------------------------
        # agency.txt
        # ------------------------------------------------------------------
        agencies: dict[str, dict] = {}
        reader = _open_csv(zf, members["agency.txt"])
        for i, row in enumerate(reader, start=2):
            aid = (row.get("agency_id") or "").strip()
            name = _req(row, "agency_name", "agency.txt", i, errors)
            tzname = _req(row, "agency_timezone", "agency.txt", i, errors)
            if not aid:
                aid = "(default)"
            if name is None or tzname is None:
                continue
            if aid in agencies:
                errors.append(f"agency.txt 第 {i} 行：agency_id {aid!r} 重复")
                continue
            tz = get_zone(tzname)  # 非法时区直接拒绝
            agencies[aid] = {"name": name, "tz": str(tz)}
            if len(errors) >= MAX_ERRORS:
                fail()
        if not agencies:
            errors.append("agency.txt 没有任何 agency 记录")

        # ------------------------------------------------------------------
        # stops.txt（两遍：先收集，再校验 parent_station 引用）
        # ------------------------------------------------------------------
        stops: dict[str, dict] = {}
        reader = _open_csv(zf, members["stops.txt"])
        for i, row in enumerate(reader, start=2):
            sid = _req(row, "stop_id", "stops.txt", i, errors)
            sname = _req(row, "stop_name", "stops.txt", i, errors)
            if sid is None or sname is None:
                continue
            if sid in stops:
                errors.append(f"stops.txt 第 {i} 行：stop_id {sid!r} 重复")
                continue
            stops[sid] = {
                "name": sname,
                "parent": (row.get("parent_station") or "").strip() or None,
            }
            if len(errors) >= MAX_ERRORS:
                fail()
        if not stops:
            errors.append("stops.txt 没有任何 stop 记录")
        for sid, info in stops.items():
            if info["parent"] and info["parent"] not in stops:
                errors.append(
                    f"stops.txt：stop {sid!r} 的 parent_station "
                    f"{info['parent']!r} 不存在")
        if not stops and errors:
            fail()

        # ------------------------------------------------------------------
        # routes.txt
        # ------------------------------------------------------------------
        routes: dict[str, dict] = {}
        reader = _open_csv(zf, members["routes.txt"])
        for i, row in enumerate(reader, start=2):
            rid = _req(row, "route_id", "routes.txt", i, errors)
            if rid is None:
                continue
            if rid in routes:
                errors.append(f"routes.txt 第 {i} 行：route_id {rid!r} 重复")
                continue
            aid = (row.get("agency_id") or "").strip()
            if not aid:
                if len(agencies) == 1:
                    aid = next(iter(agencies))
                else:
                    errors.append(
                        f"routes.txt 第 {i} 行：route {rid!r} 未指定 agency_id，"
                        f"而数据中有多个 agency，无法判定归属")
                    continue
            elif aid not in agencies:
                errors.append(
                    f"routes.txt 第 {i} 行：route {rid!r} 引用了不存在的 "
                    f"agency_id {aid!r}")
                continue
            short = (row.get("route_short_name") or "").strip()
            longn = (row.get("route_long_name") or "").strip()
            if not short and not longn:
                errors.append(
                    f"routes.txt 第 {i} 行：route {rid!r} 必须提供 "
                    f"route_short_name 或 route_long_name")
                continue
            routes[rid] = {"agency": aid, "short": short, "long": longn}
            if len(errors) >= MAX_ERRORS:
                fail()
        if not routes:
            errors.append("routes.txt 没有任何 route 记录")

        # ------------------------------------------------------------------
        # calendar.txt / calendar_dates.txt
        # ------------------------------------------------------------------
        services: set[str] = set()
        cal_rows: list[tuple] = []
        if "calendar.txt" in members:
            reader = _open_csv(zf, members["calendar.txt"])
            for i, row in enumerate(reader, start=2):
                sid = _req(row, "service_id", "calendar.txt", i, errors)
                sd_raw = _req(row, "start_date", "calendar.txt", i, errors)
                ed_raw = _req(row, "end_date", "calendar.txt", i, errors)
                if sid is None or sd_raw is None or ed_raw is None:
                    continue
                if sid in services:
                    errors.append(
                        f"calendar.txt 第 {i} 行：service_id {sid!r} 重复")
                    continue
                try:
                    sd, ed = parse_gtfs_date(sd_raw), parse_gtfs_date(ed_raw)
                except GTFSError as exc:
                    errors.append(f"calendar.txt 第 {i} 行：{exc}")
                    continue
                if sd > ed:
                    errors.append(
                        f"calendar.txt 第 {i} 行：start_date 晚于 end_date")
                    continue
                bits = 0
                ok = True
                for b, col in enumerate(WEEKDAY_COLS):
                    v = (row.get(col) or "").strip()
                    if v not in ("0", "1"):
                        errors.append(
                            f"calendar.txt 第 {i} 行：{col} 必须是 0 或 1")
                        ok = False
                        break
                    if v == "1":
                        bits |= 1 << b
                if not ok:
                    continue
                services.add(sid)
                cal_rows.append(
                    (sid, sd.strftime("%Y%m%d"), ed.strftime("%Y%m%d"), bits))

        cd_rows: list[tuple] = []
        cd_seen: set[tuple[str, str]] = set()
        if "calendar_dates.txt" in members:
            reader = _open_csv(zf, members["calendar_dates.txt"])
            for i, row in enumerate(reader, start=2):
                sid = _req(row, "service_id", "calendar_dates.txt", i, errors)
                d_raw = _req(row, "date", "calendar_dates.txt", i, errors)
                t_raw = _req(row, "exception_type", "calendar_dates.txt",
                             i, errors)
                if sid is None or d_raw is None or t_raw is None:
                    continue
                if t_raw not in ("1", "2"):
                    errors.append(
                        f"calendar_dates.txt 第 {i} 行：exception_type "
                        f"必须是 1（加开）或 2（停运）")
                    continue
                try:
                    ds = parse_gtfs_date(d_raw).strftime("%Y%m%d")
                except GTFSError as exc:
                    errors.append(f"calendar_dates.txt 第 {i} 行：{exc}")
                    continue
                key = (sid, ds)
                if key in cd_seen:
                    errors.append(
                        f"calendar_dates.txt 第 {i} 行：service {sid!r} "
                        f"在 {ds} 的例外日期重复")
                    continue
                cd_seen.add(key)
                if sid not in services:
                    # calendar_dates 可以独立定义服务（仅有例外日的服务）
                    services.add(sid)
                cd_rows.append((sid, ds, int(t_raw)))
                if len(errors) >= MAX_ERRORS:
                    fail()

        # ------------------------------------------------------------------
        # trips.txt
        # ------------------------------------------------------------------
        trips: dict[str, tuple[str, str]] = {}
        reader = _open_csv(zf, members["trips.txt"])
        for i, row in enumerate(reader, start=2):
            tid = _req(row, "trip_id", "trips.txt", i, errors)
            rid = _req(row, "route_id", "trips.txt", i, errors)
            sid = _req(row, "service_id", "trips.txt", i, errors)
            if tid is None or rid is None or sid is None:
                continue
            if tid in trips:
                errors.append(f"trips.txt 第 {i} 行：trip_id {tid!r} 重复")
                continue
            if rid not in routes:
                errors.append(
                    f"trips.txt 第 {i} 行：trip {tid!r} 引用了不存在的 "
                    f"route_id {rid!r}")
                continue
            if sid not in services:
                errors.append(
                    f"trips.txt 第 {i} 行：trip {tid!r} 引用了不存在的 "
                    f"service_id {sid!r}")
                continue
            trips[tid] = (rid, sid)
            if len(errors) >= MAX_ERRORS:
                fail()
        if not trips:
            errors.append("trips.txt 没有任何 trip 记录")

        # ------------------------------------------------------------------
        # stop_times.txt（完整站时；顺序与时序校验）
        # ------------------------------------------------------------------
        st_buf: list[tuple] = []
        prev: dict[str, tuple[int, int]] = {}  # trip -> (stop_sequence, dep_sec)
        trip_seen: set[str] = set()
        reader = _open_csv(zf, members["stop_times.txt"])
        for i, row in enumerate(reader, start=2):
            tid = _req(row, "trip_id", "stop_times.txt", i, errors)
            seq_raw = _req(row, "stop_sequence", "stop_times.txt", i, errors)
            sid = _req(row, "stop_id", "stop_times.txt", i, errors)
            ar_raw = _req(row, "arrival_time", "stop_times.txt", i, errors)
            dp_raw = _req(row, "departure_time", "stop_times.txt", i, errors)
            if None in (tid, seq_raw, sid, ar_raw, dp_raw):
                continue
            if tid not in trips:
                errors.append(
                    f"stop_times.txt 第 {i} 行：引用了不存在的 trip_id {tid!r}")
                continue
            if sid not in stops:
                errors.append(
                    f"stop_times.txt 第 {i} 行：trip {tid!r} 引用了不存在的 "
                    f"stop_id {sid!r}")
                continue
            try:
                seq = int(seq_raw)
                if seq < 0:
                    raise ValueError
            except ValueError:
                errors.append(
                    f"stop_times.txt 第 {i} 行：stop_sequence 必须是非负整数")
                continue
            try:
                ar = parse_gtfs_time(ar_raw)
                dp = parse_gtfs_time(dp_raw)
            except GTFSError as exc:
                errors.append(f"stop_times.txt 第 {i} 行：{exc}")
                continue
            if dp < ar:
                errors.append(
                    f"stop_times.txt 第 {i} 行：trip {tid!r} 在 stop {sid!r} "
                    f"的发车时刻早于到达时刻")
                continue
            p = prev.get(tid)
            if p is not None:
                pseq, pdep = p
                if seq <= pseq:
                    errors.append(
                        f"stop_times.txt 第 {i} 行：trip {tid!r} 的 "
                        f"stop_sequence 未严格递增（{seq} <= {pseq}）")
                    continue
                if ar < pdep:
                    errors.append(
                        f"stop_times.txt 第 {i} 行：trip {tid!r} 站时倒序——"
                        f"stop {sid!r} 到达 {ar_raw} 早于上一站发车")
                    continue
            prev[tid] = (seq, dp)
            trip_seen.add(tid)
            st_buf.append((tid, seq, sid, ar, dp))
            if len(st_buf) >= BATCH:
                cur.executemany(
                    "INSERT INTO stg_stop_times VALUES (?,?,?,?,?)", st_buf)
                st_buf.clear()
            if len(errors) >= MAX_ERRORS:
                fail()
        _batched_insert(
            cur, "INSERT INTO stg_stop_times VALUES (?,?,?,?,?)", st_buf)

        for tid in trips:
            if tid not in trip_seen:
                errors.append(f"trip {tid!r} 没有任何 stop_times 记录")
        # 每个 trip 至少两站（否则无意义，也无法构成乘行段）
        counts = {r[0]: r[1] for r in cur.execute(
            "SELECT trip_id, COUNT(*) c FROM stg_stop_times GROUP BY trip_id")}
        for tid, c in counts.items():
            if c < 2:
                errors.append(f"trip {tid!r} 至少需要 2 个停站（当前 {c}）")

        # ------------------------------------------------------------------
        # transfers.txt（站点级显式有向换乘）
        # ------------------------------------------------------------------
        # (from,to) -> {type: min_sec}
        tr: dict[tuple[str, str], dict[int, int]] = {}
        if "transfers.txt" in members:
            reader = _open_csv(zf, members["transfers.txt"])
            for i, row in enumerate(reader, start=2):
                fr = _req(row, "from_stop_id", "transfers.txt", i, errors)
                to = _req(row, "to_stop_id", "transfers.txt", i, errors)
                t = (row.get("transfer_type") or "0").strip()
                if t not in ("0", "2", "3"):
                    errors.append(
                        f"transfers.txt 第 {i} 行：不支持的 transfer_type={t}"
                        f"（仅支持 0/2/3）")
                    continue
                if fr is None or to is None:
                    continue
                if fr not in stops:
                    errors.append(
                        f"transfers.txt 第 {i} 行：from_stop_id {fr!r} 不存在")
                    continue
                if to not in stops:
                    errors.append(
                        f"transfers.txt 第 {i} 行：to_stop_id {to!r} 不存在")
                    continue
                t = int(t)
                secs = 0
                mt = (row.get("min_transfer_time") or "").strip()
                if mt:
                    try:
                        secs = int(mt)
                        if secs < 0 or str(secs) != mt.lstrip("+"):
                            raise ValueError
                    except ValueError:
                        errors.append(
                            f"transfers.txt 第 {i} 行：min_transfer_time "
                            f"必须是非负整数秒")
                        continue
                elif t == 2:
                    errors.append(
                        f"transfers.txt 第 {i} 行：transfer_type=2 必须提供 "
                        f"min_transfer_time")
                    continue
                d = tr.setdefault((fr, to), {})
                d[t] = min(d.get(t, secs), secs)
                if len(errors) >= MAX_ERRORS:
                    fail()

        if errors:
            fail()

        # ------------------------------------------------------------------
        # 落临时表
        # ------------------------------------------------------------------
        cur.executemany("INSERT INTO stg_agencies VALUES (?,?,?)",
                        [(a, v["name"], v["tz"]) for a, v in agencies.items()])
        cur.executemany("INSERT INTO stg_stops VALUES (?,?,?)",
                        [(s, v["name"], v["parent"])
                         for s, v in stops.items()])
        cur.executemany("INSERT INTO stg_routes VALUES (?,?,?,?)",
                        [(r, v["agency"], v["short"], v["long"])
                         for r, v in routes.items()])
        cur.executemany("INSERT INTO stg_calendar VALUES (?,?,?,?)", cal_rows)
        cur.executemany(
            "INSERT OR IGNORE INTO stg_calendar_dates VALUES (?,?,?)", cd_rows)
        cur.executemany("INSERT INTO stg_trips VALUES (?,?,?)",
                        [(t, v[1], v[0]) for t, v in trips.items()])
        tr_rows = [(f, t, ty, sec) for (f, t), dm in tr.items()
                   for ty, sec in dm.items()]
        cur.executemany("INSERT OR REPLACE INTO stg_transfers VALUES (?,?,?,?)",
                        tr_rows)

        # ------------------------------------------------------------------
        # 推导 pattern：相同 route + 相同有序停站序列
        # ------------------------------------------------------------------
        trip_stops: dict[str, list[str]] = {t: [] for t in trips}
        for tid, _seq, sid, _ar, _dp in cur.execute(
                "SELECT trip_id, stop_sequence, stop_id, arrival_sec, "
                "departure_sec FROM stg_stop_times ORDER BY trip_id, "
                "stop_sequence"):
            trip_stops[tid].append(sid)

        pattern_key: dict[tuple, int] = {}
        pattern_rows: list[tuple[int, str, str]] = []
        trip_pattern: dict[str, int] = {}
        for tid, (rid, _sid) in trips.items():
            key = (rid, tuple(trip_stops[tid]))
            pid = pattern_key.get(key)
            if pid is None:
                pid = len(pattern_rows) + 1
                pattern_key[key] = pid
                pattern_rows.append((pid, rid, "\x00".join(key[1])))
            trip_pattern[tid] = pid
        cur.executemany("INSERT INTO stg_patterns VALUES (?,?,?)", pattern_rows)

        # ------------------------------------------------------------------
        # 正式表整批替换（同一事务）
        # ------------------------------------------------------------------
        for tbl in ("transfer_forbidden", "transfers", "stop_times", "trips",
                    "patterns", "calendar_dates", "calendar", "routes",
                    "stops", "agencies", "meta"):
            cur.execute(f"DELETE FROM {tbl}")

        cur.execute("INSERT INTO agencies SELECT agency_id, agency_name, "
                    "agency_timezone FROM stg_agencies")
        cur.execute("INSERT INTO stops SELECT stop_id, stop_name FROM stg_stops")
        cur.execute("INSERT INTO routes SELECT route_id, agency_id, "
                    "route_short_name, route_long_name FROM stg_routes")
        cur.execute("INSERT INTO calendar SELECT service_id, start_date, "
                    "end_date, weekdays FROM stg_calendar")
        cur.execute("INSERT INTO calendar_dates SELECT service_id, date, "
                    "exception_type FROM stg_calendar_dates")
        cur.execute("INSERT INTO patterns SELECT pattern_id, route_id, "
                    "stop_ids FROM stg_patterns")
        cur.execute("INSERT INTO trips (trip_id, service_id, route_id, "
                    "pattern_id) SELECT t.trip_id, t.service_id, t.route_id, 0 "
                    "FROM stg_trips t")
        cur.executemany("UPDATE trips SET pattern_id=? WHERE trip_id=?",
                        [(pid, tid) for tid, pid in trip_pattern.items()])
        cur.execute("INSERT INTO stop_times SELECT * FROM stg_stop_times")

        # 换乘规则聚合：type3 进禁止表；type0/2 取最小耗时。
        # 同一 (from,to) 若同时存在 type3 与其他类型，以 type3 为准（禁止优先）。
        cur.execute("""
            INSERT OR IGNORE INTO transfer_forbidden
            SELECT DISTINCT from_stop_id, to_stop_id
            FROM stg_transfers WHERE transfer_type = 3
        """)
        cur.execute("""
            INSERT INTO transfers (from_stop_id, to_stop_id, min_transfer_sec)
            SELECT from_stop_id, to_stop_id, MIN(min_transfer_sec)
            FROM stg_transfers
            WHERE transfer_type IN (0, 2)
            GROUP BY from_stop_id, to_stop_id
        """)
        cur.execute("""
            DELETE FROM transfers
            WHERE EXISTS (
                SELECT 1 FROM transfer_forbidden f
                WHERE f.from_stop_id = transfers.from_stop_id
                  AND f.to_stop_id = transfers.to_stop_id)
        """)

        summary = {
            "agencies": len(agencies),
            "stops": len(stops),
            "routes": len(routes),
            "trips": len(trips),
            "stop_times": sum(counts.values()),
            "transfers": cur.execute(
                "SELECT COUNT(*) FROM transfers").fetchone()[0],
            "forbidden": cur.execute(
                "SELECT COUNT(*) FROM transfer_forbidden").fetchone()[0],
            "patterns": len(pattern_rows),
        }
        cur.execute("INSERT INTO meta VALUES ('imported_at', ?)",
                    (datetime.now(timezone.utc).isoformat(),))
        for k, v in summary.items():
            cur.execute("INSERT INTO meta VALUES (?, ?)", (k, str(v)))

        conn.commit()
        return summary
    except Exception:
        conn.rollback()
        raise
    finally:
        try:
            cur.execute("; ".join(f"DROP TABLE IF EXISTS {t}" for t in (
                "stg_agencies", "stg_stops", "stg_routes", "stg_calendar",
                "stg_calendar_dates", "stg_trips", "stg_stop_times",
                "stg_transfers", "stg_patterns")) + ";")
        except Exception:  # noqa: BLE001
            pass
