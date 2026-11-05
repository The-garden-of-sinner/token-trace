#!/usr/bin/env python3
# trace_server.py —— token-trace 的完整模式服务端
#
# 目标：内容 100% 完整，但浏览器每次只渲染一屏。
# 做法：把「展开」变成一次后端请求 —— 翻页、看某次调用、看某个块的全文，
#       全部由服务端查数据、算缓存边界、渲染 HTML，返回几 KB 的一页。
#       零 JavaScript：核心路径全是 <a href>，在禁用脚本的托管预览里也能用。
#
# 启动： python3 trace_server.py [--port 8765] [--host 127.0.0.1]
# 数据： 只读 ~/.workbuddy/projects/**/*.jsonl 与 workbuddy.db，不写任何源文件。
#
# 依赖：Python 3.8+，仅标准库；复用 gen_trace.py 的解析逻辑。

import argparse
import bisect
import hmac
import html
import json
import os
import re
import sys
import threading
import time
import traceback
from collections import Counter, OrderedDict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlencode, urlsplit

# 主脚本 import gen_trace 时 Python 默认会写 __pycache__/*.pyc。
# 服务承诺「运行期零写盘」，在 import 之前就关掉字节码落盘（代价仅几毫秒重编译）。
sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import gen_trace as G  # noqa: E402  复用 build / scan_all / brief / cmd_html

# 每屏渲染上限 —— 调大只会让单页变重，不会让内容变多
PAGE_TURNS = 20        # 会话页每页轮数
PAGE_CALLS = 40        # 调用列表每页条数
PAGE_BLOCKS = 40       # 单次调用的上下文块列表每页条数
PART_CHARS = 12000     # 全文每片字数（约 30 KB HTML）
SEARCH_MAX = 80        # 搜索结果上限
CACHE_SESSIONS = 32    # 内存保留几个已解析会话：实测全量 28 个仅 83 MB，默认全缓存
API_MAX_LIMIT = 500    # /api/ 单次返回条数上限
MAX_CHARS = 10_000_000  # 块/输出全文的字符 offset 上限，与 limit 同一量级

TYPES = {"user": "你的输入", "injected": "系统注入", "tool_result": "工具结果",
         "tool_call": "工具调用参数", "assistant": "助手回答"}
ORDER = ["tool_result", "injected", "user", "tool_call", "assistant"]
PALETTE = {"tool_result": "#888780", "injected": "#EF9F27", "user": "#378ADD",
           "tool_call": "#B4B2A9", "assistant": "#1D9E75"}
SID_RE = re.compile(r"^[0-9A-Za-z_-]+$")
TOKEN = ""          # --token / TRACE_TOKEN；非空时所有请求都要鉴权
LOOPBACK = {"127.0.0.1", "localhost", "::1", "[::1]"}


def esc(s):
    return html.escape("" if s is None else str(s), quote=True)


def nfmt(n):
    return "{:,}".format(int(n or 0))


def cut(s, n):
    s = s or ""
    return s if len(s) <= n else s[:n] + "…"


# ---------------------------------------------------------------- 数据层

class Store(object):
    """会话索引 + 按需解析 + LRU 缓存。

    索引常驻；单个会话全文只在第一次访问时解析（最大会话 0.7 s）。
    实测 28 个会话全部常驻仅 83 MB RSS，所以默认全缓存，翻页不再重复解析；
    会话特别多时可用 --cache 调小，走 LRU 淘汰。
    """

    def __init__(self, cap=CACHE_SESSIONS):
        self.cap = cap
        self.lock = threading.RLock()
        self._index = None
        self._index_at = 0.0
        self._cache = OrderedDict()
        self._loading = {}          # sid -> Event，防止并发首访重复解析

    def sessions(self, refresh=False):
        with self.lock:
            if refresh or self._index is None or time.time() - self._index_at > 60:
                self._index = G.scan_all()
                self._index_at = time.time()
            return self._index

    def by_id(self, sid):
        if not SID_RE.match(sid or ""):
            raise KeyError("bad session id")
        for s in self.sessions():
            if s["id"] == sid:
                return s
        raise KeyError(sid)

    def get(self, sid):
        with self.lock:
            if sid in self._cache:
                d = self._cache.pop(sid)
                self._cache[sid] = d
                return d
            ev = self._loading.get(sid)
            owner = ev is None
            if owner:
                ev = threading.Event()
                self._loading[sid] = ev
        if not owner:
            ev.wait()
            with self.lock:
                if sid in self._cache:
                    return self._cache[sid]
            return self.get(sid)      # 缓存已被淘汰或解析失败，重新走一遍
        try:
            s = self.by_id(sid)
            t0 = time.time()
            ctx, calls, meta, turns = G.build(s["path"], full=True)
            d = self._derive(s, ctx, calls, meta, turns)
            d["parse_ms"] = int((time.time() - t0) * 1000)
            with self.lock:
                self._cache[sid] = d
                while len(self._cache) > self.cap:
                    self._cache.popitem(last=False)
            return d
        finally:
            with self.lock:
                self._loading.pop(sid, None)
            ev.set()

    @staticmethod
    def _derive(s, ctx, calls, meta, turns):
        # 前缀和：pref[i] = 前 i 块的累计字数，O(1) 求任意区间的可见字数
        pref = [0]
        for b in ctx:
            pref.append(pref[-1] + b["n"])
        ctxns = [c["ctxN"] for c in calls]

        by_turn = {}
        for i, c in enumerate(calls):
            by_turn.setdefault(c.get("turn", 0), []).append(i)

        # 缓存冷启动：上一次 >80% 命中、这一次 <50%
        cold = []
        prev = None
        for i, c in enumerate(calls):
            if not c["pin"]:
                continue
            r = c["hit"] / c["pin"]
            if prev is not None and prev > 0.8 and r < 0.5:
                cold.append(i)
            prev = r

        billed = [c for c in calls if c["pin"]]
        tin = sum(c["pin"] for c in billed)
        thit = sum(c["hit"] for c in billed)
        tcredit = sum(c.get("credit") or 0 for c in calls)
        sz = Counter()
        for b in ctx:
            sz[b["k"]] += b["n"]

        return {
            "s": s, "ctx": ctx, "calls": calls, "meta": meta, "turns": turns,
            "pref": pref, "ctxns": ctxns, "by_turn": by_turn, "cold": cold,
            "billed": billed,
            "title": meta.get("aiTitle") or s.get("title") or s["id"][:8],
            "stats": {
                "turns": len(turns), "calls": len(calls), "blocks": len(ctx),
                "chars": pref[-1], "pin": tin, "hit": thit,
                "miss": tin - thit, "pout": sum(c["pout"] for c in calls),
                "credit": tcredit, "rate": (thit / tin) if tin else 0,
                "model": (billed[0]["model"] if billed else "?"),
                "compose": {k: sz[k] for k in ORDER},
                "cold": len(cold),
                "skipped_lines": meta.get("_skipped_lines", 0),
            },
        }


def rss_mb():
    """进程常驻内存（MB）。Linux 读 /proc，Windows 用 ctypes，其他平台返回 None。"""
    try:
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes

            class PMC(ctypes.Structure):
                _fields_ = [("cb", wintypes.DWORD),
                            ("PageFaultCount", wintypes.DWORD),
                            ("PeakWorkingSetSize", ctypes.c_size_t),
                            ("WorkingSetSize", ctypes.c_size_t),
                            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                            ("QuotaPagedPoolUsage", ctypes.c_size_t),
                            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                            ("PagefileUsage", ctypes.c_size_t),
                            ("PeakPagefileUsage", ctypes.c_size_t)]

            m = PMC()
            m.cb = ctypes.sizeof(m)
            ctypes.windll.psapi.GetProcessMemoryInfo(
                ctypes.windll.kernel32.GetCurrentProcess(), ctypes.byref(m), m.cb)
            return round(m.WorkingSetSize / 1048576.0, 1)
        with open("/proc/self/status", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("VmRSS:"):
                    return round(int(line.split()[1]) / 1024.0, 1)
    except (OSError, AttributeError, ValueError):
        pass
    return None


STORE = Store()
CALC_LOCK = threading.RLock()   # stats / search 这类全库遍历串行，避免与 LRU 互相驱逐
START_TS = time.time()


def call_state(d, seq, i):
    """第 seq 次调用里第 i 个块的状态 —— O(1)，不展开整个上下文"""
    c = d["calls"][seq]
    n = min(c["ctxN"], len(d["ctx"]))
    if i >= n:
        return None
    if not c["pin"]:
        return "none"
    cut_chars = d["pref"][n] * c["hit"] / c["pin"]
    st, acc = d["pref"][i], d["pref"][i + 1]
    if acc <= cut_chars:
        return "hit"
    if st >= cut_chars:
        return "miss"
    return "part"


def call_outline(d, seq):
    """单次调用的上下文概要（不取正文）"""
    c = d["calls"][seq]
    n = min(c["ctxN"], len(d["ctx"]))
    total = d["pref"][n]
    if not c["pin"]:
        return {"n": n, "chars": total, "ratio": None, "cut": None,
                "boundary": None, "hit_blocks": 0, "miss_blocks": 0}
    ratio = c["hit"] / c["pin"]
    cut_chars = total * ratio
    b = bisect.bisect_left(d["pref"], cut_chars, 0, n + 1)
    boundary = max(0, min(n - 1, b - 1)) if n else None
    # 精确计数（O(n) 的位运算级别，可接受；列表仍按页渲染）
    hit_blocks = sum(1 for i in range(n) if call_state(d, seq, i) == "hit")
    return {"n": n, "chars": total, "ratio": ratio, "cut": cut_chars,
            "boundary": boundary, "hit_blocks": hit_blocks,
            "miss_blocks": n - hit_blocks}


def block_users(d, idx):
    """哪些调用带上了第 idx 块。ctxN 单调不减，二分即可"""
    return range(bisect.bisect_right(d["ctxns"], idx), len(d["calls"]))


def call_brief(c):
    """调用列表用的轻量字典，不含上下文与输出正文"""
    r = (c["hit"] / c["pin"]) if c["pin"] else None
    return {"seq": c["seq"], "ts": c["ts"], "model": c["model"], "tool": c["tool"],
            "pin": c["pin"], "hit": c["hit"], "miss": c["pin"] - c["hit"],
            "pout": c["pout"], "credit": c.get("credit") or 0, "turn": c.get("turn", 0),
            "rate": r, "ctxN": c["ctxN"], "outputs": len(c["out"])}


def turn_brief(d, t):
    cs = [d["calls"][i] for i in d["by_turn"].get(t["i"], [])]
    pin = sum(c["pin"] for c in cs)
    hit = sum(c["hit"] for c in cs)
    first = next((c for c in cs if c["pin"]), None)
    return {"i": t["i"], "q": t["q"], "calls": len(cs), "pin": pin, "hit": hit,
            "rate": (hit / pin) if pin else 0,
            "pout": sum(c["pout"] for c in cs),
            "credit": sum(c["credit"] for c in cs),
            "inj": t.get("inj") or 0,
            "cold": bool(first and (first["hit"] / first["pin"]) < 0.5)}


def global_stats():
    ss = STORE.sessions()
    out = {"sessions": len(ss), "workspaces": len(set(s["ws"] for s in ss)),
           "calls": 0, "turns": 0, "blocks": 0, "chars": 0,
           "pin": 0, "hit": 0, "pout": 0, "credit": 0, "compose": Counter(),
           "cold": [], "top_calls": [], "top_blocks": []}
    for s in ss:
        d = STORE.get(s["id"])
        st = d["stats"]
        for k in ("calls", "turns", "blocks", "chars", "pin", "hit", "pout", "credit"):
            out[k] += st[k]
        out["compose"].update(st["compose"])
        for i in d["cold"]:
            c = d["calls"][i]
            out["cold"].append({"sid": s["id"], "ws": s["ws"], "seq": c["seq"],
                                "ts": c["ts"], "pin": c["pin"], "hit": c["hit"],
                                "rate": c["hit"] / c["pin"], "credit": c["credit"]})
        for i, c in sorted(enumerate(d["calls"]), key=lambda x: -(x[1]["credit"]))[:5]:
            out["top_calls"].append({"sid": s["id"], "ws": s["ws"], "seq": c["seq"],
                                     "credit": c["credit"], "pin": c["pin"],
                                     "rate": (c["hit"] / c["pin"]) if c["pin"] else 0,
                                     "tool": c["tool"]})
        for i, b in sorted(enumerate(d["ctx"]), key=lambda x: -x[1]["n"])[:3]:
            out["top_blocks"].append({"sid": s["id"], "idx": i, "chars": b["n"],
                                      "kind": b["k"], "label": b["l"]})
    out["rate"] = (out["hit"] / out["pin"]) if out["pin"] else 0
    out["miss"] = out["pin"] - out["hit"]
    # 实测单价：积分 / 未命中输入
    billed_credit = out["credit"]
    out["unit_miss"] = (billed_credit / out["miss"] * 10000) if out["miss"] else 0
    out["cold"].sort(key=lambda x: -x["pin"])
    out["top_calls"].sort(key=lambda x: -x["credit"])
    out["top_calls"] = out["top_calls"][:20]
    out["top_blocks"].sort(key=lambda x: -x["chars"])
    out["top_blocks"] = out["top_blocks"][:20]
    out["compose"] = dict(out["compose"])
    return out


def search(q, sid=None, scope="all", limit=SEARCH_MAX):
    """跨会话全文搜索。用 re 直接在原文上扫，不复制 940 万字的小写副本。"""
    q = (q or "").strip()
    if len(q) < 2:
        return [], "关键词至少 2 个字"
    if len(q) > 200:
        return [], "关键词过长"
    if scope not in ("all", "blocks", "outputs", "turns"):
        return [], "scope 只能是 all / blocks / outputs / turns"
    rx = re.compile(re.escape(q), re.I)
    hits = []
    truncated = False
    targets = [STORE.by_id(sid)] if sid else STORE.sessions()
    for s in targets:
        d = STORE.get(s["id"])
        if scope in ("all", "blocks"):
            for i, b in enumerate(d["ctx"]):
                m = rx.search(b["t"])
                if not m:
                    continue
                lo, hi = max(0, m.start() - 60), min(len(b["t"]), m.end() + 90)
                hits.append({"kind": "block", "sid": s["id"], "ws": s["ws"],
                             "idx": i, "label": b["l"], "btype": b["k"],
                             "chars": b["n"], "pos": m.start(),
                             "snippet": b["t"][lo:hi],
                             "href": "/s/%s/b/%d" % (s["id"], i)})
                if len(hits) >= limit:
                    truncated = True
                    break
        if truncated:
            break
        if scope in ("all", "outputs"):
            for seq, c in enumerate(d["calls"]):
                for k, o in enumerate(c["out"]):
                    m = rx.search(o["text"])
                    if not m:
                        continue
                    lo, hi = max(0, m.start() - 60), min(len(o["text"]), m.end() + 90)
                    hits.append({"kind": "output", "sid": s["id"], "ws": s["ws"],
                                 "seq": c["seq"], "label": o["label"] + " · #%d" % c["seq"],
                                 "chars": len(o["text"]), "pos": m.start(),
                                 "snippet": o["text"][lo:hi],
                                 "href": "/s/%s/o/%d?seg=%d" % (s["id"], seq + 1, k)})
                    if len(hits) >= limit:
                        truncated = True
                        break
                if truncated:
                    break
        if truncated:
            break
        if scope in ("all", "turns"):
            for t in d["turns"]:
                if not rx.search(t["q"]):
                    continue
                hits.append({"kind": "turn", "sid": s["id"], "ws": s["ws"],
                             "idx": t["i"], "label": "轮 %d" % t["i"],
                             "chars": len(t["q"]), "pos": 0, "snippet": t["q"],
                             "href": "/s/%s/t/%d" % (s["id"], t["i"])})
                if len(hits) >= limit:
                    truncated = True
                    break
        if truncated:
            break
    return hits, ("结果超过 %d 条，已截断；缩小范围或换更具体的词" % limit) if truncated else ""


# ---------------------------------------------------------------- HTML 外观

CSS = """
*{box-sizing:border-box}
body{margin:0;padding:0 0 50px;font:14px/1.65 system-ui,-apple-system,"Noto Sans CJK SC",sans-serif;color:#2C2C2A;background:#fff}
.wrap{max-width:1120px;margin:0 auto;padding:0 26px}
header{background:#F7F6F2;border-bottom:1px solid #E3E1D9;padding:16px 0 13px;margin-bottom:20px}
.crumb{font-size:11.5px;color:#888780;margin-bottom:5px}
.crumb a{color:#378ADD;text-decoration:none}
.crumb a:hover{text-decoration:underline}
.crumb .sep{color:#B4B2A9;margin:0 6px}
h1{font-size:19px;font-weight:500;margin:0 0 3px}
.sub{color:#5F5E5A;font-size:12px}
.badge{display:inline-block;font-size:10.5px;color:#0F6E56;background:#E1F5EE;border-radius:9px;padding:0 8px;line-height:17px;margin-left:8px;vertical-align:2px}
h2{font-size:13px;font-weight:500;margin:24px 0 9px;padding-bottom:6px;border-bottom:1px solid #E3E1D9;color:#444441}
h2 small{font-weight:400;color:#B4B2A9;font-size:11.5px;margin-left:8px}
h3{font-size:12.5px;font-weight:500;margin:18px 0 7px;color:#444441}
.kpi{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:16px}
.kpi div{background:#F1EFE8;border-radius:9px;padding:9px 13px;min-width:112px}
.kpi b{display:block;font-size:18px;font-weight:500;line-height:1.35;font-variant-numeric:tabular-nums}
.kpi span{font-size:11px;color:#5F5E5A}
.compose{display:flex;height:19px;border-radius:5px;overflow:hidden;background:#F1EFE8;margin-bottom:6px}
.compose i{display:block;height:100%}
.legend{font-size:11.5px;color:#5F5E5A;margin-bottom:14px;display:flex;gap:13px;flex-wrap:wrap}
.legend span{display:inline-flex;align-items:center;gap:5px}
.legend i{display:inline-block;width:9px;height:9px;border-radius:2px}
.note{background:#E6F1FB;border-left:2px solid #378ADD;padding:9px 13px;border-radius:0 7px 7px 0;font-size:12px;color:#0C447C;margin-bottom:16px}
.warn{background:#FAEEDA;border-left:2px solid #EF9F27;padding:9px 13px;border-radius:0 7px 7px 0;font-size:12px;color:#854F0B;margin-bottom:16px}
.fine{color:#888780;font-size:11px}
form.searchbox{display:flex;gap:7px;margin-bottom:16px;flex-wrap:wrap}
form.searchbox input[type=text]{flex:1;min-width:220px;padding:7px 11px;border:1px solid #D3D1C7;border-radius:7px;font:13px inherit;background:#fff}
form.searchbox select{padding:7px 9px;border:1px solid #D3D1C7;border-radius:7px;background:#fff;font:12px inherit}
button,.btn{display:inline-block;padding:6px 13px;border:1px solid #D3D1C7;border-radius:7px;background:#fff;color:#2C2C2A;font:12px inherit;text-decoration:none;cursor:pointer}
button:hover,.btn:hover{border-color:#378ADD;color:#185FA5;background:#E6F1FB22}
.btn.on{background:#2C2C2A;color:#fff;border-color:#2C2C2A}
.tabs{display:flex;gap:6px;margin:14px 0;flex-wrap:wrap}
.row{display:grid;align-items:center;gap:9px;padding:7px 11px;border:1px solid #E3E1D9;border-radius:7px;margin-bottom:4px;background:#fff;font-size:12px}
.row:hover{border-color:#B9CFE4;background:#F8FBFE}
.row .grow{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.row a{color:#185FA5;text-decoration:none}
.row a:hover{text-decoration:underline}
.row .num{font-size:11.5px;color:#5F5E5A;font-variant-numeric:tabular-nums;text-align:right;white-space:nowrap}
.row .num.credit{color:#854F0B;font-weight:500}
.row.bad{border-left:3px solid #E24B4A}
.row.warn2{border-left:3px solid #EF9F27}
.idx{color:#888780;font-size:11.5px;min-width:38px;font-variant-numeric:tabular-nums}
.bar{height:9px;background:#F1EFE8;border-radius:3px;overflow:hidden;display:flex;min-width:60px}
.bar i{display:block;height:100%}
.bar .h{background:#378ADD}.bar .m{background:#EF9F27}
.chip{display:inline-block;font-size:10.5px;padding:0 7px;border-radius:9px;line-height:17px;white-space:nowrap}
.chip.hit{background:#E6F1FB;color:#185FA5}.chip.miss{background:#FAEEDA;color:#BA7517}
.chip.part{background:#EEEDFE;color:#534AB7}.chip.none{background:#F1EFE8;color:#888780}
.chip.cold{background:#FCEBEB;color:#A32D2D}
.chip.t{background:#F1EFE8;color:#5F5E5A}
.cmd{display:block;font:11px/1.5 ui-monospace,"Noto Sans Mono CJK SC",monospace;color:#185FA5;word-break:break-all;margin-top:3px}
.pager{display:flex;gap:6px;align-items:center;flex-wrap:wrap;margin:16px 0 6px;font-size:12px;color:#5F5E5A}
.pager .info{margin:0 8px;font-variant-numeric:tabular-nums}
.pager .btn{min-width:64px;text-align:center}
.pager .btn.off{opacity:.35;pointer-events:none}
pre.text{margin:12px 0;padding:13px 15px;background:#F7F6F2;border:1px solid #E9E7DF;border-radius:8px;
  font:12px/1.7 ui-monospace,"Noto Sans Mono CJK SC",monospace;white-space:pre-wrap;word-break:break-word;overflow:auto;max-height:70vh}
mark{background:#FFE9A8;color:#2C2C2A;padding:0 1px;border-radius:2px}
.snip{font:11.5px/1.6 ui-monospace,"Noto Sans Mono CJK SC",monospace;color:#5F5E5A;
  background:#F7F6F2;border-radius:5px;padding:6px 9px;margin-top:4px;word-break:break-word}
.split{display:flex;align-items:center;gap:8px;font-size:11px;color:#BA7517;margin:10px 0}
.split:before,.split:after{content:"";flex:1;height:1px;background:#EF9F27;opacity:.5}
table{width:100%;border-collapse:collapse;font-size:12px}
th,td{text-align:left;padding:6px 9px;border-bottom:1px solid #EFEDE5;font-variant-numeric:tabular-nums}
th{color:#5F5E5A;font-weight:500;font-size:11.5px;background:#F7F6F2}
td.r,th.r{text-align:right}
footer{margin-top:26px;padding-top:10px;border-top:1px solid #EFEDE5;font-size:11px;color:#B4B2A9;display:flex;gap:14px;flex-wrap:wrap}
footer a{color:#888780;text-decoration:none}
footer a:hover{color:#378ADD}
details.grp{margin-bottom:9px;border:1px solid #E3E1D9;border-radius:9px;overflow:hidden}
details.grp>summary{padding:9px 13px;cursor:pointer;list-style:none;background:#FAFAF7;font-size:12.5px}
details.grp>summary::-webkit-details-marker{display:none}
details.grp>summary small{color:#B4B2A9;font-weight:400;margin-left:8px}
details.grp .in{padding:8px 11px 3px}
code{font:11.5px ui-monospace,monospace;background:#F1EFE8;padding:1px 4px;border-radius:3px}
.apirow{display:grid;grid-template-columns:minmax(260px,1fr) 2fr;gap:10px;padding:7px 10px;border-bottom:1px solid #EFEDE5;font-size:12px}
.apirow code{background:none;padding:0;color:#185FA5}
"""


def crumb(items):
    out = []
    for i, (label, href) in enumerate(items):
        if i:
            out.append('<span class="sep">/</span>')
        out.append('<a href="%s">%s</a>' % (esc(href), esc(label)) if href
                   else '<span>%s</span>' % esc(label))
    return '<div class="crumb">' + "".join(out) + "</div>"


def page(title, crumbs, body, sub="", extra_head=""):
    head = ('<!DOCTYPE html><html lang="zh-CN"><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            '<title>%s · Token 轨迹</title><style>%s</style>' % (esc(title), CSS))
    return (head + '<header><div class="wrap">' + crumb(crumbs) +
            '<h1>%s<span class="badge">完整模式 · 零截断</span></h1>' % esc(title) +
            ('<div class="sub">%s</div>' % esc(sub) if sub else '') +
            '</div></header><div class="wrap">' + extra_head + body +
            '<footer><span>本页 HTML %.1f KB</span>' % (len(body.encode("utf-8")) / 1024.0) +
            '<span>数据只读 · 全文由后端按需提供 · 无需 JavaScript</span>'
            '<a href="/">索引</a><a href="/stats">全局统计</a><a href="/api">API</a></footer>'
            '</div></html>')


def pager(base, q, p, size, total, label="条"):
    """分页条。base 是路径，q 是已解析的 query dict（保留除 p 外的参数）"""
    pages = max(1, (total + size - 1) // size)
    p = max(0, min(p, pages - 1))
    rest = {k: v[0] for k, v in q.items() if k != "p" and v}

    def link(n):
        d = dict(rest)
        if n:
            d["p"] = n
        return base + ("?" + urlencode(d) if d else "")

    return ('<div class="pager">'
            '<a class="btn %s" href="%s">← 上一页</a>' % ("off" if p == 0 else "", esc(link(p - 1))) +
            '<span class="info">第 %d / %d 页 · 共 %s %s · 本页 %s</span>' %
            (p + 1, pages, nfmt(total), label, nfmt(min(size, max(0, total - p * size)))) +
            '<a class="btn %s" href="%s">下一页 →</a>' % ("off" if p >= pages - 1 else "", esc(link(p + 1))) +
            '</div>')


def qint(q, key, default=0, lo=0, hi=1000000):
    try:
        return max(lo, min(hi, int(q.get(key, [default])[0])))
    except (ValueError, TypeError):
        return default


def qstr(q, key, default=""):
    return (q.get(key) or [default])[0]


def kpi(items):
    return '<div class="kpi">' + "".join(
        '<div><b>%s</b><span>%s</span></div>' % (esc(v), esc(k)) for k, v in items) + '</div>'


def compose_bar(comp):
    total = sum(comp.values()) or 1
    bar = "".join('<i style="width:%.2f%%;background:%s"></i>' % (comp[k] / total * 100, PALETTE[k])
                  for k in ORDER if comp.get(k))
    legend = "".join('<span><i style="background:%s"></i>%s %s 字 (%.1f%%)</span>' %
                     (PALETTE[k], TYPES[k], nfmt(comp[k]), comp[k] / total * 100)
                     for k in ORDER if comp.get(k))
    return '<div class="compose">%s</div><div class="legend">%s</div>' % (bar, legend)


def rate_bar(c, width=True):
    r = (c["hit"] / c["pin"]) if c["pin"] else None
    if r is None:
        return '<span class="fine">并列 · 无独立 usage</span>'
    return ('<span class="bar" style="%s"><i class="h" style="width:%.2f%%"></i>'
            '<i class="m" style="width:%.2f%%"></i></span>' %
            ("flex:1" if width else "width:100px", r * 100, (1 - r) * 100))


def call_row(d, c, sid, show_turn=True):
    r = (c["hit"] / c["pin"]) if c["pin"] else None
    cls = "bad" if (r is not None and r < 0.5) else ("warn2" if (r is not None and r < 0.8) else "")
    pct = "%.0f%%" % (r * 100) if r is not None else "—"
    return ('<a class="row %s" href="/s/%s/c/%d" style="grid-template-columns:40px 90px 1fr 78px 78px 78px 66px 60px 92px">'
            '<span class="idx">#%d</span>%s'
            '<span class="grow"><b style="font-weight:500">%s</b>%s</span>'
            '<span class="num">in %s</span><span class="num">未命中 %s</span>'
            '<span class="num">out %s</span><span class="num">%s</span>'
            '<span class="num credit">%.2f 分</span><span class="num">%s</span></a>' %
            (cls, esc(sid), c["seq"], c["seq"], rate_bar(c),
             esc(c["tool"] or "—"),
             ('<span class="chip t" style="margin-left:6px">轮 %d</span>' % c["turn"]) if show_turn and c["turn"] else "",
             nfmt(c["pin"]), nfmt(c["pin"] - c["hit"]), nfmt(c["pout"]), pct,
             c.get("credit") or 0,
             '<span style="color:#378ADD">详情 →</span>'))


def turn_row(d, t, sid):
    b = turn_brief(d, t)
    return ('<a class="row" href="/s/%s/t/%d" style="grid-template-columns:52px 1fr 68px 92px 72px 90px 74px 62px">'
            '<span class="idx">轮 %d</span>'
            '<span class="grow"><b style="font-weight:500">%s</b>%s</span>'
            '<span class="num">%d 次</span><span class="num">in %s</span>'
            '<span class="num">命中 %s</span><span class="num">out %s</span>'
            '<span class="num credit">%.2f 分</span>'
            '<span class="num" style="color:#378ADD">展开 →</span></a>' %
            (esc(sid), b["i"], b["i"], esc(b["q"] or "（只有系统注入）"),
             '<span class="chip cold" style="margin-left:6px">冷启动</span>' if b["cold"] else "",
             b["calls"], nfmt(b["pin"]), "%.0f%%" % (b["rate"] * 100),
             nfmt(b["pout"]), b["credit"]))


# ---------------------------------------------------------------- HTML 视图

def v_index(q):
    ss = STORE.sessions(refresh=qstr(q, "refresh") == "1")
    ws = qstr(q, "ws")
    sort = qstr(q, "sort", "mtime")
    shown = [s for s in ss if not ws or s["ws"] == ws]
    if sort == "credit":
        shown = sorted(shown, key=lambda s: -s["credit"])
    elif sort == "calls":
        shown = sorted(shown, key=lambda s: -s["calls"])
    else:
        shown = sorted(shown, key=lambda s: -s["mtime"])

    total_cr = sum(s["credit"] for s in ss)
    total_calls = sum(s["calls"] for s in ss)
    body = kpi([("会话", nfmt(len(ss))), ("工作区", nfmt(len(set(s["ws"] for s in ss)))),
                ("模型调用", nfmt(total_calls)), ("累计积分", "%.1f" % total_cr),
                ("累计输入 tok", nfmt(sum(s["pin"] for s in ss))),
                ("累计输出 tok", nfmt(sum(s["pout"] for s in ss)))])
    if any(not s.get("meta_ok", True) for s in ss):
        body += ('<div class="warn">workbuddy.db 未读到：会话标题、工作区与模型名已退回兜底值，'
                 '详情见服务端终端输出。</div>')
    body += ('<form class="searchbox" method="get" action="/search">'
             '<input type="text" name="q" placeholder="搜上下文块 / 输出 / 提问原文（跨全部会话）" value="">'
             '<select name="scope"><option value="all">全部范围</option><option value="blocks">只看上下文块</option>'
             '<option value="outputs">只看模型输出</option><option value="turns">只看提问</option></select>'
             '<button type="submit">搜索</button></form>')
    body += ('<div class="tabs"><a class="btn" href="/stats">全局统计与冷启动断点</a>'
             '<a class="btn" href="/api">后端 API 契约</a>'
             '<a class="btn" href="/?refresh=1">重新扫描源文件</a>'
             '<span class="fine" style="align-self:center;margin-left:6px">源文件 68 MB · 只读</span></div>')
    body += ('<div class="tabs"><span class="fine" style="align-self:center">排序</span>'
             '<a class="btn %s" href="/?sort=mtime">最近修改</a>' % ("on" if sort == "mtime" else "") +
             '<a class="btn %s" href="/?sort=credit">积分</a>' % ("on" if sort == "credit" else "") +
             '<a class="btn %s" href="/?sort=calls">调用数</a>' % ("on" if sort == "calls" else "") +
             (('<a class="btn" href="/">清除工作区筛选：%s ✕</a>' % esc(ws)) if ws else '') + '</div>')

    groups = OrderedDict()
    for s in shown:
        groups.setdefault(s["ws"], []).append(s)
    for name in sorted(groups, key=lambda w: -max(x["mtime"] for x in groups[w])):
        items = groups[name]
        cards = []
        for s in items:
            t = time.strftime("%m-%d %H:%M", time.localtime(s["mtime"]))
            cards.append(
                '<a class="row" href="/s/%s" style="grid-template-columns:1fr 96px 108px 92px 92px 82px 70px">'
                '<span class="grow"><b style="font-weight:500">%s</b>'
                '<span class="fine" style="display:block">%s · %s</span></span>'
                '<span class="num">%d 轮</span><span class="num">%d 次调用</span>'
                '<span class="num">in %s</span><span class="num">out %s</span>'
                '<span class="num credit">%.1f 分</span>'
                '<span class="num">%s</span></a>' %
                (esc(s["id"]), esc(s["title"][:70]), esc(s["id"][:8]), esc(s["model"] or "?"),
                 s["turns"], s["calls"], "%.1fM" % (s["pin"] / 1e6),
                 "%.0fk" % (s["pout"] / 1000), s["credit"], t))
        body += ('<details class="grp" open><summary>%s<small>%d 个会话 · %.1f 分</small></summary>'
                 '<div class="in">%s</div></details>' %
                 (esc(name), len(items), sum(s["credit"] for s in items), "".join(cards)))
    return page("会话索引", [("索引", "")], body,
                sub="%d 个会话 · 点任一会话进入全量轨迹（不裁调用、不裁文本）" % len(shown))


def v_session(q, sid):
    d = STORE.get(sid)
    s, st = d["s"], d["stats"]
    p = qint(q, "p")
    view = qstr(q, "view", "turns")
    crumbs = [("索引", "/"), (d["title"][:30], "")]
    body = kpi([("对话轮次", nfmt(st["turns"])), ("模型调用", nfmt(st["calls"])),
                ("上下文块", nfmt(st["blocks"])), ("可见字数", nfmt(st["chars"])),
                ("累计输入 tok", nfmt(st["pin"])), ("缓存命中", "%.1f%%" % (st["rate"] * 100)),
                ("未命中（计费）", nfmt(st["miss"])), ("输出 tok", nfmt(st["pout"])),
                ("累计积分", "%.2f" % st["credit"]), ("冷启动断点", nfmt(st["cold"]))])
    body += '<h3>进入上下文的文本构成 <span class="fine">按字符数，全文口径</span></h3>' + compose_bar(st["compose"])
    if st.get("skipped_lines"):
        body += ('<div class="warn">源文件有 %d 行无法解析（多半是 WorkBuddy 写入中途被读到），'
                 '已跳过；本页统计与上下文不含这些行。刷新页面可重试。</div>' % st["skipped_lines"])
    body += ('<div class="note">这个会话的 %s 次调用、%s 个上下文块、%s 字原文<b>全部可查</b>：'
             '按轮 / 按调用翻页，点任意一次调用看它当时带上的完整块列表，'
             '再点任意一块看<b>不截断</b>的全文（超长块自动分片）。</div>' %
             (nfmt(st["calls"]), nfmt(st["blocks"]), nfmt(st["chars"])))
    body += ('<form class="searchbox" method="get" action="/s/%s/search">'
             '<input type="text" name="q" placeholder="只在这个会话里搜（含 58 万字的长块）">'
             '<button type="submit">搜索</button></form>' % esc(sid))
    body += ('<div class="tabs"><a class="btn %s" href="/s/%s?view=turns">按轮浏览</a>' %
             ("on" if view == "turns" else "", esc(sid)) +
             '<a class="btn %s" href="/s/%s?view=calls">按调用浏览</a>' %
             ("on" if view == "calls" else "", esc(sid)) +
             '<a class="btn" href="/api/s/%s">这个会话的 JSON</a>' % esc(sid) + '</div>')

    if view == "calls":
        total = len(d["calls"])
        lo = p * PAGE_CALLS
        body += '<h2>全部 %s 次调用 <small>每页 %d 条，一次都不省</small></h2>' % (nfmt(total), PAGE_CALLS)
        for c in d["calls"][lo:lo + PAGE_CALLS]:
            body += call_row(d, c, sid)
        pq = dict(q)
        pq["view"] = ["calls"]
        body += pager("/s/%s" % sid, pq, p, PAGE_CALLS, total, "次调用")
    else:
        total = len(d["turns"])
        lo = p * PAGE_TURNS
        body += '<h2>全部 %s 轮 <small>每页 %d 轮</small></h2>' % (nfmt(total), PAGE_TURNS)
        for t in d["turns"][lo:lo + PAGE_TURNS]:
            body += turn_row(d, t, sid)
        body += pager("/s/%s" % sid, q, p, PAGE_TURNS, total, "轮")
        if d["cold"]:
            body += '<h3>缓存冷启动断点 <small>上一次 &gt;80% 命中、这一次 &lt;50%</small></h3>'
            for i in d["cold"][:10]:
                c = d["calls"][i]
                body += ('<a class="row bad" href="/s/%s/c/%d" style="grid-template-columns:56px 1fr 100px 92px 82px">'
                         '<span class="idx">#%d</span><span class="grow">轮 %d · 命中率从 &gt;80%% 掉到 %.1f%%</span>'
                         '<span class="num">in %s</span><span class="num">未命中 %s</span>'
                         '<span class="num credit">%.2f 分</span></a>' %
                         (esc(sid), c["seq"], c["seq"], c["turn"], c["hit"] / c["pin"] * 100,
                          nfmt(c["pin"]), nfmt(c["pin"] - c["hit"]), c["credit"]))
    return page(d["title"][:50], crumbs, body,
                sub="%s · %s · 模型 %s · 解析 %d ms" %
                    (s["ws"], os.path.basename(s["path"]), st["model"], d["parse_ms"]))


def v_turn(q, sid, turn):
    d = STORE.get(sid)
    idxs = d["by_turn"].get(turn, [])
    if turn and not idxs:
        raise KeyError("no such turn")
    label = "轮 %d" % turn if turn else "开场（首条用户消息之前）"
    tb = turn_brief(d, d["turns"][turn - 1]) if turn and turn <= len(d["turns"]) else None
    crumbs = [("索引", "/"), (d["title"][:24], "/s/%s" % sid), (label, "")]
    body = ""
    if tb:
        body += kpi([("调用次数", nfmt(tb["calls"])), ("输入 tok", nfmt(tb["pin"])),
                     ("命中率", "%.0f%%" % (tb["rate"] * 100)), ("输出 tok", nfmt(tb["pout"])),
                     ("积分", "%.2f" % tb["credit"]), ("系统注入", nfmt(tb["inj"]) + " 字")])
        body += ('<div class="note"><b>你这一轮问的是：</b>%s</div>' % esc(tb["q"] or "（只有系统注入）"))
        if tb["cold"]:
            body += '<div class="warn">这一轮首次调用命中率 &lt;50%，是缓存冷启动：整段历史按全价重算。</div>'
    body += '<h2>这一轮的 %d 次调用</h2>' % len(idxs)
    for i in idxs:
        body += call_row(d, d["calls"][i], sid, show_turn=False)
    return page(label + " · " + d["title"][:28], crumbs, body,
                sub="点任意一次调用 → 看它当时带上的完整上下文")


def v_call(q, sid, seq):
    d = STORE.get(sid)
    if seq < 1 or seq > len(d["calls"]):
        raise KeyError("no such call")
    i = seq - 1
    c = d["calls"][i]
    o = call_outline(d, i)
    state = qstr(q, "state", "all")
    p = qint(q, "p")
    crumbs = [("索引", "/"), (d["title"][:20], "/s/%s" % sid),
              ("轮 %d" % c["turn"] if c["turn"] else "开场", "/s/%s/t/%d" % (sid, c["turn"])),
              ("调用 #%d" % seq, "")]
    r = (c["hit"] / c["pin"]) if c["pin"] else None
    body = kpi([("接口输入 tok", nfmt(c["pin"])), ("缓存命中 tok", nfmt(c["hit"])),
                ("未命中 tok", nfmt(c["pin"] - c["hit"])),
                ("命中率", "%.1f%%" % (r * 100) if r is not None else "—"),
                ("输出 tok", nfmt(c["pout"])), ("积分", "%.2f" % (c.get("credit") or 0)),
                ("可见上下文", "%s 块 / %s 字" % (nfmt(o["n"]), nfmt(o["chars"]))),
                ("缓存边界", "#%s" % (o["boundary"] + 1) if o["boundary"] is not None else "—")])
    body += ('<div class="note">工具 <b>%s</b> · 模型 %s · %s<br>'
             '下面列出这次请求<b>实际带上的全部 %s 个块</b>：蓝色=命中缓存的前缀，琥珀=本次未命中，'
             '紫色块是跨边界的那一块。<b>点任意一行看该块不截断的全文</b>。</div>' %
             (esc(c["tool"] or "最终回答"), esc(c["model"]),
              time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(c["ts"])) if c["ts"] else "时间未知",
              nfmt(o["n"])))

    filt = []
    for k, lab in (("all", "全部"), ("miss", "只看未命中"), ("hit", "只看命中"), ("part", "跨边界")):
        filt.append('<a class="btn %s" href="/s/%s/c/%d?state=%s">%s</a>' %
                    ("on" if state == k else "", esc(sid), seq, k, lab))
    body += '<div class="tabs">' + "".join(filt) + '</div>'

    if state == "all":
        chosen = list(range(o["n"]))
    else:
        chosen = [j for j in range(o["n"]) if call_state(d, i, j) == state]
    lo = p * PAGE_BLOCKS
    page_idx = chosen[lo:lo + PAGE_BLOCKS]
    body += '<h2>上下文块 <small>共 %s 个，当前筛选 %s 个 · 每页 %d</small></h2>' % (
        nfmt(o["n"]), nfmt(len(chosen)), PAGE_BLOCKS)
    for j in page_idx:
        b = d["ctx"][j]
        st = call_state(d, i, j)
        cmd = G.cmd_html(b, 140)
        body += ('<a class="row" href="/s/%s/b/%d?ref=%d" '
                 'style="grid-template-columns:52px 64px 92px 1fr 84px 62px">'
                 '<span class="idx">#%d</span><span class="chip %s">%s</span>'
                 '<span class="chip t">%s</span>'
                 '<span class="grow"><b style="font-weight:500">%s</b>%s</span>'
                 '<span class="num">%s 字</span><span class="num" style="color:#378ADD">全文 →</span></a>' %
                 (esc(sid), j, seq, j, st, G.CHIP[st], TYPES.get(b["k"], b["k"]),
                  esc(b["l"]), cmd, nfmt(b["n"])))
    body += pager("/s/%s/c/%d" % (sid, seq), q, p, PAGE_BLOCKS, len(chosen), "块")

    if c["out"]:
        body += '<h2>本次输出 <small>不占输入上下文；每段全文可分片查看</small></h2>'
        for k, seg in enumerate(c["out"]):
            body += ('<a class="row" href="/s/%s/o/%d?seg=%d" style="grid-template-columns:70px 1fr 100px 70px">'
                     '<span class="chip t">%s</span><span class="grow">%s</span>'
                     '<span class="num">%s 字</span><span class="num" style="color:#378ADD">全文 →</span></a>' %
                     (esc(sid), seq, k, esc(seg["kind"]), esc(seg["label"]),
                      nfmt(len(seg["text"]))))
    nav = ('<div class="tabs">'
           '<a class="btn %s" href="/s/%s/c/%d">← 上一次调用</a>' %
           ("off" if seq <= 1 else "", esc(sid), seq - 1) +
           '<a class="btn %s" href="/s/%s/c/%d">下一次调用 →</a>' %
           ("off" if seq >= len(d["calls"]) else "", esc(sid), seq + 1) +
           '<a class="btn" href="/api/s/%s/calls/%d">这次调用的 JSON</a></div>' % (esc(sid), seq))
    return page("调用 #%d" % seq, crumbs, body + nav,
                sub="%s · %s" % (d["title"][:30], c["tool"] or "最终回答"))


def v_block(q, sid, idx):
    d = STORE.get(sid)
    if idx < 0 or idx >= len(d["ctx"]):
        raise KeyError("no such block")
    b = d["ctx"][idx]
    ref = qint(q, "ref")
    part = qint(q, "part")
    raw = qstr(q, "raw") == "1"
    text = b["t"]
    total_parts = max(1, (len(text) + PART_CHARS - 1) // PART_CHARS)
    part = min(part, total_parts - 1)
    chunk = text[part * PART_CHARS:(part + 1) * PART_CHARS]
    if raw:
        return 200, "text/plain; charset=utf-8", chunk.encode("utf-8")

    users = block_users(d, idx)
    crumbs = [("索引", "/"), (d["title"][:20], "/s/%s" % sid),
              ("调用 #%d" % ref if ref else "上下文块", "/s/%s/c/%d" % (sid, ref) if ref else ""),
              ("块 #%d 全文" % idx, "")]
    body = kpi([("原文长度", nfmt(b["n"]) + " 字"),
                ("类型", TYPES.get(b["k"], b["k"])),
                ("分片", "%d / %d" % (part + 1, total_parts)),
                ("本片字数", nfmt(len(chunk))),
                ("被带上", "%s 次调用" % nfmt(len(users)))])
    if b["c"]:
        body += '<div class="note"><b>产生这个结果的原始指令</b><span class="cmd">$ %s</span></div>' % esc(b["c"])
    body += ('<div class="tabs"><a class="btn" href="/s/%s/b/%d?raw=1&amp;part=%d">本片纯文本</a>' %
             (esc(sid), idx, part) +
             '<a class="btn" href="/api/s/%s/blocks/%d?offset=%d&amp;limit=%d">这一片的 JSON</a>' %
             (esc(sid), idx, part * PART_CHARS, PART_CHARS) +
             '<a class="btn %s" href="/s/%s/b/%d?part=%d&amp;ref=%d">← 上一片</a>' %
             ("off" if part == 0 else "", esc(sid), idx, part - 1, ref) +
             '<a class="btn %s" href="/s/%s/b/%d?part=%d&amp;ref=%d">下一片 →</a>' %
             ("off" if part >= total_parts - 1 else "", esc(sid), idx, part + 1, ref) + '</div>')
    body += '<pre class="text">%s</pre>' % esc(chunk)
    if total_parts > 1:
        body += ('<div class="fine">这是第 %d/%d 片（每片 %s 字）。全文共 %s 字，'
                 '一片都不缺，用「下一片」翻完，或用 API 一次取全文。</div>' %
                 (part + 1, total_parts, nfmt(PART_CHARS), nfmt(len(text))))

    body += '<h3>哪些调用带上了这个块 <small>共 %s 次，列最近 12 次</small></h3>' % nfmt(len(users))
    for k in list(users)[-12:][::-1]:
        c = d["calls"][k]
        body += ('<a class="row" href="/s/%s/c/%d" style="grid-template-columns:64px 1fr 100px 80px">'
                 '<span class="idx">#%d</span><span class="grow">轮 %d · %s</span>'
                 '<span class="num">in %s</span><span class="num credit">%.2f 分</span></a>' %
                 (esc(sid), c["seq"], c["seq"], c["turn"], esc(c["tool"] or "最终回答"),
                  nfmt(c["pin"]), c.get("credit") or 0))
    nav = ('<div class="tabs"><a class="btn %s" href="/s/%s/b/%d">← 上一块</a>' %
           ("off" if idx == 0 else "", esc(sid), idx - 1) +
           '<a class="btn %s" href="/s/%s/b/%d">下一块 →</a>' %
           ("off" if idx >= len(d["ctx"]) - 1 else "", esc(sid), idx + 1) + '</div>')
    return page("块 #%d · %s" % (idx, b["l"]), crumbs, body + nav,
                sub="完整原文（分片显示）· %s 字" % nfmt(b["n"]))


def v_output(q, sid, seq):
    d = STORE.get(sid)
    if seq < 1 or seq > len(d["calls"]):
        raise KeyError("no such call")
    c = d["calls"][seq - 1]
    seg = qint(q, "seg")
    part = qint(q, "part")
    if not c["out"]:
        body = '<div class="warn">这次调用没有单独记录的输出（并行工具调用常这样）。</div>'
        return page("输出 #%d" % seq, [("索引", "/"), ("会话", "/s/%s" % sid), ("输出", "")], body)
    seg = min(seg, len(c["out"]) - 1)
    o = c["out"][seg]
    text = o["text"]
    parts = max(1, (len(text) + PART_CHARS - 1) // PART_CHARS)
    part = min(part, parts - 1)
    chunk = text[part * PART_CHARS:(part + 1) * PART_CHARS]
    crumbs = [("索引", "/"), (d["title"][:20], "/s/%s" % sid),
              ("调用 #%d" % seq, "/s/%s/c/%d" % (sid, seq)), ("输出全文", "")]
    body = ('<div class="tabs">' + "".join(
        '<a class="btn %s" href="/s/%s/o/%d?seg=%d">%s %s 字</a>' %
        ("on" if k == seg else "", esc(sid), seq, k, esc(x["label"]), nfmt(len(x["text"])))
        for k, x in enumerate(c["out"])) + '</div>')
    body += ('<div class="fine">第 %d/%d 片 · 每片 %s 字 · 全文 %s 字</div>' %
             (part + 1, parts, nfmt(PART_CHARS), nfmt(len(text))))
    body += '<pre class="text">%s</pre>' % esc(chunk)
    body += ('<div class="tabs"><a class="btn %s" href="/s/%s/o/%d?seg=%d&amp;part=%d">← 上一片</a>' %
             ("off" if part == 0 else "", esc(sid), seq, seg, part - 1) +
             '<a class="btn %s" href="/s/%s/o/%d?seg=%d&amp;part=%d">下一片 →</a>' %
             ("off" if part >= parts - 1 else "", esc(sid), seq, seg, part + 1) + '</div>')
    return page("调用 #%d 的输出" % seq, crumbs, body,
                sub="%s · 不占输入上下文" % o["label"])


def v_search(q, sid=None):
    term = qstr(q, "q")
    scope = qstr(q, "scope", "all")
    d = STORE.get(sid) if sid else None
    t0 = time.time()
    with CALC_LOCK:
        hits, warn = search(term, sid=sid, scope=scope)
    ms = int((time.time() - t0) * 1000)
    action = "/s/%s/search" % sid if sid else "/search"
    crumbs = [("索引", "/")] + ([(d["title"][:24], "/s/%s" % sid)] if d else []) + [("搜索", "")]
    body = ('<form class="searchbox" method="get" action="%s">'
            '<input type="text" name="q" value="%s" placeholder="全文关键词">'
            '<select name="scope">%s</select><button type="submit">搜索</button></form>' %
            (action, esc(term), "".join(
                '<option value="%s" %s>%s</option>' %
                (k, "selected" if scope == k else "", lab)
                for k, lab in (("all", "全部范围"), ("blocks", "只看上下文块"),
                               ("outputs", "只看模型输出"), ("turns", "只看提问")))))
    if warn:
        body += '<div class="warn">%s</div>' % esc(warn)
    if term:
        body += '<h2>命中 %d 条 <small>扫描 %s · %d ms</small></h2>' % (
            len(hits), "这个会话" if sid else "全部 %d 个会话" % len(STORE.sessions()), ms)
        rx = re.compile(re.escape(term), re.I)
        for h in hits:
            raw = h["snippet"]
            parts, pos = [], 0
            for m in rx.finditer(raw):
                parts.append(esc(raw[pos:m.start()]))
                parts.append("<mark>%s</mark>" % esc(m.group(0)))
                pos = m.end()
            parts.append(esc(raw[pos:]))
            sn = "".join(parts)
            body += ('<a class="row" href="%s" style="display:block;padding:9px 12px">'
                     '<span class="chip t">%s</span> <b style="font-weight:500">%s</b> '
                     '<span class="fine">%s 字 · 命中位置 %s · %s</span>'
                     '<div class="snip">…%s…</div></a>' %
                     (esc(h["href"]), esc(h["kind"]), esc(h["label"]),
                      nfmt(h["chars"]), nfmt(h["pos"]), esc(h["ws"]), sn))
    else:
        body += '<div class="note">输入关键词搜索；结果里的每一条都能点进去看不截断的全文。</div>'
    return page("全文搜索", crumbs, body, sub="服务端检索 940 万字原文，不建索引、不落盘")


def v_stats(q):
    t0 = time.time()
    with CALC_LOCK:
        st = global_stats()
    ms = int((time.time() - t0) * 1000)
    body = kpi([("会话", nfmt(st["sessions"])), ("工作区", nfmt(st["workspaces"])),
                ("模型调用", nfmt(st["calls"])), ("对话轮次", nfmt(st["turns"])),
                ("上下文块", nfmt(st["blocks"])), ("可见原文", nfmt(st["chars"]) + " 字"),
                ("累计输入 tok", nfmt(st["pin"])), ("缓存命中率", "%.2f%%" % (st["rate"] * 100)),
                ("未命中 tok", nfmt(st["miss"])), ("输出 tok", nfmt(st["pout"])),
                ("累计积分", "%.2f" % st["credit"]),
                ("积分/万未命中", "%.3f" % st["unit_miss"])])
    body += '<h3>全库上下文构成</h3>' + compose_bar(st["compose"])
    body += ('<div class="note">上面每个数字都是<b>全量</b>算出来的（%d ms，遍历 %d 个会话全文），'
             '不是抽样、不是截断后的静态页统计。</div>' % (ms, st["sessions"]))
    body += '<h2>最贵的 20 次调用 <small>按 credit</small></h2>'
    for x in st["top_calls"]:
        body += ('<a class="row bad" href="/s/%s/c/%d" style="grid-template-columns:64px 1fr 104px 84px 84px">'
                 '<span class="idx">#%d</span><span class="grow">%s · %s</span>'
                 '<span class="num">in %s</span><span class="num">命中 %.0f%%</span>'
                 '<span class="num credit">%.2f 分</span></a>' %
                 (esc(x["sid"]), x["seq"], x["seq"], esc(x["ws"]), esc(x["tool"] or "最终回答"),
                  nfmt(x["pin"]), x["rate"] * 100, x["credit"]))
    body += '<h2>最大的 20 个上下文块 <small>点进去分片看全文</small></h2>'
    for x in st["top_blocks"]:
        body += ('<a class="row" href="/s/%s/b/%d" style="grid-template-columns:64px 1fr 110px 76px">'
                 '<span class="idx">#%d</span><span class="grow">%s · %s</span>'
                 '<span class="num">%s 字</span><span class="num" style="color:#378ADD">全文 →</span></a>' %
                 (esc(x["sid"]), x["idx"], x["idx"], TYPES.get(x["kind"], x["kind"]),
                  esc(x["label"]), nfmt(x["chars"])))
    body += '<h2>缓存冷启动断点 <small>共 %d 次；按当次输入量排序，列前 30</small></h2>' % len(st["cold"])
    for x in st["cold"][:30]:
        ts = time.strftime("%m-%d %H:%M", time.localtime(x["ts"])) if x["ts"] else "?"
        body += ('<a class="row bad" href="/s/%s/c/%d" style="grid-template-columns:64px 1fr 104px 84px 84px 66px">'
                 '<span class="idx">#%d</span><span class="grow">%s · %s</span>'
                 '<span class="num">in %s</span><span class="num">命中 %.1f%%</span>'
                 '<span class="num credit">%.2f 分</span><span class="num">%s</span></a>' %
                 (esc(x["sid"]), x["seq"], x["seq"], esc(x["ws"]), esc(x["sid"][:8]),
                  nfmt(x["pin"]), x["rate"] * 100, x["credit"], ts))
    body += ('<div class="tabs"><a class="btn" href="/api/stats">同一份数据的 JSON</a>'
             '<a class="btn" href="/">← 回索引</a></div>')
    return page("全局统计", [("索引", "/"), ("全局统计", "")], body,
                sub="跨会话全量聚合 · 每次访问现算，不缓存结论")


def v_api_doc(q):
    rows = [
        ("/api/health", "服务状态、会话数、缓存命中数"),
        ("/api/sessions", "全部会话摘要（工作区、轮数、调用数、积分、token）"),
        ("/api/stats", "跨会话全量聚合：构成、冷启动断点、最贵调用、最大块、单价"),
        ("/api/s/&lt;sid&gt;", "单会话全量统计 + 每一轮的摘要"),
        ("/api/s/&lt;sid&gt;/calls?offset=&amp;limit=&amp;turn=", "调用列表（分页；不含上下文正文）"),
        ("/api/s/&lt;sid&gt;/calls/&lt;seq&gt;", "单次调用：usage、缓存边界、上下文块索引、输出段索引"),
        ("/api/s/&lt;sid&gt;/calls/&lt;seq&gt;/context?offset=&amp;limit=&amp;state=", "上下文块清单（分页/按状态筛选，不含正文）"),
        ("/api/s/&lt;sid&gt;/blocks/&lt;idx&gt;?offset=&amp;limit=&amp;full=1", "块全文（Range 语义；offset/limit 按字符）"),
        ("/api/s/&lt;sid&gt;/blocks/&lt;idx&gt;/users?offset=&amp;limit=", "哪些调用带上了这个块"),
        ("/api/s/&lt;sid&gt;/outputs/&lt;seq&gt;?seg=&amp;offset=&amp;limit=", "某次调用的输出全文（分段+分片）"),
        ("/api/s/&lt;sid&gt;/turns/&lt;n&gt;", "某一轮的全部调用"),
        ("/api/search?q=&amp;sid=&amp;scope=&amp;limit=", "全文搜索，返回命中片段与跳转链接"),
    ]
    body = ('<div class="note">所有接口只读、返回 JSON，统一信封 '
            '<code>{"ok":true,"data":...,"meta":{...}}</code>；出错为 '
            '<code>{"ok":false,"error":"..."}</code> 加对应 HTTP 状态码。'
            '分页参数统一 <code>offset/limit</code>，<code>meta</code> 里给 <code>total</code> '
            '和下一页的 <code>next</code>。跨域默认关闭，只监听本机。</div>')
    body += '<h2>接口清单</h2>'
    for path, desc in rows:
        body += '<div class="apirow"><code>GET %s</code><span>%s</span></div>' % (path, esc(desc))
    body += ('<div class="apirow"><code>POST /shutdown</code>'
             '<span>停止服务（<code>start.sh stop</code> 用；校验 Host/Origin/Sec-Fetch-Site，'
             '配置令牌时还需鉴权）</span></div>')
    body += ('<h2>调用示例</h2><pre class="text">'
             'curl -s "http://127.0.0.1:8765/api/sessions" | python3 -m json.tool\n'
             'curl -s "http://127.0.0.1:8765/api/s/&lt;sid&gt;/calls?limit=5"\n'
             'curl -s "http://127.0.0.1:8765/api/s/&lt;sid&gt;/blocks/42?offset=0&amp;limit=1000"\n'
             'curl -s "http://127.0.0.1:8765/api/search?q=缓存冷启动"</pre>')
    body += ('<h2>HTML 路由</h2><div class="apirow"><code>GET /</code><span>索引</span></div>'
             '<div class="apirow"><code>GET /s/&lt;sid&gt;?view=turns|calls&amp;p=</code><span>会话（按轮 / 按调用分页）</span></div>'
             '<div class="apirow"><code>GET /s/&lt;sid&gt;/t/&lt;n&gt;</code><span>单轮</span></div>'
             '<div class="apirow"><code>GET /s/&lt;sid&gt;/c/&lt;seq&gt;?state=&amp;p=</code><span>单次调用的完整块清单</span></div>'
             '<div class="apirow"><code>GET /s/&lt;sid&gt;/b/&lt;idx&gt;?part=</code><span>块全文分片（<code>raw=1</code> 出纯文本）</span></div>'
             '<div class="apirow"><code>GET /s/&lt;sid&gt;/o/&lt;seq&gt;?seg=&amp;part=</code><span>输出全文分片</span></div>'
             '<div class="apirow"><code>GET /search?q=</code><span>全文搜索</span></div>')
    return page("后端 API", [("索引", "/"), ("API", "")], body,
                sub="零 JavaScript 的前端，本身就建立在这套按需查询之上")


# ---------------------------------------------------------------- JSON API

def env(data, **meta):
    return {"ok": True, "data": data, "meta": meta}


def paged(data, total, offset, limit, base, q):
    m = {"total": total, "offset": offset, "limit": limit}
    if offset + limit < total:
        rest = {k: v[0] for k, v in q.items() if k not in ("offset", "limit") and v}
        rest.update({"offset": offset + limit, "limit": limit})
        m["next"] = base + "?" + urlencode(rest)
    return env(data, **m)


def api_route(path, q):
    def json_bytes(obj, status=200):
        return status, "application/json; charset=utf-8", json.dumps(
            obj, ensure_ascii=False, default=str).encode("utf-8")

    if path == "/api/health":
        return json_bytes(env({
            "status": "ok", "pid": os.getpid(),
            "uptime_s": int(time.time() - START_TS),
            "rss_mb": rss_mb(),
            "sessions": len(STORE.sessions()),
            "cached_sessions": len(STORE._cache),
            "write_files": False,
            "write_files_basis": ("源码无写文件调用；sys.dont_write_bytecode=%s（非 OS 级探测）"
                                  % sys.dont_write_bytecode),
            "python": sys.version.split()[0],
        }))
    if path == "/api/sessions":
        return json_bytes(env([dict(s) for s in STORE.sessions()], total=len(STORE.sessions())))
    if path == "/api/stats":
        with CALC_LOCK:
            return json_bytes(env(global_stats()))
    if path == "/api/search":
        sid = qstr(q, "sid") or None
        if sid:
            try:
                STORE.by_id(sid)
            except KeyError:
                return json_bytes({"ok": False, "error": "unknown session: " + sid}, 404)
        with CALC_LOCK:
            hits, warn = search(qstr(q, "q"), sid=sid,
                                scope=qstr(q, "scope", "all"),
                                limit=min(API_MAX_LIMIT, qint(q, "limit", SEARCH_MAX, 1)))
        return json_bytes(env(hits, total=len(hits), note=warn))

    m = re.match(r"^/api/s/([0-9A-Za-z_-]+)(/.*)?$", path)
    if not m:
        return json_bytes({"ok": False, "error": "unknown endpoint: " + path}, 404)
    sid, rest = m.group(1), m.group(2) or ""
    try:
        d = STORE.get(sid)
    except KeyError:
        return json_bytes({"ok": False, "error": "unknown session: " + sid}, 404)

    if rest == "":
        return json_bytes(env({
            "session": {k: v for k, v in d["s"].items() if k != "path"},
            "source": d["s"]["path"], "stats": d["stats"],
            "turns": [turn_brief(d, t) for t in d["turns"]],
            "cold_calls": [d["calls"][i]["seq"] for i in d["cold"]],
        }))
    if rest == "/calls":
        off = qint(q, "offset")
        lim = min(API_MAX_LIMIT, qint(q, "limit", 50, 1))
        turn = qstr(q, "turn")
        cs = [(i, c) for i, c in enumerate(d["calls"]) if not turn or str(c["turn"]) == turn]
        return json_bytes(paged([call_brief(c) for _, c in cs[off:off + lim]],
                                len(cs), off, lim, "/api/s/%s/calls" % sid, q))
    mm = re.match(r"^/calls/(\d+)(/context)?$", rest)
    if mm:
        seq = int(mm.group(1))
        if seq < 1 or seq > len(d["calls"]):
            return json_bytes({"ok": False, "error": "call out of range"}, 404)
        c = d["calls"][seq - 1]
        o = call_outline(d, seq - 1)
        if mm.group(2):
            off = qint(q, "offset")
            lim = min(API_MAX_LIMIT, qint(q, "limit", 50, 1))
            state = qstr(q, "state", "all")
            idxs = list(range(o["n"])) if state == "all" else [
                j for j in range(o["n"]) if call_state(d, seq - 1, j) == state]
            rows = []
            for j in idxs[off:off + lim]:
                b = d["ctx"][j]
                rows.append({"idx": j, "state": call_state(d, seq - 1, j),
                             "kind": b["k"], "label": b["l"], "chars": b["n"],
                             "cmd": b["c"], "text_url": "/api/s/%s/blocks/%d" % (sid, j)})
            return json_bytes(paged(rows, len(idxs), off, lim,
                                    "/api/s/%s/calls/%d/context" % (sid, seq), q))
        return json_bytes(env({
            "call": call_brief(c), "model": c["model"], "tool": c["tool"],
            "context": {"blocks": o["n"], "chars": o["chars"], "cache_ratio": o["ratio"],
                        "cut_chars": o["cut"], "boundary_block": o["boundary"],
                        "hit_blocks": o["hit_blocks"], "miss_blocks": o["miss_blocks"],
                        "list_url": "/api/s/%s/calls/%d/context" % (sid, seq)},
            "outputs": [{"seg": k, "kind": x["kind"], "label": x["label"],
                         "chars": len(x["text"]),
                         "url": "/api/s/%s/outputs/%d?seg=%d" % (sid, seq, k)}
                        for k, x in enumerate(c["out"])],
            "prev_call": ("/api/s/%s/calls/%d" % (sid, seq - 1)) if seq > 1 else None,
            "next_call": ("/api/s/%s/calls/%d" % (sid, seq + 1)) if seq < len(d["calls"]) else None,
        }))
    mm = re.match(r"^/blocks/(\d+)(/users)?$", rest)
    if mm:
        idx = int(mm.group(1))
        if idx < 0 or idx >= len(d["ctx"]):
            return json_bytes({"ok": False, "error": "block out of range"}, 404)
        b = d["ctx"][idx]
        if mm.group(2):
            off = qint(q, "offset")
            lim = min(API_MAX_LIMIT, qint(q, "limit", 50, 1))
            users = block_users(d, idx)
            return json_bytes(paged([call_brief(d["calls"][k]) for k in list(users)[off:off + lim]],
                                    len(users), off, lim,
                                    "/api/s/%s/blocks/%d/users" % (sid, idx), q))
        text = b["t"]
        full = qstr(q, "full") == "1"
        off = qint(q, "offset", 0, 0, MAX_CHARS)
        lim = (max(0, len(text) - off) if full
               else qint(q, "limit", PART_CHARS, 1, MAX_CHARS))
        return json_bytes(env({
            "idx": idx, "kind": b["k"], "label": b["l"], "cmd": b["c"],
            "total_chars": len(text), "offset": off, "limit": lim,
            "text": text[off:off + lim], "eof": off + lim >= len(text),
            "html_url": "/s/%s/b/%d" % (sid, idx),
        }))
    mm = re.match(r"^/outputs/(\d+)$", rest)
    if mm:
        seq = int(mm.group(1))
        if seq < 1 or seq > len(d["calls"]):
            return json_bytes({"ok": False, "error": "call out of range"}, 404)
        seg = qint(q, "seg")
        outs = d["calls"][seq - 1]["out"]
        if not outs:
            return json_bytes(env({"seq": seq, "segments": [], "text": ""}))
        seg = min(seg, len(outs) - 1)
        text = outs[seg]["text"]
        off = qint(q, "offset", 0, 0, MAX_CHARS)
        lim = (max(0, len(text) - off) if qstr(q, "full") == "1"
               else qint(q, "limit", PART_CHARS, 1, MAX_CHARS))
        return json_bytes(env({
            "seq": seq, "seg": seg, "kind": outs[seg]["kind"], "label": outs[seg]["label"],
            "segments": [{"seg": k, "kind": x["kind"], "label": x["label"],
                          "chars": len(x["text"])} for k, x in enumerate(outs)],
            "total_chars": len(text), "offset": off, "limit": lim,
            "text": text[off:off + lim], "eof": off + lim >= len(text),
        }))
    mm = re.match(r"^/turns/(\d+)$", rest)
    if mm:
        n = int(mm.group(1))
        return json_bytes(env({
            "turn": turn_brief(d, d["turns"][n - 1]) if 0 < n <= len(d["turns"]) else None,
            "calls": [call_brief(d["calls"][k]) for k in d["by_turn"].get(n, [])],
        }))
    return json_bytes({"ok": False, "error": "unknown endpoint: " + path}, 404)


# ---------------------------------------------------------------- 路由 / 服务

HTML_ROUTES = [
    (r"^/$", lambda q, m: v_index(q)),
    (r"^/stats$", lambda q, m: v_stats(q)),
    (r"^/api$", lambda q, m: v_api_doc(q)),
    (r"^/search$", lambda q, m: v_search(q)),
    (r"^/s/([0-9A-Za-z_-]+)/search$", lambda q, m: v_search(q, m.group(1))),
    (r"^/s/([0-9A-Za-z_-]+)/calls$", lambda q, m: v_session(dict(q, view=["calls"]), m.group(1))),
    (r"^/s/([0-9A-Za-z_-]+)/t/(\d+)$", lambda q, m: v_turn(q, m.group(1), int(m.group(2)))),
    (r"^/s/([0-9A-Za-z_-]+)/c/(\d+)$", lambda q, m: v_call(q, m.group(1), int(m.group(2)))),
    (r"^/s/([0-9A-Za-z_-]+)/b/(\d+)$", lambda q, m: v_block(q, m.group(1), int(m.group(2)))),
    (r"^/s/([0-9A-Za-z_-]+)/o/(\d+)$", lambda q, m: v_output(q, m.group(1), int(m.group(2)))),
    (r"^/s/([0-9A-Za-z_-]+)$", lambda q, m: v_session(q, m.group(1))),
]


class Handler(BaseHTTPRequestHandler):
    server_version = "TokenTrace/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        if os.environ.get("TRACE_VERBOSE"):
            sys.stderr.write("  %s %s\n" % (self.address_string(), fmt % args))

    def _authorized(self):
        """--token 非空时校验 Bearer 头 / ?token= / Cookie。

        浏览器首次带 ?token= 访问会种下 Cookie，后续零 JS 的 <a href> 自动带上，
        不需要给每个链接拼令牌。
        """
        if not TOKEN:
            return True
        auth = self.headers.get("Authorization") or ""
        if hmac.compare_digest(auth, "Bearer " + TOKEN):
            return True
        q = parse_qs(urlsplit(self.path).query).get("token", [""])[0]
        if q and hmac.compare_digest(q, TOKEN):
            self._set_cookie = True
            return True
        for part in (self.headers.get("Cookie") or "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == "trace_token" and hmac.compare_digest(v, TOKEN):
                return True
        return False

    def _shutdown_ok(self):
        """挡住跨站 POST 与 DNS rebinding：Host 必须 loopback，来源必须同源。"""
        port = self.server.server_address[1]
        host = (self.headers.get("Host") or "").lower()
        host_only = host[1:].split("]")[0] if host.startswith("[") else host.split(":")[0]
        if host_only not in LOOPBACK:
            return False
        origin = (self.headers.get("Origin") or "").rstrip("/").lower()
        if origin and origin not in ("http://127.0.0.1:%d" % port,
                                    "http://localhost:%d" % port,
                                    "http://[::1]:%d" % port):
            return False
        sfs = (self.headers.get("Sec-Fetch-Site") or "").lower()
        if sfs and sfs not in ("same-origin", "none"):
            return False
        return self._authorized()

    def _drain_body(self):
        """消费请求体并关闭连接，避免 HTTP/1.1 下残留 body 被当成下一个请求行。"""
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        if n > 0:
            self.rfile.read(min(n, 65536))
        self.close_connection = True

    def _reply(self, status, ctype, payload):
        try:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            if getattr(self, "_set_cookie", False):
                self.send_header("Set-Cookie",
                                 "trace_token=%s; Path=/; HttpOnly; SameSite=Strict" % TOKEN)
            self.end_headers()
            if not getattr(self, "_head_only", False):
                self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self):
        u = urlsplit(self.path)
        path, q = u.path, parse_qs(u.query, keep_blank_values=True)
        t0 = time.time()
        if not self._authorized():
            self._reply(401, "application/json; charset=utf-8",
                        b'{"ok":false,"error":"unauthorized"}')
            return
        try:
            if path == "/favicon.ico":
                self.send_response(204)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if getattr(self, "_head_only", False) and path in (
                    "/stats", "/api/stats", "/search", "/api/search"):
                # HEAD 用于探活/链路检查，不触发全库遍历；其余路径仍走真实 GET 保持状态码语义
                status, ctype, payload = 200, (
                    "application/json; charset=utf-8" if path.startswith("/api/")
                    else "text/html; charset=utf-8"), b""
            elif path == "/health":
                status, ctype, payload = 200, "text/plain; charset=utf-8", b"ok"
            elif path.startswith("/api/"):
                status, ctype, payload = api_route(path, q)
            else:
                found = None
                for pat, fn in HTML_ROUTES:
                    m = re.match(pat, path)
                    if m:
                        found = (fn, m)
                        break
                if not found:
                    raise KeyError("no route: " + path)
                out = found[0](q, found[1])
                if isinstance(out, tuple):
                    status, ctype, payload = out
                else:
                    status, ctype, payload = 200, "text/html; charset=utf-8", out.encode("utf-8")
        except KeyError as e:
            status = 404
            ctype = "text/html; charset=utf-8"
            payload = page("404", [("索引", "/"), ("未找到", "")],
                           '<div class="warn">%s</div><a class="btn" href="/">回索引</a>' %
                           esc(str(e))).encode("utf-8")
        except Exception:
            traceback.print_exc()
            status = 500
            ctype = "text/html; charset=utf-8"
            payload = page("500", [("索引", "/"), ("错误", "")],
                           '<div class="warn">服务端出错，详见终端日志。</div>').encode("utf-8")
        self._reply(status, ctype, payload)
        if os.environ.get("TRACE_VERBOSE"):
            sys.stderr.write("  %3d %6.1f KB %5d ms  %s\n" %
                             (status, len(payload) / 1024.0,
                              (time.time() - t0) * 1000, self.path))

    def do_HEAD(self):
        self._head_only = True
        try:
            self.do_GET()
        finally:
            self._head_only = False

    def do_POST(self):
        """一键停止：POST /shutdown。校验来源，避免任意网页静默关停服务。"""
        if urlsplit(self.path).path != "/shutdown":
            self._drain_body()
            self.send_error(404)
            return
        self._drain_body()
        if not self._shutdown_ok():
            self._reply(403, "application/json; charset=utf-8",
                        b'{"ok":false,"error":"forbidden: cross-site or bad host/token"}')
            return
        self._reply(200, "text/plain; charset=utf-8", b"ok")
        threading.Thread(target=self.server.shutdown, daemon=True).start()


def main():
    # pythonw.exe（无控制台）下 sys.stdout/stderr 为 None，print() 会崩；
    # Windows 控制台默认 GBK，中文输出会乱码。两者都在这里兜住。
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")
    if os.name == "nt":
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (AttributeError, ValueError, OSError):
                pass
    ap = argparse.ArgumentParser(description="token-trace 完整模式服务（零脚本、按需渲染）")
    ap.add_argument("--host", default="127.0.0.1", help="默认只监听本机")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--cache", type=int, default=CACHE_SESSIONS, help="内存中保留几个已解析会话")
    ap.add_argument("--warm", action="store_true", help="启动时预解析全部会话（可选，约 3 秒）")
    ap.add_argument("--token", default=os.environ.get("TRACE_TOKEN") or "",
                    help="访问令牌；绑定非本机地址时必填，也可由 TRACE_TOKEN 提供")
    a = ap.parse_args()
    if a.host not in ("127.0.0.1", "localhost", "::1") and not a.token:
        ap.error("绑定非本机地址（--host %s）时必须用 --token 设置访问令牌，"
                 "否则会话原文会暴露在网络上" % a.host)
    global STORE, TOKEN
    TOKEN = a.token
    STORE = Store(a.cache)
    ss = STORE.sessions()
    print("索引 %d 个会话 / %d 个工作区" % (len(ss), len(set(s["ws"] for s in ss))))
    if a.warm:
        t0 = time.time()
        for s in ss:
            STORE.get(s["id"])
        print("预热完成 %.2fs" % (time.time() - t0))
    url = "http://%s:%d/" % (a.host, a.port)
    print("服务已启动: %s" % url)
    if TOKEN:
        print("  已启用访问令牌：浏览器用 %s?token=*** 打开，或带 Authorization: Bearer ***" % url)
    print("  纯 HTML 导航，无 JavaScript；每次请求只返回一屏。Ctrl+C 停止。")
    srv = ThreadingHTTPServer((a.host, a.port), Handler)
    srv.daemon_threads = True
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
    print("已停止")


if __name__ == "__main__":
    main()
