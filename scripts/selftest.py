#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""token-trace 一键回归：静态一致性 + 起临时服务跑全部路由与 API。

    python3 scripts/selftest.py
    TRACE_TEST_PORT=8901 python3 scripts/selftest.py

不写任何文件（服务以 -B 启动，脚本自身也关掉字节码）；
退出码 0 = 全通过，1 = 有失败项。
"""

import html
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

sys.dont_write_bytecode = True
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import trace_server as T  # noqa: E402

PORT = int(os.environ.get("TRACE_TEST_PORT", "8899"))
BASE = "http://127.0.0.1:%d" % PORT
MAX_HTML = 80 * 1024
RESULTS = []


def check(ok, msg):
    RESULTS.append(bool(ok))
    print("  %s %s" % ("OK  " if ok else "FAIL", msg))


def get(path, timeout=40, method="GET"):
    try:
        req = urllib.request.Request(
            BASE + path, data=b"" if method == "POST" else None, method=method)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except Exception as e:
        return 0, str(e).encode("utf-8", "replace")


def main():
    print("== 静态一致性 ==")
    html_items = [r for r in T.ROUTE_SPEC if r["kind"] == "html"]
    api_items = [r for r in T.ROUTE_SPEC if r["kind"] == "api"]
    pats = [r["pattern"] for r in html_items]
    check(len(pats) == len(set(pats)), "ROUTE_SPEC 无重复 pattern")
    check([p for p, _ in T.HTML_ROUTES] == pats,
          "HTML_ROUTES 与声明逐条一致（%d 条）" % len(pats))
    check(all(callable(r["view"]) for r in html_items), "所有 view 可调用")
    check(len({r["sample"] for r in T.ROUTE_SPEC}) == len(T.ROUTE_SPEC),
          "sample 无重复（%d 项）" % len(T.ROUTE_SPEC))

    print("== 启动临时服务 ==")
    proc = subprocess.Popen(
        [sys.executable, "-B", "trace_server.py", "--port", str(PORT)],
        cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        ready = False
        for _ in range(80):
            st, body = get("/health", timeout=2)
            if st == 200 and body.strip() == b"ok":
                ready = True
                break
            time.sleep(0.25)
        check(ready, "端口 %d 服务就绪" % PORT)
        if not ready:
            return

        st, body = get("/api/sessions")
        sessions = json.loads(body)["data"]
        sid = max(sessions, key=lambda s: s["calls"])["id"]
        print("  会话 %d 个，取最大会话 %s… 作样例" % (len(sessions), sid[:8]))

        print("== HTML 路由（状态码 / 体积 / 零脚本）==")
        for r in html_items:
            path = r["sample"].replace("{sid}", sid)
            st, body = get(path)
            sc = body.count(b"<script")
            check(st == 200 and sc == 0 and len(body) <= MAX_HTML,
                  "%-38s %d %6.1f KB script=%d" %
                  (path[:38], st, len(body) / 1024, sc))

        print("== JSON API（统一信封）==")
        for r in api_items:
            path = r["sample"].replace("{sid}", sid)
            st, body = get(path)
            try:
                good = st == 200 and json.loads(body).get("ok") is True
            except Exception:
                good = False
            check(good, "%-48s %d" % (path[:48], st))

        print("== /api 文档与声明同步 ==")
        st, doc = get("/api")
        page = doc.decode("utf-8", "replace")
        missing = [r["route"] for r in T.ROUTE_SPEC
                   if html.escape(r["route"], quote=True) not in page]
        check(not missing, "32 条路由/端点全部出现在 /api 文档页（缺失 %d）" % len(missing))
        if missing:
            print("     缺失:", missing[:4])

        print("== 分页越界一致性 ==")
        paged = ["/s/{sid}?view=calls&p=999", "/s/{sid}/c/1?p=999",
                 "/find?q=token&p=999", "/tools?p=999",
                 "/s/{sid}/trend?p=999"]
        for path in paged:
            st, body = get(path.replace("{sid}", sid))
            rows = body.count(b'class="row') + body.count(b'class="trow')
            check(st == 200 and rows > 0,
                  "%-40s %d 数据行=%d（越界不空表）" %
                  (path[:40], st, rows))

        print("== 运行期零写盘 ==")
        check(not os.path.isdir(os.path.join(ROOT, "__pycache__")),
              "项目目录无 __pycache__")
        st, body = get("/api/health")
        check(json.loads(body)["data"].get("write_files") is False,
              "/api/health 自报 write_files=false")
    finally:
        try:
            get("/shutdown", timeout=3, method="POST")
        except Exception:
            pass
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except Exception:
            proc.kill()


if __name__ == "__main__":
    main()
    bad = RESULTS.count(False)
    print("")
    print("共 %d 项检查，失败 %d 项" % (len(RESULTS), bad))
    sys.exit(1 if bad else 0)
