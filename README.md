# 公交班次换乘 Web 查询台

基于 **GTFS Schedule** 的定时班次公交换乘查询系统。

- 后端：Python 3.11 + FastAPI + SQLite
- 前端：原生 JavaScript（无构建步骤）
- 查询算法：RAPTOR 风格的按圈标记算法 + 显式换乘边多源 Dijkstra 闭包
- 部署：Docker / Docker Compose 一键启动

## 功能一览

- 上传 **GTFS Schedule ZIP** 后解析并入库：
  - 站点 `stops.txt`、线路 `routes.txt`、班次 `trips.txt`
  - 完整站时 `stop_times.txt`（支持 `24:00` 之后的跨夜时刻）
  - 周历 `calendar.txt`、例外日期 `calendar_dates.txt`
  - 站点级换乘 `transfers.txt`（`transfer_type=0/2/3`）
- **整包事务导入**：
  - 缺引用（trip/route/service/stop/agency 不存在）、站时倒序、
    发车早于到达、停站序号不递增等错误 -> **整包拒绝（HTTP 422）**
  - 回滚后**旧数据仍可正常查询**
- 查询条件：起点站、终到站、agency 时区下的出发日期与时刻、最大换乘次数
- 自动展开**前一日 / 当日 / 次日**三天服务，搜索出发时刻起 **24 小时内**
  可乘的车（例外日期覆盖周历）
- 换乘语义：
  - 跨站**只走显式有向换乘边**，并满足最小换乘耗时；
  - `transfer_type=3` 的方向**禁止换乘**（同方向若同时存在 0/2 与 3，禁止优先）；
  - **同站无规则即可换车**；若存在 `(s,s) type=3` 自环，则该站禁止同站换乘；
  - **同一辆车继续乘坐不计换乘**；
  - 零秒换乘环在 Dijkstra 闭包中不会产生更优标号，**必然终止**。
- 结果：按 **（到达时刻, 换乘次数）保留非支配行程（Pareto）**；
  指标相同时只保留 **trip_id 序列字典序最小**者；每段给出上下车站点与时刻。
- 起终站相同：返回 **1 条零段行程**；无可达路径：返回空列表 `[]`。
- 性能：**10 万条站时、结果 ≤ 10 条时，服务端查询 < 2 秒**
  （合成数据实测约 0.4–0.8 秒，见 `tests/test_performance.py`）。

## 快速开始（Docker Compose）

```bash
docker compose up -d --build
```

启动后浏览器访问：

```
http://localhost:8000
```

SQLite 数据库保存在命名卷 `transit-data`（容器内 `/data/transit.db`），
重新构建镜像不会丢数据。

停止 / 查看日志：

```bash
docker compose down
docker compose logs -f
```

## 本地开发（不使用 Docker）

需要 Python 3.11+：

```bash
pip install -r requirements.txt
export DB_PATH=./data/transit.db     # 默认为 /data/transit.db
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

访问 http://localhost:8000 。

## 导入时刻表

### 方式一：页面上传（推荐）

打开首页 →「1. 导入时刻表」选择 GTFS ZIP 文件 →「上传并导入」。
导入成功会显示各实体数量；失败会列出具体校验错误（整包拒绝，旧数据保留）。

### 方式二：命令行 / API

```bash
curl -X POST http://localhost:8000/api/import \
  -F "file=@/path/to/gtfs.zip"
```

成功响应：

```json
{ "status": "imported",
  "summary": { "agencies": 1, "stops": 5, "routes": 2,
               "trips": 3, "stop_times": 8,
               "transfers": 1, "forbidden": 0, "patterns": 3 } }
```

失败响应（HTTP 422，整包拒绝）：

```json
{ "status": "rejected",
  "detail": "整包已拒绝，旧数据仍可查询",
  "errors": ["stop_times.txt 第 12 行：trip 'T9' 站时倒序——..."] }
```

ZIP 中必须包含：`agency.txt`、`stops.txt`、`routes.txt`、`trips.txt`、
`stop_times.txt`，以及 `calendar.txt` / `calendar_dates.txt` 至少其一；
`transfers.txt` 可选。

## 查询换乘

页面上选择/输入起终站（支持按 ID 或名称模糊搜索）、出发日期时刻和最大换乘
次数后点击「搜索行程」。也可以直接调用 API：

```bash
curl -X POST http://localhost:8000/api/journeys \
  -H 'Content-Type: application/json' \
  -d '{
    "origin_id": "S1",
    "destination_id": "S5",
    "depart_date": "2026-10-04",
    "depart_time": "08:00:00",
    "max_transfers": 2,
    "agency_id": "A1"
  }'
```

> 日期与时刻均按所选 **agency 的时区**解释（`agency_timezone`）；
> 跨夜 trip 的 `HH:MM:SS` 小时可超过 24（例如 `25:10:00` 表示服务日次日
> 01:10）。多 agency 数据下按 `agency_id` 限定线路。

响应示例：

```json
{
  "origin": {"stop_id": "S1", "stop_name": "一号站"},
  "destination": {"stop_id": "S5", "stop_name": "五号站"},
  "timezone": "Asia/Shanghai",
  "depart_after": "2026-10-04 08:00:00",
  "search_until": "2026-10-05 08:00:00",
  "count": 1,
  "journeys": [{
    "transfers": 1,
    "arrival_time": "2026-10-04 09:50:00",
    "legs": [
      {"seq": 1, "trip_id": "T1",
       "from_stop": "S1", "to_stop": "S3",
       "board_time": "2026-10-04 08:00:00",
       "alight_time": "2026-10-04 08:30:00"},
      {"seq": 2, "trip_id": "T3",
       "from_stop": "S2", "to_stop": "S5",
       "board_time": "2026-10-04 09:20:00",
       "alight_time": "2026-10-04 09:50:00"}
    ]
  }]
}
```

其他接口：

| 方法 | 路径 | 说明 |
| ---- | ---- | ---- |
| GET  | `/api/health` | 健康检查 |
| GET  | `/api/meta` | 已导入的 agency 与统计信息 |
| GET  | `/api/stops?q=&limit=` | 按 ID / 名称模糊检索站点 |
| POST | `/api/import` | 上传 GTFS ZIP（multipart 字段 `file`） |
| POST | `/api/journeys` | 行程查询（JSON 参数如上） |

## 目录结构

```
app/
  main.py           FastAPI 入口与 API
  planner.py        RAPTOR 行程规划（服务日展开/换乘/Pareto）
  gtfs_importer.py  GTFS ZIP 解析、校验、整包事务导入
  db.py             SQLite 连接与表结构
  timeutils.py      GTFS 时刻/日期/时区工具
static/index.html   单页前端
tests/              功能、API 与 10 万站时性能测试
Dockerfile
docker-compose.yml
requirements.txt
```

## 运行测试

```bash
pip install -r requirements.txt pytest httpx
python -m pytest tests/ -q
```

## 算法说明（简要）

1. 按查询 agency 时区确定出发日零点，展开 D-1 / D / D+1 三个服务日，
   由 calendar + calendar_dates 求各日实际运营 service（例外覆盖周历）。
2. 将三日服务内的 trip 实例化，GTFS 相对秒换算为绝对秒（D-1 减 86400、
   D+1 加 86400，`>24:00` 时刻天然落入次日）。
3. trip 按“线路 + 有序停站序列”归为 pattern；每个停站位置维护
   `(发车时刻, trip_id)` 有序索引，二分定位可乘车次。
4. RAPTOR 圈迭代（圈数 = 乘段数 ≤ max_transfers + 1）：
   - 圈内扫描相关 pattern，逐站先下车、再判断上车/换车（同实例不回登）；
  - 圈末对所有下车标号做显式换乘边的多源 Dijkstra 闭包（同站接续 0 秒，
    type=3 方向剔除），得到下一圈可上车站点集合。
5. 收集终到站全部标号，按 `(到达, 换乘次数, trip 序列)` 取 Pareto 非支配集，
   重建各乘行段的上下车时刻后返回。
