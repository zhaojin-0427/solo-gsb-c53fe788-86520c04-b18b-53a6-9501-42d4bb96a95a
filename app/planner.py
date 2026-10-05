"""行程规划（基于 RAPTOR 思想的按圈标记算法）。

输入：起/终站、agency 时区下的出发日期与时刻、最大换乘次数。
搜索展开三个服务日（前一日 / 当日 / 次日），在出发时刻起 24 小时内找车；
例外日期（calendar_dates）覆盖周历（calendar）。

换乘规则：
- 跨站只走 transfers 表中的显式有向边，并满足最小耗时；
- transfer_type=3 的方向禁止换乘（不可用）；
- 同站默认可以换车（无规则即允许同站），type=3 的自环会禁止同站换乘；
- 同一辆车继续乘坐不计换乘（扫描中不回登同一 trip 实例）；
- 步行边在每圈内做多源 Dijkstra 闭包，零秒环不会产生更优标号，自然终止。

同一线路同一停站序列（pattern）的不同班次可能“反超”（后发先到）：
只跟踪每站最早发的一班会漏掉中途反超的班次。因此同一 pattern 内
并行扫描多个班次实例，并按支配关系剪枝——班次 A 支配班次 B 当且仅当
A 的上车前驱不差（rides 不更多、trip 序列不更大）且 A 在后续每一站
的到达都不晚于 B；被支配者的后续标号不可能更优，可安全跳过/移除。
该支配关系是预序（逐分量 <= 可传递），跳过与移除可任意次序进行；
最坏情况（互不支配）全部保留，仍满足查询时限。

结果在（到达时刻, 换乘次数）上保留非支配行程；
同指标只留 trip_id 序列字典序最小者。
"""

from __future__ import annotations

from bisect import bisect_left
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from heapq import heappop, heappush
from itertools import islice

from .timeutils import fmt_local_dt, get_zone

MAX_TRANSFERS_LIMIT = 16
WINDOW_SEC = 24 * 3600
DAY_SEC = 86400
EPOCH = datetime(1970, 1, 1)


class PlanError(ValueError):
    pass


@dataclass(slots=True)
class Label:
    """到达某站的标号。

    kind: init=起点；ride=乘 trip 实例下车；walk=经换乘边走到。
    内部时间用相对出发日零点的绝对秒（可负、可 >86400）。
    """

    stop: str
    arrival: float
    rides: int
    kind: str
    parent: "Label | None" = None
    trip_id: str | None = None
    trip_index: int = -1
    board_time: float = 0.0
    alight_time: float = 0.0
    from_stop: str | None = None
    to_stop: str | None = None
    seq: tuple[str, ...] = ()

    def key(self):
        # 越早越好；同到达时刻段数越少越好；再同则 trip_id 序列越小越好
        return (self.arrival, self.rides, self.seq)


@dataclass(slots=True)
class Pattern:
    pid: int
    route_id: str
    stops: list[str]
    pos: dict[str, int] = field(default_factory=dict)
    # 每个停站位置：按 (发车绝对秒, trip_id, 实例下标) 排序
    index: list[list[tuple[float, str, int]]] = field(default_factory=list)


def _suffix_le(arr_a: tuple, arr_b: tuple, j: int) -> bool:
    """从位置 j 起 arr_a 逐站都不晚于 arr_b（班次支配判定的到达部分）。"""
    for k in range(j, len(arr_a)):
        if arr_a[k] > arr_b[k]:
            return False
    return True


def _active_services(conn, days: list[date]) -> dict[date, set[str]]:
    """每个日期实际运营的 service_id 集合；例外日期覆盖周历。"""
    cal = {
        r["service_id"]: (r["start_date"], r["end_date"], r["weekdays"])
        for r in conn.execute("SELECT service_id, start_date, end_date, "
                              "weekdays FROM calendar")
    }
    exc: dict[tuple[str, str], int] = {}
    for r in conn.execute(
            "SELECT service_id, date, exception_type FROM calendar_dates"):
        exc[(r["service_id"], r["date"])] = r["exception_type"]

    result: dict[date, set[str]] = {}
    for d in days:
        ds = d.strftime("%Y%m%d")
        bit = 1 << d.weekday()  # Python Monday=0；库中 bit0=周一
        active = {sid for sid, (sd, ed, wk) in cal.items()
                  if sd <= ds <= ed and (wk & bit)}
        for (sid, ds2), typ in exc.items():
            if ds2 != ds:
                continue
            if typ == 1:
                active.add(sid)
            else:
                active.discard(sid)  # 停运覆盖周历
        result[d] = active
    return result


def plan_journey(
    conn,
    origin_id: str,
    target_id: str,
    depart_date: date,
    depart_time: str,  # HH:MM:SS，agency 本地
    max_transfers: int,
    agency_id: str | None = None,
) -> dict:
    if not (0 <= max_transfers <= MAX_TRANSFERS_LIMIT):
        raise PlanError(
            f"max_transfers 必须在 0..{MAX_TRANSFERS_LIMIT} 之间")

    # ------------------------------------------------------------------
    # 时区 / 站点 / 搜索窗口
    # ------------------------------------------------------------------
    agencies = conn.execute(
        "SELECT agency_id, agency_timezone FROM agencies").fetchall()
    if not agencies:
        raise PlanError("尚未导入任何时刻表数据")
    if agency_id is None:
        agency_id = agencies[0]["agency_id"]
    tz_name = None
    for r in agencies:
        if r["agency_id"] == agency_id:
            tz_name = r["agency_timezone"]
            break
    if tz_name is None:
        raise PlanError(f"agency_id {agency_id!r} 不存在")
    get_zone(tz_name)  # 校验

    stop_names = {
        r["stop_id"]: r["stop_name"]
        for r in conn.execute("SELECT stop_id, stop_name FROM stops")
    }
    if origin_id not in stop_names:
        raise PlanError(f"起点站 {origin_id!r} 不存在")
    if target_id not in stop_names:
        raise PlanError(f"终到站 {target_id!r} 不存在")

    day0 = datetime(depart_date.year, depart_date.month, depart_date.day)
    try:
        hh, mm, ss = (int(x) for x in depart_time.split(":"))
        if not (0 <= hh and 0 <= mm < 60 and 0 <= ss < 60):
            raise ValueError
    except (ValueError, AttributeError):
        raise PlanError("depart_time 必须是 HH:MM:SS")
    t0 = day0 + timedelta(hours=hh, minutes=mm, seconds=ss)
    t0_abs = (t0 - day0).total_seconds()
    tend_abs = t0_abs + WINDOW_SEC

    if origin_id == target_id:
        return _result(origin_id, target_id, tz_name, stop_names, day0,
                       t0_abs, tend_abs, zero=True)

    days = [depart_date - timedelta(days=1), depart_date,
            depart_date + timedelta(days=1)]
    bases = [-DAY_SEC, 0, DAY_SEC]
    active = _active_services(conn, days)

    # ------------------------------------------------------------------
    # pattern（限定该 agency 线路）
    # ------------------------------------------------------------------
    agency_routes = {
        r["route_id"] for r in conn.execute(
            "SELECT route_id FROM routes WHERE agency_id = ?", (agency_id,))
    }
    patterns: dict[int, Pattern] = {}
    for r in conn.execute(
            "SELECT pattern_id, route_id, stop_ids FROM patterns"):
        if r["route_id"] not in agency_routes:
            continue
        stop_list = r["stop_ids"].split("\x00")
        patterns[r["pattern_id"]] = Pattern(
            pid=r["pattern_id"], route_id=r["route_id"], stops=stop_list,
            pos={sid: i for i, sid in enumerate(stop_list)},
            index=[[] for _ in stop_list])

    # 三天服务合并，一次性取 trip 元数据
    all_services: set[str] = set()
    for s in active.values():
        all_services |= s

    trip_pid: dict[str, int] = {}
    trip_route: dict[str, str] = {}
    if all_services:
        qm = ",".join("?" for _ in all_services)
        for r in conn.execute(
                f"SELECT trip_id, route_id, pattern_id FROM trips "
                f"WHERE service_id IN ({qm})", tuple(all_services)):
            trip_pid[r["trip_id"]] = r["pattern_id"]
            trip_route[r["trip_id"]] = r["route_id"]

    # ------------------------------------------------------------------
    # 装载 trip 实例
    # instances[i] = [trip_id, route_id, pid, day_idx,
    #                 [(arr_abs, dep_abs)|None, ... 与 pattern 同序]]
    # ------------------------------------------------------------------
    instances: list[list] = []
    for day_idx, d in enumerate(days):
        svcs = active[d]
        base = bases[day_idx]
        rows: list = []
        if svcs:
            qm = ",".join("?" for _ in svcs)
            lo, hi = int(t0_abs - base), int(tend_abs - base)
            # 第一步：找出当天在窗口内有可上车站的 trip
            elig = {r[0] for r in conn.execute(
                "SELECT DISTINCT t.trip_id FROM trips t "
                "JOIN stop_times st ON st.trip_id = t.trip_id "
                f"WHERE t.service_id IN ({qm}) "
                "AND st.departure_sec BETWEEN ? AND ?",
                tuple(svcs) + (lo, hi)).fetchall()}
            rows = []
            if elig:
                qm2 = ",".join("?" for _ in elig)
                # 第二步：仅对这些 trip 拉完整站时（到达可晚于窗口）
                rows = conn.execute(
                    "SELECT trip_id, stop_sequence, arrival_sec, "
                    "departure_sec FROM stop_times "
                    f"WHERE trip_id IN ({qm2}) "
                    "ORDER BY trip_id, stop_sequence",
                    tuple(elig)).fetchall()

        def make_flush(cur_trip, buf, pat, route_id):
            def flush():
                if cur_trip is None or pat is None:
                    return
                n = len(pat.stops)
                arr = [None] * n
                dep = [None] * n
                # buf 已按 stop_sequence 排序，与 pattern 停站顺序一致
                for pos, (_seq, ar, dp) in enumerate(buf[:n]):
                    arr[pos] = base + ar
                    dep[pos] = base + dp
                # 任一站在窗口内发车即纳入；能否在上车点被乘由
                # 各位置索引的窗口过滤决定（到达可晚于窗口上界）
                boardable = False
                for x in dep:
                    if x is not None and t0_abs <= x <= tend_abs:
                        boardable = True
                        break
                if not boardable:
                    return
                inst_idx = len(instances)
                atup = tuple(arr)
                instances.append(
                    (cur_trip, route_id, pat.pid, day_idx, atup, tuple(dep)))
                for pos, x in enumerate(dep):
                    if x is not None and t0_abs <= x <= tend_abs:
                        pat.index[pos].append((x, cur_trip, inst_idx))
            return flush

        cur_trip = None
        buf: list = []
        flush = lambda: None
        for trip_id, seq, ar, dp in rows:
            if trip_id != cur_trip:
                flush()
                cur_trip = trip_id
                pid = trip_pid.get(cur_trip)
                pat = patterns.get(pid)
                buf = []
                flush = make_flush(
                    cur_trip, buf, pat, trip_route.get(cur_trip))
            buf.append((seq, ar, dp))
        flush()

    for pat in patterns.values():
        for lst in pat.index:
            lst.sort()  # (发车绝对秒, trip_id, 实例下标)

    stop_patterns: dict[str, list[tuple[int, int]]] = {}
    for pid, pat in patterns.items():
        for i, sid in enumerate(pat.stops):
            stop_patterns.setdefault(sid, []).append((pid, i))

    # ------------------------------------------------------------------
    # 换乘图
    # ------------------------------------------------------------------
    graph: dict[str, list[tuple[str, int]]] = {}
    for r in conn.execute(
            "SELECT from_stop_id, to_stop_id, min_transfer_sec "
            "FROM transfers"):
        graph.setdefault(r["from_stop_id"], []).append(
            (r["to_stop_id"], r["min_transfer_sec"]))
    forbidden = {
        (r["from_stop_id"], r["to_stop_id"])
        for r in conn.execute(
            "SELECT from_stop_id, to_stop_id FROM transfer_forbidden")
    }

    # ------------------------------------------------------------------
    # RAPTOR
    # ------------------------------------------------------------------
    def closure(sources: dict[str, Label]) -> tuple[
            dict[str, Label], dict[str, Label]]:
        """对一圈下车标号做多源 Dijkstra 步行闭包。

        同站接续默认 0 秒；(s,s) 在禁止表中则该站不能再上车
        （但仍可作为到达终点）。仅严格更优才松弛 -> 零秒环自然终止。

        返回 (闭包标号, 本闭包新产生的标号)；后者用于标记下一圈可上车站。
        """
        out: dict[str, Label] = {}
        generated: dict[str, Label] = {}
        heap: list = []
        for stop, lab in sources.items():
            if lab.kind != "init" and (stop, stop) in forbidden:
                continue  # 下车即禁止同站接续：不放入可上车闭包
            out[stop] = lab
            generated[stop] = lab
            heappush(heap, (lab.arrival, lab.rides, lab.seq, stop))
        while heap:
            arr, rides, seq, u = heappop(heap)
            cur = out[u]
            if (arr, rides, seq) != (cur.arrival, cur.rides, cur.seq):
                continue
            for v, sec in graph.get(u, ()):
                na = arr + sec
                old = out.get(v)
                if old is not None and not (
                        (na, rides, seq) < (old.arrival, old.rides, old.seq)):
                    continue
                lab = Label(stop=v, arrival=na, rides=rides, kind="walk",
                            parent=cur, seq=seq)
                out[v] = lab
                generated[v] = lab
                heappush(heap, (na, rides, seq, v))
        return out, generated

    def fmt(abs_sec: float) -> str:
        return fmt_local_dt(day0 + timedelta(seconds=abs_sec))

    init_label = Label(stop=origin_id, arrival=t0_abs, rides=0, kind="init")
    cl0, gen0 = closure({origin_id: init_label})
    closures = [cl0]
    generated = [gen0]
    target_labels: list[Label] = []

    if target_id in closures[0]:
        target_labels.append(closures[0][target_id])  # 纯步行可达（通常无）

    for rnd in range(1, max_transfers + 2):  # 圈数=乘段数
        # 上一圈闭包中真正新到 / 变优的站才需要重新扫描
        marked = set(generated[rnd - 1].keys())

        ride_here: dict[str, Label] = {}
        ride_best: dict[str, tuple] = {}  # sid -> ride_here[sid].key()
        scanned = {pid for s in marked
                   for pid, _ in stop_patterns.get(s, ())}

        prev = closures[rnd - 1]

        for pid in scanned:
            pat = patterns[pid]
            # 并行在扫班次：同 pattern 内可能“反超”（后发先到），
            # 只跟踪最早发的一班会漏报。active 元素：
            # (实例下标, 上车位置, 上车时刻, 前驱标号, trip_id, rides, seq)
            active: list[tuple] = []
            active_inst: set[int] = set()
            for j, sid in enumerate(pat.stops):
                # 先下车：各在扫班次在本站的到达标号
                if active:
                    best = ride_best.get(sid)
                    for (inst_idx, bp, bt, plab, trip_id,
                         rides, seq) in active:
                        arr_j = instances[inst_idx][4][j]
                        ck = (arr_j, rides, seq)
                        if best is None or ck < best:
                            best = ck
                            ride_here[sid] = Label(
                                stop=sid, arrival=arr_j, rides=rides,
                                kind="ride", parent=plab, trip_id=trip_id,
                                trip_index=inst_idx, board_time=bt,
                                alight_time=arr_j, from_stop=pat.stops[bp],
                                to_stop=sid, seq=seq)
                    ride_best[sid] = best

                # 再上车：本站新可乘的班次，与在扫班次做支配剪枝
                if sid in marked and sid in prev:
                    plab = prev[sid]
                    lst = pat.index[j]
                    k = bisect_left(lst, (plab.arrival, "", -1))
                    rides = plab.rides + 1
                    for dep_j, cand_trip, inst_idx in islice(lst, k, None):
                        if inst_idx in active_inst:
                            continue  # 同车经停不回登同一实例
                        seq = plab.seq + (cand_trip,)
                        arr_c = instances[inst_idx][4]
                        # 剪枝 1：某在扫班次的前驱不差（rides/seq 不更大）
                        # 且后续各站到达都不晚 -> 候选的标号不可能更优。
                        dominated = False
                        for ent in active:
                            if (ent[5] <= rides and ent[6] <= seq
                                    and _suffix_le(instances[ent[0]][4],
                                                   arr_c, j)):
                                dominated = True
                                break
                        if dominated:
                            continue
                        # 剪枝 2：候选支配的在扫班次移出（其后续标号都被
                        # 候选覆盖；已产生的下车标号不受影响）。
                        if active:
                            kept = []
                            for ent in active:
                                if (rides <= ent[5] and seq <= ent[6]
                                        and _suffix_le(
                                            arr_c, instances[ent[0]][4], j)):
                                    active_inst.discard(ent[0])
                                else:
                                    kept.append(ent)
                            active = kept
                        active.append((inst_idx, j, dep_j, plab,
                                       cand_trip, rides, seq))
                        active_inst.add(inst_idx)

        if not ride_here:
            break

        for sid, lab in ride_here.items():
            if sid == target_id:
                target_labels.append(lab)

        new_cl, new_gen = closure(ride_here)
        merged = dict(prev)
        for sid, lab in new_cl.items():
            old = merged.get(sid)
            if old is None or lab.key() < old.key():
                merged[sid] = lab
        closures.append(merged)
        # 只把“相对上一圈闭包确实更新”的新标号交给下一圈标记
        kept_gen: dict[str, Label] = {}
        for sid, lab in new_gen.items():
            old = prev.get(sid)
            if old is None or lab.key() < old.key():
                kept_gen[sid] = lab
        generated.append(kept_gen)
        # 目标候选：闭包里的步行到达也计入（终到站可步行抵达）
        if target_id in new_cl:
            target_labels.append(new_cl[target_id])

    # ------------------------------------------------------------------
    # Pareto：(到达, 换乘次数) 非支配；同指标取 trip_id 序列最小
    # ------------------------------------------------------------------
    best_by_key: dict[tuple, Label] = {}
    for lab in target_labels:
        if lab.kind == "init":
            continue
        k = lab.key()
        old = best_by_key.get(k)
        if old is None or lab.seq < old.seq:
            best_by_key[k] = lab
    ordered = sorted(best_by_key.values(),
                     key=lambda l: (l.arrival, l.rides, l.seq))
    pareto: list[Label] = []
    best_r = None
    for lab in ordered:
        if best_r is None or lab.rides < best_r:
            pareto.append(lab)
            best_r = lab.rides

    journeys = [_rebuild(lab, fmt) for lab in pareto]
    return _result(origin_id, target_id, tz_name, stop_names, day0,
                   t0_abs, tend_abs, journeys=journeys)


def _rebuild(lab: Label, fmt) -> list[dict]:
    segs: list[Label] = []
    cur = lab
    while cur is not None and cur.kind != "init":
        if cur.kind == "ride":
            segs.append(cur)
        cur = cur.parent
    segs.reverse()
    legs = []
    for i, seg in enumerate(segs):
        legs.append({
            "seq": i + 1,
            "trip_id": seg.trip_id,
            "from_stop": seg.from_stop,
            "to_stop": seg.to_stop,
            "board_time": fmt(seg.board_time),
            "alight_time": fmt(seg.alight_time),
        })
    return legs


def _result(origin_id, target_id, tz_name, stop_names, day0,
            t0_abs, tend_abs, journeys=None, zero=False):
    def dt(abs_sec):
        return fmt_local_dt(day0 + timedelta(seconds=abs_sec))

    out_journeys = []
    if zero:
        out_journeys.append(
            {"transfers": 0, "arrival_time": dt(t0_abs), "legs": []})
    for legs in journeys or []:
        out_journeys.append({
            "transfers": max(0, len(legs) - 1),
            "arrival_time": legs[-1]["alight_time"] if legs else None,
            "legs": legs,
        })
    return {
        "origin": {"stop_id": origin_id,
                   "stop_name": stop_names.get(origin_id)},
        "destination": {"stop_id": target_id,
                        "stop_name": stop_names.get(target_id)},
        "timezone": tz_name,
        "depart_after": dt(t0_abs),
        "search_until": dt(tend_abs),
        "count": len(out_journeys),
        "journeys": out_journeys,
    }
