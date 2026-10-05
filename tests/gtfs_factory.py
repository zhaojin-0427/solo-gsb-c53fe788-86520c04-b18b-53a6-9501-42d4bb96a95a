"""合成 GTFS ZIP 辅助工具。"""

from __future__ import annotations

import io
import zipfile


def make_zip(files: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, content in files.items():
            zf.writestr(name, content)
    return buf.getvalue()


def base_gtfs(**over) -> dict[str, str]:
    """构造一套最小但完整的 GTFS 文件文本（可覆盖/增补）。"""
    files = {
        "agency.txt": (
            "agency_id,agency_name,agency_url,agency_timezone\n"
            "A1,公交一公司,http://a,Asia/Shanghai\n"),
        "stops.txt": (
            "stop_id,stop_name\n"
            "S1,一号站\nS2,二号站\nS3,三号站\nS4,四号站\nS5,五号站\n"),
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
            "T1,1,S3,08:30:00,08:30:00\n"
            "T1,2,S4,08:40:00,08:40:00\n"
            "T2,0,S1,09:00:00,09:00:00\n"
            "T2,1,S2,09:10:00,09:10:00\n"
            "T2,2,S5,09:40:00,09:40:00\n"
            "T3,0,S2,09:20:00,09:20:00\n"
            "T3,1,S4,09:30:00,09:30:00\n"
            "T3,2,S5,09:50:00,09:50:00\n"),
        "transfers.txt": (
            "from_stop_id,to_stop_id,transfer_type,min_transfer_time\n"
            "S3,S2,2,600\n"),
    }
    files.update(over)
    return files
