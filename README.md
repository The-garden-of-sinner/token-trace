# Token 轨迹

把 WorkBuddy 的会话记录还原成逐次调用的 token 轨迹：每一轮花了多少积分、分几次模型调用、
每次调用带上了哪些上下文、缓存命中到哪里。

**两种模式，按需选：**

| 模式 | 入口 | 内容 | 前端负担 | 适用 |
|---|---|---|---|---|
| **完整模式（推荐）** | `trace_server.py` + 浏览器 | 零截断，全部调用 / 全部原文 | 每屏 10–50 KB、110–520 个标签 | 日常排查、要看全文 |
| **静态模式（兜底）** | `gen_trace.py` → `trace/index.html` | 最近 150 次调用、每块 3000 字 | 单页 1–4.6 MB | 离线拷给别人、双击就开 |

完整模式解决的是旧架构里那对矛盾：**内容要全，页面又不能重**。
做法是把「展开」变成一次后端请求 —— 翻页、点某次调用、看某个块的全文，
都由服务端查数据、算缓存边界、渲染好一屏 HTML 再返回。浏览器拿到的永远只有一屏，
但任何一条数据都能顺着链接走到，一个字符都不丢。

所有页面**零 JavaScript**。这是 v3 踩出来的硬约束：托管预览（`/static-html/<hash>/`）不执行
`<script>`，所以核心路径全部是 `<a href>` + `<details>`，禁用脚本照样能用。

---

## 快速开始

### 桌面一键启动（最省事）

双击桌面上的「**Token 轨迹**」图标即可：服务没在跑就后台启动 → 等就绪 → 自动打开浏览器；
已经在跑就直接打开浏览器，**不会多起一个进程**。全程不用开终端。

```bash
./start.sh                       # 命令行手动跑，效果同上
./start.sh stop                  # 一键停止
TRACE_PORT=9000 ./start.sh       # 换端口（避免和别的服务冲突）
TRACE_NO_BROWSER=1 ./start.sh    # 只起服务、不开浏览器（无头 / 自动化）
```

本机已装好 `~/桌面/Token 轨迹.desktop`（桌面双击）和
`~/.local/share/applications/token-trace.desktop`（应用菜单）。
克隆到新机器上跑一次 `./install-desktop.sh`：它把 `token-trace.desktop` 模板里的 `@EXEC@`
替换成当前目录的 `start.sh` 绝对路径后再安装，不写死任何用户名或目录。

`start.sh` 用系统自带的 `/usr/bin/python3 -B`（3.8 实测可跑，不依赖 WorkBuddy 内部解释器），
`-B` 保持零写盘。实测冷启动 **2.0 s**、常驻 **23.5 MB**（未预热，随访问按需增长）；
停止走 `POST /shutdown` 让服务自己退出，不依赖 `pkill` 找 PID。

### Windows

双击 `windows\start.cmd`，或在 PowerShell 里运行 `.\windows\start.ps1`；
`.\windows\start.ps1 stop` 停止，`-Port 9000` 换端口。
首次执行 `.\windows\install-shortcut.ps1` 可创建桌面 / 开始菜单快捷方式。
详见 [Windows 支持](#windows-支持)。

### 命令行启动

```bash
cd token-trace

# 完整模式：起一个只监听本机的服务
python3 trace_server.py                  # http://127.0.0.1:8765/
python3 trace_server.py --port 9000 --warm   # 换端口；启动时预解析全部会话
python3 trace_server.py --cache 4        # 内存吃紧时用 LRU 只留 4 个会话

# 静态模式：生成纯 HTML（默认行为不变）
python3 gen_trace.py                     # 全部会话 → trace/index.html + s-*.html
python3 gen_trace.py path/to/x.jsonl -o out.html
python3 gen_trace.py --all              # 关掉截断（可能几十 MB 单页，慎用）
```

完整模式启动后打开首页：按工作区分组 → 会话 → 轮 / 调用 → 上下文块清单 → 块全文。
每一级都是独立 URL，可以直接收藏、分享、用 `curl` 取。

`Ctrl+C` 停止服务。服务**只读**源文件，不写 `~/.workbuddy/` 下任何东西。

---

## 架构总览

```
~/.workbuddy/projects/**/*.jsonl   workbuddy.db
              │ 只读
              ▼
┌─────────────────────────────────────────────────────────┐
│ 数据层  gen_trace.build(path, full=True) + Store          │
│  · 重建 ctx 全局块序列，每个 call 存 ctxN 快照            │
│  · full=True 保留原文（旧静态模式仍用默认的截断版）       │
│  · 前缀和 pref[]、ctxNs[]、by_turn、cold 断点             │
│  · LRU 缓存已解析会话，默认 32（实测全量常驻 83 MB）      │
├─────────────────────────────────────────────────────────┤
│ 计算层  call_state / call_outline / block_users / search │
│  · O(1) 判块状态，二分求反向索引，正则流式全文检索        │
├──────────────────────┬──────────────────────────────────┤
│ HTML 渲染层           │ JSON API 层                      │
│  page() 统一外壳      │  /api/* 统一 {ok,data,meta} 信封 │
│  每页只渲染一屏       │  分页用 offset/limit，Range 语义 │
│  零 <script>          │  供脚本 / 外部工具调用           │
└──────────────────────┴──────────────────────────────────┘
              │ 一次请求 = 一屏
              ▼
        浏览器（不需要 JS，不需要构建）
```

四层都在 `trace_server.py` 一个文件里；`gen_trace.py` 只提供解析原语（`build`、
`scan_all`、`brief`、`cmd_html`、`seg_user`），两个模式共用同一套口径，不会算出两套数字。

### 数据层：一次解析，全文常驻

- `Store.sessions()` 常驻会话索引（28 条摘要，60 秒或 `/?refresh=1` 失效），
  从 `workbuddy.db` 补 `cwd` / 标题 / 模型，从 jsonl 补统计。
- `Store.get(sid)` 第一次访问某会话时调 `gen_trace.build(path, full=True)`。
  最大会话 1328 次调用 / 2575 块 / 256 万字，解析 **0.69 s**。
- 派生索引在 `Store._derive()` 一次算好：
  - `pref[]` 累积字数前缀和 → 任意区间的可见字数 O(1)；
  - `ctxNs[]` 每次调用前的块数（单调不减）→ 供二分；
  - `by_turn` 调用归属、`cold` 冷启动断点、`stats` 全量 KPI、按类型的字数构成。
- **不建倒排索引、不落盘、不改源文件**。28 个会话全部常驻实测 **83 MB RSS**，
  预热 7.1 s；预热后翻页、搜索、统计都是内存操作。

### 计算层：后端把重活干完

| 函数 | 做的事 | 复杂度 |
|---|---|---|
| `call_state(d, seq, i)` | 第 seq 次调用里第 i 块是命中 / 未命中 / 跨边界 | **O(1)**，用 `pref` 前缀和比边界，不展开整个上下文 |
| `call_outline(d, seq)` | 本次带上几块 / 多少字 / 命中比例 / 缓存边界落在第几块 | 边界用 `bisect` 定位 |
| `block_users(d, idx)` | 哪些调用带上了这个块 | `bisect_right(ctxNs, idx)`，O(log C) |
| `global_stats()` | 跨会话聚合构成、最贵调用、最大块、冷启动断点、计费单价 | 内存遍历一次 |
| `search()` | 940 万字原文里找关键词，返回命中片段与跳转链接 | `re.compile(escape(q), I)` 直接扫原文，不复制小写副本 |

**缓存边界仍是估算**：接口只给命中 token 总数，不给切分位置。按「可见字数 × 命中比例」
在块边界上切，块级基本准确，跨界那一块标「跨边界」。这一点和静态模式口径完全一致。

### 前端：为什么最终渲染得很少

浏览器从头到尾只做三件事：显示 HTML、点链接、原生 `<details>` 折叠。
没有虚拟滚动库、没有一次性注入的大 JSON、没有前端模板。

| 控制点 | 常量 | 值 |
|---|---|---|
| 会话页每页轮数 | `PAGE_TURNS` | 20 |
| 调用列表每页条数 | `PAGE_CALLS` | 40 |
| 单次调用的上下文块清单每页 | `PAGE_BLOCKS` | 40 |
| 块 / 输出全文每片字数 | `PART_CHARS` | 12 000 |
| 搜索结果上限 | `SEARCH_MAX` | 80 |
| API 单次返回上限 | `API_MAX_LIMIT` | 500 |

关键在**正文不进列表页**：调用详情页只列块的「序号 / 状态 / 类型 / 标题 / 字数 / 命令摘要」，
点进去才由 `/b/<idx>` 单独返回该块的一片正文。同一个 58 万字的大块无论被 800 次调用带上，
列表页都只出现一行引用，全文放在自己那一页分 49 片。

**实测（2026-11-05，取本机调用数最多的一个会话）**

| 页面 | 大小 | 标签数 |
|---|---:|---:|
| 索引 `/` | 23.2 KB | 372 |
| 全局统计 `/stats` | 32.2 KB | 517 |
| 搜索结果（80 条） | 52.9 KB | 515 |
| 会话页 | 24.7 KB | 351 |
| 调用详情（1328 页，区间 10.1–28.2 KB） | 27.7 KB（中位） | — |
| 块全文分片 | 17.2 KB | 113 |

对照静态模式：同一个会话只保留 150 次调用就要 **1.9 MB** 单页；不截断全量生成是
**220 MB**、最大单页 4.6 MB。完整模式把单页压到 **10–53 KB**，同时把 1328 次调用
和 2575 个块全部变成可访问的 URL。

---

## 后端 call：路由与 API

### HTML 路由（每次点击 = 一次后端请求）

| 路由 | 返回 |
|---|---|
| `/` | 会话索引，按工作区分组；`?sort=mtime\|credit\|calls`、`?ws=`、`?refresh=1` |
| `/stats` | 跨会话全量聚合、最贵 20 次调用、最大 20 个块、冷启动断点 |
| `/search?q=&scope=` | 全文搜索；`scope=blocks\|outputs\|turns\|all` |
| `/s/<sid>?view=turns\|calls&p=` | 会话概览；按轮或按调用分页 |
| `/s/<sid>/t/<n>` | 单轮的全部调用 |
| `/s/<sid>/c/<seq>?state=&p=` | 单次调用的**完整上下文块清单**；`state=all\|hit\|miss\|part` |
| `/s/<sid>/b/<idx>?part=&ref=` | 块全文分片；`raw=1` 返回纯文本 |
| `/s/<sid>/o/<seq>?seg=&part=` | 该次调用的输出（思考 / 回答）全文分片 |
| `/api` | API 契约说明页 |

`<sid>` 必须匹配 `[0-9A-Za-z_-]+` 且存在于会话索引，防目录穿越；所有输出统一 `html.escape`。

### JSON API（给脚本 / 外部工具）

统一信封：`{"ok":true,"data":...,"meta":{...}}`；出错是 `{"ok":false,"error":"..."}` 加 4xx/5xx。
分页统一 `offset` / `limit`，`meta` 给 `total`、`offset`、`limit`，有下一页时给 `next` 完整 URL。

| 端点 | 说明 |
|---|---|
| `GET /api/health` | 状态、`pid`、`rss_mb`、`uptime_s`、会话数、缓存会话数、`write_files=false` |
| `GET /api/sessions` | 全部会话摘要 |
| `GET /api/stats` | 跨会话全量聚合 |
| `GET /api/s/<sid>` | 会话统计 + 每一轮摘要 + 冷启动调用号 |
| `GET /api/s/<sid>/calls?offset=&limit=&turn=` | 调用列表（不含正文） |
| `GET /api/s/<sid>/calls/<seq>` | 单次调用：usage、缓存边界、块总数、输出段索引 |
| `GET /api/s/<sid>/calls/<seq>/context?offset=&limit=&state=` | 块清单（不含正文，可按状态筛选） |
| `GET /api/s/<sid>/blocks/<idx>?offset=&limit=&full=1` | 块全文，**Range 语义**（`limit` 按字符）；`full=1` 一次取完 |
| `GET /api/s/<sid>/blocks/<idx>/users` | 哪些调用带上了这个块 |
| `GET /api/s/<sid>/outputs/<seq>?seg=&offset=&limit=` | 输出全文，分段 + 分片 |
| `GET /api/s/<sid>/turns/<n>` | 某一轮的全部调用 |
| `GET /api/search?q=&sid=&scope=&limit=` | 全文搜索，返回命中位置、片段和跳转链接 |
| `POST /shutdown` | 停止服务，供 `start.sh stop` 调用；校验 Host/Origin，配置令牌时还需鉴权 |

```bash
curl -s "http://127.0.0.1:8765/api/sessions" | python3 -m json.tool
curl -s "http://127.0.0.1:8765/api/s/<sid>/calls?limit=5"
curl -s "http://127.0.0.1:8765/api/s/<sid>/blocks/42?offset=0&limit=1000"
curl -s "http://127.0.0.1:8765/api/search?q=缓存冷启动"
```

`/health`（纯文本 `ok`）、`HEAD`、404/500 都按 HTTP 语义处理；响应带 `Cache-Control: no-store`，
源文件改了刷新即可看到新数据，不会拿到陈旧页。

---

## 内容完整性：逐项对照

旧静态模式为压体积做了四处裁剪，完整模式全部取消：

| 旧限制 | 完整模式 |
|---|---|
| `MAX_CALLS=150`，超长会话只留最近 150 次 | 全部调用可访问，按 40 条/页翻 |
| `MAX_TEXT=3000`，块正文截到 3000 字 | 原文全存，按 12 000 字/片翻完 |
| `PREVIEW=600` / `INLINE_BLOCKS=3`，其余只给标题 | 块清单全列，正文点开即完整 |
| 无搜索、无筛选 | 全库全文搜索 + 按命中状态筛选 + 排序 |

**实测验证（2026-11-05）**

- 最大会话 **1328 / 1328** 个调用详情页全部 200；
- **2575 / 2575** 个块全文页全部 200；
- 最大的块 **579 969 字 / 49 片**，逐片取回拼接后与 API 全文**逐字一致**；
- 全部页面 `<script>` 标签 **0 个**；
- 12 个 JSON 端点全部返回 `{ok:true, data, meta}`；
- 全库：28 会话 / 5357 次调用 / 378 轮 / 10 465 块 / 948 万字 / 8.24 亿输入 tok /
  98.33% 命中 / 893.26 积分 / 49 次冷启动断点。

数字会随新会话增长，页面上的都是实时算的，不是写死的结论。

---

## 磁盘与内存行为（实测过）

**结论：服务运行期零写盘，也不会每打开一次页面就多占一份存储。**

`trace_server.py` 里没有任何 `open(...,'w')`、日志落盘、缓存文件或数据库写入。
读源数据只有只读 `open()` 和 `sqlite3.connect(..., mode=ro)`；页面直接通过 socket 返回，
不生成临时文件。启动时在 `import gen_trace` **之前**设了 `sys.dont_write_bytecode = True`，
所以连 Python 默认会写的 `__pycache__/*.pyc`（曾实测 141 KB）都不再产生。

> 对照：**静态模式 `gen_trace.py` 会写 `trace/`（约 45 MB）**，那是它的产物，与服务无关。
> 服务不写 `~/.workbuddy/` 下任何文件。源文件会随新会话增长，但那是 WorkBuddy 写的。

**内存有硬上限，不随点击累积。** 会话全文放在 `Store` 的 LRU 里，上限 `--cache 32`（默认），
超出淘汰最久未用的；重复打开同一会话命中缓存，不会重复占用。每请求由
`ThreadingHTTPServer` 起一个线程，返回即释放。`GET /api/health` 会自报
`pid` / `rss_mb` / `uptime_s` / `cached_sessions` / `write_files`，随时可以核对。

**但 LRU 淘汰不等于 RSS 回落。** 实测 `--cache 4` 遍历完 29 个会话后，逻辑上只留 4 个，
进程 RSS 仍是 84.0 MB —— Python 分配器释放对象后通常不把 arena 归还 OS。
所以 `--cache` 限制的是缓存条目数和重复解析，**不是进程峰值内存**；
想真正压低驻留，得限制峰值（少用会遍历全部会话的 `/stats`、`/api/stats`）或重启服务。
默认全缓存反而更划算：内存一样会涨到 ~88 MB，还省掉反复解析。

**RSS ≈ 88 MB 花在哪**（测量进程稳态 84.3 MB / VSZ 116.1 MB）

| 组成 | 大小 |
|---|---:|
| Python 解释器 + 标准库基线 | 19.9 MB |
| 模块代码、常量、29 个会话索引 | 7.4 MB |
| 会话全文与派生索引（29 个会话） | 57.1 MB |
| 其中：去重后的原文字符串 13 441 个 | 30.2 MB |

剩余约 27 MB 是 dict / list / 数值对象、`pref` 前缀和、`by_turn` 等容器开销。
运行中的服务比测量进程多约 4 MB，来自 HTTP 线程栈和请求期堆峰值。
占本机 15.1 GiB 内存的 **0.57%**。只访问 1 个最大会话时是 60.6 MB。

**压测：两轮共 600 个请求**（覆盖索引、统计、搜索、6 个会话的会话页 / 调用页 / 块页 / API）

| 时刻 | RSS | 缓存会话 | 目录字节 | 文件数 | `__pycache__` |
|---|---:|---:|---:|---:|---|
| 刚启动（`--warm` 预热 29 会话） | 88.0 MB | 29 | 46 332 588 | 32 | 不存在 |
| 第 1 轮 300 请求后 | 88.7 MB | 29 | 46 332 588 | 32 | 不存在 |
| 第 2 轮 300 请求后 | 88.7 MB | 29 | 46 332 588 | 32 | 不存在 |

RSS 只涨 0.7 MB（Python 堆的一次性分配），**第二轮完全不涨**；磁盘 **0 字节、0 文件变化**。
压测里出现的 5 个 404 是同一个 URL：某会话只有 1 次调用，请求 `/c/2` 越界，属正确边界行为。

## 文件

| 文件 | 作用 |
|---|---|
| `trace_server.py` | **完整模式**：数据层 + 计算层 + HTML 渲染 + JSON API + HTTP 服务；运行期零写盘 |
| `start.sh` | 一键启动 / 停止脚本；幂等、自动开浏览器、`-B` 零写盘 |
| `token-trace.desktop` | 启动器模板（`Exec=@EXEC@`，不直接双击） |
| `install-desktop.sh` | 按当前路径生成并安装桌面 / 应用菜单启动器 |
| `.gitignore` | 排除含会话原文的静态页与运行时产物 |
| `gen_trace.py` | 解析原语 + 静态模式生成器；`build(path, full=True)` 供服务复用 |
| `trace/` | 静态模式产物（`index.html` + `s-<id>.html`），与完整模式互不依赖 |

---

## Git 仓库与隐私

本目录本身是一个独立的 git 仓库（仓库根即 `token-trace/`）。**刻意不入库**的东西：

| 排除项 | 原因 |
|---|---|
| `trace/*.html`（约 45 MB） | 静态页内嵌会话原文：工作区绝对路径、用户提问、工具输出全文，隐私密度最高 |
| `.workbuddy/`、`.Trash-0/` | 上层目录的项目记忆与回收站，本就不在仓库范围内，`.gitignore` 里另留了双保险 |
| `__pycache__/`、`*.pyc`、`*.log`、`*.bak*` | 运行时产物；服务以 `-B` 启动，正常不会产生 |
| `token-trace.html`、`_compat.html` | 单文件模式的临时输出 |
| `patch_cli.sh` | 本机系统补丁：改 `/opt` 下的 WorkBuddy CLI 文件，与工具本身无关 |

`trace/` 随时可用 `python3 gen_trace.py` 从本机 jsonl 重建，不需要版本管理。
代码里的本机绝对路径都已去掉：数据目录用 `os.path.expanduser("~")` 动态取，
启动器是模板、由 `install-desktop.sh` 按当前目录生成 Exec；文档只保留聚合统计，
不含会话 id。仓库地址：<https://github.com/The-garden-of-sinner/token-trace>（分支 `main`）。

## 数据来源与口径

全部来自本地文件，只读，不联网：

| 路径 | 内容 |
|---|---|
| `~/.workbuddy/projects/**/*.jsonl` | 每次调用记录：消息、工具调用与结果、`providerData.rawUsage` |
| `~/.workbuddy/workbuddy.db` | `sessions` 表提供 `cwd`、标题、模型名 |

`rawUsage` 用到 `prompt_tokens`、`prompt_cache_hit_tokens`、`completion_tokens`、`credit`。
并行工具调用里只有第一个 `function_call` 带 usage，其余标「并列调用 · 无独立 usage」。

**积分单价（本机实测，2026-11）**

| 类型 | 单价 |
|---|---|
| 未命中输入 | 0.29 分 / 万 token |
| 缓存命中输入 | 0.014 分 / 万 token |

差 21 倍，所以「积分差异基本等于未命中量差异」，不是单价在变。

**可见上下文 ≠ 完整请求。** jsonl 里只有消息和工具结果，不含系统提示与工具 schema，
所以「本次带上 N 字」和「接口报 M token」不能换算，两者都列出来仅供参考。

---

## 这个工具查出来的几件事

1. **输入的大头是缓存，不是新内容。** 全库 8.24 亿输入 token，98.33% 命中缓存，
   真正计费的是剩余部分加输出。
2. **上下文构成里，你的输入只占很小一块。** 工具结果与系统注入占绝对多数，
   想省 token 先管工具输出，不是少说话。
3. **缓存失效是常态。** 全库找出 49 次「前一次 >80% 命中、这次 <50%」的断点，
   短间隔（1–10 分钟）最常见，触发机制无法从本地数据确定。
4. **重启很贵。** 重启后第一次调用要重算整段历史，成本 ≈ 上下文 token × 0.29 / 万。
   实测 33.7 万上下文 → 9.47 分，第二次调用就恢复到 0.43 分。

这些结论现在都能在 `/stats` 里实时重算，不用再手写脚本统计。

---

## 已知限制

- 完整模式需要本地起服务，不能像静态页那样 `file://` 双击打开。
- 静态模式仍保留 150 次调用 / 3000 字的截断，它是离线兜底，不是完整视图。
- 缓存命中边界是**估算**（接口不给切分位置），块级准确、跨界块内部有误差。
- 搜索是流式扫描而非倒排索引：冷缓存首次全库搜索约 2.4 s，加 `--warm` 后不到 0.5 s。
- 服务默认只监听 `127.0.0.1`。绑定非本机地址时必须提供 `--token`；
  `POST /shutdown` 会校验 Host / Origin / Sec-Fetch-Site，避免跨站关停。
  `POST /shutdown` 是唯一的写操作端点（只是让进程退出，不动磁盘）。
- `start.sh stop` 的 `pkill` 兜底在 PID namespace 隔离的环境（如 WorkBuddy 的 exec 沙箱）里看不到桌面进程，
  但优先走的 HTTP `/shutdown` 不受影响；极老的、没有该端点的实例只能靠 `pkill` 或重启桌面会话。
- 积分来自本地记录，云端补扣或赠送的部分不在里面。

## Windows 支持

两个核心 Python 文件只依赖标准库，跨平台；Linux 上的 `start.sh` / `.desktop` 启动器由
`windows/` 下的三个文件替代：

| 文件 | 作用 |
| --- | --- |
| `windows/start.cmd` | 双击入口，转调 PowerShell 并绕过执行策略 |
| `windows/start.ps1` | 幂等启动 / 停止 / 查看状态；对应 `./start.sh` 与 `./start.sh stop` |
| `windows/install-shortcut.ps1` | 创建桌面 / 开始菜单 `.lnk`，对应 `install-desktop.sh` |

数据目录与 Linux 同构：会话记录在 `%USERPROFILE%\.workbuddy\projects\`，索引库在
`%USERPROFILE%\.workbuddy\workbuddy.db`。若装在别处，设置 `WORKBUDDY_HOME` 覆盖。

实现要点：

- sqlite 只读连接用 `Path.as_uri()` 生成 `file:///C:/...?mode=ro`，避开 Windows 路径里的反斜杠与盘符；
- `/api/health` 的 `rss_mb` 在 Windows 上走 `ctypes` 调 `GetProcessMemoryInfo`（Linux 仍读 `/proc`）；
- 会话扫描用 `Path.rglob("*.jsonl")`，不依赖路径分隔符，也能覆盖 `projects\` 下的多层目录；
- 启动器用 `python.exe -WindowStyle Hidden`，不用 `pythonw.exe`（后者 `sys.stdout` 为 `None`，会让 `print()` 崩）；
- `PYTHONUTF8=1` 避免 Windows 控制台中文乱码；两个 `.ps1` 以 UTF-8 BOM 保存，供 PowerShell 5.1 正确读取。

`TRACE_PORT` / `TRACE_NO_BROWSER` 对应的 PowerShell 参数是 `-Port` / `-NoBrowser`；
`TRACE_TOKEN` 对应 `-Token`（别名 `-ShutdownToken`），会同时传给服务的 `--token`。

**尚未在真实 Windows 机器上验证**（本次改动在 Linux 沙箱内完成）。需要在 Windows 上确认四点：
`projects\` 的实际层级深度、中文或空格用户名下 WAL 模式 `workbuddy.db` 的只读打开、
`python.exe -WindowStyle Hidden` 的隐藏效果、以及含中文路径时 PowerShell 的传参。

## 依赖

Python 3.8+，仅标准库（`http.server` / `json` / `pathlib` / `sqlite3` / `re` / `bisect` / `urllib` / `ctypes`）。
不需要第三方包，不需要前端构建，不需要数据库服务。
