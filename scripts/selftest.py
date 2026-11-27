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
MAX_HTML = 96 * 1024
RESULTS = []


def check(ok, msg):
    RESULTS.append(bool(ok))
    print("  %s %s" % ("OK  " if ok else "FAIL", msg))


def get(path, timeout=120, method="GET"):
    try:
        req = urllib.request.Request(
            BASE + path, data=b"" if method == "POST" else None, method=method)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except Exception as e:
        return 0, str(e).encode("utf-8", "replace")


def get_noredirect(path, timeout=120, headers=None):
    """不跟随 303，用来核验 /check 的 Location 与状态码。"""
    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, hdrs, newurl):
            return None

    req = urllib.request.Request(BASE + path, headers=headers or {})
    try:
        with urllib.request.build_opener(_NoRedirect).open(req, timeout=timeout) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()
    except Exception as e:
        return 0, {}, str(e).encode("utf-8", "replace")


def main():
    demo_dir = None
    # 是否需要合成数据：只看 WorkBuddy 主源有没有 jsonl。不要在这里调
    # STORE.sessions()——它会连带触发外部源 import，把开发机上真实的
    # codex / antigravity 日志读进来，导致演示库被真实会话污染。
    import glob as _glob
    _wb = T.G.data_dir()
    has_wb = bool(_glob.glob(os.path.join(str(_wb), "projects", "*", "*.jsonl")))
    if not has_wb:
        import tempfile
        import atexit
        demo_dir = tempfile.mkdtemp(prefix="tt-selftest-demo-")
        # 双保险：正常路径在末尾 finally 清理；若在此之前抛异常，交给 atexit。
        atexit.register(lambda d=demo_dir: __import__("shutil").rmtree(d, ignore_errors=True))
        subprocess.run([sys.executable, "-B", os.path.join(ROOT, "scripts", "make_demo.py"), demo_dir],
                       check=True, timeout=60)
        os.environ["WORKBUDDY_HOME"] = demo_dir
        # 外部源模块在首次 scan 时才把 HOME 固化成模块常量，必须在第一次
        # STORE.sessions() 之前把它们指向演示目录下的空子目录。变量名以源码为准：
        # antigravity 读的是 AG_HOME（不是 ANTIGRAVITY_HOME）。
        for _var in ("CODEX_HOME", "ZCODE_HOME", "DSH_HOME", "AG_HOME"):
            _empty = os.path.join(demo_dir, "_empty_" + _var.lower())
            os.makedirs(_empty, exist_ok=True)
            os.environ[_var] = _empty
        T.STORE.sessions(refresh=True)

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

    print("== 发行脚本：版本号校验 ==")
    bad = subprocess.run([sys.executable, "-B", "scripts/build_deb.py", "../evil"],
                         cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         timeout=60)
    check(bad.returncode != 0,
          "build_deb.py 拒绝带路径的版本号，不会写出 dist/（exit=%d）" % bad.returncode)

    print("== 时间戳跨平台（Windows CRT 越界保护）==")
    check(T.fmt_ts(1780000000000).startswith("2026-"),
          "毫秒时间戳归一为 2026 年（修复前 Windows 抛 Errno 22）")
    check(T.fmt_ts(1780000000).startswith("2026-"),
          "秒级时间戳按同一口径渲染")
    check(all(T.fmt_ts(v) == "时间未知" for v in (0, -1, 1e15, 1.78e17, None, "x")),
          "零值 / 负值 / 越界 / 非数字退回「时间未知」")
    sessions_all = T.STORE.sessions()
    if sessions_all:
        ts_sid = max(sessions_all, key=lambda x: x["calls"])["id"]
        ts_views = [T.v_call({}, ts_sid, 1), T.v_compact({}, ts_sid), T.v_stats({})]
        check(all("58375" not in h for h in ts_views),
              "调用详情 / 压缩页 / 全局统计均无荒谬年份")
        check(bool(re.search(r"20\d{2}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", ts_views[0])),
              "调用详情渲染真实本地时间戳")
    else:
        check(False, "无可用会话进行时间戳渲染测试")

    print("== DSH 源：多帧 zstd 事件流 ==")
    try:
        import ctypes
        import shutil
        import tempfile
        from sources import dsh as D
        import sources._zstd as Z

        def _zpack(chunk):
            """压一帧 zstd：优先 CLI，退回 libzstd ctypes（无 zstd 命令的机器也能测）。"""
            try:
                return subprocess.run(["zstd", "-q", "-c"], input=chunk,
                                      stdout=subprocess.PIPE, check=True).stdout
            except FileNotFoundError:
                pass
            for cand in Z._candidates():
                try:
                    lib = ctypes.CDLL(cand)
                    lib.ZSTD_compressBound.argtypes = [ctypes.c_size_t]
                    lib.ZSTD_compressBound.restype = ctypes.c_size_t
                    lib.ZSTD_compress.argtypes = [
                        ctypes.c_void_p, ctypes.c_size_t,
                        ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int]
                    lib.ZSTD_compress.restype = ctypes.c_size_t
                    lib.ZSTD_isError.argtypes = [ctypes.c_size_t]
                    lib.ZSTD_isError.restype = ctypes.c_uint
                    dst = ctypes.create_string_buffer(lib.ZSTD_compressBound(len(chunk)))
                    n = lib.ZSTD_compress(dst, len(dst), chunk, len(chunk), 3)
                    if not lib.ZSTD_isError(n):
                        return dst.raw[:n]
                except (OSError, AttributeError):
                    continue
            raise FileNotFoundError("zstd")

        root = tempfile.mkdtemp(prefix="tt-dsh-")

        def _write(sid, events, ws="--ws--"):
            d = os.path.join(root, "sessions", ws, "session-" + sid)
            os.makedirs(d)
            lines = [json.dumps(e, ensure_ascii=False).encode() for e in events]
            # 两段各自压缩再拼接 = 两个独立 zstd 帧，复现「追加写入」
            blob = b"".join(_zpack(b"\n".join(c) + b"\n")
                            for c in (lines[:3], lines[3:]) if c)
            with open(os.path.join(d, "session.v4.jsonl.zstd"), "wb") as fh:
                fh.write(blob)

        ev = [
            {"type": "session", "version": 4, "id": "session-t1",
             "cwd": "/w", "delegationDepth": 0},
            {"type": "turn/start", "seq": 1, "time": 1780000000100, "data": {}},
            {"type": "user/message", "seq": 2, "time": 1780000000200,
             "data": {"content": "第一问"}},
            {"type": "assistant/message", "seq": 3, "time": 1780000001000,
             "data": {"message": {"source": {"model": "m1"},
                                  "content": [{"type": "thinking", "text": "想一下"},
                                              {"type": "text", "text": "答一"}]},
                      "usage": {"inputTokens": 10, "outputTokens": 5,
                                "cacheReadTokens": 100, "cacheWriteTokens": 0}}},
            {"type": "tool/call", "seq": 4, "time": 1780000001100,
             "data": {"name": "bash", "callId": "c1"}},
            {"type": "tool/result", "seq": 5, "time": 1780000001200,
             "data": {"callId": "c1", "content": "ok"}},
            {"type": "turn/start", "seq": 6, "time": 1780000002000, "data": {}},
            {"type": "user/message", "seq": 7, "time": 1780000002100,
             "data": {"content": "第二问"}},
            {"type": "assistant/message", "seq": 8, "time": 1780000003000,
             "data": {"message": {"source": {"model": "m1"}, "content": "答二"},
                      "usage": {"inputTokens": 20, "outputTokens": 7,
                                "cacheReadTokens": 200, "cacheWriteTokens": 0}}},
        ]
        _write("t1", ev)
        sub_ev = [
            {"type": "session", "id": "session-t2", "cwd": "/w",
             "delegationDepth": 1, "parentSessionId": "session-t1"},
            {"type": "user/message", "time": 1780000000500,
             "data": {"content": "子代理问"}},
            {"type": "assistant/message", "time": 1780000000900,
             "data": {"message": {"source": {"model": "m2"}, "content": "子代理答"},
                      "usage": {"inputTokens": 3, "outputTokens": 2}}},
        ]
        _write("t2", sub_ev)
        _write("other", [{"type": "session", "id": "other", "cwd": "/x",
                          "delegationDepth": 0},
                         {"type": "user/message", "time": 1780000000100,
                          "data": {"content": "无关会话"}}], ws="--other--")
        old_home = D.DSH_HOME
        D.DSH_HOME = root
        D._PARSED.clear()
        try:
            ss = D.scan()
            check(len(ss) == 3, "dsh 多会话扫描：主 / 子代理 / 无关会话各一条")
            s = next(x for x in ss if x["id"] == "dsh-session-t1")
            check(s["pin"] == 330 and s["hit"] == 300 and s["pout"] == 12,
                  "dsh 用量映射：pin=input+cacheRead+cacheWrite、hit=cacheRead（%d/%d/%d）"
                  % (s["pin"], s["hit"], s["pout"]))
            check(s["turns"] == 2 and s["ai"] == "第一问",
                  "dsh 轮次按 turn/start 计数、标题取首条用户消息")
            check(sum(1 for _ in D.rows(os.path.join(
                root, "sessions", "--ws--", "session-t1", "session.v4.jsonl.zstd")))
                  == len(ev), "dsh 多帧文件完整解析（2 帧拼接，不是只解首帧）")
            ctx, calls, meta, turns = D.build("dsh://session-t1")
            check(len(calls) == 2 and calls[0]["tool"] == "bash",
                  "dsh build 的调用数与工具归属正确")
            check([b["k"] for b in ctx][:4] == ["user", "assistant", "tool_call", "tool_result"],
                  "dsh 块序列按事件顺序还原")
            check(len(calls[0]["out"]) == 2
                  and calls[0]["out"][0]["kind"] == "thinking"
                  and calls[0]["out"][-1]["text"] == "答一",
                  "dsh 助手回复写入 out（思考 + 回答），输出全文与搜索可用")
            check(any(b["l"] == "工具返回 · bash" for b in ctx),
                  "dsh tool/result 块标签带工具名，与 Codex / ZCode 一致")
            rel, unmatched = T.subagent_relations(ss)
            check(any(x["id"] == "dsh-session-t2" for x in rel.get("dsh-session-t1", []))
                  and not unmatched,
                  "dsh 子代理 root/agent_reqs 建立父子关联，首页可自动折叠")
            seen = []
            orig_parse = D.parse

            def _spy(p, keep_ctx=True):
                seen.append(p)
                return orig_parse(p, keep_ctx=keep_ctx)

            D.parse = _spy
            try:
                sub_ctx, sub_calls, _m, _t = D.build("dsh://session-t2")
            finally:
                D.parse = orig_parse
            check(len(seen) == 1 and "session-t2" in seen[0],
                  "dsh build 只解压命中的会话，不扫全库（实际 %d 个）" % len(seen))
            check(bool(sub_calls) and bool(sub_calls[0]["out"])
                  and sub_calls[0]["out"][0]["text"] == "子代理答",
                  "dsh 子会话 build 的 out 非空")
        finally:
            D.DSH_HOME = old_home
            D._PARSED.clear()
            shutil.rmtree(root, ignore_errors=True)
    except FileNotFoundError:
        print("  SKIP 本机没有可用的 zstd 压缩器，跳过 DSH 多帧用例")

    print("== 启动临时服务 ==")
    proc = subprocess.Popen(
        [sys.executable, "-B", "trace_server.py", "--port", str(PORT),
         "--no-update-check"],   # 自测不联网：同时验证这个开关确实生效
        cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        ready = False
        for _ in range(240):
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
            # 同机工具互链与「检查更新」的 GitHub releases 都是用户主动点的
            # 导航链接，不属于「依赖外部资源」；抹掉再扫，避免误判成 CDN。
            scan = body.replace(b"http://127.0.0.1", b"loopback") \
                       .replace(b"http://localhost", b"loopback") \
                       .replace(T.RELEASES_URL.encode(), b"releases") \
                       .replace(T.ZHIHU_URL.encode(), b"zhihu") \
                       .replace(T.GITHUB_URL.encode(), b"github")
            ev = len(RE_EVENT.findall(scan))
            ext = len(RE_EXT.findall(scan)) + len(RE_LINK.findall(scan))
            jsu = scan.lower().count(b"javascript:")
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
                  "真实累计输入 token 等于 rawUsage 原值累加（未被统计修正影响）")
        if plain:
            pd = T.STORE.get(plain[0]["id"])
            check(all(c.get("vf", 0) == 0 for c in pd["calls"]),
                  "未压缩会话 vf 全为 0（口径完全不变）")

        print("== 分页越界一致性 ==")
        # 越界检索词从样例会话正文里现取，保证真实库与合成演示库都必定命中：
        # 写死 "token" 时演示数据讲的是 checkout 排查、不含该词，第 1 页即 0 行，
        # 会把「越界不空表」误判成失败。
        probe_term = ""
        for _b in T.STORE.get(sid)["ctx"]:
            _m = re.search(r"[A-Za-z]{4,}", _b.get("t") or "")
            if _m:
                probe_term = _m.group(0)
                break
        if not probe_term:   # 纯中文会话退而求其次：取一个双字词，find 下限是 2 字
            _m = re.search(r"[\u4e00-\u9fff]{2,}", " ".join(
                (_b.get("t") or "") for _b in T.STORE.get(sid)["ctx"]))
            probe_term = _m.group(0)[:4] if _m else ""
        check(bool(probe_term), "从样例会话正文取到越界检索词（%s）" % probe_term)
        paged = ["/s/{sid}?view=calls&p=999", "/s/{sid}/c/1?p=999",
                 "/s/{sid}/t/1?p=999",
                 "/find?q=%s&p=999" % probe_term, "/tools?p=999",
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

            # 缓存自适应：cap 必须覆盖全部会话（否则全库统计每轮都被 LRU 冲掉）
            st = T.Store(0)
            st._index = [{"id": "s%d" % i, "path": "p", "ws": "w", "mtime": 0}
                         for i in range(33)]
            st._index_at = time.time() + 9999
            st.sessions()
            check(st.cap >= 33, "自适应缓存覆盖全部会话（cap=%d / 33）" % st.cap)
            check(T.CACHE_SESSIONS == 0,
                  "默认 --cache 为自适应（0），不是写死的固定值")

            # 分组标题必须用全量统计：60 个会话同一工作区分两页，
            # 两页的标题都应写 60 个会话 / 90.0 分，只有「本页 N」不同。
            same = []
            for i in range(60):
                s = _sess(i)
                s["ws"] = "D:/ws/same/p1"
                s["credit"] = 1.5
                same.append(s)
            T.STORE = _FakeStore(same)
            p0 = T.v_index({"p": ["0"]})
            p1 = T.v_index({"p": ["1"]})
            n1 = min(T.PAGE_SESSIONS, 60)
            n2 = 60 - n1
            check(n2 > 0 and "60 个会话 · 90.0 分" in p0 and "本页 %d" % n1 in p0,
                  "分页第 1 页：分组标题用全量（60 个会话 · 90.0 分 · 本页 %d）" % n1)
            check(n2 > 0 and "60 个会话 · 90.0 分" in p1 and "本页 %d" % n2 in p1,
                  "分页第 2 页：标题口径一致（60 个会话 · 90.0 分 · 本页 %d）" % n2)
        finally:
            T.STORE = orig_store

        print("== 全库画像：按平台拼接环 ==")
        import gen_trace as G
        k1, _ = G.model_bucket({"model": "deepseek-v4.1-flash",
                                "requestModelName": "DeepSeek-V4.1-Flash"})
        k2, _ = G.model_bucket({"model": "deepseek-v4.1-flash",
                                "requestModelName": "Deepseek-V4.1-Flash"})
        k3, _ = G.model_bucket({"requestModelName": "deepseek-v4.1-flash"})
        check(k1 == k2 == k3 == "deepseek-v4.1-flash",
              "model_bucket 按底层 ID 分组，三种展示名写法同组")
        merged = G.merge_model_counter({"DeepSeek-V4.1-Flash": 500,
                                        "Deepseek-V4.1-Flash": 200,
                                        "deepseek-v4.1-flash": 50,
                                        "Hy4": 200, "Hy4 preview": 50})
        check(len(merged) == 3 and merged.get("DeepSeek-V4.1-Flash") == 750,
              "merge 合并为 750 token 并保留得票最多的展示名（%d 组）" % len(merged))

        def _plat(sid, src, pin, day):
            return {"id": sid, "ws": "D:/x/y", "title": "t", "model": "m",
                    "mtime": time.time(), "calls": 1, "turns": 1,
                    "pin": pin, "pout": 1, "credit": 0.1, "src": src,
                    "days": {day: [pin, 1]}}

        today = time.strftime("%Y-%m-%d", time.localtime())
        tri = [_plat("a", "workbuddy", 600, today),
               _plat("b", "zcode", 300, today),
               _plat("c", "codex", 100, today)]
        ov = T.index_overview(tri)
        names = re.findall(r'<span class="g">(.*?)</span>', ov)
        check(names == ["WorkBuddy", "ZCode", "Codex"],
              "构成环改为按平台拼接三段，不再按跨平台模型聚合（%s）" % names)
        shuffled = T.index_overview(list(reversed(tri)))
        names_shuf = re.findall(r'<span class="g">(.*?)</span>', shuffled)
        check(names_shuf == ["WorkBuddy", "ZCode", "Codex"],
              "平台环图例按输入 token 降序，与单平台按模型环一致（%s）" % names_shuf)
        pcts = [float(x) for x in re.findall(r"<em>([\d.]+)%</em>", ov)]
        check(len(pcts) == 3 and abs(sum(pcts) - 100) < 0.15
              and abs(pcts[0] - 60) < 0.15,
              "平台环图例合计 100%% 且按输入 token 占比（%s）" % pcts)
        ovx = T.index_overview(tri + [_plat("d", "mystery", 200, today)])
        pctsx = [float(x) for x in re.findall(r"<em>([\d.]+)%</em>", ovx)]
        check("其他" in ovx and len(pctsx) == 4 and abs(sum(pctsx) - 100) < 0.15,
              "未知来源归「其他」，环仍闭合到 100%%（%s）" % pctsx)
        names_x = re.findall(r'<span class="g">(.*?)</span>', ovx)
        check(names_x[-1] == "其他",
              "「其他」固定排在末尾，与单平台环的合并尾部一致（%s）" % names_x)
        ovm = T.index_overview(tri, focus="zcode")
        check("按模型 · 输入 token" in ovm and "近 3 个月" in ovm
              and "按平台 · 输入 token" not in ovm and "ZCode 平台内" in ovm,
              "单平台视图回到按模型环 + 平台色月历，不再做平台拼接")

        print("== 全库画像：月历热力图（连续色） ==")
        check("<polyline" not in ov and "<svg class=\"spark\"" not in ov,
              "折线图已整体换成热力图，不再输出 polyline")
        check(ov.count('<div class="cal-m">') == T.CAL_MONTHS
              and len(re.findall(r'<div class="cal-c', ov)) == 42 * T.CAL_MONTHS,
              "按自然月分块、每月 7×6 方格（%d 块 / %d 格）"
              % (ov.count('<div class="cal-m">'),
                 len(re.findall(r'<div class="cal-c', ov))))
        tips = re.findall(r'<div class="cal-c" data-tip="([^"]+)"', ov)
        check(all("&#10;" in t for t in tips) and
              any(t.startswith(today) and "WorkBuddy" in t for t in tips),
              "每格都有「日期 + 合计 + 平台拆分」气泡（%d 格）" % len(tips))
        check(any(t.endswith("无记录") for t in tips),
              "没有会话的那天留空格（--track 底），不写成 0 token")
        check('class="cal-c fut"' in ov and 'class="cal-c pad"' in ov
              and '.cal-c.pad,.cal-c.fut{background:transparent}' in T.CSS_VIZ,
              "本月未到与月初补位都只占位、不显示任何底色或边框")
        check('.cal-c.fut{background:transparent;box-shadow' not in T.CSS_VIZ,
              "未来日期不再画细边空心框，月末不会多出一片空心格")
        fold = T._workspace_cell(tri, sum(s["pin"] for s in tri))
        check(fold.count('<span class="wsl l') == 3
              and '个 parent 目录' in fold and '个工作区' in fold
              and '个会话' in fold and '<span class="wsfold-t">' not in fold,
              "工作区折叠摘要分三行树状展示（parent → 工作区 → 会话），不再挤成一行")
        check(ov.count('<div class="ovbody">') == 3
              and 'class="wsdist"' in ov and 'class="wsband"' in ov
              and '.ovbody{flex:1' in T.CSS_INDEX,
              "三列用 .ovbody 吃掉剩余高度并垂直居中，右列换成按 parent 目录的会话分布带")
        check('wspeek' not in ov and 'wspk' not in fold,
              "折叠态不再塞截断目录名的紧凑树预览")
        segs = re.findall(r'data-tip="[^"]*&#10;(\d+) 个会话"', fold)
        check(segs and all(int(n) > 0 for n in segs),
              "分布带每段都给绝对会话数（%d 段）" % len(segs))
        boot = T.page("会话索引", [("索引", "")],
                      '<div class="ovgrid"></div>', need=("index",),
                      boot_note="正在整理 119 个会话 · 12,532 次模型调用")
        plain = T.page("全局统计", [("索引", "/"), ("统计", "")], '<p>x</p>')
        check('class="boot"' in boot and 'class="boot"' not in plain
              and '会话解析中' in boot and '正在整理 119 个会话' in boot,
              "启动层只在首页注入，文案带当前视图真实会话数 / 调用数")
        back_boot = T.page("会话索引", [("索引", "")],
                           '<div class="ovgrid"></div>', need=("index",),
                           with_boot=False)
        check('class="boot"' not in back_boot,
              "检查更新回跳首页不重播「会话解析中」启动层")
        check('<script' not in boot and '服务端渲染' in boot,
              "启动层零 JS：撤除交给 CSS 文档时间线与绘制节奏，页脚口径不变")
        check('.boot{position:fixed' in T.CSS_INDEX
              and 'visibility:hidden;pointer-events:none' in T.CSS_INDEX
              and 'animation:boot-hold .85s' in T.CSS_INDEX
              and '@media(prefers-reduced-motion:reduce){.boot{display:none}}' in T.CSS_INDEX
              and '@media print{.boot{display:none}}' in T.CSS_INDEX,
              "启动层基础态隐藏 + reduced-motion / 打印整层撤掉，绝不会盖住内容")
        check('conic-gradient' in T.CSS_INDEX
              and '@keyframes boot-spin{to{transform:rotate(360deg)}}' in T.CSS_INDEX
              and 'will-change:transform' in T.CSS_INDEX
              and 'stroke-dashoffset' not in T.CSS_INDEX
              and 'infinite' not in T.CSS_INDEX
              and T.CSS_INDEX.count('@keyframes boot-') == 4,
              "灰边彩循环只用 transform / opacity 合成层动画，且有限次数、"
              "遮罩撤除后不留活动动画")
        check('--in-delay' not in T.CSS_INDEX
              and 'calc(var(--in-delay' not in T.CSS
              and 'animation:tt-in .52s var(--ease-in) backwards' in T.CSS
              and T.CSS.count('animation-delay:.') == 12,
              "入场动画保持原 12 组错峰节奏，不整体延后（实测会拉低稳态 fps）")

        print("== 版本与更新提示（L1） ==")
        ver_file = open("scripts/VERSION", encoding="utf-8").read().strip()
        check(T.APP_VERSION == ver_file,
              "版本号取自 scripts/VERSION（%s），代码里不另存常量" % T.APP_VERSION)
        check(T._ver_key("1.4.10") > T._ver_key("1.4.9")
              and T._ver_key("v1.5.0") > T._ver_key("1.4.99")
              and T._ver_key("1.4") == T._ver_key("1.4.0")
              and T._ver_key("dev") is None,
              "版本按数字段比较：1.4.10 > 1.4.9；dev 不参与比较")
        check(T._ver_key("2.1.0") == T._ver_key("2.1.0.0")
              and T._ver_key("2.1.0") > T._ver_key("2.1.0-exp")
              and T._ver_key("2.1.0") > T._ver_key("2.0.0.3-exp")
              and T._ver_key("2.0.0") > T._ver_key("2.0.0-exp"),
              "预发布比较：同数字正式版严格大于 -exp，四段与三段归一")
        check(T.UPDATE["state"] == "off",
              "导入模块不联网：更新只在页眉开关开启后或 /check 时才查")
        check(T.UPDATE["auto"] is False,
              "新安装默认关闭自动检查更新（零写盘，不记住上次选择）")
        saved = dict(T.UPDATE)
        try:
            T.UPDATE.update(state="ok", latest="", url="", at=0.0, error="",
                            auto=True)
            quiet = T.page("索引", [("索引", "")], "<p>x</p>")
            check('class="upd"' not in quiet and not T.update_newer(),
                  "没查到新版本时不出现提示条")
            check('class="updbox"' in quiet
                  and '<span class="sw-t">自动检查更新</span>' in quiet
                  and 'href="/update-toggle?on=0"' in quiet
                  and 'aria-checked="true"' in quiet,
                  "开启态点一下即关，零脚本")
            check('class="hd-top"' in quiet
                  and quiet.index('class="updbox"') < quiet.index('class="hdrow"')
                  and 'class="hdrow"' in quiet,
                  "更新控件独占页眉右上角（面包屑行），与平台切换分行")
            css = T._squeeze_css(T.CSS)
            check('<a class="upd-ver" href="/check" '
                  'title="主动比对版本：检查后在本页弹出结果浮窗">检查更新</a>' in quiet
                  and "版本检查 ↗" not in quiet,
                  "「检查更新」无箭头，同窗口走 /check 检查并弹出结果浮窗")
            check('id="upd-result"' in quiet
                  and ".upd-result:target{display:flex}" in css
                  and "已是最新版本" in quiet
                  and 'class="upd-scrim" href="#"' in quiet,
                  "结果浮窗复用 :target 显隐与确认层皮肤，无脚本、可点遮罩关闭")
            check("calc(100% - 20px)" in T._squeeze_css(".box { width: calc(100% - 20px); }")
                  and "calc(100% + 20px)" in T._squeeze_css(".box { width: calc(100% + 20px); }"),
                  "_squeeze_css 保留 calc() 运算符两侧空格")
            check('id="upd-confirm"' in quiet
                  and "可能消耗少量流量（每次约几KB）" in quiet
                  and "不会上传任何本地数据" in quiet
                  and "是否确定开启？" in quiet
                  and '.upd-modal:target{display:flex}' in css,
                  "开启前弹确认层：说明流量与隐私，纯 CSS :target 显隐")
            check('class="upd-scrim" href="#"' in quiet
                  and 'class="upd-eyebrow"' in quiet
                  and 'class="upd-title"' in quiet
                  and ".upd-scrim{position:absolute;inset:0;background:#0009;" in css
                  and "backdrop-filter:blur(4px)" in css
                  and "@keyframes updRise" in css
                  and ".upd-scrim,.upd-card{animation:none}" in css,
                  "确认层重做：可点遮罩关闭、轻模糊与克制上浮，减弱动态偏好下停动画")
            check(quiet.index('href="#">取消</a>')
                  < quiet.index('href="/update-toggle?on=1">确定开启</a>')
                  and ".upd-acts .btn{font-size:12px;padding:6px 16px;" in css,
                  "确认层按钮分层：取消在前，实心主按钮「确定开启」在右")
            check(quiet.count('class="upd-sep"') == 1
                  and quiet.index('class="upd-sep"') < quiet.index('class="upd-cur"')
                  and quiet.index('class="upd-cur"') < quiet.index('class="upd-ver"')
                  and 'class="upd-cur" title="当前运行的版本">当前版本 '
                      '<code>v%s</code></span>' % T.APP_VERSION in quiet
                  and ".upd-cur code{background:var(--track);color:var(--accent-d);"
                      "padding:0 4px; border:1px solid var(--upd-bd);"
                      "border-radius:2px}" in css
                  and ">检查更新</a>" in quiet,
                  "胶囊只分两区一线：自动 | 当前版本 + 检查更新，版本号是灰底细边框的 markdown 行内代码小块")
            check(".upd-ver{display:inline-flex;align-items:center;padding:1px 10px; "
                  "border:1px solid var(--upd-bd);border-radius:6px;background:var(--track); " in css
                  and ".upd-ver:hover{background:var(--accent);color:var(--bg);"
                      "border-color:var(--accent)}" in css
                  and ".updbox{display:inline-flex;align-items:center;gap:10px;font-size:12px; "
                       "padding:3px 12px;border:1px solid var(--upd-bd);border-radius:9px; " in css
                  and "--upd-bg:#E8EEE8;--upd-bd:#C9D6CB;" in css
                  and "--upd-bg:#2A3731;--upd-bd:#41524A;" in css,
                  "检查更新按钮与版本号统一灰底 + 细边框一族，胶囊压窄（3px 上下 + 9px 圆角）+ 莫兰迪双主题底")
            check('class="foot-brand"' in quiet
                  and '<b>token-trace</b><i>v%s</i>' % T.APP_VERSION in quiet
                  and ".foot-brand{display:inline-flex;align-items:center;gap:9px;"
                      "margin-left:auto;" in css
                  and "border-left:1px solid var(--border)" in css,
                  "页脚品牌签名：项目名加粗 + 版本徽章，细竖线与左侧说明隔离")
            check('<a href="/">索引</a>' not in quiet
                  and '<a href="/stats">全局统计</a>' not in quiet
                  and '<a href="/rank">筛选与排序</a>' not in quiet
                  and 'href="/api">API</a>' not in quiet
                  and 'href="/check"' not in quiet[quiet.index("<footer>"):]
                  and "· 检查更新" not in quiet
                  and "© 大晋帝国之谶" in quiet
                  and T.ZHIHU_URL in quiet and T.GITHUB_URL in quiet
                  and ">知乎 ↗</a>" in quiet and ">GitHub ↗</a>" in quiet,
                  "页脚不再重复目录导航；版权保留 © 与知乎/GitHub 双链接")
            T.UPDATE.update(state="ok", latest="9.9.9",
                            url="https://github.com/x/y/releases/tag/v9.9.9")
            upd = T.page("索引", [("索引", "")], "<p>x</p>")
            check('class="upd"' in upd and "新版本 9.9.9" in upd
                  and "releases/tag/v9.9.9" in upd
                  and ("当前 v%s" % T.APP_VERSION) in upd,
                  "查到更新时出一条提示，带当前版本与 release 链接")
            check('class="upd-ver upd-new"' in upd
                  and '<a class="upd-ver upd-new" href="#upd-result"' in upd
                  and ">有可用更新 v9.9.9</a>" in upd
                  and ">打开 Release ↗</a>" in upd,
                  "发现新版本时按钮变成「有可用更新」，点击就地弹结果浮窗")
            check("<script" not in upd, "更新提示不引入任何脚本")
            T.UPDATE.update(latest=T.APP_VERSION)
            check(not T.update_newer()
                  and 'class="upd"' not in T.page("索引", [("索引", "")], "<p>x</p>"),
                  "最新版等于当前版时不提示（不制造假更新）")
            T.UPDATE.update(latest="0.1.0", state="ok")
            check(not T.update_newer(), "更旧的 release 不提示（拒绝降级提示）")
            T.UPDATE.update(state="error", error="URLError: timed out", latest="", url="")
            err = T.page("索引", [("索引", "")], "<p>x</p>")
            check("检查未完成" in err and "URLError: timed out" in err
                  and 'href="/check">重新检查</a>' in err,
                  "检查失败时浮窗显示原因并给重试入口")
            T.UPDATE.update(auto=False, state="off", latest="", url="")
            off = T.page("索引", [("索引", "")], "<p>x</p>")
            check('href="#upd-confirm"' in off
                  and 'aria-checked="false"' in off,
                  "关闭态点开关才走确认层")
            check(not T.check_update() and T.UPDATE["state"] == "off",
                  "关掉后 check_update 直接返回，不产生任何网络请求")
            was_off = T.UPDATE_OFF
            try:
                T.UPDATE_OFF = True
                dis = T.page("索引", [("索引", "")], "<p>x</p>")
                check("检查已禁用" in dis and "--no-update-check" in dis,
                      "禁用自动检查时浮窗说明原因，不制造假更新")
            finally:
                T.UPDATE_OFF = was_off
        finally:
            T.UPDATE.clear()
            T.UPDATE.update(saved)
        check("只读" in T.page("索引", [("索引", "")], "<p>x</p>"),
              "更新检查不改变「数据只读」口径：只查版本号，不下载、不安装")

        print("== 更新下载（可选 L2）：只下载校验，不自动安装 ==")
        T.UPDATE.update(state="ok", latest="9.9.9", url="https://x/y",
                        download={"ok": True, "path": "/tmp/x.deb",
                                  "info": "/tmp/x.deb", "at": 1.0})
        dlh = T.page("索引", [("索引", "")], "<p>x</p>")
        check("更新包已就绪" in dlh and "sudo dpkg -i" in dlh
              and "x.deb" in dlh,
              "下载成功后浮窗给出本地路径与安装命令，不自动执行")
        T.UPDATE.update(download={"ok": False, "info": "URLError: timed out",
                                  "at": 1.0})
        dlf = T.page("索引", [("索引", "")], "<p>x</p>")
        check("下载未完成" in dlf and "TRACE_MIRROR" in dlf
              and 'href="/download">重试下载' in dlf,
              "下载失败时给出原因、镜像提示与重试入口")
        T.UPDATE.update(state="off", latest="", url="")
        T.UPDATE.pop("download", None)
        was_off2 = T.UPDATE_OFF
        try:
            T.UPDATE_OFF = False
            ok0, why0 = T.download_update()
        finally:
            T.UPDATE_OFF = was_off2
        check(not ok0 and "最新" in why0,
              "无新版本时 download_update 直接返回，不发起网络请求")

        print("== 翻页 / 排序 / 停止服务 / 空闲退出 ==")
        pg = T.pager("/s/x", {"p": ["5"], "kind": ["all"]}, 5, 10, 300, "次调用")
        check("« 首页" in pg and "尾页 »" in pg and 'class="pg on"' in pg
              and 'class="pgjump"' in pg and 'name="p1"' in pg,
              "分页条有首页 / 尾页 / 当前页页码 / 跳页表单")
        check(T.clamp_p({"p1": ["3"]}, 100, 10) == 2
              and T.clamp_p({"p": ["99"]}, 100, 10) == 9,
              "跳页 p1 按 1 基换算，越界仍夹到最后一页")
        check(T.pager("/x", {}, 0, 40, 40) == "" and T.pager("/x", {}, 0, 40, 41),
              "只有一页时不渲染分页条：会话页最多挂五个，不留禁用按钮与跳页表单")
        wide = T.pager("/", {}, 15, 10, 300)
        check(wide.count('class="pg-gap"') >= 1
              and wide.count('class="pg"') + wide.count('class="pg on"') <= 7,
              "页数多时页码窗口收敛到首尾 + 当前左右，空档显示省略号")
        asc_h = T.v_index({"sort": ["calls"], "order": ["asc"]})
        desc_h = T.v_index({"sort": ["calls"], "order": ["desc"]})
        rows_of = lambda h: re.findall(r'<a class="row" href="/s/([^"?]+)"', h)
        check("调用数 ↑" in asc_h and "调用数 ↓" in desc_h
              and "order=asc" in desc_h
              and (rows_of(asc_h) != rows_of(desc_h) or len(rows_of(asc_h)) < 2),
              "会话排序支持正序 / 倒序，当前字段带方向箭头且行序随之变化")
        stop_h = T.page("索引", [("索引", "")], "<p>x</p>")
        check('id="stop-confirm"' in stop_h and 'action="/shutdown"' in stop_h
              and '<form method="post"' in stop_h,
              "页脚停止服务走确认浮窗 + 同源 POST 表单")
        T.touch_activity()
        check(T.idle_seconds() < 1 and T.IDLE_SECONDS == 10 * 3600,
              "请求活动重置空闲计时，默认 10 小时")

        # 连续色：同一平台三天给 100 / 400 / 1600，opacity 必须是三个不同值
        d1 = time.strftime("%Y-%m-%d", time.localtime(time.time() - 86400))
        d2 = time.strftime("%Y-%m-%d", time.localtime(time.time() - 2 * 86400))
        cont = [_plat("h1", "workbuddy", 100, today),
                _plat("h2", "workbuddy", 400, d1),
                _plat("h3", "workbuddy", 1600, d2)]
        ovc = T.index_overview(cont)
        # 只认月历格子的 <i>：分布色带同样用 var(--accent)+opacity，
        # 但它的 style 后面还跟着 data-tip，用 "></i> 收尾才不会误抓进来。
        ops = re.findall(r'<i style="background:var\(--accent\);opacity:([\d.]+)"></i>',
                         ovc)
        vals = sorted(set(float(o) for o in ops))
        check(len(vals) == 3 and vals[0] > 0.1 and abs(vals[-1] - 1.0) < .01,
              "同色色深按值连续变化，不是分档（%s）" % vals)
        check(all(0.10 < v <= 1.0 for v in vals),
              "最浅不早于 10%% 不透明，0 值才用空底，浅色仍可辨")
        ovm2 = T.index_overview(tri, focus="zcode")
        check(len(re.findall(r'<div class="cal-c', ovm2)) == 42 * T.CAL_MONTHS
              and "background:var(--p1)" in ovm2
              and "近 3 个月" in ovm2 and "按平台拆分" not in ovm2,
              "单平台视图同一套月历，格子改用该平台色")
        check("<polyline" not in ovm2, "单平台视图也不再画折线")
        # 「全部」视图：当天只有一家平台时，气泡不能只剩裸 token
        solo_day = time.strftime("%Y-%m-%d", time.localtime(time.time() - 3 * 86400))
        solo = T.index_overview([_plat("solo", "zcode", 700, solo_day)])
        check("ZCode 700 token" in solo,
              "全部热力图单平台日：token 旁标出平台名，不再只显示裸数字")

        print("== 非 WorkBuddy 平台：不摆积分，只给金额估算 ==")
        orig_store = T.STORE
        try:
            ext = []
            for i in range(3):
                s = _sess(i)
                s["src"] = "codex"
                s["credit"] = 0.0
                s["cost"] = 0.29
                s["ws"] = "D:/ws/codex"
                ext.append(s)
            T.STORE = _FakeStore(ext)
            p = T.v_index({"src": ["codex"]})
            check("0.0 分" not in p and "积分" not in p,
                  "非 WB 首页的分组标题与列表不再出现 0.0 分 / 积分")
            check("≈¥" in p, "非 WB 首页改用金额估算口径")
            check(T.money_label(1.5, None) == "1.50 分"
                  and T.money_label(0, 0.29) == "≈¥0.29"
                  and T.money_label(0, 0) == "—",
                  "money_label：有积分显示分，无积分显示 ¥，都没有留破折号")
            import pricing
            check(pricing.FX_USD_CNY == 6.67
                  and pricing.rates_for("deepseek-v4.1-flash")[:3] == (1.0, 0.02, 4.0)
                  and pricing.price_src("glm-5.2") == "approx"
                  and pricing.price_src("unknown-model") == "fallback",
                  "价目表：汇率 6.67、DeepSeek 用官方人民币价、近似档与未收录分开标记")
            check(pricing.rates_for("gemini-4-neon")[:3]
                  == (3.00 * 6.67, 0.15 * 6.67, 15.0 * 6.67)
                  and pricing.rates_for("gemini-3.8-flash")[:3]
                  == (0.75 * 6.67, 0.075 * 6.67, 3.75 * 6.67)
                  and pricing.price_src("gemini-3.8-flash-tiered") == "approx"
                  and pricing.price_src("claude-sonnet-5-5-medium") == "list",
                  "价目表：Gemini 3.8 Flash / 4 Neon 按刊例价，Antigravity 变体标近似，Sonnet 5.5 已收录")
            check(pricing.rates_for("hy4-flash")[:3] == (0.5, 0.02, 2.0)
                  and pricing.rates_for("hy3")[:3] == (1.0, 0.25, 4.0)
                  and pricing.rates_for("gpt-6.2-sol")[:3]
                  == (2.0 * 6.67, 0.10 * 6.67, 10.0 * 6.67)
                  and pricing.rates_for("gpt-6.2-astra")[:3]
                  == (8.0 * 6.67, 0.40 * 6.67, 40.0 * 6.67)
                  and pricing.rates_for("claude-sonnet-5.5-medium")[:3]
                  == pricing.rates_for("claude-sonnet-5-5-medium")[:3],
                  "价目表：hy4-flash 不被 hy4 前缀吃掉，Hy3/GPT-6.2 已收录，Sonnet 5.5 点号别名同价")
        finally:
            T.STORE = orig_store
        # 平台切换：没有会话的平台置灰并写明原因，有数据的可点
        sw = T.plat_switch(tri, "", "/stats")
        check('class="off"' in sw and "无 DeepSeek Harness 会话记录" in sw,
              "平台切换：本机没有 DeepSeek Harness 时该项置灰并给出原因")
        sw2 = T.plat_switch(tri, "zcode", "/stats")
        check('href="/stats?src=zcode"' in sw2 and 'class="on"' in sw2,
              "平台切换：有数据的平台可点，当前平台高亮")
        # 平台注册表与切换栏：全称给宽屏、短名留给窄屏，无数据的新平台置灰
        sw = T.plat_switch(tri, "", "/stats")
        check(len(T.PLATFORMS) == 5
              and T.PLAT_NAME["dsh"] == "DeepSeek Harness"
              and T.PLAT_SHORT["antigravity"] == "AG"
              and '<span class="pfull">DeepSeek Harness</span>' in sw
              and '<span class="pshort">DSH</span>' in sw
              and "无 Antigravity 会话记录" in sw,
              "平台注册表：DSH 全称、Antigravity 短名，新平台无数据置灰")
        big = 10340000000
        fake_big = [{"id": "c", "ws": "D:/x/y", "title": "t", "model": "m",
                     "models": {"m": big}, "mtime": time.time(), "calls": 1,
                     "turns": 1, "pin": big, "pout": 1, "credit": 0.1}]
        ov3 = T.index_overview(fake_big)
        ctr = re.search(r'<div class="ctr"><b style="font-size:([\d.]+)px">([^<]*)</b>', ov3)
        check(ctr is not None and float(ctr.group(1)) < 12.0
              and len(ctr.group(2)) >= 8,
              "环心 8 字符数字收到 %s px，不压环" % (ctr.group(1) if ctr else "缺失"))
        try:
            idx = G.scan_all()
            prim = set(s["model"] for s in idx if s.get("model"))
            keys = set()
            for s in idx:
                keys.update(s.get("models") or {})
            check(len(prim) == len(set(G.canon_model(x) for x in prim))
                  and len(keys) == len(set(G.canon_model(x) for x in keys)),
                  "scan_all 索引层全库同名：%d 个主模型 / %d 个 models 键无变体"
                  % (len(prim), len(keys)))
        except Exception as e:
            check(False, "scan_all 索引层全库同名（异常 %s）" % type(e).__name__)

        print("== 按调用真实日期分桶（days）==")
        try:
            idx = G.scan_all()
            bad = [s["id"] for s in idx
                   if "days" in s and (
                       sum(b[0] for b in s["days"].values()) != s["pin"]
                       or sum(b[1] for b in s["days"].values()) != s["calls"])]
            check(all("days" in s for s in idx) and not bad,
                  "scan_all 每个会话 days 的 token/调用数与 pin/calls 自洽（%d 会话，%d 异常）"
                  % (len(idx), len(bad)))
            spanning = [s for s in idx if len(s.get("days") or {}) > 1]
            check(bool(spanning),
                  "真实库存在跨天会话，days 确实把调用分散到多天（%d 个）" % len(spanning))
        except Exception as e:
            check(False, "days 口径自洽（异常 %s）" % type(e).__name__)

        # 构造：mtime 是今天，但调用全部发生在 9 天前 / 3 天前。
        # 旧口径会把整个会话压到今天；新口径应按 days 分散到真实那两天。
        today = T.datetime.date.today()
        d9 = (today - T.datetime.timedelta(days=9)).strftime("%Y-%m-%d")
        d3 = (today - T.datetime.timedelta(days=3)).strftime("%Y-%m-%d")
        d0 = today.strftime("%Y-%m-%d")
        fake_days = dict(_sess(0))
        fake_days.update({"mtime": time.time(), "calls": 105, "pin": 105000,
                          "days": {d9: [100000, 100], d3: [5000, 5]}})
        hm = T.activity_heatmap([fake_days])
        check(("%s · 100 次调用" % d9) in hm and ("%s · 5 次调用" % d3) in hm,
              "活动日历按 days 分色：9 天前 100 次 / 3 天前 5 次各归其日")
        check(d0 not in hm,
              "活动日历不再把整段会话压到 mtime（今天）那一天")
        # 窗口外的历史活动日进 agg 但不进热力图：「N 天有活动」只数有色格子
        old = (today - T.datetime.timedelta(days=400)).strftime("%Y-%m-%d")
        fake_days2 = dict(fake_days)
        fake_days2["days"] = {d9: [100000, 100], d3: [5000, 5], old: [9, 1]}
        hm3 = T.activity_heatmap([fake_days2])
        n_tip = len(re.findall(r'data-tip="[\d-]+ · [\d,]+ 次调用"', hm3))
        n_label = int(re.search(r'(\d+) 天有活动', hm3).group(1))
        check(n_tip == 2 and n_label == 2,
              "窗口外活动日不计入「N 天有活动」（格子 %d / 标注 %d）" % (n_tip, n_label))
        # 排版：7 行 = 周一..周日、26 列 = 26 周，星期标签占第一列，无补位格
        check('class="hmw"' in hm3 and "--hmc:26" in hm3
              and hm3.count('<i') == 26 * 7 + 5        # 182 格 + 5 个图例色块
              and hm3.count('<b class="wk">') == 7
              and 'class="pad"' not in hm3
              and "最近 26 周" in hm3
              and 'grid-column:2/span' in hm3,
              "活动日历 7 行 × 26 列周对齐，星期标签占首列、无补位格")
        # 无 days 的旧索引 / 外部数据必须回退 mtime，不抛异常
        try:
            hm2 = T.activity_heatmap([_sess(0)])
            ov2 = T.index_overview([_sess(0)])
            check("会话活动日历" in hm2 and "全库画像" in ov2,
                  "无 days 字段时回退 mtime 兜底，首页与日历正常渲染")
        except Exception as e:
            check(False, "无 days 回退不抛异常（异常 %s）" % type(e).__name__)

        print("== 文案口径：token / 输入 / 输出 / 两栏布局 ==")
        src = open(os.path.join(ROOT, "trace_server.py"), encoding="utf-8").read()
        check('class="num">in %s' not in src and 'class="num">out %s' not in src,
              "列表行不再用 in/out 简写，统一为「输入」「输出」")
        check('"累计输入 tok"' not in src and '"输出 tok"' not in src
              and "<span>tok</span>" not in src,
              "KPI 与环心不再用 tok 简写，统一为 token")
        ss = T.STORE.sessions()
        st_html = T.v_stats({})
        check('class="viz2"' in st_html and st_html.count('class="viz"') >= 2,
              "全局统计两张卡片并排为左右两栏（viz2 容器）")
        check("@media(max-width:900px){.viz2{grid-template-columns:1fr}}" in st_html,
              "窄屏单列回退随 CSS_VIZ 注入 /stats（不只在首页的 CSS_INDEX 里）")
        ov = T.index_overview(ss)
        check('class="fine ovnote"' not in ov
              and "按模型 ID 分组" not in ov
              and "按每次调用实际发生日" not in ov
              and "右侧数字为会话数" not in ov,
              "首页三栏下方的小字脚注已移除")
        check(re.search(r"<b>\d+</b> 个 parent 目录", ov)
              and re.search(r"<b>\d+</b> 个工作区", ov)
              and re.search(r"<b>\d+</b> 个会话", ov)
              and ov.count('<span class="wsl l') == 3,
              "递归关系摘要分三行树状展示 parent 目录 / 工作区 / 会话")

        print("== 子代理关系推断 ==")

        def _sm(sid, **kw):
            """造一个最小会话条目；agent-* 视为子代理。"""
            d = {"id": sid, "is_sub": sid.startswith("agent-"), "root": "",
                 "first_ts": 0, "agent_calls": [], "agent_reqs": []}
            d.update(kw)
            return d

        m1 = _sm("main-1", agent_reqs=["req-a"], agent_calls=[1780000000000],
                 first_ts=1779999999000)
        m2 = _sm("main-2", agent_reqs=["req-b"], agent_calls=[1780000600000])
        s1 = _sm("agent-1", root="req-a", first_ts="1794062198000")  # 字符串时间 + 远：靠 root
        s2 = _sm("agent-2", first_ts="1780000000500")                 # 无 root：时间窗兜底
        s3 = _sm("agent-3", root="missing", first_ts="0")             # 0 不参与匹配
        s4 = _sm("agent-4", root="req-b", first_ts=None)
        rel, un = T.subagent_relations([m1, m2, s1, s2, s3, s4])
        check([x["id"] for x in rel.get("main-1", [])] == ["agent-1", "agent-2"],
              "rootRequestId 精确命中优先，字符串时间戳不再抛 TypeError（原首页 500）")
        check([x["id"] for x in rel.get("main-2", [])] == ["agent-4"],
              "第二个父会话按 root 命中")
        check([x["id"] for x in un] == ["agent-3"],
              "时间戳为 0 的子代理既不误挂载、也不混进已匹配")
        s5 = _sm("agent-5", agent_reqs=["req-c"], agent_calls=[1780000001000])
        s6 = _sm("agent-6", root="req-c")
        rel2, _ = T.subagent_relations([s5, s6])
        check(bool(rel2.get("agent-5")) and rel2["agent-5"][0]["id"] == "agent-6",
              "嵌套子代理挂到直接父级（子会话也可作为父）")

        print("== 首页：子代理树状隶属 ==")
        _b = _sess(0)
        _p = dict(_b, id="main-tree", ws="D:/ws/main", title="主会话",
                  agent_reqs=["req-t"], agent_calls=[], is_sub=False,
                  mtime=time.time())
        _k = dict(_b, id="agent-tree", ws="subagents", title="(无标题)",
                  is_sub=True, root="req-t", agent="general-purpose",
                  first_ts=0, agent_reqs=[], agent_calls=[],
                  mtime=time.time() - 60)
        try:
            T.STORE = _FakeStore([_p, _k])
            h = T.v_index({})
            check('<details class="subtree"' in h
                  and h.count('class="row subagent child"') == 1,
                  "同页父子收编为可折叠子树，子代理行缩进挂载")
            check("派生自「主会话」" in h and "由本会话派生" in h,
                  "子树与子行双向标明隶属关系")
            check("含 1 个子代理" in h and "1 个子代理" in h,
                  "分组标题与父行 chip 都标出子代理数量")
            check("<summary>subagents" not in h,
                  "子代理不再单独占一个工作区分组")
            check("<script" not in h and len(h.encode("utf-8")) <= MAX_HTML,
                  "子树仍是零脚本且未越过 96 KB")
            h2 = T.v_index({"ws": ["subagents"]})
            check("未能定位父级" in h2,
                  "筛选掉父会话后，子代理行仍标注未能定位父级")
        finally:
            T.STORE = orig_store

        print("== 首页：脏时间戳不再 500 ==")
        dirty = [dict(x) for x in T.STORE.sessions()[:3]]
        dirty[0] = dict(dirty[0], agent_calls=["1780000000000"], agent_reqs=[],
                        first_ts="1780000000000")
        dirty.append(dict(dirty[0], id="agent-dirty", is_sub=True, root="",
                          first_ts="1794062198000"))
        class _DirtyStore(_FakeStore):
            def get(self, sid):
                raise KeyError(sid)
        T.STORE = _DirtyStore(dirty)
        try:
            h = T.v_index({})
            check('class="hero"' in h and 'class="kpi"' in h,
                  "首页遇字符串时间戳正常渲染（修复前 subagent_relations 抛 TypeError → 500）")
        finally:
            T.STORE = orig_store

        print("== /stats 不重复解析 ==")
        T.v_stats({})                       # 预热全部会话
        orig_build = T.G.build
        n_build = {"n": 0}

        def _count_build(*a, **k):
            n_build["n"] += 1
            return orig_build(*a, **k)

        T.G.build = _count_build
        try:
            # 清空会话缓存与汇总缓存：第一次渲染必须把全部会话各解析一遍（且只一遍），
            # 第二次应当全部命中缓存 —— 只断言 cap 大小不足以证明这一点。
            with T.STORE.lock:
                T.STORE._cache.clear()
            T._GS_CACHE.clear()
            T.v_stats({})
            first = n_build["n"]
            T.v_stats({})
            again = n_build["n"] - first
            check(first == len(sessions) and again == 0,
                  "/stats 首次解析 %d/%d 个会话、连刷第二次 %d 次（不重复解析）"
                  % (first, len(sessions), again))
        finally:
            T.G.build = orig_build

        print("== 同机工具互链（用量监视）==")
        st, body = get("/api/health")
        hd = json.loads(body)["data"]
        check(hd.get("monitor") in ("up", "down", "disabled") and "monitor_url" in hd,
              "/api/health 自报 monitor=%s（%s）" % (hd.get("monitor"), hd.get("monitor_url")))
        orig_mon = T.monitor_url
        try:
            T.monitor_url = lambda: "http://127.0.0.1:8791/"
            h = T.v_index({})
            check('href="http://127.0.0.1:8791/"' in h and "实时用量" in h
                  and 'rel="noopener"' in h,
                  "监视器在跑时：首页工具条出现「实时用量」外链（noopener）")
            deep = T.v_stats({})
            check("实时用量" in deep, "深页页脚也有同一入口（不必回首页）")
            T.monitor_url = lambda: ""
            h = T.v_index({})
            check("实时用量" not in h, "监视器没在跑时不渲染死链")
        finally:
            T.monitor_url = orig_mon

        print("== 轮来源筛选：非法值 / 分页 / 转义 ==")
        st, body = get("/s/%s?kind=%%3Cscript%%3Ealert(1)%%3C%%2Fscript%%3E" % sid)
        check(st == 200 and b"<script" not in body and b"alert(1)" not in body,
              "?kind= 注入串按非法值丢弃，不回显、不破坏页面（%d）" % st)
        st, body = get("/s/%s?kind=user&p=999" % sid)
        check(st == 200 and body.count(b'class="row') > 0,
              "?kind=user 越界分页仍有数据行（%d / %d 行）"
              % (st, body.count(b'class="row')))

        print("== 会话页：模型切换提示 ==")

        def _real_models(s0):
            out = set()
            for c in T.STORE.get(s0["id"])["calls"]:
                nm = T.display_model(c.get("model") or "").strip()
                if nm and nm.lower() not in ("unknown", "?", "none", "default", "n/a"):
                    out.add(nm.lower())
            return out

        multis = [s0 for s0 in T.STORE.sessions() if len(_real_models(s0)) > 1]
        singles = [s0 for s0 in T.STORE.sessions() if len(_real_models(s0)) <= 1]
        if multis:
            hm = T.v_session({}, multis[0]["id"])
            check("本会话切换过 <b>%d 个模型</b>" % len(_real_models(multis[0])) in hm
                  and "换模型会重建提示缓存" in hm,
                  "多模型会话一行提示「切换过 N 个模型 + 换模型重建缓存 + 命中率」（%d 个会话）"
                  % len(multis))
            # 提示要短：一条 warn 不该再写三句解释，超出即视为文案回涨
            seg = hm[hm.find("本会话切换过"):]
            seg = seg[:seg.find("</div>")] if "</div>" in seg else seg
            check(len(seg) < 220, "模型切换提示保持一行（%d 字符，阈值 220）" % len(seg))
        if singles:
            hs = T.v_session({}, singles[0]["id"])
            check("本会话切换过" not in hs,
                  "单模型会话不出现模型切换提示，不制造噪音")

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

        print("== 分析宽表 /api/analytics/calls ==")
        st, body = get("/api/analytics/calls?limit=3")
        d = json.loads(body)
        check(st == 200 and d.get("ok") is True and len(d["data"]) == 3
              and d["meta"]["total"] > 3,
              "宽表端点返回统一信封、3 行与全库 total=%s"
              % d.get("meta", {}).get("total"))
        check(d["meta"].get("ts_unit") == "unix_ms"
              and len(d["meta"]["fields"]) == 18,
              "meta 声明毫秒时间戳单位与 18 列字段字典")
        check(all(set(r) == set(T.ANALYTICS_FIELDS) for r in d["data"]),
              "默认每行字段完整（18 列宽表，含 cost / src / is_sub / root）")
        check(all(isinstance(r["is_sub"], bool) and isinstance(r["root"], str)
                  and isinstance(r["src"], str) and isinstance(r["cost"], (int, float))
                  for r in d["data"]),
              "新增 cost / src / is_sub / root 类型正确（bool / str / num）")
        st_src, b_src = get("/api/analytics/calls?src=workbuddy&limit=3")
        d_src = json.loads(b_src)
        check(st_src == 200 and all(r["src"] == "workbuddy" for r in d_src["data"]),
              "宽表支持 ?src= 平台筛选")

        allrows = T.analytics_calls({"limit": ["500"]})[0]["data"]
        sess_names = set(k for s in T.STORE.sessions()
                         for k in (s.get("models") or {}))
        api_names = set(r["model"] for r in allrows)
        check(all(not n or n in sess_names for n in api_names),
              "调用级模型名与会话索引展示名一致（%d 种）" % len(api_names))
        check("?" not in api_names,
              "缺模型名的调用收敛为空串，不产生假类别")
        by_variant = {}
        for s in T.STORE.sessions():
            # 多平台接入后各源有各自的模型命名（如 DSH 的 deepseek-v4 与
            # WorkBuddy 的 DeepSeek-V4.1-Flash 是不同模型），只验主源的写法变体
            if (s.get("src") or "workbuddy") != "workbuddy":
                continue
            for c in T.STORE.get(s["id"])["calls"]:
                if "deepseek" in (c.get("model") or "").lower():
                    by_variant.setdefault(c["model"], c)
        fixed = set(T.call_brief(c)["model"] for c in by_variant.values())
        check(len(by_variant) > 1 and len(fixed) == 1,
              "call_brief 把 %d 种大小写变体收敛成 1 个展示名"
              % len(by_variant))

        st2, b2 = get("/api/analytics/calls?fields=ts,model,pin&limit=2")
        check(st2 == 200 and all(set(r) == {"ts", "model", "pin"}
                                 for r in json.loads(b2)["data"]),
              "fields 投影只返回指定列")
        st3, b3 = get("/api/analytics/calls?fields=nope")
        check(st3 == 400 and json.loads(b3).get("ok") is False,
              "非法 fields 返回 400 + 错误信封，不静默忽略")
        st4, b4 = get("/api/analytics/calls?from=abc")
        check(st4 == 400, "非法 from 返回 400")
        any_date = T.analytics_calls({"limit": ["1"]})[0]["data"][0]["date"]
        st5, b5 = get("/api/analytics/calls?from=%s&to=%s&limit=500"
                      % (any_date, any_date))
        d5 = json.loads(b5)
        expected = 0
        for s in T.STORE.sessions():
            for c in T.STORE.get(s["id"])["calls"]:
                if T.fmt_ts(c.get("ts"), "%Y-%m-%d", "") == any_date:
                    expected += 1
        check(st5 == 200 and d5["meta"]["total"] == expected
              and all(r["date"] == any_date for r in d5["data"]),
              "from/to 单日过滤生效且会话级粗筛不漏算（%s：%d 条）"
              % (any_date, expected))
        st6, b6 = get("/api/analytics/calls?offset=99999999&limit=3")
        d6 = json.loads(b6)
        check(st6 == 200 and d6["data"] == []
              and d6["meta"]["total"] == d["meta"]["total"],
              "offset 越界返回空页但 total 不变")
        check("offset=3" in (d["meta"].get("next") or "")
              and "limit=3" in (d["meta"].get("next") or ""),
              "meta.next 保留分页参数")
        st7, b7 = get("/api/analytics/calls?limit=99999")
        check(st7 == 200
              and len(json.loads(b7)["data"]) <= T.ANALYTICS_MAX_LIMIT,
              "limit 超上限被夹到 %d" % T.ANALYTICS_MAX_LIMIT)

        print("== 三桶字段 / sid 精筛 / 数据新鲜度 ==")
        st_s, b_s = get("/api/sessions")
        sess_s = json.loads(b_s)["data"]
        check(all("hit" in s and "miss" in s and "rate" in s for s in sess_s)
              and all(s["miss"] == s["pin"] - s["hit"]
                      and 0 <= s["rate"] <= 1
                      and (s["rate"] == 0 if not s["pin"] else True)
                      for s in sess_s),
              "/api/sessions 每行含 hit/miss/rate，miss=pin-hit，rate∈[0,1] 且 pin=0 时为 0（%d 会话）"
              % len(sess_s))
        st_sid, b_sid = get("/api/analytics/calls?sid=%s&limit=500" % sid)
        d_sid = json.loads(b_sid)
        check(st_sid == 200 and d_sid["meta"]["total"] > 0
              and all(r["sid"] == sid for r in d_sid["data"]),
              "sid 精筛只返回该会话（total=%s）" % d_sid["meta"].get("total"))
        st_none, b_none = get("/api/analytics/calls?sid=no-such-session&limit=3")
        d_none = json.loads(b_none)
        check(st_none == 200 and d_none["data"] == [] and d_none["meta"]["total"] == 0,
              "不存在的 sid 返回空页而非 404/500")
        st_proj, b_proj = get("/api/analytics/calls?fields=is_sub,root,pin&limit=2")
        check(st_proj == 200 and all(set(r) == {"is_sub", "root", "pin"}
                                     for r in json.loads(b_proj)["data"]),
              "新增 is_sub / root 可参与 fields 投影")
        st_rf, b_rf = get("/api/analytics/calls?refresh=1&limit=2")
        m_rf = json.loads(b_rf)["meta"]
        check(st_rf == 200 and m_rf.get("refreshed") is True
              and isinstance(m_rf.get("indexed_at"), (int, float))
              and isinstance(m_rf.get("source_mtime_max"), (int, float))
              and bool(re.match(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}",
                                m_rf.get("data_as_of", ""))),
              "refresh=1 的 meta 给出 indexed_at / source_mtime_max / data_as_of / refreshed")
        check("refresh=1" not in (m_rf.get("next") or ""),
              "分页 next 不携带 refresh，翻页不会每页强制重扫")
        # 取一个小会话，避免 limit=500 截断影响口径对齐
        small = [x for x in sess_s if 0 < x["calls"] < 400]
        t_sid = next((x["id"] for x in small if x.get("is_sub")),
                     small[0]["id"] if small else sid)
        st_al, b_al = get("/api/analytics/calls?sid=%s&refresh=1&limit=500"
                          "&fields=tool,pin,hit,pout" % t_sid)
        aligned = [r for r in json.loads(b_al)["data"]
                   if r["tool"] != "最终回答" and r["pin"] > 0]
        # 源 jsonl 在测试期间仍可能增长：refresh=1 重扫后必须重取会话摘要，
        # 否则拿测试开头缓存的旧摘要对比新宽表会稳定假失败（实测同一会话
        # pin 从 1691092 涨到 1744143，重取后三个字段差值均为 0）。
        s_al = next(x for x in json.loads(get("/api/sessions")[1])["data"]
                    if x["id"] == t_sid)
        check(sum(r["pin"] for r in aligned) == s_al["pin"]
              and sum(r["hit"] for r in aligned) == s_al["hit"]
              and sum(r["pout"] for r in aligned) == s_al["pout"],
              "refresh 后宽表按同口径过滤与 /api/sessions 的 pin/hit/pout 完全一致")
        T.STORE.get(t_sid)
        had = t_sid in T.STORE._cache
        T.STORE.sessions(refresh=True)
        cleared = t_sid not in T.STORE._cache
        T.STORE.get(t_sid)
        check(had and cleared and t_sid in T.STORE._cache,
              "refresh=1 失效会话全文缓存，下次访问重新解析最新 jsonl")

        print("== 块时间戳 / 估算 token / 成本排名 ==")
        d0 = T.STORE.get(sid)
        check(all("ts" in b and "e" in b for b in d0["ctx"]),
              "解析期给每个块写入 ts 与估算 token e（%d 块）" % len(d0["ctx"]))
        check(T.G.est_tokens("你好世界") == 4
              and T.G.est_tokens("a" * 100) == 25,
              "估算口径：CJK 1 字 ≈1 token，ASCII 4 字 ≈1 token")
        check(T.block_use_counts(d0) ==
              [len(T.block_users(d0, i)) for i in range(len(d0["ctx"]))],
              "差分 block_use_counts 与逐块二分完全一致")
        hb = T.v_block({}, sid, 0)
        check("写入时间" in hb and "估算 token" in hb and "≈" in hb,
              "块全文页 KPI 显示时间戳与 ≈ 估算 token")
        cb = T.global_stats()["costly_blocks"]
        # 真实库携带块通常远超 20，演示库不足 20；只要求非空、至多 20、降序且口径自洽。
        check(0 < len(cb) <= 20
              and all(cb[i]["cost"] >= cb[i + 1]["cost"] for i in range(len(cb) - 1))
              and all(x["cost"] == x["est"] * x["users"] for x in cb)
              and all(x["carry_tokens"] == x["cost"] for x in cb),
              "最贵的块按「估算 token × 被携带次数」降序（%d 条），并给自解释别名 carry_tokens"
              % len(cb))
        gs = T.global_stats()
        check(abs(gs["cost"] - (gs["cost_fresh"] + gs["cost_cache"]
                                + gs["cost_out"])) < 1e-6
              and all(gs[k] >= 0 for k in ("cost_fresh", "cost_cache", "cost_out")),
              "成本拆成未命中 / 缓存读 / 输出三段，三段之和等于总估算（¥%.2f）"
              % gs["cost"])
        check("成本构成" in T.v_stats({}),
              "统计页 KPI 显示成本构成占比，而不只给一个总额")
        tl = gs["tools"]
        check(tl and abs(sum(v["fresh"] for v in tl.values()) - gs["miss"])
              <= max(1.0, gs["miss"] * 0.001)
              and abs(sum(v["cost"] for v in tl.values()) - gs["cost"]) < 1e-6,
              "按工具归因的未命中 / 估算之和与全局一致（%d 个工具）" % len(tl))
        check("最终回答" in tl and not any("×" in k for k in tl),
              "工具名归一：并行调用 Bash×2 并入 Bash，空工具并入「最终回答」")
        vs = T.v_stats({})
        check('<details class="attrib"' in vs and "未命中 token 归因" in vs
              and 'class="row hd"' in vs and "占未命中" in vs,
              "统计页把归因折叠成一行摘要（details），展开才是逐工具表格")

        print("== 逐块 / 逐调用排行 /rank ==")
        for what in ("block", "call"):
            st, body = get("/rank?what=%s" % what)
            check(st == 200 and b"<script" not in body and len(body) <= MAX_HTML,
                  "/rank what=%s 200 / 零脚本 / 未越 96KB" % what)
        for what, sorts in (("block", ("token", "count", "product")),
                            ("call", ("token", "in", "out"))):
            for sk in sorts:
                st, body = get("/rank?what=%s&sort=%s" % (what, sk))
                check(st == 200 and b"<script" not in body
                      and len(body) <= MAX_HTML,
                      "/rank what=%s sort=%s 200 / 零脚本 / 未越 96KB" % (what, sk))
        h = T.v_rank({"what": ["../../x"], "sort": ["<script>"], "p": ["999999"]})
        check("<script" not in h and "按块 token" in h and "对象" in h
              and 'class="pager"' in h,
              "非法 what/sort 与越界分页回落白名单，不回显注入串")
        # 每一行就是一个对象：块行数 = 全库块数，调用行数 = 全库调用数，不做聚合
        nb = sum(len(T.STORE.get(s["id"])["ctx"]) for s in T.STORE.sessions())
        nc = sum(len(T.STORE.get(s["id"])["calls"]) for s in T.STORE.sessions())
        brows = T.rank_rows("block")
        crows = T.rank_rows("call")
        check(len(brows) == nb and len(crows) == nc,
              "逐块 / 逐调用行数与全库对象数一致（%d 块 / %d 次）" % (nb, nc))
        check(all(r[4] >= 0 and r[5] >= 0 for r in brows)
              and all(r[2]["pin"] >= 0 and r[2]["pout"] >= 0 for r in crows),
              "块 token / 携带次数、调用输入 / 输出均非负")
        for what, sorts, rows in (("block", ("token", "count", "product"), brows),
                                  ("call", ("token", "in", "out"), crows)):
            for sk in sorts:
                rr = sorted(rows, key=T.rank_sort_key(what, sk))
                vals = [T.rank_metric(what, sk, r) for r in rr]
                check(all(vals[i] >= vals[i + 1] for i in range(len(vals) - 1)),
                      "%s 按 %s 排序结果单调不增（%d 行）" % (what, sk, len(rr)))
                ra = sorted(rows, key=T.rank_sort_key(what, sk, "asc"))
                vals = [T.rank_metric(what, sk, r) for r in ra]
                check(all(vals[i] <= vals[i + 1] for i in range(len(vals) - 1)),
                      "%s 按 %s 正序结果单调不减（%d 行）" % (what, sk, len(ra)))
        # 筛选：种类 / 时间 / 任意组合
        kk = next((x for x in T.ORDER if any(r[3]["k"] == x for r in brows)),
                  brows[0][3]["k"])
        f1 = T.rank_filter("block", brows, kk, "", "")
        check(len(f1) > 0 and all(r[3]["k"] == kk for r in f1),
              "块种类筛选只保留该类型（%s，%d 行）" % (kk, len(f1)))
        day = next((T.G.day_key(r[3].get("ts")) for r in brows
                    if T.G.day_key(r[3].get("ts"))), "")
        f2 = T.rank_filter("block", brows, "", day, day)
        check(len(f2) > 0 and all(T.G.day_key(r[3].get("ts")) == day for r in f2),
              "块时间筛选只保留该日期（%s，%d 行）" % (day, len(f2)))
        f3 = T.rank_filter("block", brows, kk, day, day)
        check(all(r[3]["k"] == kk and T.G.day_key(r[3].get("ts")) == day
                  for r in f3),
              "块种类 + 时间组合筛选（%d 行）" % len(f3))
        mk = T.G.canon_model(crows[0][2]["model"])
        cday = next((T.G.day_key(r[2].get("ts")) for r in crows
                     if T.G.canon_model(r[2]["model"]) == mk
                     and T.G.day_key(r[2].get("ts"))), "")
        f4 = T.rank_filter("call", crows, mk, "", "")
        check(len(f4) > 0 and all(T.G.canon_model(r[2]["model"]) == mk
                                  for r in f4),
              "调用模型筛选（%s，%d 行）" % (mk, len(f4)))
        f5 = T.rank_filter("call", crows, mk, cday, cday)
        check(bool(cday) and len(f5) > 0
              and all(T.G.canon_model(r[2]["model"]) == mk
                      and T.G.day_key(r[2].get("ts")) == cday for r in f5),
              "调用模型 + 时间组合筛选（%d 行）" % len(f5))
        hf = T.v_rank({"what": ["block"], "k": [kk], "from": ["2026-13-99"],
                       "to": ["<script>"], "p": ["999999"]})
        check("<script" not in hf and "全部种类" in hf and 'class="on"' in hf
              and 'class="datepick"' in hf and 'name="from" value=""' in hf
              and 'value="%s"' % kk in hf,
              "筛选表单回显种类、非法日期被忽略，日期改自绘日历")
        check('<details class="pick">' in hf and '<select name=' not in hf,
              "种类筛选用自绘 details 菜单，不再出现原生 select")
        hc = T.v_rank({"what": ["call"], "k": [mk], "from": [day], "to": [day]})
        check("全部模型" in hc and 'class="on"' in hc and day in hc,
              "调用筛选表单回显模型与日期")
        hnone = T.v_rank({"what": ["block"], "k": [kk], "from": ["2099-01-01"],
                          "to": ["2099-12-31"]})
        check("没有匹配当前筛选条件" in hnone,
              "空结果给出提示而不是空白表格")
        # 原生控件统一：搜索范围改 radio、color-scheme 跟随主题、日历改自绘
        st, sbody = get("/search?q=token")
        check(st == 200 and b'name="scope"' in sbody
              and b'<select name="scope"' not in sbody,
              "检索范围用 radio 分段控件，不再出现原生 select")
        check(b"color-scheme:light" in sbody and b"color-scheme:dark" in sbody,
              "页面声明 color-scheme，原生控件跟随明暗主题")
        st, rbody = get("/rank?what=block")
        check(st == 200 and b'class="datepick"' in rbody
              and b'class="calgrid"' in rbody and b'type="date"' not in rbody,
              "日期改自绘日历，页面不再有原生 date 弹层")
        hcal = T.v_rank({"what": ["block"], "k": [kk], "from": ["2026-11-01"],
                         "to": ["2026-11-30"], "cal": ["2026-10"],
                         "calfield": ["from"]})
        check("2026 年 10 月" in hcal and 'class="datepick" open' in hcal
              and "calfield=from" in hcal and "cal=2026-09" in hcal
              and "cal=2026-11" in hcal
              and "from=2026-11-01" in hcal and "to=2026-11-30" in hcal
              and "k=" + kk in hcal,
              "自绘日历翻月保持展开，日期链接保留全部筛选参数")
        check("筛选后块数" not in T.v_rank({"what": ["block"],
                                          "cal": ["2026-10"],
                                          "calfield": ["from"]}),
              "日历翻页参数不参与筛选口径")
        # 种类多选：逗号分隔多值是并集；下拉里逐项点选 / 取消，状态跟着 URL 回显
        brows = T.rank_sorted("block", "token")
        n_u = len(T.rank_filter("block", brows, "user", "", ""))
        n_t = len(T.rank_filter("block", brows, "tool_result", "", ""))
        n_both = len(T.rank_filter("block", brows, "user,tool_result", "", ""))
        hmulti = T.v_rank({"what": ["block"], "k": ["user,tool_result"]})
        check(n_both == n_u + n_t and "已选 2 类" in hmulti
              and 'name="k" value="tool_result,user"' in hmulti,
              "种类可多选组合，多值是并集且按规范顺序回显到表单")
        # 默认排除压缩摘要块，cx=1 才纳入
        hdef = T.v_rank({"what": ["block"]})
        hcx = T.v_rank({"what": ["block"], "cx": ["1"]})
        check("显示压缩摘要" in hdef and 'name="cx" value="1"' not in hdef
              and hdef.count(">压缩摘要</b>") == 0,
              "排行默认不纳入压缩摘要块")
        check("隐藏压缩摘要" in hcx and 'name="cx" value="1"' in hcx
              and hcx.count(">压缩摘要</b>") > 0,
              "cx=1 可把压缩摘要块纳入排行")
        # 正序：标签带 ↑、翻页与筛选表单都保留 order=asc，零用量行压到末尾
        ha = T.v_rank({"what": ["call"], "sort": ["token"], "order": ["asc"]})
        nzero = sum(1 for r in crows if r[2]["pin"] + r[2]["pout"] <= 0)
        check("按合计 ↑" in ha and "order=asc" in ha
              and 'name="order" value="asc"' in ha,
              "排行页正序：标签带 ↑，翻页与筛选表单都保留 order=asc")
        hd = T.v_rank({"what": ["call"], "sort": ["token"]})
        check("按合计 ↓" in hd and 'name="order"' not in hd
              and "排到了末尾" not in hd,
              "排行页默认仍是降序，URL 不回显 order")
        if nzero:
            m0 = re.search(r'<span class="num credit">([\d,]+)</span>', ha)
            check(bool(m0) and int(m0.group(1).replace(",", "")) > 0
                  and "排到了末尾" in ha,
                  "正序首页首行不是 0：%d 行零用量对象已压到末尾" % nzero)
        # 点当前排序标签翻转方向，点其他标签只换字段并保留方向
        s_tabs = re.search(r"<b>排序</b>(.*?)</div>", ha).group(1)
        links = re.findall(r'href="([^"]+)"', s_tabs)
        check(any("sort=token" in x and "order=asc" not in x for x in links)
              and all("order=asc" in x for x in links if "sort=token" not in x),
              "正序下点当前标签翻回降序，点其他标签保留 order=asc")
        # 块类型标签按类别配色，五类各一色
        check(b".chip.bk0" in rbody and b".chip.bk4" in rbody
              and b'class="chip t bk' in rbody,
              "块类型标签按类别配不同颜色")
        st, stats_body = get("/stats")
        check(b".viz2{display:grid;grid-template-columns:1fr 1fr;gap:14px;align-items:stretch" in stats_body,
              "全局统计两张卡片等高对齐，不再一高一低")
        hm_rule = re.search(rb"\.hm\{[^}]*\}", stats_body)
        check(st == 200 and hm_rule is not None
              and b"overflow" not in hm_rule.group(0)
              and b"grid-template-rows:repeat(7,auto)" in hm_rule.group(0)
              and b"grid-template-columns:18px repeat(var(--hmc)" in hm_rule.group(0),
              "热力图容器不裁切气泡，显式 7 行且首列留给星期标签")
        # 色阶不能再用 opacity：它会把格子里的 ::after 气泡一起带淡
        check(b"color-mix(in srgb,var(--accent) 26%,var(--panel))" in stats_body
              and b"opacity:.3}.hm i.h2" not in stats_body,
              "色阶用 color-mix 混色，气泡不被格子 opacity 带淡")
        check(b"width:max-content" in stats_body,
              "气泡宽度用 max-content，不被小格子的包含块压成竖条")
        check(b"--ease-in:cubic-bezier(.4,0,.2,1)" in stats_body
              and b"::view-transition-new(root){animation:vt-in .46s" in stats_body,
              "入场用均衡缓动，跨页过渡只做透明度")
        # 性能：索引增量复用 + 已排序缓存
        a1 = G.scan_all()
        sig1 = G._SCAN_LAST["sig"]
        a2 = G.scan_all()
        sig2 = G._SCAN_LAST["sig"]
        # WorkBuddy 会实时追加 jsonl / 更新 sessions 表，两次扫描之间全库签名
        # 变化属正常：只在签名未变时才要求复用同一对象，避免实时写入造成假失败。
        check(a2 is a1 or sig2 != sig1,
              "scan_all 数据未变时复用同一索引对象（下游缓存继续命中）")
        a3 = G.scan_all(force=True)
        check(a3 is not a1 and (len(a3) == len(a1)
                                or len(G._SCAN_LAST["sig"]) != len(sig1)),
              "scan_all(force=True) 强制重建列表对象但复用文件摘要")
        s1 = T.rank_sorted("block", "product")
        s2 = T.rank_sorted("block", "product")
        check(s2 is s1, "rank_sorted 重复调用命中已排序缓存")
        ff = T.rank_filter("block", s1, "tool_result", "", "")
        expect = [r for r in s1 if r[3]["k"] == "tool_result"]
        check(len(ff) == len(expect) and all(a is b for a, b in zip(ff, expect)),
              "筛选结果保持已排序顺序，不重新排序")
        before = T.rank_rows("block")
        T.STORE.sessions(refresh=True)
        after = T.rank_rows("block")
        check(after is not before and len(after) > 0,
              "索引刷新后逐块排行重算，缓存不误命中复用的 id")
        hj = T.v_rank({"what": ["<script>"], "sort": ["y"], "p": ["1"]})
        check("%3Cscript" not in hj and "what=block" in hj and "sort=" in hj,
              "分页链接改用白名单值，不继续传播原始非法参数")

        print("== 外部源缓存：WAL 追加必须让 Antigravity 重新解析 ==")
        import sqlite3
        import tempfile
        import shutil as _shutil
        from sources import antigravity as _ag

        def _pv(n):
            out = bytearray()
            while True:
                b = n & 0x7f
                n >>= 7
                out.append(b | (0x80 if n else 0))
                if not n:
                    return bytes(out)

        def _pbuf(fld, payload):
            return _pv(fld << 3 | 2) + _pv(len(payload)) + payload

        def _pint(fld, value):
            return _pv(fld << 3) + _pv(value)

        _blob = _pbuf(1, _pbuf(4, _pint(2, 100) + _pint(3, 10))
                      + _pbuf(19, b"test-model"))
        _home = tempfile.mkdtemp(prefix="tt-selftest-agwal-")
        try:
            _cdir = os.path.join(_home, "conversations")
            os.makedirs(_cdir)
            _db = os.path.join(_cdir, "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee.db")
            _con = sqlite3.connect(_db)
            _con.execute("pragma journal_mode=wal")
            _con.execute("create table gen_metadata(idx integer primary key, data blob)")
            _con.execute("create table steps(idx integer, step_type integer, metadata blob, step_payload blob)")
            _con.execute("insert into gen_metadata values(1, ?)", (_blob,))
            _con.commit()
            _n0 = sum(s["calls"] for s in _ag.scan(home=_home))
            _con.execute("insert into gen_metadata values(2, ?)", (_blob,))
            _con.commit()
            _n1 = sum(s["calls"] for s in _ag.scan(home=_home))
            check(_n0 == 1 and _n1 == 2,
                  "WAL 追加后 scan() 看见新调用，不再只认主库签名（%d -> %d）" % (_n0, _n1))
            _con.close()
        finally:
            _shutil.rmtree(_home, ignore_errors=True)

        print("== 运行期零写盘 ==")
        check(not os.path.isdir(os.path.join(ROOT, "__pycache__")),
              "项目目录无 __pycache__")
        st, body = get("/api/health")
        check(json.loads(body)["data"].get("write_files") is False,
              "/api/health 自报 write_files=false")
        hv = json.loads(body)["data"]
        check(hv.get("version") == T.APP_VERSION
              and (hv.get("update") or {}).get("state") == "off"
              and (hv.get("update") or {}).get("auto") is False,
              "/api/health 暴露版本号与自动检查开关（v%s / %s，--no-update-check 生效）"
              % (hv.get("version"), (hv.get("update") or {}).get("state")))
        st_chk, hd_chk, _ = get_noredirect(
            "/check", headers={"Referer": BASE + "/stats?x=1"})
        loc_chk = hd_chk.get("Location", "")
        check(st_chk == 303 and loc_chk == "/stats?x=1&_upd=1#upd-result",
              "/check 查完 303 回来源页带 #upd-result，不渲染独立结果页（%s %s）"
              % (st_chk, loc_chk))
        st_evil, hd_evil, _ = get_noredirect(
            "/check", headers={"Referer": "https://evil.example/x"})
        check(st_evil == 303 and hd_evil.get("Location") == "/?_upd=1#upd-result",
              "外站 Referer 一律回首页，不给开放跳转留口子")
        st_dl, hd_dl, _ = get_noredirect(
            "/download", headers={"Referer": BASE + "/stats"})
        check(st_dl == 303 and hd_dl.get("Location") == "/stats?_upd=1#upd-result",
              "/download 同源校验后 303 回原页；--no-update-check 下不联网")
        st_back, b_back = get("/?_upd=1")
        check(st_back == 200 and b'class="boot"' not in b_back,
              "带 _upd=1 回跳的首页跳过首屏加载动画，直接展示检查结果")
        st_stop, b_stop = get("/shutdown", method="POST")
        check(st_stop == 200 and "已停止".encode("utf-8") in b_stop,
              "页面「停止服务」POST 返回停止回执并关停进程")
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
        if demo_dir and os.path.isdir(demo_dir):
            import shutil
            shutil.rmtree(demo_dir, ignore_errors=True)


if __name__ == "__main__":
    main()
    bad = RESULTS.count(False)
    print("")
    print("共 %d 项检查，失败 %d 项" % (len(RESULTS), bad))
    sys.exit(1 if bad else 0)
