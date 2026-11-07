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
import re
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

    print("== 时间戳跨平台（Windows CRT 越界保护）==")
    check(T.fmt_ts(1780000000000).startswith("2026-"),
          "毫秒时间戳归一为 2026 年（修复前 Windows 抛 Errno 22）")
    check(T.fmt_ts(1780000000).startswith("2026-"),
          "秒级时间戳按同一口径渲染")
    check(all(T.fmt_ts(v) == "时间未知" for v in (0, -1, 1e15, 1.78e17, None, "x")),
          "零值 / 负值 / 越界 / 非数字退回「时间未知」")
    ts_sid = max(T.STORE.sessions(), key=lambda x: x["calls"])["id"]
    ts_views = [T.v_call({}, ts_sid, 1), T.v_compact({}, ts_sid), T.v_stats({})]
    check(all("58375" not in h for h in ts_views),
          "调用详情 / 压缩页 / 全局统计均无荒谬年份")
    check(bool(re.search(r"20\d{2}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", ts_views[0])),
          "调用详情渲染真实本地时间戳")

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
        # 零脚本按严格口径：除了 <script>，还要没有内联事件、javascript: URL、
        # 外链资源与 <link>（避免靠 CDN 偷偷引入脚本或样式）。
        RE_EVENT = re.compile(rb"<[^>]+\son[a-z]+\s*=", re.I)
        RE_EXT = re.compile(rb"""(?:src|href)\s*=\s*["'](?:https?:)?//""", re.I)
        RE_LINK = re.compile(rb"<link\b", re.I)
        for r in html_items:
            path = r["sample"].replace("{sid}", sid)
            st, body = get(path)
            sc = body.count(b"<script")
            ev = len(RE_EVENT.findall(body))
            ext = len(RE_EXT.findall(body)) + len(RE_LINK.findall(body))
            jsu = body.lower().count(b"javascript:")
            m = re.search(rb"HTML ([\d.]+) KB", body)
            kb_ok = bool(m) and abs(float(m.group(1)) - len(body) / 1024.0) <= 0.15
            check(st == 200 and sc == 0 and ev == 0 and ext == 0 and jsu == 0
                  and len(body) <= MAX_HTML and kb_ok,
                  "%-34s %d %6.1f KB script=%d 事件=%d 外链=%d 页脚口径=%s" %
                  (path[:34], st, len(body) / 1024, sc, ev, ext,
                   "一致" if kb_ok else "偏差"))

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
        check(not missing, "%d 条路由/端点全部出现在 /api 文档页（缺失 %d）" %
              (len(T.ROUTE_SPEC), len(missing)))
        if missing:
            print("     缺失:", missing[:4])

        print("== 压缩感知统计（方向性 / 不变式）==")
        compact_sessions = [s for s in sessions if T.STORE.get(s["id"])["compacts"]]
        plain = [s for s in sessions if not T.STORE.get(s["id"])["compacts"]]
        check(bool(compact_sessions), "存在发生压缩的会话（%d 个）" % len(compact_sessions))
        if compact_sessions:
            cd = T.STORE.get(compact_sessions[0]["id"])
            rows = T.compact_rows(cd)
            check(bool(rows) and all(r.get("consistent") for r in rows),
                  "压缩点可见字数与真实 pin 同方向（%d/%d 一致）" %
                  (sum(1 for r in rows if r.get("consistent")), len(rows)))
            check(all(r["after"]["chars_new"] < r["after"]["chars_old"] for r in rows
                      if r.get("after")),
                  "压缩后可见字数低于修正前旧口径")
            # 旧口径在压缩点必须反向（这是修复前的 bug）
            check(all(r.get("chars_old_delta_pct", 0) > 0 for r in rows),
                  "修正前旧口径确实在压缩点上涨（复现原缺陷）")
            # 块不删、索引不变、全文仍可达
            c0 = cd["compacts"][0]["at"]
            check(c0 < len(cd["ctx"]) and all(b["n"] >= 0 for b in cd["ctx"]),
                  "旧块未删除，块索引连续（%d 块）" % len(cd["ctx"]))
            st, body = get("/s/%s/b/%d" % (compact_sessions[0]["id"], c0 - 1))
            check(st == 200, "压缩前的旧块 #%d 全文仍可打开 %d" % (c0 - 1, st))
            # 可见区间外块不计入任何状态
            seq_after = rows[0]["after_seq"]
            o = T.call_outline(cd, seq_after - 1)
            check(o["lo"] > 0 and o["n"] < o["hi"],
                  "压缩后调用 #%d 区间 [%d,%d) 生效" % (seq_after, o["lo"], o["hi"]))
            check(T.call_state(cd, seq_after - 1, 0) is None,
                  "退役块在该次调用返回 None（不计 hit/miss）")
            # 真实 usage 未被改动
            st, body = get("/api/s/%s/compact" % compact_sessions[0]["id"])
            j = json.loads(body)
            check(st == 200 and j["ok"] and j["data"]["consistent"] == len(rows),
                  "/api/s/<sid>/compact 信封与一致数正确")
            # 同进程独立累加 rawUsage 原值：既真正验证「账单没被压缩修正改动」，
            # 也避免服务子进程与主进程在会话文件仍被写入时解析出不同快照而偶发误报。
            raw_pin = 0
            with open(cd["s"]["path"], encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    try:
                        rec = json.loads(line)
                    except Exception:
                        continue
                    tp = rec.get("type")
                    if tp != "function_call" and not (
                            tp == "message" and rec.get("role") == "assistant"):
                        continue
                    prov = rec.get("providerData") or {}
                    ru, u = prov.get("rawUsage") or {}, prov.get("usage") or {}
                    raw_pin += ru.get("prompt_tokens") or u.get("inputTokens") or 0
            check(raw_pin == cd["stats"]["pin"],
                  "真实累计输入 tok 等于 rawUsage 原值累加（未被统计修正影响）")
        if plain:
            pd = T.STORE.get(plain[0]["id"])
            check(all(c.get("vf", 0) == 0 for c in pd["calls"]),
                  "未压缩会话 vf 全为 0（口径完全不变）")

        print("== 分页越界一致性 ==")
        paged = ["/s/{sid}?view=calls&p=999", "/s/{sid}/c/1?p=999",
                 "/s/{sid}/t/1?p=999",
                 "/find?q=token&p=999", "/tools?p=999",
                 "/s/{sid}/trend?p=999", "/?p=999"]
        for path in paged:
            st, body = get(path.replace("{sid}", sid))
            rows = body.count(b'class="row') + body.count(b'class="trow')
            check(st == 200 and rows > 0,
                  "%-40s %d 数据行=%d（越界不空表）" %
                  (path[:40], st, rows))

        print("== 首页：放大索引 / 空库 / 缓存自适应 ==")
        orig_store = T.STORE

        class _FakeStore(object):
            """只实现 v_index 需要的最小接口，用来放大索引而不碰真实数据。"""
            def __init__(self, ss):
                self._ss = ss
                self.cap = 32

            def sessions(self, refresh=False):
                return self._ss

            def get(self, sid):
                raise KeyError(sid)

        def _sess(i):
            return {"id": "sid-%04d-abcd" % i, "path": "p",
                    "ws": "D:/ws/g%d/p%d" % (i % 3, i % 5), "title": "会话 %d" % i,
                    "model": "m%d" % (i % 3), "models": {"m%d" % (i % 3): 1000 + i},
                    "mtime": time.time() - i * 3600, "calls": 10, "turns": 5,
                    "pin": 1000 + i, "pout": 500, "credit": 1.5, "meta_ok": True,
                    "is_sub": False, "agent": "", "agent_calls": 0,
                    "first_ts": 0, "root": ""}

        try:
            for n in (33, 132, 400):
                T.STORE = _FakeStore([_sess(i) for i in range(n)])
                h = T.v_index({}).encode("utf-8")
                rows = h.count(b'class="row')
                has_pager = b'class="pager"' in h
                check(len(h) <= MAX_HTML and has_pager == (n > T.PAGE_SESSIONS)
                      and rows <= T.PAGE_SESSIONS,
                      "放大索引 %3d 会话：首页 %5.1f KB / 本页 %d 行 / 分页=%s"
                      % (n, len(h) / 1024.0, rows, "有" if has_pager else "单页隐藏"))

            T.STORE = _FakeStore([])
            h = T.v_index({})
            check(".hero{" in h and ".toolbar{display" in h and ".kpi{" in h,
                  "空库首页仍注入 CSS_INDEX（hero / toolbar / kpi 不裸样式）")

            # 缓存自适应：cap 覆盖全部会话，全库统计第二次访问不应再解析
            st = T.Store(0)
            st._index = [{"id": "s%d" % i, "path": "p", "ws": "w", "mtime": 0}
                         for i in range(33)]
            st._index_at = time.time() + 9999
            st.sessions()
            check(st.cap >= 33, "自适应缓存覆盖全部会话（cap=%d / 33）" % st.cap)
            check(T.CACHE_SESSIONS == 0,
                  "默认 --cache 为自适应（0），不是写死的固定值")
        finally:
            T.STORE = orig_store

        print("== 命中块折叠不丢块行 ==")
        cd0 = T.STORE.get(sid)
        checked_fold = False
        for n in range(1, min(20, len(cd0["calls"])) + 1):
            st, body = get("/s/%s/c/%d" % (sid, n))
            txt = body.decode("utf-8", "replace")
            if "块缓存命中" not in txt:
                continue
            o = T.call_outline(cd0, n - 1)
            rows = len(re.findall(r'href="/s/%s/b/\d+' % re.escape(sid), txt))
            check(rows == o["n"],
                  "调用 #%d 折叠后块行完整（%d / %d 块，块号区间与明细一致）"
                  % (n, rows, o["n"]))
            checked_fold = True
            break
        if not checked_fold:
            print("  SKIP 样例会话没有连续命中块，跳过折叠完整性检查")

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
