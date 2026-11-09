# token-trace 程序接入指南（JSON API）

面向数据分析脚本、Notebook 与 BI 取数。网页版 `/api` 页是给人看的接口清单，本文补充
**字段字典、量纲、取数范式与扩展性说明**。

> **适用范围**：`v1.2.0` 及以上（分析端点自该版本起进入 Release）。更早版本只有
> 17 个端点、没有 `/api/analytics/calls`；任何版本都以服务自身的 `/api` 页为实时清单。

---

## 1 先理解一件事：JSON 不是前端的数据源

项目是**零 JavaScript 的服务端渲染**：18 个 HTML 页面由服务端直接算好、拼成完整网页
吐给浏览器，页面里所有 `/api` 链接都是给人点开核对的 `<a href>`，没有 `fetch` / AJAX。

所以：

- **HTML 与 JSON 不是两套系统**，是同一份内存数据的两种导出（`df.to_html()` vs `df.to_json()`）
- **JSON 不写盘**：每次请求现算 `json.dumps()` → 通过 socket 发出 → 用完回收。服务端
  全程只读源 jsonl，不生成任何 JSON 文件，也不在内存里留一份「JSON 副本」
- 网页不会因新增 API 而变化；反过来，改网页也不会影响 API 契约

---

## 2 快速开始

```bash
# 启动（默认 127.0.0.1:8765）
cd token-trace && ./start.sh
# 或
python3 -B trace_server.py --port 8765 --warm

# 探活
curl -s http://127.0.0.1:8765/api/health

# 取一页调用宽表
curl -s "http://127.0.0.1:8765/api/analytics/calls?limit=3" | python3 -m json.tool
```

**鉴权**：默认只监听 loopback、无鉴权。绑定非本机地址（`--host`）时**必须**配 `--token`，
请求带 `Authorization: Bearer <token>`。**无 CORS**，浏览器里的 BI 工具不能直连，
请用后端脚本 / Notebook 取数，或自行加一层反向代理。

> 本机若设了 `HTTP_PROXY` / `HTTPS_PROXY`，`127.0.0.1` 的请求需绕过代理
> （curl 加 `--noproxy '*'`；Python 用 `ProxyHandler({})`），否则可能拿到 502。

---

## 3 统一约定

| 项 | 约定 |
| --- | --- |
| 信封 | 成功 `{"ok":true,"data":...,"meta":{...}}`；失败 `{"ok":false,"error":"..."}` + 4xx/5xx |
| 分页 | `offset` / `limit`；`meta` 给 `total`、`offset`、`limit`、`next`（相对 URL，已保留全部其它查询参数） |
| 方法 | 除 `POST /shutdown` 外全部只读 `GET` |
| 编码 | UTF-8，`ensure_ascii=false` |
| 并发 | `ThreadingHTTPServer`；重计算路径有锁，索引与会话全文有 LRU 缓存 |
| 写盘 | 服务进程**零写盘**（只读源数据）；详见 §9 |

### 时间戳量纲（最容易踩的坑）

| 位置 | 单位 | 说明 |
| --- | --- | --- |
| `/api/sessions[].mtime` | **秒**（float） | `os.path.getmtime`，文件最后修改时间 |
| `/api/sessions[].first_ts` | **毫秒** | 会话首条记录 |
| `/api/sessions[].days` | `YYYY-MM-DD` 字符串 | 按**每次调用真实发生日**聚合，值 `[输入 token, 调用数]` |
| `/api/analytics/calls[].ts` | **Unix 毫秒** | 新端点已统一，并在 `meta.ts_unit` 声明 |
| `/api/analytics/calls[].date` | `YYYY-MM-DD` | 由 `ts` 换算，**服务器本地时区**，不是 UTC |

旧端点里秒 / 毫秒混用是历史口径；新端点全部统一为毫秒。做时间对齐时请以
`ts` + `meta.ts_unit` 为准，不要用 `mtime` 当调用时间。

---

## 4 分析宽表：`GET /api/analytics/calls`

跨会话的**调用级扁平宽表**——分析程序最常用的一张表。以前要先取 `/api/sessions`，
再对每个会话调 `/api/s/<sid>/calls` 翻页（本库实测 **42 次请求 / 408 ms** 才能凑齐），
现在一次请求即可。

### 参数

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `from` / `to` | 不限 | `YYYY-MM-DD` 或 Unix 秒 / 毫秒数字；闭区间，`to` 为日期时含当天 23:59:59.999 |
| `model` | 不限 | 归一后**精确匹配**（大小写、`-`/`_` 差异忽略），例如 `deepseek-v4.1-flash` |
| `ws` | 不限 | 工作区路径**大小写不敏感子串**匹配 |
| `tool` | 不限 | 工具名**大小写不敏感子串**匹配 |
| `fields` | 全部 14 列 | 逗号分隔的列投影；非法列名返回 **400**（不静默忽略） |
| `offset` / `limit` | `0` / `200` | `limit` 上限 **500**，超出自动夹到 500 |

非法 `from` / `to`、`from > to`、非法 `fields` 都返回 400 + 错误信封。

### 返回示例

```json
{
  "ok": true,
  "data": [
    {
      "sid": "b8bcd5a3-...", "seq": 1, "ts": 1789031263099,
      "date": "2026-11-09", "model": "DeepSeek-V4.1-Flash", "tool": "Bash",
      "pin": 123456, "hit": 120000, "miss": 3456, "pout": 812,
      "credit": 0.19, "rate": 0.9736, "turn": 1,
      "ws": "/home/linwei/work/order-service"
    }
  ],
  "meta": {
    "total": 7891, "offset": 0, "limit": 200,
    "next": "/api/analytics/calls?offset=200&limit=200",
    "ts_unit": "unix_ms", "date_tz": "server local time",
    "fields": ["sid", "seq", "..."],
    "model_key": "providerData.model（全库统一展示名）"
  }
}
```

**实现要点**：过滤下推到会话级（用 `first_ts`/`mtime` 保守区间跳过整段无关会话），
且**只物化 `[offset, offset+limit)` 这一段**。因此序列化开销随 `limit` 走、不随全库调用数走；
`meta.total` 是过滤后的真实命中数，不受 `limit` 影响。

### 字段字典

| 列 | 类型 | 单位 | 说明 |
| --- | --- | --- | --- |
| `sid` | str | — | 会话 id（jsonl 文件名） |
| `seq` | int | — | 会话内调用序号，从 1 起 |
| `ts` | int | **Unix 毫秒** | 调用真实时间；异常/缺失为 `0` |
| `date` | str | `YYYY-MM-DD` | `ts` 的本地日期；异常为 `""` |
| `model` | str | — | 全库统一展示名；**未知模型为 `""`**（不是 `"?"`） |
| `tool` | str | — | 工具名；模型最终回答为 `"最终回答"` |
| `pin` | int | token | 输入 token |
| `hit` | int | token | 缓存命中 token |
| `miss` | int | token | `pin - hit`，未命中 |
| `pout` | int | token | 输出 token |
| `credit` | float | 积分 | 本次调用计费 |
| `rate` | float \| null | 0–1 | `hit / pin`；`pin=0` 时为 **`null`** |
| `turn` | int | — | 所属轮次 |
| `ws` | str | — | 工作区路径（每行冗余，宽表自包含，便于直接 group by） |

---

## 5 取数示例

### 翻完整个结果集

```python
import requests

BASE = "http://127.0.0.1:8765"
url, params = BASE + "/api/analytics/calls", {"from": "2026-11-01", "limit": 500}
rows = []
while url:
    r = requests.get(url, params=params, timeout=60).json()
    if not r["ok"]:
        raise RuntimeError(r["error"])
    rows += r["data"]
    nxt = r["meta"].get("next")
    url, params = (BASE + nxt if nxt else None), None   # next 已含全部参数
print(len(rows), "rows / total", r["meta"]["total"])
```

### 直接读成 DataFrame 并聚合

```python
import pandas as pd

df = pd.DataFrame(rows)
df["dt"] = pd.to_datetime(df["ts"], unit="ms")
by_day   = df.groupby("date")[["pin", "hit", "pout", "credit"]].sum()
by_model = df.groupby("model").agg(
    calls=("seq", "size"), hit_rate=("hit", "sum"),
    input_tokens=("pin", "sum")).assign(
        hit_rate=lambda x: x.hit_rate / x.input_tokens)
```

也可用 `fields=ts,model,pin,hit,pout,credit` 做列投影，减少传输量。

---

## 6 原有 17 个端点

按用途分组，完整清单与实时说明见服务的 `/api` 页（由 `ROUTE_SPEC` 单一事实源自动生成，
与分发、自检同步）：

| 组 | 端点 |
| --- | --- |
| 全局 | `/api/health`、`/api/sessions`、`/api/stats`、`/api/tools` |
| 单会话 | `/api/s/<sid>`、`.../calls`、`.../calls/<seq>`、`.../turns/<n>`、`.../trend`、`.../compact` |
| 上下文块 | `/api/s/<sid>/calls/<seq>/context`、`.../blocks/<idx>`、`.../blocks/<idx>/users`、`.../blocks/<idx>/find` |
| 输出 | `/api/s/<sid>/outputs/<seq>` |
| 检索 | `/api/search`、`/api/find` |

本次同步修正：这些端点里由 `call_brief()` 返回的 `model` 原先是随客户端变化的原始展示名
（同一模型出现三种大小写），现已与新端点、`/api/sessions` **统一为全库唯一展示名**；
没有模型名的调用由字面量 `"?"` 收敛为空串。按 `model` 分组不会再裂成多份。

---

## 7 已知口径与坑

1. **宽表 `total` 与 `/api/sessions` 的 `calls` 求和不相等**（某次实测 7,891 vs 5,482，
   数据会持续增长）。索引里的 `calls` 只数「有 `prompt_tokens` 的 function_call」；
   宽表还包含 1,821 条「最终回答」和 588 条无 token 的调用。跨端点对数前先确认口径。
2. **`rate` 可能为 `null`**，pandas 里记得 `fillna(0)` 或先过滤 `pin > 0`。
3. **`from`/`to` 是服务器本地时区**，跨时区部署需要在客户端自行换算成毫秒传参。
4. **`limit` 上限 500**；单页 500 行本机实测 **138 KB**，比最大 HTML 页（67 KB）还大。
   80 KB 的自检上限 **只约束 HTML 路由**，API 不受限——但仍建议分页，别一次拉全量。
5. **无 CORS、默认 loopback**；浏览器 BI 直连不通，非本机绑定必须配 `--token`。
6. **模型名是展示名**（`DeepSeek-V4.1-Flash`），不是底层 ID；需要稳定机器键时
   自行做 `lower()` + 把 `-`/`_`/空格折叠。
7. 服务**只读**：数据由 WorkBuddy 写入 `~/.workbuddy/projects/**/*.jsonl`，
   本服务不会修改、整理或删除源文件。

---

## 8 规划中（尚未实现）

以下都在同一套信封 / 分页 / `ROUTE_SPEC` 下扩展，不需要改动 18 个 HTML 路由。
先落地宽表这一小步，是为了覆盖大部分取数需求，同时把范围控制住：

| 端点 | 用途 |
| --- | --- |
| `/api/analytics/timeseries?by=day\|model\|ws&metric=...` | 直接消费索引里已有的 `days`，省去客户端聚合 |
| `/api/schema` | 机器可读字段字典（目前 `meta.fields` 只给列名，不给类型/单位） |
| `format=csv\|jsonl` | 让 BI / 流式读取直接吃，避免整包 JSON 解析 |

在此之前，用宽表 + pandas 自己 `groupby` 即可等价实现。

---

## 9 扩展性：数据多了要不要落盘？

这一节回应「数据多了全放内存不妥帖、是否考虑读写盘」。结论先说：
**写入寿命完全不是约束（判断成立，余量比预想大几个数量级）；内存也不是当前瓶颈，
因为它已有硬上界。真正值得落盘解决的是冷启动全量扫描。**

### 9.1 内存里到底放了什么（本机实测）

| 项 | 实测 | 说明 |
| --- | --- | --- |
| 源数据 | **104 MB** / 34 个 jsonl | WorkBuddy 写的，**只读** |
| 会话索引对象 | **20.8 KB**（每会话 627 B） | 摘要字段 + `days` + `models`，极小 |
| 服务常驻 `rss` | **114 MB** | 大头是 `STORE._cache` 里的**会话全文**（输出段、上下文块） |
| 缓存容量 | `cap = max(32, min(会话数+8, 256))` | **`CACHE_MAX=256` 是硬上界**，单会话约 1.5 MB → 约 380 MB |
| 服务启动 → 可用 | **6.3 s** | 主要是全量扫描解析 104 MB |
| 启动后查询 | 二次 `/api/sessions` **6 ms** | 全走内存 |

所以「数据多了全放内存」并不准确：索引只有 21 KB，全文缓存有 256 个会话的上界，
超过就退化为 LRU（代价是 `/stats` 重新解析超出部分），**不会无限增长**。

### 9.2 硬盘寿命：你的直觉是对的，而且余量极大

若只落一份**派生索引快照**（21 KB，纯摘要，不含会话全文）：

- 全量重写一次 ≈ 21 KB；每新增 1 个会话增量 ≈ 627 B
- 近 7 天源数据新增 58.7 MB（日均 8.4 MB，由 WorkBuddy 写入）；按日均 2 个新会话、
  每天重写一次快照估算 ≈ **41.6 KB/天，一年约 15 MB**
- 保守取消费级 SSD 的 TBW = 100 TB：`100 TB / 15 MB ≈ 660 万年`
- 即便每天写 1 MB（比实际激进 25 倍），寿命仍在万年量级

**追加写入 + 读为主对 SSD 寿命的影响可以忽略。** 真正需要考虑的是下面这些工程约束，
它们跟「磨损」无关。

### 9.3 打破「运行期零写盘」契约

目前这是项目的显式承诺，落盘会连带改动：`/api/health` 的 `write_files` 自报字段、
`selftest.py` 的「运行期零写盘」断言、README 口径，以及 Windows 单文件 bat
「只把 launcher.py 和图标写进 `%LOCALAPPDATA%`、主程序从内存加载」的设计前提。
这不是加几行代码，是一次需要明确决策的架构变更。

### 9.4 真正的成本：一致性与恢复

1. **源数据异步追加**：jsonl 由 WorkBuddy 随时写入，读时可能遇到写了一半的行；
   快照必须带指纹（`path` + `size` + `mtime`，最好加尾部 hash）才能判断失效。
2. **原子性**：不能就地改，要 `write temp → os.replace`；服务和独立
   `gen_trace.py` 命令行工具可能同时访问，需要文件锁。
3. **崩溃恢复**：快照带 `version` + `checksum`，损坏时要有「回退全量扫描」的兜底。
4. **隐私**：磁盘上多一份可直接读取的会话摘要（源 jsonl 本身已在磁盘，但可读性不同）。
5. **收益边界**：落盘只省**冷启动**；查询期本来就在内存，不会因为落盘变快。

### 9.5 建议的演进路径（**尚未实现，需单独决策**）

| 阶段 | 内容 | 触发条件 |
| --- | --- | --- |
| 0（当前） | 不写盘；索引 21 KB + 全文 LRU 上界 256 | 会话数 < 200、冷启动 < 15 s |
| 1 | **只落派生索引快照**：用 SQLite（Python 标准库，WAL / 原子写 / 并发安全），表 `sessions(sid PRIMARY KEY, size, mtime, payload_json)`；启动时按指纹只重解析变化的文件，冷启动从秒级降到毫秒级 | 会话数长期 > 200，或冷启动 > 15 s |
| 2 | 可选：把宽表物化成按日分区的只读视图，供 BI 直连 | 确实出现跨进程 / 跨机取数需求 |

**关键不变式**：源 jsonl 永远只读，会话全文不落第二份（那才是 1.5 MB/会话的大头）。
阶段 1 的写入量就是 §9.2 估算的几十 KB/天，寿命与容量都不是问题。

验收建议：冷启动 < 1 s、二次查询不变慢、把「零写盘」断言改成「只写派生索引」
并单独标注，同时在 `/api/health` 增加 `index_backend: memory | sqlite` 字段。

---

## 10 变更记录

| 日期 | 变更 |
| --- | --- |
| 2026-11-09 | 新增 `/api/analytics/calls` 扁平宽表（`from/to/model/ws/tool/fields` + 分页）；`call_brief()` 模型名归一、`?` → 空串；`ROUTE_SPEC` 18 个 JSON 端点；固化自检 113 → 127 项 |
