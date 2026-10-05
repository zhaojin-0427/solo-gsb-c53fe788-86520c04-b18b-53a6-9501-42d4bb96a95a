"""行程规划功能测试。"""

from datetime import date

import pytest

from app.db import connect, init_db
from app.gtfs_importer import ImportRejected, import_gtfs_zip
from app.planner import plan_journey
from tests.gtfs_factory import base_gtfs, make_zip

D = date(2026, 10, 4)  # 周日，calendar 全周运营


@pytest.fixture()
def conn(tmp_path):
    c = connect(str(tmp_path / "t.db"))
    init_db(c)
    yield c
    c.close()


def load(c, **over):
    import_gtfs_zip(c, make_zip(base_gtfs(**over)))


# --------------------------------------------------------------------------
# 基本查询
# --------------------------------------------------------------------------

def test_direct_trip(conn):
    load(conn)
    r = plan_journey(conn, "S1", "S4", D, "08:00:00", max_transfers=0)
    assert r["count"] == 1
    jy = r["journeys"][0]
    assert jy["transfers"] == 0
    assert len(jy["legs"]) == 1
    leg = jy["legs"][0]
    assert leg["trip_id"] == "T1"
    assert leg["board_time"] == "2026-10-04 08:00:00"
    assert leg["alight_time"] == "2026-10-04 08:40:00"


def test_one_transfer_same_stop(conn):
    """S1->S2 乘 T2，S2 同站换 T3 到 S4。"""
    load(conn)
    r = plan_journey(conn, "S1", "S4", D, "08:55:00", max_transfers=1)
    # 同站无规则可换车：S2 没有 (S2,S2) 禁止边
    assert r["count"] >= 1
    best = None
    for jy in r["journeys"]:
        if [l["trip_id"] for l in jy["legs"]] == ["T2", "T3"]:
            best = jy
    assert best is not None
    assert best["transfers"] == 1
    assert best["legs"][0]["alight_time"] == "2026-10-04 09:10:00"
    assert best["legs"][1]["board_time"] == "2026-10-04 09:20:00"
    assert best["arrival_time"] == "2026-10-04 09:30:00"


def test_cross_stop_transfer_requires_edge(conn):
    """跨站换乘只走显式有向边，并满足最小耗时。"""
    files = base_gtfs()
    # 线网：
    #  T1: S1 08:00 -> S3 08:30 -> S4 08:40
    #  T3: S2 09:20 -> S5 09:50
    # 换乘边 S3->S2 需 600s；无 S4->S2 边
    files["stop_times.txt"] = (
        "trip_id,stop_sequence,stop_id,arrival_time,departure_time\n"
        "T1,0,S1,08:00:00,08:00:00\n"
        "T1,1,S3,08:30:00,08:30:00\n"
        "T1,2,S4,08:40:00,08:40:00\n"
        "T3,0,S2,09:20:00,09:20:00\n"
        "T3,1,S5,09:50:00,09:50:00\n")
    files["trips.txt"] = (
        "trip_id,route_id,service_id\nT1,R1,SVC1\nT3,R2,SVC1\n")
    files["transfers.txt"] = (
        "from_stop_id,to_stop_id,transfer_type,min_transfer_time\n"
        "S3,S2,2,600\n")
    load(conn, **files)

    # S4->S2 没有边：无法换 T3
    r0 = plan_journey(conn, "S4", "S5", D, "08:40:00", max_transfers=1)
    assert r0["count"] == 0

    # 最小耗时 1 秒都不能少：要求 600s 时，8:30 下车、8:39 才到 S2 的边不存在
    r = plan_journey(conn, "S1", "S5", D, "08:00:00", max_transfers=1)
    assert r["count"] == 1
    jy = r["journeys"][0]
    assert [l["trip_id"] for l in jy["legs"]] == ["T1", "T3"]
    assert jy["legs"][1]["board_time"] == "2026-10-04 09:20:00"
    assert jy["arrival_time"] == "2026-10-04 09:50:00"


def test_min_transfer_time_enforced(conn):
    files = base_gtfs()
    # T1 到 S2 后想换紧邻发车的 TX，但换乘边要求 300s
    files["stop_times.txt"] = (
        "trip_id,stop_sequence,stop_id,arrival_time,departure_time\n"
        "T1,0,S1,08:00:00,08:00:00\n"
        "T1,1,S3,08:30:00,08:30:00\n"
        "TX,0,S2,08:34:00,08:34:00\n"   # 08:30+300s=08:35 才到，赶不上
        "TX,1,S5,08:40:00,08:40:00\n"
        "TY,0,S2,08:40:00,08:40:00\n"
        "TY,1,S5,08:50:00,08:50:00\n")
    files["trips.txt"] = (
        "trip_id,route_id,service_id\nT1,R1,SVC1\nTX,R2,SVC1\nTY,R2,SVC1\n")
    files["transfers.txt"] = (
        "from_stop_id,to_stop_id,transfer_type,min_transfer_time\n"
        "S3,S2,2,300\n")
    load(conn, **files)
    r = plan_journey(conn, "S1", "S5", D, "08:00:00", max_transfers=1)
    assert r["count"] == 1
    assert [l["trip_id"] for l in r["journeys"][0]["legs"]] == ["T1", "TY"]


def test_forbidden_transfer_type3(conn):
    load(conn, **{"transfers.txt": (
        "from_stop_id,to_stop_id,transfer_type,min_transfer_time\n"
        "S2,S2,3,\n")})
    # S2 同站换乘被禁止：T2->T3 不再可行
    r = plan_journey(conn, "S1", "S4", D, "08:55:00", max_transfers=2)
    chains = [tuple(l["trip_id"] for l in j["legs"]) for j in r["journeys"]]
    assert ("T2", "T3") not in chains


def test_same_origin_destination_zero_legs(conn):
    load(conn)
    r = plan_journey(conn, "S2", "S2", D, "08:00:00", max_transfers=3)
    assert r["count"] == 1
    assert r["journeys"][0]["legs"] == []
    assert r["journeys"][0]["transfers"] == 0
    assert r["journeys"][0]["arrival_time"] == "2026-10-04 08:00:00"


def test_no_solution_empty(conn):
    # 线网中根本没有可达 S5 的路径（S5 无任何经过的运营车经改线后）
    files = base_gtfs()
    files["stop_times.txt"] = (
        "trip_id,stop_sequence,stop_id,arrival_time,departure_time\n"
        "T1,0,S1,08:00:00,08:00:00\n"
        "T1,1,S3,08:30:00,08:30:00\n"
        "T1,2,S4,08:40:00,08:40:00\n")
    files["trips.txt"] = "trip_id,route_id,service_id\nT1,R1,SVC1\n"
    load(conn, **files)
    r = plan_journey(conn, "S1", "S5", D, "20:00:00", max_transfers=3)
    assert r["count"] == 0
    assert r["journeys"] == []


def test_no_service_on_dark_day(conn):
    # 服务仅工作日运营；周日当天停运，且前/后日车也不在窗口内可达目标
    files = base_gtfs()
    files["calendar.txt"] = (
        "service_id,monday,tuesday,wednesday,thursday,friday,saturday,"
        "sunday,start_date,end_date\n"
        # 周一到周五，周六日不运营
        "SVC1,1,1,1,1,1,0,0,20260101,20261231\n")
    load(conn, **files)
    # 2026-10-04 周日：前一日周六也停运；最近的周一 08:00 车在窗口外
    r = plan_journey(conn, "S1", "S4", date(2026, 10, 4),
                     "07:00:00", max_transfers=0)
    assert r["count"] == 0


def test_after_midnight_times_and_prev_day_service(conn):
    """25:xx 站时与前一日服务展开。"""
    files = base_gtfs()
    # 跨夜 trip：S4 -> S1，跨午夜（次日 01:10）
    files["routes.txt"] += "R3,A1,3路,夜线\n"
    files["trips.txt"] += "TN,R3,SVC1\n"
    files["stop_times.txt"] += (
        "TN,0,S4,24:30:00,24:30:00\n"
        "TN,1,S1,25:10:00,25:10:00\n")
    load(conn, **files)
    # 当日 00:30 出发：前日服务的夜车 24:30 = 当日 00:30
    r = plan_journey(conn, "S4", "S1", date(2026, 10, 4),
                     "00:20:00", max_transfers=0)
    assert r["count"] == 1
    assert r["journeys"][0]["legs"][0]["trip_id"] == "TN"
    assert r["journeys"][0]["legs"][0]["board_time"] == \
        "2026-10-04 00:30:00"
    assert r["journeys"][0]["arrival_time"] == "2026-10-04 01:10:00"


def test_calendar_dates_override(conn):
    """例外停运覆盖周历；加开日可乘车。"""
    files = base_gtfs()
    files["calendar_dates.txt"] = (
        "service_id,date,exception_type\n"
        "SVC1,20261004,2\n")  # 2026-10-04 周日停运
    load(conn, **files)
    # 07:00 出发：周六末班车（前日 09:00）已过，周一首车 08:00 在窗口外
    r = plan_journey(conn, "S1", "S4", D, "07:00:00", max_transfers=0)
    assert r["count"] == 0

    # 周六 10-03 正常运营
    r2 = plan_journey(conn, "S1", "S4", date(2026, 10, 3),
                      "08:00:00", max_transfers=0)
    assert r2["count"] == 1


def test_service_only_in_calendar_dates(conn):
    files = base_gtfs()
    del files["calendar.txt"]
    files["calendar_dates.txt"] = (
        "service_id,date,exception_type\n"
        "SVC1,20261004,1\n")
    load(conn, **files)
    r = plan_journey(conn, "S1", "S4", D, "08:00:00", max_transfers=0)
    assert r["count"] == 1


def test_tie_smaller_trip_sequence(conn):
    """同到达同换乘次数：保留 trip_id 序列字典序最小者。"""
    files = base_gtfs()
    # 再加一条与 T2 同时刻、trip_id 字典序更小的平行车 T0
    files["trips.txt"] += "T0,R1,SVC1\n"
    files["stop_times.txt"] += (
        "T0,0,S1,09:00:00,09:00:00\n"
        "T0,1,S2,09:10:00,09:10:00\n"
        "T0,2,S5,09:40:00,09:40:00\n")
    load(conn, **files)
    r = plan_journey(conn, "S1", "S5", D, "08:55:00", max_transfers=0)
    # T0 与 T2 同达，只留一个，且为字典序更小的 T0
    assert r["count"] == 1
    assert r["journeys"][0]["legs"][0]["trip_id"] == "T0"


def test_same_vehicle_ride_through_not_transfer(conn):
    """直达车经停 S3 不产生换乘。"""
    load(conn)
    r = plan_journey(conn, "S1", "S4", D, "08:00:00", max_transfers=0)
    jy = r["journeys"][0]
    assert len(jy["legs"]) == 1
    assert jy["transfers"] == 0


# --------------------------------------------------------------------------
# 同线路同停站序列“后发先至”
# --------------------------------------------------------------------------

def _overtake_gtfs():
    """T1 先发晚到、T2 后发先至（同 pattern S1,S2,S3）；
    T3 08:25 自 S3 去 S5，仅 T2 可接续。"""
    return {
        "agency.txt": (
            "agency_id,agency_name,agency_url,agency_timezone\n"
            "A1,公交一公司,http://a,Asia/Shanghai\n"),
        "stops.txt": (
            "stop_id,stop_name\nS1,一号站\nS2,二号站\nS3,三号站\nS5,五号站\n"),
        "routes.txt": (
            "route_id,agency_id,route_short_name,route_long_name\n"
            "R1,A1,1路,一号线\nR2,A1,2路,二号线\n"),
        "trips.txt": (
            "trip_id,route_id,service_id\n"
            "T1,R1,SVC1\nT2,R1,SVC1\nT3,R2,SVC1\n"),
        "calendar.txt": (
            "service_id,monday,tuesday,wednesday,thursday,friday,saturday,"
            "sunday,start_date,end_date\n"
            "SVC1,1,1,1,1,1,1,1,20260101,20261231\n"),
        "calendar_dates.txt": "",
        "stop_times.txt": (
            "trip_id,stop_sequence,stop_id,arrival_time,departure_time\n"
            "T1,0,S1,08:00:00,08:00:00\n"
            "T1,1,S2,08:10:00,08:10:00\n"
            "T1,2,S3,09:00:00,09:00:00\n"
            "T2,0,S1,08:05:00,08:05:00\n"
            "T2,1,S2,08:15:00,08:15:00\n"
            "T2,2,S3,08:20:00,08:20:00\n"
            "T3,0,S3,08:25:00,08:25:00\n"
            "T3,1,S5,08:40:00,08:40:00\n"),
        "transfers.txt": (
            "from_stop_id,to_stop_id,transfer_type,min_transfer_time\n"),
    }


def test_overtaking_later_trip_arrives_earlier(conn):
    """同 pattern 后发先至：T2 08:05 发但 08:20 到 S3，必须进入结果；
    只按最早发车/本站到达会漏报 T2。"""
    import_gtfs_zip(conn, make_zip(_overtake_gtfs()))
    r = plan_journey(conn, "S1", "S3", D, "08:00:00", max_transfers=0)
    assert r["count"] == 1
    jy = r["journeys"][0]
    assert jy["transfers"] == 0
    assert jy["legs"][0]["trip_id"] == "T2"
    assert jy["legs"][0]["board_time"] == "2026-10-04 08:05:00"
    assert jy["arrival_time"] == "2026-10-04 08:20:00"


def test_overtaking_trip_can_continue_transfer(conn):
    """T2 的 08:20 下车标号须进入换乘闭包并接续 T3（T1 09:00 赶不上）。"""
    import_gtfs_zip(conn, make_zip(_overtake_gtfs()))
    r = plan_journey(conn, "S1", "S5", D, "08:00:00", max_transfers=1)
    chains = [tuple(l["trip_id"] for l in j["legs"]) for j in r["journeys"]]
    assert ("T2", "T3") in chains
    best = next(j for j in r["journeys"]
                if [l["trip_id"] for l in j["legs"]] == ["T2", "T3"])
    assert best["arrival_time"] == "2026-10-04 08:40:00"
    assert best["legs"][0]["to_stop"] == "S3"
    assert best["legs"][0]["alight_time"] == "2026-10-04 08:20:00"
    assert best["legs"][1]["from_stop"] == "S3"
    assert best["legs"][1]["board_time"] == "2026-10-04 08:25:00"


def test_crossing_trips_both_needed_for_different_downstream(conn):
    """T1 在 S2 更早（可接 S2 的 T3），T2 在 S3 更早（可接 S3 的 T4）：
    剪枝必须覆盖各车后续站的到达与上车前驱，两车都不能被丢。"""
    files = _overtake_gtfs()
    files["stops.txt"] = (
        "stop_id,stop_name\nS1,一号站\nS2,二号站\nS3,三号站\n"
        "S4,四号站\nS5,五号站\n")
    files["trips.txt"] = (
        "trip_id,route_id,service_id\n"
        "T1,R1,SVC1\nT2,R1,SVC1\nT3,R2,SVC1\nT4,R2,SVC1\n")
    files["stop_times.txt"] = (
        "trip_id,stop_sequence,stop_id,arrival_time,departure_time\n"
        "T1,0,S1,08:00:00,08:00:00\n"
        "T1,1,S2,08:10:00,08:10:00\n"
        "T1,2,S3,09:00:00,09:00:00\n"
        "T2,0,S1,08:05:00,08:05:00\n"
        "T2,1,S2,08:15:00,08:15:00\n"
        "T2,2,S3,08:20:00,08:20:00\n"
        "T3,0,S2,08:12:00,08:12:00\nT3,1,S4,08:25:00,08:25:00\n"
        "T4,0,S3,08:22:00,08:22:00\nT4,1,S5,08:40:00,08:40:00\n")
    import_gtfs_zip(conn, make_zip(files))
    r1 = plan_journey(conn, "S1", "S2", D, "08:00:00", max_transfers=0)
    assert [l["trip_id"] for l in r1["journeys"][0]["legs"]] == ["T1"]
    r2 = plan_journey(conn, "S1", "S3", D, "08:00:00", max_transfers=0)
    assert [l["trip_id"] for l in r2["journeys"][0]["legs"]] == ["T2"]
    r3 = plan_journey(conn, "S1", "S4", D, "08:00:00", max_transfers=1)
    assert [l["trip_id"] for l in r3["journeys"][0]["legs"]] == ["T1", "T3"]
    r4 = plan_journey(conn, "S1", "S5", D, "08:00:00", max_transfers=1)
    assert [l["trip_id"] for l in r4["journeys"][0]["legs"]] == ["T2", "T4"]


def test_non_overtaking_results_unchanged(conn):
    """无反超时同 pattern 逐站只留最早可乘车，结果不应膨胀。"""
    files = _overtake_gtfs()
    files["stops.txt"] = "stop_id,stop_name\nS1,一号站\nS2,二号站\nS3,三号站\n"
    files["routes.txt"] = (
        "route_id,agency_id,route_short_name,route_long_name\n"
        "R1,A1,1路,一号线\n")
    files["trips.txt"] = (
        "trip_id,route_id,service_id\nT1,R1,SVC1\nT2,R1,SVC1\n")
    files["stop_times.txt"] = (
        "trip_id,stop_sequence,stop_id,arrival_time,departure_time\n"
        "T1,0,S1,08:00:00,08:00:00\nT1,1,S2,08:10:00,08:10:00\n"
        "T1,2,S3,08:20:00,08:20:00\n"
        "T2,0,S1,08:05:00,08:05:00\nT2,1,S2,08:15:00,08:15:00\n"
        "T2,2,S3,08:25:00,08:25:00\n")
    import_gtfs_zip(conn, make_zip(files))
    r = plan_journey(conn, "S1", "S3", D, "08:00:00", max_transfers=0)
    assert r["count"] == 1
    assert r["journeys"][0]["legs"][0]["trip_id"] == "T1"
    assert r["journeys"][0]["arrival_time"] == "2026-10-04 08:20:00"


def test_overtaking_tie_smaller_trip_id(conn):
    """后发先至同达场景的字典序决胜：同到达同换乘数取 trip_id 序列最小者。"""
    files = _overtake_gtfs()
    files["trips.txt"] = (
        "trip_id,route_id,service_id\nT1,R1,SVC1\nT2,R1,SVC1\nT0,R1,SVC1\n")
    files["stop_times.txt"] = (
        "trip_id,stop_sequence,stop_id,arrival_time,departure_time\n"
        "T1,0,S1,08:00:00,08:00:00\nT1,1,S2,08:10:00,08:10:00\n"
        "T1,2,S3,09:00:00,09:00:00\n"
        "T2,0,S1,08:05:00,08:05:00\nT2,1,S2,08:15:00,08:15:00\n"
        "T2,2,S3,08:20:00,08:20:00\n"
        "T0,0,S1,08:06:00,08:06:00\nT0,1,S2,08:16:00,08:16:00\n"
        "T0,2,S3,08:20:00,08:20:00\n")
    import_gtfs_zip(conn, make_zip(files))
    r = plan_journey(conn, "S1", "S3", D, "08:00:00", max_transfers=0)
    assert r["count"] == 1
    assert r["journeys"][0]["legs"][0]["trip_id"] == "T0"


def test_overtaking_same_instance_not_reboarded(conn):
    """后发先至扫描中同车经停仍不计换乘：已在途实例不在本站重新上车。"""
    files = _overtake_gtfs()
    files["stops.txt"] = (
        "stop_id,stop_name\nS1,一号站\nS2,二号站\nS3,三号站\n"
        "S5,五号站\nSX,厅\n")
    # S1 下车闭包经 0 秒边也能到 S2：S2 被标记，但 T2 不得在 S2 被回登
    files["transfers.txt"] = (
        "from_stop_id,to_stop_id,transfer_type,min_transfer_time\n"
        "S1,SX,2,0\nSX,S2,2,0\n")
    import_gtfs_zip(conn, make_zip(files))
    r = plan_journey(conn, "S1", "S3", D, "08:00:00", max_transfers=1)
    direct = [j for j in r["journeys"]
              if [l["trip_id"] for l in j["legs"]] == ["T2"]]
    assert direct and direct[0]["transfers"] == 0
    assert len(direct[0]["legs"]) == 1
    assert direct[0]["legs"][0]["from_stop"] == "S1"


def test_zero_second_transfer_cycle_terminates(conn):
    """零秒换乘有向环不会导致死循环。"""
    files = base_gtfs()
    files["stops.txt"] += "SX,环站X\nSY,环站Y\n"
    files["transfers.txt"] += (
        "SX,SY,2,0\nSY,SX,2,0\n"
        "S1,SX,2,0\n")
    load(conn, **files)
    # 关键：查询必须正常返回
    r = plan_journey(conn, "S1", "S4", D, "08:00:00", max_transfers=3)
    assert r["count"] >= 1


def test_transfer_type0_default_zero_seconds(conn):
    files = base_gtfs()
    files["stop_times.txt"] = (
        "trip_id,stop_sequence,stop_id,arrival_time,departure_time\n"
        "T1,0,S1,08:00:00,08:00:00\nT1,1,S3,08:30:00,08:30:00\n"
        "T3,0,S2,08:35:00,08:35:00\nT3,1,S5,09:00:00,09:00:00\n")
    files["trips.txt"] = "trip_id,route_id,service_id\nT1,R1,SVC1\nT3,R2,SVC1\n"
    files["transfers.txt"] = (
        "from_stop_id,to_stop_id,transfer_type,min_transfer_time\n"
        "S3,S2,0,\n")  # type=0 推荐换乘，无耗时 -> 0 秒
    load(conn, **files)
    r = plan_journey(conn, "S1", "S5", D, "08:00:00", max_transfers=1)
    assert r["count"] == 1
    assert [l["trip_id"] for l in r["journeys"][0]["legs"]] == ["T1", "T3"]


def test_chained_transfers(conn):
    """S3->SX(20s)->S2，两段步行边链式接续。"""
    files = base_gtfs()
    files["stops.txt"] += "SX,换乘厅\n"
    files["stop_times.txt"] = (
        "trip_id,stop_sequence,stop_id,arrival_time,departure_time\n"
        "T1,0,S1,08:00:00,08:00:00\nT1,1,S3,08:30:00,08:30:00\n"
        "T3,0,S2,08:32:00,08:32:00\nT3,1,S5,09:00:00,09:00:00\n")
    files["trips.txt"] = "trip_id,route_id,service_id\nT1,R1,SVC1\nT3,R2,SVC1\n"
    files["transfers.txt"] = (
        "from_stop_id,to_stop_id,transfer_type,min_transfer_time\n"
        "S3,SX,2,10\nSX,S2,2,10\n")
    load(conn, **files)
    r = plan_journey(conn, "S1", "S5", D, "08:00:00", max_transfers=1)
    assert r["count"] == 1
    assert [l["trip_id"] for l in r["journeys"][0]["legs"]] == ["T1", "T3"]

    # 步行总耗时必须满足：改成各 100s 则赶不上 08:32
    files["transfers.txt"] = (
        "from_stop_id,to_stop_id,transfer_type,min_transfer_time\n"
        "S3,SX,2,100\nSX,S2,2,100\n")
    import_gtfs_zip(conn, make_zip(files))
    r2 = plan_journey(conn, "S1", "S5", D, "08:00:00", max_transfers=1)
    assert r2["count"] == 0


def test_type3_takes_precedence_over_type2(conn):
    files = base_gtfs()
    files["stop_times.txt"] = (
        "trip_id,stop_sequence,stop_id,arrival_time,departure_time\n"
        "T1,0,S1,08:00:00,08:00:00\nT1,1,S3,08:30:00,08:30:00\n"
        "T3,0,S2,08:35:00,08:35:00\nT3,1,S5,09:00:00,09:00:00\n")
    files["trips.txt"] = "trip_id,route_id,service_id\nT1,R1,SVC1\nT3,R2,SVC1\n"
    files["transfers.txt"] = (
        "from_stop_id,to_stop_id,transfer_type,min_transfer_time\n"
        "S3,S2,2,0\nS3,S2,3,\n")  # 同方向既允许又禁止 -> 禁止优先
    load(conn, **files)
    r = plan_journey(conn, "S1", "S5", D, "08:00:00", max_transfers=2)
    assert r["count"] == 0


def test_multi_agency_scoped(conn):
    files = base_gtfs()
    files["agency.txt"] = (
        "agency_id,agency_name,agency_url,agency_timezone\n"
        "A1,甲公司,http://a,Asia/Shanghai\n"
        "A2,乙公司,http://b,America/New_York\n")
    files["routes.txt"] = (
        "route_id,agency_id,route_short_name,route_long_name\n"
        "R1,A1,甲线,J\nR2,A2,乙线,Y\n")
    files["trips.txt"] = "trip_id,route_id,service_id\nTA,R1,SVC1\nTB,R2,SVC1\n"
    files["stop_times.txt"] = (
        "trip_id,stop_sequence,stop_id,arrival_time,departure_time\n"
        "TA,0,S1,08:00:00,08:00:00\nTA,1,S2,08:30:00,08:30:00\n"
        "TB,0,S1,09:00:00,09:00:00\nTB,1,S2,09:30:00,09:30:00\n")
    load(conn, **files)
    r1 = plan_journey(conn, "S1", "S2", D, "08:00:00", 0, agency_id="A1")
    assert [l["trip_id"] for l in r1["journeys"][0]["legs"]] == ["TA"]
    r2 = plan_journey(conn, "S1", "S2", D, "08:00:00", 0, agency_id="A2")
    assert [l["trip_id"] for l in r2["journeys"][0]["legs"]] == ["TB"]
    assert r2["timezone"] == "America/New_York"


def test_two_transfer_deep_chain(conn):
    files = base_gtfs()
    files["stops.txt"] += "S6,六号站\nS7,七号站\n"
    files["routes.txt"] += "R3,A1,3路,三号线\n"
    files["trips.txt"] = (
        "trip_id,route_id,service_id\nT1,R1,SVC1\nT2,R2,SVC1\nT3,R3,SVC1\n")
    files["stop_times.txt"] = (
        "trip_id,stop_sequence,stop_id,arrival_time,departure_time\n"
        "T1,0,S1,08:00:00,08:00:00\nT1,1,S2,08:30:00,08:30:00\n"
        "T2,0,S3,09:00:00,09:00:00\nT2,1,S4,09:30:00,09:30:00\n"
        "T3,0,S5,10:00:00,10:00:00\nT3,1,S6,11:00:00,11:00:00\n")
    files["transfers.txt"] = (
        "from_stop_id,to_stop_id,transfer_type,min_transfer_time\n"
        "S2,S3,2,0\nS4,S5,2,0\n")
    load(conn, **files)
    r = plan_journey(conn, "S1", "S6", D, "08:00:00", max_transfers=2)
    assert r["count"] == 1
    assert [l["trip_id"] for l in r["journeys"][0]["legs"]] == \
        ["T1", "T2", "T3"]
    r2 = plan_journey(conn, "S1", "S6", D, "08:00:00", max_transfers=1)
    assert r2["count"] == 0


def test_max_transfers_bound(conn):
    load(conn)
    r = plan_journey(conn, "S1", "S5", D, "08:00:00", max_transfers=0)
    chains = [tuple(l["trip_id"] for l in j["legs"]) for j in r["journeys"]]
    # 0 次换乘时不允许 T1->T3
    assert ("T1", "T3") not in chains


def test_search_window_24h_boundary(conn):
    """恰在 24h 边界发车的车可取；超过窗口不取。"""
    def gtfs(dep, arr):
        files = base_gtfs()
        del files["calendar.txt"]
        files["calendar_dates.txt"] = (
            "service_id,date,exception_type\n"
            "SVC2,20261005,1\n")  # 仅次日运营的服务
        files["trips.txt"] = "trip_id,route_id,service_id\nTL,R1,SVC2\n"
        files["stop_times.txt"] = (
            "trip_id,stop_sequence,stop_id,arrival_time,departure_time\n"
            f"TL,0,S1,{dep},{dep}\nTL,1,S4,{arr},{arr}\n")
        return files

    # 次日 08:00:00 发车（相对出发时刻恰为 24h）-> 可乘
    import_gtfs_zip(conn, make_zip(gtfs("08:00:00", "08:05:00")))
    r = plan_journey(conn, "S1", "S4", D, "08:00:00", max_transfers=0)
    assert r["count"] == 1
    assert r["journeys"][0]["legs"][0]["trip_id"] == "TL"
    assert r["journeys"][0]["legs"][0]["board_time"] == \
        "2026-10-05 08:00:00"

    # 晚 1 秒：超出 24h 窗口，无车可乘
    import_gtfs_zip(conn, make_zip(gtfs("08:00:01", "08:05:00")))
    r2 = plan_journey(conn, "S1", "S4", D, "08:00:00", max_transfers=0)
    assert r2["count"] == 0


# --------------------------------------------------------------------------
# 整包拒绝
# --------------------------------------------------------------------------

def test_reject_missing_reference(conn):
    files = base_gtfs()
    files["trips.txt"] += "TX,R1,NO_SUCH_SERVICE\n"
    files["stop_times.txt"] += (
        "TX,0,S1,10:00:00,10:00:00\nTX,1,S2,10:10:00,10:10:00\n")
    with pytest.raises(ImportRejected):
        import_gtfs_zip(conn, make_zip(files))


def test_reject_stop_time_reversed(conn):
    files = base_gtfs()
    files["stop_times.txt"] += (
        "T1,3,S5,08:05:00,08:05:00\n"  # 早于 T1 前一站发车 08:40
        "T1,4,S2,08:50:00,08:50:00\n")
    with pytest.raises(ImportRejected):
        import_gtfs_zip(conn, make_zip(files))


def test_reject_departure_before_arrival(conn):
    files = base_gtfs()
    files["stop_times.txt"] += (
        "T1,3,S5,09:50:00,09:00:00\n")
    with pytest.raises(ImportRejected):
        import_gtfs_zip(conn, make_zip(files))


def test_old_data_remains_after_reject(conn):
    load(conn)
    files = base_gtfs()
    files["trips.txt"] += "TX,R1,NOPE\n"
    files["stop_times.txt"] += (
        "TX,0,S1,10:00:00,10:00:00\nTX,1,S2,10:10:00,10:10:00\n")
    with pytest.raises(ImportRejected):
        import_gtfs_zip(conn, make_zip(files))
    # 旧数据仍可查询
    r = plan_journey(conn, "S1", "S4", D, "08:00:00", max_transfers=0)
    assert r["count"] == 1
