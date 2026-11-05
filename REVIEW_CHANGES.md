# token-trace 审阅改动与验证记录

本文件记录一次代码审阅后对仓库的分阶段修改。每阶段按「改动 → 验证 → 提交」推进，验证通过后立即 commit，文档随该次提交一起更新。

- **基线 commit**：`c6d2b4f`（`初始化 Token 轨迹工具仓库`）
- **提交账户**：`The-garden-of-sinner <50914620+The-garden-of-sinner@users.noreply.github.com>`，author 与 committer 一致，沿用初始 commit
- **验证环境**：Ubuntu 24.04.3 LTS / Python 3.11.1；用隔离的临时 `HOME` 构造 `~/.workbuddy` 数据，不触碰真实数据目录
- **验证脚本**：见各阶段「验证」小节，均为独立复现脚本，未加入仓库

## 阶段总览

| 阶段 | 主题 | 对应问题 | commit | 验证结果 |
| --- | --- | --- | --- | --- |
| 1 | 输入容错 | P0-1 | 待填 | 10/10 通过 |
| 2 | 安全边界 | P0-2 / P0-3 | — | — |
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
