"""10 万条站时规模下的性能测试（查询 < 2 秒，结果 <= 10 条）。"""

import random
import time
from datetime import date, timedelta

import pytest

from app.db import connect, init_db
from app.gtfs_importer import import_gtfs_zip
from app.planner import plan_journey


def _build_grid_gtfs(n_stops: int = 200, n_trips: int = 1000,
                     stops_per_trip: int = 100, seed: int = 7) -> bytes:
    """生成网格化线网：约 n_trips*stops_per_trip 条站时（>=10 万）。"""
    import io
    import zipfile

    rnd = random.Random(seed)
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as zf:
        zf.writestr("agency.txt",
                    "agency_id,agency_name,agency_url,agency_timezone\n"
                    "A1,公交公司,http://a,Asia/Shanghai\n")
        stops = "\n".join(
            f"S{i:04d},站点{i}" for i in range(n_stops))
        zf.writestr("stops.txt", "stop_id,stop_name\n" + stops + "\n")

        n_routes = 20
        routes = "\n".join(
            f"R{r:02d},A1,{r}路,线路{r}" for r in range(n_routes))
        zf.writestr("routes.txt",
                    "route_id,agency_id,route_short_name,route_long_name\n"
                    + routes + "\n")
        zf.writestr("calendar.txt",
                    "service_id,monday,tuesday,wednesday,thursday,friday,"
                    "saturday,sunday,start_date,end_date\n"
                    "SVC1,1,1,1,1,1,1,1,20260101,20261231\n")
        trips = "\n".join(f"T{i:05d},R{i % n_routes:02d},SVC1"
                          for i in range(n_trips))
        zf.writestr("trips.txt", "trip_id,route_id,service_id\n"
                    + trips + "\n")

        lines = []
        for t in range(n_trips):
            start = rnd.randrange(0, n_stops - stops_per_trip)
            tmin = 6 * 60 + rnd.randrange(0, 15 * 60)  # 06:00-21:00 发车
            cur = tmin
            for k in range(stops_per_trip):
                sid = f"S{start + k:04d}"
                hh, mm = divmod(cur, 60)
                ts = f"{hh:02d}:{mm:02d}:00"
                lines.append(f"T{t:05d},{k},{sid},{ts},{ts}")
                cur += rnd.choice([1, 2, 3, 4])  # 单调递增停站间隔
        zf.writestr("stop_times.txt",
                    "trip_id,stop_sequence,stop_id,arrival_time,"
                    "departure_time\n" + "\n".join(lines) + "\n")

        # 给少量相邻站加显式换乘边，制造换乘机会（同时含一些 0 秒边环）
        tr = ["from_stop_id,to_stop_id,transfer_type,min_transfer_time"]
        for i in range(0, n_stops, 2):
            tr.append(f"S{i:04d},S{(i+1) % n_stops:04d},2,120")
            tr.append(f"S{(i+1) % n_stops:04d},S{i:04d},2,120")
        zf.writestr("transfers.txt", "\n".join(tr) + "\n")
    return out.getvalue()


def test_perf_100k_stop_times(tmp_path):
    payload = _build_grid_gtfs()
    assert payload.count(b"T0") > 0
    db = str(tmp_path / "perf.db")
    conn = connect(db)
    init_db(conn)
    t = time.perf_counter()
    summary = import_gtfs_zip(conn, payload)
    imp = time.perf_counter() - t
    assert summary["stop_times"] >= 100_000

    d = date(2026, 10, 4)
    queries = [
        ("S0000", "S0199", "08:00:00", 2),
        ("S0050", "S0150", "09:30:00", 3),
        ("S0000", "S0100", "07:00:00", 1),
    ]
    for o, dst, tm, mt in queries:
        t = time.perf_counter()
        r = plan_journey(conn, o, dst, d, tm, max_transfers=mt)
        elapsed = time.perf_counter() - t
        assert len(r["journeys"]) <= 10, "结果不得超过 10 条（非支配集）"
        assert elapsed < 2.0, f"查询 {o}->{dst} 耗时 {elapsed:.3f}s 超过 2s"
    conn.close()


def _build_overtaking_gtfs(n_stops: int = 100, n_trips: int = 1000,
                           stops_per_trip: int = 100, seed: int = 11) -> bytes:
    """10 万站时、全部车次同属一个 pattern 且相邻车次系统性“后发先至”：
    站间间隔随机，后车常在前车之后发车却更早到达。迫使扫描并行保留多车，
    无法靠发车/本站到达先后剪枝。"""
    import io
    import zipfile

    rnd = random.Random(seed)
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as zf:
        zf.writestr("agency.txt",
                    "agency_id,agency_name,agency_url,agency_timezone\n"
                    "A1,公交公司,http://a,Asia/Shanghai\n")
        stops = "\n".join(f"S{i:04d},站点{i}" for i in range(n_stops))
        zf.writestr("stops.txt", "stop_id,stop_name\n" + stops + "\n")
        zf.writestr("routes.txt",
                    "route_id,agency_id,route_short_name,route_long_name\n"
                    "R00,A1,反超线,反超线路\n")
        zf.writestr("calendar.txt",
                    "service_id,monday,tuesday,wednesday,thursday,friday,"
                    "saturday,sunday,start_date,end_date\n"
                    "SVC1,1,1,1,1,1,1,1,20260101,20261231\n")
        trips = "\n".join(f"T{i:05d},R00,SVC1" for i in range(n_trips))
        zf.writestr("trips.txt", "trip_id,route_id,service_id\n"
                    + trips + "\n")
        lines = []
        for t in range(n_trips):
            cur = 6 * 60 + rnd.randrange(0, 20 * 60)
            for k in range(stops_per_trip):
                hh, mm = divmod(cur, 60)
                ts = f"{hh:02d}:{mm:02d}:00"
                lines.append(f"T{t:05d},{k},S{k:04d},{ts},{ts}")
                cur += rnd.choice([1, 2, 3, 4, 5, 6])
        zf.writestr("stop_times.txt",
                    "trip_id,stop_sequence,stop_id,arrival_time,"
                    "departure_time\n" + "\n".join(lines) + "\n")
        zf.writestr("transfers.txt",
                    "from_stop_id,to_stop_id,transfer_type,min_transfer_time\n")
    return out.getvalue()


def test_perf_100k_full_overtaking(tmp_path):
    """同 pattern 全面反超的 10 万站时线网：并行多车扫描仍须 < 2 秒。"""
    payload = _build_overtaking_gtfs()
    db = str(tmp_path / "perf_overtake.db")
    conn = connect(db)
    init_db(conn)
    summary = import_gtfs_zip(conn, payload)
    assert summary["stop_times"] >= 100_000

    d = date(2026, 10, 4)
    queries = [
        ("S0000", "S0099", "08:00:00", 2),
        ("S0000", "S0050", "07:00:00", 1),
        ("S0020", "S0090", "09:00:00", 3),
    ]
    for o, dst, tm, mt in queries:
        t = time.perf_counter()
        r = plan_journey(conn, o, dst, d, tm, max_transfers=mt)
        elapsed = time.perf_counter() - t
        assert len(r["journeys"]) <= 10, "结果不得超过 10 条（非支配集）"
        assert elapsed < 2.0, f"反超查询 {o}->{dst} 耗时 {elapsed:.3f}s 超过 2s"
    conn.close()
