"""FastAPI 端到端（TestClient）测试。"""

import os
import tempfile

import pytest

os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(), "api.db")

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402
from tests.gtfs_factory import base_gtfs, make_zip  # noqa: E402

client = TestClient(app)


def test_health():
    assert client.get("/api/health").json() == {"status": "ok"}


def test_full_flow_import_query_index():
    # 首页可访问
    r = client.get("/")
    assert r.status_code == 200
    assert "换乘" in r.text

    payload = make_zip(base_gtfs())
    r = client.post(
        "/api/import",
        files={"file": ("gtfs.zip", payload, "application/zip")})
    assert r.status_code == 200, r.text
    assert r.json()["summary"]["stop_times"] >= 8

    r = client.get("/api/meta")
    j = r.json()
    assert j["agencies"][0]["agency_timezone"] == "Asia/Shanghai"

    r = client.get("/api/stops?q=二号")
    assert any(s["stop_id"] == "S2" for s in r.json())

    body = {
        "origin_id": "S1",
        "destination_id": "S4",
        "depart_date": "2026-10-04",
        "depart_time": "08:00:00",
        "max_transfers": 0,
    }
    r = client.post("/api/journeys", json=body)
    assert r.status_code == 200
    j = r.json()
    assert j["count"] == 1
    assert j["journeys"][0]["legs"][0]["trip_id"] == "T1"


def test_bad_zip_rejected_422_and_old_data_kept():
    # 先导入好数据
    client.post("/api/import",
                files={"file": ("g.zip", make_zip(base_gtfs()),
                                "application/zip")})
    bad = base_gtfs()
    bad["stop_times.txt"] += "T1,3,S5,07:00:00,07:00:00\n"  # 倒序
    r = client.post("/api/import",
                    files={"file": ("bad.zip", make_zip(bad),
                                    "application/zip")})
    assert r.status_code == 422
    body = r.json()
    assert body["status"] == "rejected"
    assert body["errors"]

    # 旧数据仍可查
    r = client.post("/api/journeys", json={
        "origin_id": "S1", "destination_id": "S4",
        "depart_date": "2026-10-04", "depart_time": "08:00:00",
        "max_transfers": 0})
    assert r.status_code == 200
    assert r.json()["count"] == 1


def test_missing_stop_400():
    client.post("/api/import",
                files={"file": ("g.zip", make_zip(base_gtfs()),
                                "application/zip")})
    r = client.post("/api/journeys", json={
        "origin_id": "NOPE", "destination_id": "S4",
        "depart_date": "2026-10-04", "depart_time": "08:00:00",
        "max_transfers": 0})
    assert r.status_code == 400


def test_non_zip_400():
    r = client.post("/api/import",
                    files={"file": ("a.txt", b"hello", "text/plain")})
    assert r.status_code == 400


def test_zero_leg_and_empty():
    client.post("/api/import",
                files={"file": ("g.zip", make_zip(base_gtfs()),
                                "application/zip")})
    r = client.post("/api/journeys", json={
        "origin_id": "S2", "destination_id": "S2",
        "depart_date": "2026-10-04", "depart_time": "08:00:00",
        "max_transfers": 2})
    assert r.status_code == 200
    assert r.json()["journeys"][0]["legs"] == []
