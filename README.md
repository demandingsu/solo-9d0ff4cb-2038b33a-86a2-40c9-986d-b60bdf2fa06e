# 电台档案 UTC 查档服务

按录音的 **UTC 时间** 查节目。旧日志只记本地开播时刻，服务接收单频道日志与
**IANA 时区**，负责把本地时刻换算成 UTC；夏令时回拨导致同一本地时刻出现两次时，
**两种 UTC 时刻都会纳入核对**。

值班员还可提交**对账单**：给出一段（最长 1 小时）UTC 核对窗口与监听记录给出的
「在播总秒数」。服务只保留对得上该总秒数的完整排法，此后排法数与按秒查档都按
这套排法计算；每频道至多保留一张对账单，可换单，换单后原来被排除的候选可重新成立。

- 框架：FastAPI + SQLite（WAL 模式）
- 时段语义：半开区间 `[start, end)`，含起点、不含终点
- 排法：每个节目从其合法 UTC 开播候选中选一个；按流水号顺序首尾不相叠、
  且不与任何停播段相交，即为一种完整排法
- 对账：节目只计落在核对窗口内的秒数；只有存在一种完整排法，使窗口内各节目
  裁切秒数之和恰等于提交的在播总秒数，对账单才登记/换单
- 查询：给定一个 UTC 秒，返回在**至少一种（按当前对账单过滤后的）完整排法**中
  播到该秒的节目流水号，以及该秒是否**可能是空档**（存在一种完整排法不播任何节目）

## 目录结构

```
app/
  domain.py    # 时区换算、回拨双候选、DP 可解性/空档判定、对账窗口 DP（O(n·k·L)）
  db.py        # SQLite 访问、WAL、写事务原子提交、快照读、对账单表
  schemas.py   # Pydantic 入参/出参与校验（窗口最长 1 小时等）
  main.py      # FastAPI 端点
tests/         # pytest：API 端到端 + DP 对暴力枚举的逐秒一致性 + 随机对抗
Dockerfile, docker-compose.yml
```

## 启动

```bash
docker compose up --build
# 打开交互式文档
open http://localhost:8000/docs
```

数据库存放在命名卷 `radio-data`（容器内 `/data/radio.db`），容器重建后数据保留。

## API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/channels` | 接收/整份替换单频道日志（含 IANA 时区） |
| GET | `/channels/{id}` | 读取一份已提交档案（回显版本号、当前对账单） |
| GET | `/channels/{id}/solve` | 整档是否有解、（按当前对账单过滤后的）完整排法数 |
| PUT | `/channels/{id}/blackouts` | 登记/更正一条 UTC 停播段（按 id 覆盖） |
| DELETE | `/channels/{id}/blackouts/{bid}` | 删除停播段 |
| PUT | `/channels/{id}/reconciliation` | 登记/换单：提交核对窗口与在播总秒数 |
| GET | `/channels/{id}/reconciliation` | 读取当前对账单及其匹配排法数 |
| GET | `/channels/{id}/query?utc_second=` | 查档：播出节目 + 空档判定（有单时按单过滤） |

## 对账语义

- 入参 `start_utc`、`end_utc`（UTC 纪元秒，半开，窗口最长 3600 秒）与
  `on_air_seconds`（监听记录的窗口内在播总秒数，`0 ≤ 值 ≤ 窗口长度`）。
- 节目 `[s, s+duration)` 只把落在窗口内的部分计入，即
  `max(0, min(s+duration, end_utc) − max(s, start_utc))`；窗口外的秒不计。
- 一种完整排法的窗口总秒数 = 各节目裁切秒数之和。**只有至少一种完整排法的总和
  恰等于 `on_air_seconds`，对账单才落档**；否则返回 409，旧单、旧档原封不动。
- 每频道只保留一张对账单（表上 `channel_id` 主键）。再次 PUT 即**换单**：用新单
  重新核对，原先被旧单排除的候选可重新成立。
- 登记对账单后，`/solve`、`/query`、`/channels/{id}` 中的 `arrangement_count`
  与按秒 `on_air`/`may_be_gap` **只按对得上总秒数的完整排法**计算；
  `matched_arrangement_count` 即这套排法的数量。
- 替换日志或更正/删除停播段时，也在同一写事务内按当前对账单复核；若新档案状态
  下没有任何排法能对上当前单，整次写入退回（409），旧档与旧单继续可查。
- 每次写入都在**同一档案状态**（同事务内读到的节目+停播段+对账单）上校验并落档；
  无解或写入失败一律回滚。查询在**单快照事务**内同时取节目、空档、排法数与版本号，
  三项资料同属一个已提交版本。

## 在 /docs 试查回拨时段（2024-11-03，America/New_York）

纽约在 2024-11-03 02:00 本地回拨到 01:00，本地 `01:30` 出现两次：

- 第一次（夏令时 EDT, UTC-4）→ `05:30Z` = 纪元秒 **1730611800**
- 第二次（标准时 EST, UTC-5）→ `06:30Z` = 纪元秒 **1730615400**

### 1. `POST /channels` 上传日志

```json
{
  "channel_id": "ny-fm",
  "timezone": "America/New_York",
  "programs": [
    {"seq": 1, "local_start": "2024-11-03T01:30:00", "duration": 900},
    {"seq": 2, "local_start": "2024-11-03T01:45:00", "duration": 900}
  ]
}
```

响应 `arrangement_count = 3`，对应三种完整排法（早早 / 早晚 / 晚晚）。

### 2. `GET /channels/ny-fm/query?utc_second=1730611800`（05:30Z）

```json
{
  "on_air": [1],
  "may_be_gap": true,
  "in_blackout": false,
  "arrangement_count": 3
}
```

节目 1 在部分排法中播到该秒；选择「晚晚」排法时该秒是空档，故 `may_be_gap=true`。

### 3. `PUT /channels/ny-fm/blackouts` 登记停播段 05:30–06:00Z

```json
{"blackout_id": "bo-1", "start_utc": 1730611800, "end_utc": 1730613600, "note": "检修"}
```

封掉两种含早候选的排法，只剩「晚晚」一种；再查 05:30Z 得到
`on_air=[]`、`may_be_gap=true`、`in_blackout=true`；查 06:30Z（1730615400）
得到 `on_air=[1]`、`may_be_gap=false`（唯一排法里必播）。

### 4. 尝试让整档无解的停播段会被拒绝

再登记 06:30–07:00Z（`start_utc=1730615400, end_utc=1730617200`）将封死最后一种
排法，接口返回 **409**，旧档版本号不变、继续可查。

### 等价 curl 流程

```bash
curl -s localhost:8000/channels -H 'content-type: application/json' -d @- <<'JSON'
{"channel_id":"ny-fm","timezone":"America/New_York","programs":[
 {"seq":1,"local_start":"2024-11-03T01:30:00","duration":900},
 {"seq":2,"local_start":"2024-11-03T01:45:00","duration":900}]}
JSON
curl -s 'localhost:8000/channels/ny-fm/query?utc_second=1730611800'
curl -s -X PUT localhost:8000/channels/ny-fm/blackouts -H 'content-type: application/json' \
  -d '{"blackout_id":"bo-1","start_utc":1730611800,"end_utc":1730613600}'
curl -s 'localhost:8000/channels/ny-fm/query?utc_second=1730615400'
```

## 在 /docs 试对账（沿用上面的 ny-fm 回拨档案）

三种合法完整排法为 早早(EE)/早晚(EL)/晚晚(LL)。取核对窗口
`[05:30Z, 06:00Z)`（`start_utc=1730611800, end_utc=1730613600`，1800 秒），
各排法窗口内在播总秒数分别为：

- **EE**：P1 早 `[05:30,05:45)` + P2 早 `[05:45,06:00)` = **1800**
- **EL**：仅 P1 早 900（P2 晚 06:45 才开播）= **900**
- **LL**：窗口内无节目（P1 晚 06:30 才开播，窗口终点不含）= **0**

### 1. `PUT /channels/ny-fm/reconciliation` 登记对账单（总秒数 1800）

```json
{"start_utc": 1730611800, "end_utc": 1730613600, "on_air_seconds": 1800}
```

响应 `matched_arrangement_count = 1`（只 EE 对得上），`version` 递增。
此后 `GET /channels/ny-fm/query?utc_second=1730615400`（06:30Z）在唯一排法 EE
中无节目：`on_air=[]`、`may_be_gap=true`、`arrangement_count=1`，并回显当前对账单。

### 2. 提交落在上下限内却取不到的总秒数 → 409

窗口长度 1800，总秒数 `500` 满足 `0 ≤ 500 ≤ 1800`，但三种排法的可达总和只有
`{1800, 900, 0}`，没有任何完整排法能对上 → 返回 **409**，旧单（1800）保留、版本不变。

### 3. 换单为总秒数 0，原来被排除的晚候选重新成立

再 `PUT` 同窗口、`on_air_seconds=0`：只匹配 LL，`matched_arrangement_count=1`。
此时查 06:30Z 得到 `on_air=[1]`、`may_be_gap=false`——晚候选 P1 在 LL 中恢复播出。
每频道始终只有一张对账单（再 PUT 即换单，不会新增）。

### 4. 更正停播段/替换日志也要对上当前单

若一条停播段（或一份新日志）使当前对账单不再有任何排法对得上，写入在同一事务内
被拒绝并整体回滚（**409**），旧档、旧单原封不动、继续可查。

### 等价 curl 流程

```bash
curl -s -X PUT localhost:8000/channels/ny-fm/reconciliation \
  -H 'content-type: application/json' \
  -d '{"start_utc":1730611800,"end_utc":1730613600,"on_air_seconds":1800}'
curl -s 'localhost:8000/channels/ny-fm/query?utc_second=1730615400'
curl -s -X PUT localhost:8000/channels/ny-fm/reconciliation \
  -H 'content-type: application/json' \
  -d '{"start_utc":1730611800,"end_utc":1730613600,"on_air_seconds":0}'
curl -s 'localhost:8000/channels/ny-fm/reconciliation'
```

## 一致性保证

- 每次查询在**单快照事务**里读取整档（节目、停播段、对账单与版本号同属一个已提交
  版本），只对应一份已提交资料；响应带 `version`，交错的查档/更正可按版本号核对。
- 每次写入在**单个 `BEGIN IMMEDIATE` 事务**内、按**同一档案状态**完成落库与校验：
  先校验整档仍有完整排法，再（若已登记对账单）校验至少有一种排法对得上当前单。
- 整档无解（含春令时跳空的不存在本地时刻、停播段封死全部排法）、对账单对不上
  监听总秒数，或写入失败，一律回滚，旧档案与旧对账单原封不动、继续可查，
  `version` 不增加。

## 本地测试

```bash
pip install -r requirements.txt pytest httpx
pytest -q
```

测试包含：API 端到端、对账 DP 对暴力枚举的逐窗口/逐秒一致性，以及 60 组随机
档案（随机节目数、候选、停播段、窗口）的对抗校验。
