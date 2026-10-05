"""FastAPI 入口：GTFS 上传导入、站点检索与行程查询。"""

from __future__ import annotations

import asyncio
import os
from datetime import date

from fastapi import FastAPI, File, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .db import DB_PATH, connect, init_db
from .gtfs_importer import ImportRejected, import_gtfs_zip
from .planner import MAX_TRANSFERS_LIMIT, PlanError, plan_journey
from .timeutils import GTFSError

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATIC_DIR = os.path.join(BASE_DIR, "static")

app = FastAPI(title="公交班次换乘查询台", version="1.0.0")

# 导入串行化：一次只处理一个 ZIP，冲突返回 409
_import_lock = asyncio.Lock()


def get_conn():
    conn = connect()
    init_db(conn)
    return conn


class PlanRequest(BaseModel):
    origin_id: str = Field(..., description="起点 stop_id")
    destination_id: str = Field(..., description="终点 stop_id")
    depart_date: date = Field(..., description="出发日期（agency 本地）")
    depart_time: str = Field(..., description="出发时刻 HH:MM:SS")
    max_transfers: int = Field(2, ge=0, le=MAX_TRANSFERS_LIMIT)
    agency_id: str | None = None


@app.get("/api/health")
def health():
    return {"status": "ok"}


@app.get("/api/meta")
def meta():
    conn = get_conn()
    try:
        rows = conn.execute("SELECT key, value FROM meta").fetchall()
        info = {r["key"]: r["value"] for r in rows}
        agencies = [
            {"agency_id": r["agency_id"],
             "agency_name": r["agency_name"],
             "agency_timezone": r["agency_timezone"]}
            for r in conn.execute(
                "SELECT agency_id, agency_name, agency_timezone "
                "FROM agencies ORDER BY agency_id")
        ]
        return {"agencies": agencies, "meta": info}
    finally:
        conn.close()


@app.get("/api/stops")
def search_stops(q: str = Query("", description="ID 或名称关键字"),
                 limit: int = Query(20, ge=1, le=100)):
    conn = get_conn()
    try:
        like = f"%{q.strip()}%"
        rows = conn.execute(
            "SELECT stop_id, stop_name FROM stops "
            "WHERE stop_id LIKE ? OR stop_name LIKE ? "
            "ORDER BY stop_id LIMIT ?", (like, like, limit)).fetchall()
        return [{"stop_id": r["stop_id"], "stop_name": r["stop_name"]}
                for r in rows]
    finally:
        conn.close()


@app.post("/api/import")
async def import_gtfs(file: UploadFile = File(...)):
    if not file.filename or not file.filename.lower().endswith(".zip"):
        raise HTTPException(status_code=400, detail="仅接受 .zip 文件")
    if _import_lock.locked():
        raise HTTPException(status_code=409,
                            detail="已有导入任务正在执行，请稍后重试")
    payload = await file.read()
    if not payload:
        raise HTTPException(status_code=400, detail="上传文件为空")

    async with _import_lock:
        def run_import():
            conn = get_conn()
            try:
                return import_gtfs_zip(conn, payload)
            finally:
                conn.close()
        try:
            summary = await asyncio.to_thread(run_import)
        except ImportRejected as exc:
            return JSONResponse(
                status_code=422,
                content={"status": "rejected",
                         "detail": "整包已拒绝，旧数据仍可查询",
                         "errors": exc.errors})
    return {"status": "imported", "summary": summary}


@app.post("/api/journeys")
def journeys(req: PlanRequest):
    conn = get_conn()
    try:
        try:
            result = plan_journey(
                conn,
                origin_id=req.origin_id,
                target_id=req.destination_id,
                depart_date=req.depart_date,
                depart_time=req.depart_time,
                max_transfers=req.max_transfers,
                agency_id=req.agency_id,
            )
        except PlanError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except GTFSError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return result
    finally:
        conn.close()


@app.get("/")
def index():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
