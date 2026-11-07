#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成 README 演示截图用的**虚构**会话数据，不含任何真实用户信息。

    python3 -B scripts/make_demo.py [输出目录]     # 默认 ./.demo-data
    WORKBUDDY_HOME=./.demo-data python3 -B trace_server.py --port 8917

只写 jsonl 与一份最小的 workbuddy.db；路径、命令、日志、模型名、token 数全部是编的。
"""
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

OUT = Path(sys.argv[1] if len(sys.argv) > 1 else ".demo-data").resolve()
BASE = datetime(2026, 3, 2, 9, 0, tzinfo=timezone.utc)
T0 = int(BASE.timestamp() * 1000)

SESSIONS = [
    {
        "id": "demo0001-0000-4000-8000-000000000001",
        "ws": "playground",
        "title": "演示：定位测试超时",
        "turns": [
            ("帮我看下这个项目的测试为什么会超时", [
                ("Bash", '{"command":"python3 -m pytest -q tests/"}',
                 "3 passed, 1 failed in 12.04s\nFAILED tests/test_fetch.py::test_timeout"),
            ]),
            ("把超时配置找出来", [
                ("Read", '{"file_path":"config.yaml"}',
                 "fetch:\n  timeout: 3\n  retries: 2\n  base_url: https://example.invalid\n"),
                ("Bash", '{"command":"grep -rn \\"timeout\\" src/"}',
                 "src/fetch.py:18:    timeout = cfg.get('timeout', 3)\n"),
            ]),
            ("改成 10 秒再跑一遍", [
                ("Bash", '{"command":"python3 -m pytest -q tests/"}',
                 "4 passed in 1.87s\n"),
            ]),
        ],
    },
    {
        "id": "demo0002-0000-4000-8000-000000000002",
        "ws": "playground",
        "title": "演示：缓存命中率下降",
        "turns": [
            ("为什么这次调用突然变贵了", [
                ("Bash", '{"command":"wc -l src/*.py"}', " 142 src/fetch.py\n  88 src/store.py\n"),
            ]),
            ("看看最长的那个文件", [
                ("Read", '{"file_path":"src/fetch.py"}',
                 "def fetch(url, timeout=10):\n    return client.get(url, timeout=timeout)\n"),
            ]),
        ],
    },
    {
        "id": "demo0003-0000-4000-8000-000000000003",
        "ws": "notes",
        "title": "演示：整理发布说明",
        "turns": [
            ("把这版的改动列成三条", [
                ("Bash", '{"command":"git log --oneline -3"}',
                 "a1b2c3 docs: 更新说明\nd4e5f6 fix: 超时配置\ng7h8i9 feat: 重试逻辑\n"),
            ]),
        ],
    },
]


def raw_usage(call_no, pin, hit, out, credit):
    return {
        "prompt_tokens": pin,
        "prompt_cache_hit_tokens": hit,
        "prompt_cache_miss_tokens": pin - hit,
        "completion_tokens": out,
        "total_tokens": pin + out,
        "credit": credit,
    }


def write_session(s, idx):
    d = OUT / "projects" / ("demo-" + s["ws"])
    d.mkdir(parents=True, exist_ok=True)
    path = d / (s["id"] + ".jsonl")
    rows = [{
        "type": "session-meta",
        "sessionId": s["id"],
        "timestamp": T0 + idx * 3600_000,
        "cwd": "/home/demo/" + s["ws"],
        "aiTitle": s["title"],
    }]
    n, ts = 0, T0 + idx * 3600_000 + 1000
    for ti, (question, calls) in enumerate(s["turns"], 1):
        rows.append({
            "type": "message", "role": "user", "timestamp": ts,
            "content": [{"type": "text", "text": question}],
            "providerData": {"agent": "cli", "conversationRequestId": "demo-req-%d-%d" % (idx, ti)},
        })
        ts += 1200
        for ci, (tool, args, result) in enumerate(calls, 1):
            n += 1
            pin = 8000 + n * 2400
            hit = int(pin * (0.55 if ci == 1 else 0.93))
            out = 180 + n * 17
            prov = {
                "model": "demo-model-1",
                "requestModelId": "demo-model-1",
                "requestModelName": "DemoModel-1",
                "agent": "cli",
                "rawUsage": raw_usage(n, pin, hit, out, round(0.08 + n * 0.03, 2)),
            }
            call_id = "call_demo_%d_%d_%d" % (idx, ti, ci)
            rows.append({
                "type": "function_call", "name": tool, "callId": call_id,
                "arguments": args, "timestamp": ts,
                "providerData": prov,
            })
            ts += 900
            rows.append({
                "type": "reasoning", "timestamp": ts,
                "content": [{"type": "text", "text": "演示用思考片段：先确认参数，再检查输出。"}],
                "providerData": {"agent": "cli"},
            })
            ts += 400
            rows.append({
                "type": "function_call_result", "name": tool, "callId": call_id,
                "timestamp": ts, "output": {"text": result},
            })
            ts += 700
            rows.append({
                "type": "message", "role": "assistant", "timestamp": ts,
                "content": [{"type": "text", "text":
                             "演示回答：已确认 %s 的输出，下一步按结果调整。" % tool}],
                "providerData": {
                    "model": "demo-model-1", "requestModelName": "DemoModel-1",
                    "rawUsage": raw_usage(n, pin + 900, hit + 900, 96, round(0.04 + n * 0.01, 2)),
                },
            })
            ts += 1500
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    return len(rows)


def main():
    (OUT / "projects").mkdir(parents=True, exist_ok=True)
    db = OUT / "workbuddy.db"
    con = sqlite3.connect(db)
    con.execute("drop table if exists sessions")
    con.execute("create table sessions (id text primary key, cwd text, title text, "
                "custom_title text, model text)")
    for s in SESSIONS:
        con.execute("insert into sessions values (?,?,?,?,?)",
                    (s["id"], "/home/demo/" + s["ws"], s["title"], "", "DemoModel-1"))
    con.commit()
    con.close()
    total = sum(write_session(s, i) for i, s in enumerate(SESSIONS, 1))
    print("demo 数据: %s" % OUT)
    print("%d 个会话 / %d 条记录 / workbuddy.db" % (len(SESSIONS), total))
    print("启动: WORKBUDDY_HOME=%s python3 -B trace_server.py --port 8917" % OUT)


if __name__ == "__main__":
    main()
