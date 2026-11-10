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
            # 同机工具互链是 127.0.0.1 的外链，不属于「依赖外部资源」；
            # 只把 scheme 后面的主机抹掉再扫，避免把 loopback 误判成 CDN。
            scan = body.replace(b"http://127.0.0.1", b"loopback") \
                       .replace(b"http://localhost", b"loopback")
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
            check("60 个会话 · 90.0 分" in p0 and "本页 40" in p0,
                  "分页第 1 页：分组标题用全量（60 个会话 · 90.0 分 · 本页 40）")
            check("60 个会话 · 90.0 分" in p1 and "本页 20" in p1,
                  "分页第 2 页：标题口径一致（60 个会话 · 90.0 分 · 本页 20）")
        finally:
            T.STORE = orig_store

        print("== 全库画像：模型残差桶 ==")
        fake_one = [{"id": "a", "ws": "D:/x/y", "title": "t", "model": "m1",
                     "models": {"m1": 60}, "mtime": time.time(), "calls": 1, "turns": 1,
                     "pin": 100, "pout": 10, "credit": 0.1}]
        ov = T.index_overview(fake_one)
        pcts = [float(x) for x in re.findall(r"<em>([\d.]+)%</em>", ov)]
        check("未知模型" in ov and abs(sum(pcts) - 100) < 0.15,
              "models 之和小于 pin 时补「未知模型」残差，图例合计 %.1f%%" % sum(pcts))

        print("== 全库画像：模型名变体归一 ==")
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
        fake_vars = [{"id": "b", "ws": "D:/x/y", "title": "t",
                      "model": "DeepSeek-V4.1-Flash",
                      "models": {"DeepSeek-V4.1-Flash": 500,
                                 "Deepseek-V4.1-Flash": 200,
                                 "deepseek-v4.1-flash": 50,
                                 "Hy4": 200, "Hy4 preview": 50},
                      "mtime": time.time(), "calls": 5, "turns": 3,
                      "pin": 1000, "pout": 10, "credit": 0.1}]
        ov2 = T.index_overview(fake_vars)
        names = re.findall(r'<span class="g">(.*?)</span>', ov2)
        ds = [n for n in names if "flash" in n.lower()]
        check(len(ds) == 1, "首页构成环里同一模型只出现 1 项（%s）" % ds)
        check(any(n.strip() == "Hy4" for n in names)
              and any("Hy4 preview" in n for n in names),
              "归一不会把 Hy4 与 Hy4 preview 误并")
        pcts2 = [float(x) for x in re.findall(r"<em>([\d.]+)%</em>", ov2)]
        check(abs(sum(pcts2) - 100) < 0.15,
              "归一后图例合计仍为 100%%（%.1f%%）" % sum(pcts2))
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
        check(re.search(r"\d+ 个 parent 目录 · \d+ 个工作区 · \d+ 个会话", ov),
              "递归关系摘要含 parent 目录 / 工作区 / 会话三项")

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
                  "子树仍是零脚本且未越过 80 KB")
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
            # 清空会话缓存：第一次渲染必须把全部会话各解析一遍（且只一遍），
            # 第二次应当全部命中缓存 —— 只断言 cap 大小不足以证明这一点。
            with T.STORE.lock:
                T.STORE._cache.clear()
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
              and len(d["meta"]["fields"]) == 14,
              "meta 声明毫秒时间戳单位与 14 列字段字典")
        check(all(set(r) == set(T.ANALYTICS_FIELDS) for r in d["data"]),
              "默认每行字段完整（14 列宽表）")

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
        check(len(cb) == 20
              and all(cb[i]["cost"] >= cb[i + 1]["cost"] for i in range(19))
              and all(x["cost"] == x["est"] * x["users"] for x in cb),
              "最贵的块按「估算 token × 被携带次数」降序（20 条）")

        print("== 逐块 / 逐调用排行 /rank ==")
        for what in ("block", "call"):
            st, body = get("/rank?what=%s" % what)
            check(st == 200 and b"<script" not in body and len(body) <= MAX_HTML,
                  "/rank what=%s 200 / 零脚本 / 未越 80KB" % what)
        for what, sorts in (("block", ("token", "count", "product")),
                            ("call", ("token", "in", "out"))):
            for sk in sorts:
                st, body = get("/rank?what=%s&sort=%s" % (what, sk))
                check(st == 200 and b"<script" not in body
                      and len(body) <= MAX_HTML,
                      "/rank what=%s sort=%s 200 / 零脚本 / 未越 80KB" % (what, sk))
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
        a2 = G.scan_all()
        check(a2 is a1,
              "scan_all 数据未变时复用同一索引对象（下游缓存继续命中）")
        a3 = G.scan_all(force=True)
        check(a3 is not a1 and len(a3) == len(a1),
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
