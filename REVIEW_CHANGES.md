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
| 2 | 安全边界 | P0-2 / P0-3 | 待填 | 23/23 通过 |
| 3 | 正确性与并发 | P1-4 ~ P1-9 | — | — |
| 4 | 打磨项 | P2 全部 | — | — |
| 5 | Windows 跨平台 | 迁移评估 | — | — |

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
