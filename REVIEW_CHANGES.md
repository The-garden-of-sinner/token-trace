# token-trace 审阅改动与验证记录

本文件记录一次代码审阅后对仓库的分阶段修改。每阶段按「改动 → 验证 → 提交」推进，验证通过后立即 commit，文档随该次提交一起更新。

- **基线 commit**：`c6d2b4f`（`初始化 Token 轨迹工具仓库`）
- **提交账户**：`The-garden-of-sinner <50914620+The-garden-of-sinner@users.noreply.github.com>`，author 与 committer 一致，沿用初始 commit
- **验证环境**：Ubuntu 24.04.3 LTS / Python 3.11.1；用隔离的临时 `HOME` 构造 `~/.workbuddy` 数据，不触碰真实数据目录
- **验证脚本**：见各阶段「验证」小节，均为独立复现脚本，未加入仓库

## 阶段总览

| 阶段 | 主题 | 对应问题 | commit | 验证结果 |
| --- | --- | --- | --- | --- |
| 1 | 输入容错 | P0-1 | `f2632fc` | 10/10 通过 |
| 2 | 安全边界 | P0-2 / P0-3 | `284c98e` | 23/23 通过 |
| 3 | 正确性与并发 | P1-4 ~ P1-9 | `d3ade7b` | 32/32 通过 |
| 4 | 打磨项 | P2 全部 | `d165c5e` | 43/43 通过 |
| 5 | Windows 跨平台 | 迁移评估 | 待填 | 54/54 通过 |

---

## 阶段 1 · 修复 jsonl 半行导致整个会话 500（P0-1）

### 问题

`gen_trace.build()` 用单行列表推导解析整个 jsonl，任何一行 JSON 损坏都会抛 `JSONDecodeError`，使会话页和对应 API 全部 500；而 `scan_all()` 逐行 `try/except` 会跳过坏行。两处容错口径不一致，导致「索引里能看到这个会话，点进去必然 500」。WorkBuddy 以 append-only 方式写 jsonl，末尾半行是正常现象。

审阅时实测（改造前）：

```
scan_all()                  -> 正常返回 2 个会话
GET /?refresh=1             -> 200
GET /s/bad999-session       -> 500
GET /api/s/bad999-session   -> 500
服务端堆栈：json.decoder.JSONDecodeError: Invalid control character at line 1 column 82
```

### 改动

| 文件 | 改动 |
| --- | --- |
| `gen_trace.py` | 新增 `iter_rows(path, stats=None)` 生成器：用 `with open()` 逐行读，跳过空行与解析失败行，通过调用方传入的 `stats` 回传跳过数。`build()` 与 `scan_all()` 共用它，口径统一 |
| `gen_trace.py` | `build()` 改为 `rows = list(iter_rows(path, skipped))`；`meta` 改为副本，坏行数写入 `meta["_skipped_lines"]` |
| `gen_trace.py` | `scan_all()` 改为 `for d in iter_rows(f, skipped)`，保持流式、不增加常驻内存；索引条目新增 `skipped` 字段 |
| `gen_trace.py` | 静态模式 `render()` 在检测到坏行时输出黄色提示条 |
| `trace_server.py` | `Store._derive()` 的 `stats` 新增 `skipped_lines`；`v_session()` 显示警告条，说明统计不含被跳过的行 |

设计取舍：不在 `iter_rows` 内部把整个文件读进内存计数，而是让调用方传 `stats` 字典。这样 `scan_all()` 仍然流式处理（它每次刷新索引都要扫全部源文件），`build()` 本来就需要全量 `rows`，内存行为不变。

### 验证

回归脚本 `regress.py 1`：新建隔离 `HOME`，构造 3 个会话（正常 / 末尾半行 / 深层目录）、一份 `workbuddy.db`，起服务后逐一请求 27 条路由。

```
=== 函数级 ===
  PASS build() 遇到半行不再抛异常                calls=1
  PASS meta 记录跳过的坏行数                    skipped=1
  PASS 正常会话无坏行标记
  PASS scan_all 给索引带 skipped                broken=1 normal=0
=== 端到端 ===
  PASS 基线 27 条路由状态码                    全部符合（25 个 200 / 3 个 404）
  PASS 半行会话页返回 200                      code=200
  PASS 半行会话页显示跳过提示
  PASS 正常会话页无跳过提示
  PASS API stats 暴露 skipped_lines
  PASS 静态模式全量生成不崩                    2/2 个会话
=== 汇总 ===  10 项检查，10 通过，0 失败
```

另外用 `ast.parse()` 校验两个 Python 文件语法（不生成 `__pycache__`），并检查 `git status` 无意外产物。

### commit

```
fix: jsonl 半行不再让整个会话 500，build/scan_all 容错口径统一（阶段1/P0-1）

新增 iter_rows() 共用逐行解析器，坏行跳过并计数；
meta/stats/索引/页面/静态页逐层暴露 skipped_lines，用户能看到数据不完整。
验证：隔离 HOME 端到端 10/10 通过，27 条基线路由全绿。
```

---

## 阶段 2 · 补上 shutdown 来源校验与非本机绑定的鉴权边界（P0-2 / P0-3）

### 问题

- `do_POST /shutdown` 只看路径，不校验 `Host` / `Origin` / `Sec-Fetch-Site`，也不要求令牌。表单 POST 属于 CORS 简单请求，浏览器不发预检直接送达，因此**任意网页都能静默关停服务**（审阅时实测：`Origin: https://evil.example` 的 POST 返回 200，服务随即退出）。
- `--host` 允许任意值，但代码没有强制鉴权。绑到 `0.0.0.0` 时，`/api/s/<sid>/blocks/<idx>?full=1` 会把全部会话原文（提问、工具输出、工作区路径）开放到局域网。

### 改动

| 文件 | 改动 |
| --- | --- |
| `trace_server.py` | 新增 `--token`（也可由 `TRACE_TOKEN` 提供）。`main()` 在 `--host` 非 loopback 且无令牌时 `argparse.error` 直接拒绝启动 |
| `trace_server.py` | `Handler._authorized()`：令牌非空时校验 `Authorization: Bearer`、`?token=` 或 Cookie；用 `hmac.compare_digest` 比较。带 `?token=` 首次访问会 `Set-Cookie`，后续零 JS 的 `<a href>` 自动携带，不用给每个链接拼令牌 |
| `trace_server.py` | `Handler._shutdown_ok()`：Host 必须是 loopback（`127.0.0.1` / `localhost` / `::1`），Origin 若存在必须同源，`Sec-Fetch-Site` 若存在必须为 `same-origin` / `none`；再叠加令牌校验。不通过返回 403 |
| `trace_server.py` | 抽出 `_reply()` 统一响应头（`Content-Length` / `Cache-Control` / 可选 `Set-Cookie`），`do_GET` / `do_POST` 共用 |
| `trace_server.py` | 顺带修 P2-14：`_drain_body()` 消费 POST 请求体并 `close_connection`，避免 HTTP/1.1 长连接下残留 body 被当成下一个请求行（审阅时日志里出现过 `Bad request syntax ('x=1')`） |
| `start.sh` | 支持 `TRACE_TOKEN`：`curl_auth()` 包装给健康检查 / stop 带令牌，启动时传 `--token`，打开浏览器时 URL 带 `?token=` 以便种 Cookie |

默认行为完全不变：不设令牌、绑 `127.0.0.1` 时，桌面双击和 `./start.sh` 的用法与之前一致。

### 验证

`regress.py 2` 在阶段 1 的 10 项基线上追加 13 项安全断言：

```
=== 阶段2 安全边界 ===
  PASS 跨站 Origin 的 shutdown 被拒                  code=403
  PASS 伪造 Host 的 shutdown 被拒                    code=403
  PASS cross-site 的 shutdown 被拒                   code=403
  PASS 未知 POST 仍返回 404                          code=404
  PASS --host 0.0.0.0 无 token 拒绝启动              rc=2
  PASS 带 token 实例可启动
  PASS 无 token 的 GET 被拒
  PASS Bearer 头可通过
  PASS ?token= 可通过并种 Cookie                     code=200
  PASS Cookie 可通过后续零 JS 链接
  PASS 错误 token 被拒
  PASS 带 token 实例也拒绝跨站 shutdown
  PASS 本机关停端点仍可用                            code=200
=== 汇总 ===  23 项检查，23 通过，0 失败
```

`bash -n start.sh` 通过；两个 Python 文件 `ast.parse` 通过。

### commit

```
fix: shutdown 校验来源，非本机绑定强制访问令牌（阶段2/P0-2 P0-3）

挡住跨站 POST / DNS rebinding；--token 支持 Bearer/?token=/Cookie，
浏览器零 JS 链接可续期。顺带修 POST 请求体残留导致的 400。
验证：隔离 HOME 端到端 23/23 通过。
```

---

## 阶段 3 · 正确性与并发（P1-4 ~ P1-9）

### 改动

| # | 文件 | 改动 |
| --- | --- | --- |
| P1-4 | `trace_server.py` | `v_search` 的高亮改为在**原始** snippet 上用 `finditer` 切段、逐段 `esc()` 后拼 `<mark>`，不再对已转义文本跑正则。修掉 `q=amp` 把 `&amp;` 切成 `&<mark>amp</mark>;` 的问题 |
| P1-5 | `gen_trace.py` / `trace_server.py` | `scan_all()` 的 sqlite 失败不再 `except: pass`，改为向 stderr 打印原因、索引条目带 `meta_ok`、首页显示黄色警告条；用 `try/finally` 保证连接关闭。顺带检测同名会话 id 并告警 |
| P1-6 | `trace_server.py` | `Store` 增加 `_loading: sid -> Event`，`get()` 用 per-sid 占位避免并发首访重复解析（原来锁外的 `build()` 可能被两个线程各跑一遍） |
| P1-7 | `trace_server.py` | 新增 `CALC_LOCK`，`v_stats` / `v_search` / `/api/stats` / `/api/search` 四个调用点串行化，避免两个全库遍历交替把对方的会话挤出 LRU 导致重复解析 |
| P1-8 | `gen_trace.py` | `build()` 抽出 `model_of(prov)` 与 `val(*pairs)`，两个调用记录分支共用。修掉 `assistant` 分支漏读 `requestModelName` 导致模型显示 `?`；同时把 `or` 回退改为「取第一个非 None」，让合法的 `0` 不再被当成缺失 |
| P1-9 | `gen_trace.py` | 新增 `data_dir()`（支持 `WORKBUDDY_HOME` 覆盖）与 `iter_jsonl()`（`glob.iglob(..., recursive=True)`），替换只扫一层子目录的 `projects/*/*.jsonl`；删除死代码 `find_jsonl()` |

### 验证

`regress.py 3` 在阶段 1/2 的 23 项基线上追加 9 项：

```
=== 阶段3 正确性与并发 ===
  PASS 搜索高亮不再切开 HTML 实体     片段=…对比 A &amp;<mark>amp</mark>; B &lt;tag&gt; 的区别…
  PASS 搜索高亮仍然生效
  PASS 深层 jsonl 可被扫到            会话=normal-session,broken-session,deep-session
  PASS assistant 记录的 model 不再丢失  model='hunyuan-turbo'
  PASS 索引条目带 meta_ok             3 个会话
  PASS 0 值 usage 不再被回退          pin=0
  PASS db 不可读时 meta_ok=False 且有 stderr 提示
  PASS 并发首访只解析一次             build 调用 1 次（8 线程）
  PASS 并发 global_stats 结果一致     calls=[4, 4, 4, 4]
=== 汇总 ===  32 项检查，32 通过，0 失败
```

高亮那条的输出值得看一眼：修复后是 `A &amp;<mark>amp</mark>; B`，`&` 被单独转义成实体，高亮只包住原文本里的 `amp`，实体结构完整。

### commit

```
fix: 修正搜索高亮/模型名/深层扫描，消除静默退化与并发重复解析（阶段3/P1）

P1-4~P1-9 六项；新增 data_dir()/iter_jsonl() 与 CALC_LOCK。
验证：隔离 HOME 端到端 32/32 通过。
```

---

## 阶段 4 · P2 打磨项（11 条）

阶段 2 已顺带修掉 P2-14（POST 请求体残留），阶段 3 已删掉 P2-15（`find_jsonl()` 死代码）并修掉 P2-22 的 usage `or` 回退。本阶段收掉剩余 11 条：

| # | 文件 | 改动 |
| --- | --- | --- |
| 10 | `trace_server.py` | `full=1` 时 `limit = max(0, 总长 - offset)`，与 `offset` / `eof` 元数据自洽（原来 `limit` 恒等于全文长度） |
| 11 | `trace_server.py` | 新增 `MAX_CHARS = 10_000_000`，块/输出的字符 `offset` 不再被 `qint` 默认的 100 万上限截断 |
| 12 | `trace_server.py` | `call_outline()` 在 `n == 0` 时返回 `boundary = None`，不再显示「缓存边界 #1」却一个块都没有 |
| 13 | `trace_server.py` | `do_GET` 对 `HEAD /stats`、`/api/stats`、`/search`、`/api/search` 短路，只回状态与头、不执行全库遍历；其余路径仍走真实 GET 以保持状态码语义 |
| 16 | `trace_server.py` | 删除重复的 `CHIP` 定义，统一用 `G.CHIP` |
| 17 | `gen_trace.py` | `trim()` 对 `calls` 做浅拷贝后再改 `ctxN`，不再有隐式副作用 |
| 18 / 19 | `start.sh` | 新增 `TRACE_LOG`（默认 `/dev/null`，保持零写盘，排障时保留启动日志）；`alive()` 改为校验 body 等于 `ok`，端口被别的服务占用时不再误判；启动参数数组化 |
| 20 | `install-desktop.sh` | `.desktop` 的 `Exec` 值加双引号，并转义路径里的 `\`、`|`、`&`，支持含空格/中文的安装路径 |
| 21 | `trace_server.py` | `/api/health` 的 `write_files` 不再是孤立常量，附 `write_files_basis` 说明依据（`sys.dont_write_bytecode` 实际值）。注：完整自证需 OS 级 FD 监控，超出本次范围，因此如实标注「非 OS 级探测」 |
| 22 | `trace_server.py` | `search()` 增加 `scope` 白名单，非法值返回明确提示而不是静默空结果；`pager()` 的 `href` 补 `esc()` |

### 验证

`regress.py 4` 在阶段 1~3 的 32 项基线上追加 11 项：

```
=== 阶段4 打磨项 ===
  PASS full=1 与 offset 元数据一致      offset=3 limit=19 total=22 len=19
  PASS health 给 write_files 附可核对依据
  PASS 非法 scope 有明确提示
  PASS HEAD /stats 短路不渲染          HEAD=(200, '0')
  PASS GET /stats 仍正常渲染
  PASS 分页 href 已转义
  PASS CHIP 单点定义
  PASS 空上下文 boundary 为 None
  PASS trim 不再修改原 calls
  PASS 含空格路径的 Exec 带引号        "/tmp/.../路径 带空格/token-trace/start.sh"
  PASS alive 拒绝 body 非 ok 的 200 响应  alive_rc=1
=== 汇总 ===  43 项检查，43 通过，0 失败
```

最后两项是真实子进程验证：`install-desktop.sh` 在含空格与中文的临时目录里执行，检查生成的 `.desktop` 里 `Exec` 带引号；`alive()` 面对一个返回 200 但内容为 `not-ok` 的假服务时返回非零。两个 Python 文件 `ast.parse`、两个 shell 脚本 `bash -n` 均通过。

### commit

```
refactor: 收尾 P2 打磨项（阶段4）

full=1 元数据、字符 offset 上限、空上下文边界、HEAD 短路、CHIP 单点、
trim 无副作用、启动日志与健康检查、desktop Exec 引号、health 依据、scope 白名单。
验证：隔离 HOME 端到端 43/43 通过，bash -n / ast.parse 通过。
```

---

## 阶段 5 · Windows 跨平台

### 改动

| 文件 | 改动 |
| --- | --- |
| `gen_trace.py` | `data_dir()` 改为返回 `Path`，支持 `WORKBUDDY_HOME` 覆盖；除 `~/.workbuddy` 外，Windows 下还会探测 `%APPDATA%\WorkBuddy` / `%LOCALAPPDATA%\WorkBuddy`。删掉 import 时即固定的 `HOME` 常量 |
| `gen_trace.py` | 新增 `db_uri(db)`：用 `Path.resolve().as_uri() + "?mode=ro"` 生成 sqlite 只读 URI。Windows 路径含反斜杠与盘符，原来的 `"file:%s?mode=ro" % db` 会报 `unable to open database file` |
| `gen_trace.py` | `iter_jsonl()` 改用 `Path.rglob("*.jsonl")`，跨平台且不受分隔符影响，顺带保证扫描顺序稳定 |
| `trace_server.py` | 新增 `rss_mb()`：Linux 读 `/proc/self/status`，Windows 走 `ctypes` 调 `GetProcessMemoryInfo`，其它平台返回 `None`。`/api/health` 不再硬依赖 `/proc` |
| `trace_server.py` | `main()` 开头兜住 `pythonw.exe` 下 `sys.stdout is None` 导致的 `print()` 崩溃，并在 Windows 上把 stdout/stderr 重配为 UTF-8 |
| `windows/` | 新增 `start.cmd`、`start.ps1`、`install-shortcut.ps1`，替代 Linux 的 `start.sh` / `.desktop` 启动器；与阶段 2 的 `--token` 打通（`-Token` / `TRACE_TOKEN` 会传给服务，浏览器首访用 `?token=` 种 Cookie） |
| `README.md` | 新增「Windows 支持」章节与快速开始里的 Windows 入口；同步鉴权相关表述（原来写「无鉴权」已不准确） |
| `.gitignore` | 排除 `Thumbs.db` / `Desktop.ini` / `*.lnk` |

### 验证

`regress.py 5` 是全量回归，前四阶段 43 项全部重跑通过，另加 11 项跨平台断言：

```
=== 阶段5 Windows 跨平台 ===
  PASS db_uri 生成合法 file URI         file:///tmp/.../workbuddy.db?mode=ro
  PASS db_uri 能打开索引库             sessions=2
  PASS Windows 路径 URI 正确编码        file:///C:/Users/John%20Doe/.workbuddy/workbuddy.db?mode=ro
  PASS WORKBUDDY_HOME 覆盖生效
  PASS rss_mb 在 Linux 仍返回数值       rss_mb=28.9
  PASS rss_mb() 直接调用返回数值
  PASS iter_jsonl 仍覆盖深层目录
  PASS 三个 Windows 启动器齐备
  PASS start.ps1 / install-shortcut.ps1 带 UTF-8 BOM
  PASS PowerShell 脚本静态结构配平
=== 汇总 ===  54 项检查，54 通过，0 失败
```

Windows 路径的 URI 用 `PureWindowsPath.as_uri()` 在 Linux 上模拟验证（不依赖真 Windows）：空格被编码成 `%20`、盘符转成 `file:///C:/`。另外跑了 `python3 -B gen_trace.py` 静态模式、`bash -n` 两个 shell 脚本、`git diff --check`。

**验证边界（如实说明）**：本次改动在 Ubuntu 沙箱内完成，沙箱**没有 Windows，也没有 PowerShell**（尝试下载 Linux 版 `pwsh` 因网络超时失败）。所以三个 `.ps1` / `.cmd` 只做了静态检查（UTF-8 BOM、括号/引号配平），**没有做 PowerShell 语法解析，更没有在 Windows 上运行过**。`ctypes` 分支、`-WindowStyle Hidden` 效果、WAL 库在中文/空格用户名下的只读打开，都需要目标机实测。README 的「Windows 支持」章节列了这四项。

### commit

```
feat: 补齐 Windows 跨平台支持与启动器（阶段5）

db_uri()/data_dir()/rss_mb() 三处平台改造；新增 windows/ 启动器三件套；
README 补 Windows 章节。验证：隔离 HOME 端到端 54/54 通过，
PowerShell 脚本仅静态检查（沙箱无 Windows / pwsh）。
```

---

## 最终汇总

| 阶段 | commit | 主题 | 累计验证项 |
| --- | --- | --- | --- |
| 1 | `f2632fc` | P0-1 输入容错 | 10 |
| 2 | `284c98e` | P0-2 / P0-3 安全边界 | 23 |
| 3 | `d3ade7b` | P1 正确性与并发 | 32 |
| 4 | `d165c5e` | P2 打磨项 | 43 |
| 5 | 待填 | Windows 跨平台 | 54 |

- 五次提交的 author / committer 均为 `The-garden-of-sinner <50914620+The-garden-of-sinner@users.noreply.github.com>`，与初始 commit `c6d2b4f` 一致。
- 每阶段都是「改动 → 全量回归 → 提交」，回归脚本用隔离的临时 `HOME` 构造数据，未触碰真实 `~/.workbuddy`；`trace/` 静态产物由 `.gitignore` 排除。
- **未推送远程**：本次只在本地仓库提交。推送属于远程写操作，需另行确认。
- **仍需在真实 Windows 上确认**：`projects\` 层级深度、中文/空格用户名下 WAL 模式 `workbuddy.db` 的只读打开、`python.exe -WindowStyle Hidden` 的隐藏效果、含中文路径的 PowerShell 传参，以及三个 `.ps1` / `.cmd` 的语法与运行。
