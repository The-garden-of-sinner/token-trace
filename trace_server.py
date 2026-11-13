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
import datetime
import hmac
import html
import http.client
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
import pricing  # noqa: E402  按刊例价估算成本，只用于排序与横向对比

# 每屏渲染上限 —— 调大只会让单页变重，不会让内容变多
PAGE_TURNS = 20        # 会话页每页轮数
PAGE_CALLS = 40        # 调用列表每页条数
PAGE_BLOCKS = 40       # 单次调用的上下文块列表每页条数
PAGE_SESSIONS = 32     # 首页每页会话数：约 0.6 KB/会话，32 条约 20 KB；
                       # 多平台接入后单页基数变大，40 条会贴到 80 KB 硬闸
PART_CHARS = 12000     # 全文每片字数（约 30 KB HTML）
SEARCH_MAX = 80        # 搜索结果上限
FIND_MAX_BLOCKS = 40   # 块检索结果每页块数（与 PAGE_BLOCKS 同量级）
FIND_MAX_HITS = 500    # 单块最多记录多少处命中位置
FIND_MAX_PARTS = 60    # 单块导航最多列多少个命中分片
FIND_CACHE_MAX = 8     # 缓存几个检索词的全库扫描结果，翻页不重扫全库原文
FIND_CACHE_TTL = 60    # 与会话索引同 TTL；源文件新增会话后结果不会一直陈旧
HL_MAX = 200           # 单片最多高亮多少处（守住单页体积）
CACHE_SESSIONS = 0     # 0 = 自适应：按会话数定容，避免 cap < 会话数 导致缓存永久失效
CACHE_MIN = 32         # 自适应下限
CACHE_MAX = 256        # 自适应上限：单会话约 1.5 MB，256 个约 380 MB；超出部分走 LRU
API_MAX_LIMIT = 500    # /api/ 单次返回条数上限
MAX_CHARS = 10_000_000  # 块/输出全文的字符 offset 上限，与 limit 同一量级
# 同机 WorkBuddy 用量监视（workbuddy-token-monitor --web）的地址。
# 不去猜它装在哪，只在 /api/ping 有应答时给一个跳转入口，避免页面上出现死链。
MONITOR_URL = (os.environ.get("TRACE_MONITOR_URL") or "http://127.0.0.1:8791/").strip()
MONITOR_TTL = 15       # 探测结果缓存秒数：成功失败都缓存，翻页不会每页连一次

# 平台注册表：切换栏 / 全库画像 / 会话行 chip 的名称与配色全从这里取。
# 新增平台时这里加一行，另在 sources/ 加适配器、在 bridge 注册即可。
# (key, 全称, 窄屏短名, 分类色)。全称用于图例、说明与宽屏切换栏；短名只在
# 窄屏顶替全称，避免 6 个长标签把标题栏挤成两行（hover 的 title 始终给全称）。
PLATFORMS = (
    ("workbuddy", "WorkBuddy", "WB", "var(--p0)"),
    ("zcode", "ZCode", "ZCode", "var(--p1)"),
    ("codex", "Codex", "Codex", "var(--p2)"),
    ("dsh", "DeepSeek Harness", "DSH", "var(--p3)"),
    ("antigravity", "Antigravity", "AG", "var(--p4)"),
)
PLAT_NAME = dict((k, full) for k, full, _s, _c in PLATFORMS)
PLAT_SHORT = dict((k, short) for k, _f, short, _c in PLATFORMS)
PLAT_COLOR = dict((k, c) for k, _f, _s, c in PLATFORMS)

TYPES = {"user": "你的输入", "injected": "系统注入", "tool_result": "工具结果",
         "tool_call": "工具调用参数", "assistant": "助手回答"}
ORDER = ["tool_result", "injected", "user", "tool_call", "assistant"]
# 分类色引用 CSS 令牌：同一组颜色同时用于行内 style、SVG 与图例，
# 切换深浅色时无需重算任何一行 HTML。短名 --p0..--p4 按 ORDER 顺序对应
# tool_result / injected / user / tool_call / assistant，与趋势渐变共用。
PALETTE = {"tool_result": "var(--p0)", "injected": "var(--p1)",
           "user": "var(--p2)", "tool_call": "var(--p3)",
           "assistant": "var(--p4)"}
# 块类型标签的配色类，与 PALETTE 一一对应；所有列出块类型的地方共用，
# 避免 /rank 是灰的、调用详情是彩的、搜索结果又是另一种写法。
BK_CLS = {"tool_result": "bk0", "injected": "bk1", "user": "bk2",
          "tool_call": "bk3", "assistant": "bk4"}
SID_RE = re.compile(r"^[0-9A-Za-z_-]+$")
TOKEN = ""          # --token / TRACE_TOKEN；非空时所有请求都要鉴权
LOOPBACK = {"127.0.0.1", "localhost", "::1", "[::1]"}


def esc(s):
    return html.escape("" if s is None else str(s), quote=True)


def type_chip(k):
    """块类型标签：五类各一色（BK_CLS），未知类型退回中性灰。"""
    return '<span class="chip t %s">%s</span>' % (
        BK_CLS.get(k, ""), esc(TYPES.get(k, k)))


def nfmt(n):
    return "{:,}".format(int(n or 0))


def fmt_ts(v, fmt="%Y-%m-%d %H:%M:%S", empty="时间未知"):
    """把 WorkBuddy 时间戳格式化成本地时间（毫秒 / 秒都接受）。

    jsonl 里的 timestamp 是毫秒。Windows CRT 的 localtime 对 1970 之前、
    3000 年之后的值会抛 OSError(Errno 22)，Linux 则静默给出 58375 年之类
    的荒谬结果；这里统一做量纲归一 + 范围校验，异常值退回 empty。
    """
    try:
        n = float(v)
        if not (0 < n < 1e14):
            return empty
        sec = n / 1000.0 if n > 1e11 else n
        if not (0 < sec < 32503680000):     # 1970-01-01 .. 3000-01-01
            return empty
        return time.strftime(fmt, time.localtime(sec))
    except (OSError, ValueError, OverflowError, TypeError):
        return empty


def cut(s, n):
    s = s or ""
    return s if len(s) <= n else s[:n] + "…"


# ---------------------------------------------------------------- 数据层

class Store(object):
    """会话索引 + 按需解析 + LRU 缓存。

    索引常驻；单个会话全文只在第一次访问时解析（最大会话 0.7 s）。

    容量默认**自适应**：cap 取 max(CACHE_MIN, 会话数 + 8)，上限 CACHE_MAX。
    这一点是必要的——全库统计 / 搜索这类路径要逐个遍历所有会话，若 cap 小于
    会话数，LRU 会在每轮把开头刚解析的会话淘汰掉，下一轮又全部重解析，
    缓存等于完全失效（33 个会话 / cap 32 时 /stats 实测 ~5 s，cap 33 时 ~40 ms）。
    会话数超过 CACHE_MAX 时退回 LRU，代价是 /stats 每次重新解析超出部分，
    可用 --cache 显式指定一个更大或更小的值。
    """

    def __init__(self, cap=CACHE_SESSIONS):
        self.auto = not cap or cap <= 0
        self.cap = CACHE_MIN if self.auto else cap
        self.lock = threading.RLock()
        self._index = None
        self._index_at = 0.0
        self._cache = OrderedDict()
        self._loading = {}          # sid -> Event，防止并发首访重复解析

    def sessions(self, refresh=False):
        with self.lock:
            if refresh or self._index is None or time.time() - self._index_at > 60:
                # refresh=True 强制重建列表对象（即使数据未变），让下游缓存确定性
                # 失效；自动刷新走签名快速路径，数据没变时复用同一个列表对象，
                # rank_rows / rank_sorted 等缓存继续命中，避免每 60 秒一次全量重算。
                self._index = G.scan_all(force=refresh)
                self._index_at = time.time()
                if refresh:
                    # 显式刷新同时丢弃会话全文缓存：宽表 / 调用明细走 STORE.get()
                    # 首次解析的快照，源 jsonl 追加新行后不会自动更新。不清就会出现
                    # /api/sessions 是新的、宽表还是旧的（实测两者 pin 差 3 倍）。
                    self._cache.clear()
            if self.auto:
                # 索引刷新后同步容量：让「全部会话」能同时驻留，避免抖动。
                self.cap = max(CACHE_MIN, min(len(self._index) + 8, CACHE_MAX))
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
        vfs = [c.get("vf", 0) for c in calls]   # 单调不减，供 block_users 求可见上界

        by_turn = {}
        for i, c in enumerate(calls):
            by_turn.setdefault(c.get("turn", 0), []).append(i)

        # 缓存冷启动：上一次 >80% 命中、这一次 <50%。
        # 压缩点的命中率骤降是「历史被替换」，不是缓存失效，单独归到 compactions。
        cold = []
        prev = None
        prev_vf = None
        for i, c in enumerate(calls):
            if not c["pin"]:
                continue
            r = c["hit"] / c["pin"]
            is_compact = prev_vf is not None and c["vf"] != prev_vf
            if prev is not None and prev > 0.8 and r < 0.5 and not is_compact:
                cold.append(i)
            prev = r
            prev_vf = c["vf"]

        # 可见区间 [vf, ctxN) 的字数；chars 仍是「归档原文总量」（全部块，全文可查），
        # visible_peak/last 才是「某次请求真正带上的量」。
        vis = [pref[c["ctxN"]] - pref[c.get("vf", 0)] for c in calls]
        retired = pref[vfs[-1]] if vfs else 0
        compacts = meta.get("_compactions", [])

        billed = [c for c in calls if c["pin"]]
        tin = sum(c["pin"] for c in billed)
        thit = sum(c["hit"] for c in billed)
        tcredit = sum(c.get("credit") or 0 for c in calls)
        sz = Counter()
        for b in ctx:
            sz[b["k"]] += b["n"]

        return {
            "s": s, "ctx": ctx, "calls": calls, "meta": meta, "turns": turns,
            "pref": pref, "ctxns": ctxns, "vfs": vfs, "by_turn": by_turn,
            "cold": cold, "compacts": compacts, "billed": billed,
            "title": meta.get("aiTitle") or s.get("title") or s["id"][:8],
            "stats": {
                "turns": len(turns), "calls": len(billed), "blocks": len(ctx),
                "chars": pref[-1], "pin": tin, "hit": thit,
                "miss": tin - thit, "pout": sum(c["pout"] for c in calls),
                "credit": tcredit, "rate": (thit / tin) if tin else 0,
                "model": (billed[0]["model"] if billed else "?"),
                "compose": {k: sz[k] for k in ORDER},
                "cold": len(cold), "compactions": len(compacts),
                "visible_peak": max(vis, default=0),
                "visible_last": vis[-1] if vis else 0,
                "retired_chars": retired,
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
    """第 seq 次调用里第 i 个块的状态 —— O(1)，不展开整个上下文。

    压缩后可见区间是 [vf, ctxN)：区间外的块返回 None（不是 hit/miss）。
    缓存边界按「可见区间内的相对字数」定位，再换回绝对下标。
    """
    c = d["calls"][seq]
    n = min(c["ctxN"], len(d["ctx"]))
    lo = min(c.get("vf", 0), n)
    if i < lo or i >= n:
        return None
    if not c["pin"]:
        return "none"
    base = d["pref"][lo]
    cut_chars = (d["pref"][n] - base) * c["hit"] / c["pin"]
    st, acc = d["pref"][i] - base, d["pref"][i + 1] - base
    if acc <= cut_chars:
        return "hit"
    if st >= cut_chars:
        return "miss"
    return "part"


def call_outline(d, seq):
    """单次调用的上下文概要（不取正文）。n 是可见块数，lo/hi 是绝对块号区间。"""
    c = d["calls"][seq]
    n = min(c["ctxN"], len(d["ctx"]))
    lo = min(c.get("vf", 0), n)
    total = d["pref"][n] - d["pref"][lo]
    base = {"n": n - lo, "lo": lo, "hi": n, "compacted": bool(lo)}
    if not c["pin"]:
        return dict(base, chars=total, ratio=None, cut=None,
                    boundary=None, hit_blocks=0, miss_blocks=0)
    ratio = c["hit"] / c["pin"]
    cut_chars = total * ratio
    b = bisect.bisect_left(d["pref"], d["pref"][lo] + cut_chars, lo, n + 1)
    boundary = max(lo, min(n - 1, b - 1)) if n > lo else None
    hit_blocks = sum(1 for i in range(lo, n) if call_state(d, seq, i) == "hit")
    return dict(base, chars=total, ratio=ratio, cut=cut_chars,
                boundary=boundary, hit_blocks=hit_blocks,
                miss_blocks=(n - lo) - hit_blocks)


def block_users(d, idx):
    """哪些调用带上了第 idx 块。

    ctxN 单调不减 → 下界用 bisect_right(ctxns, idx)；
    vf 也单调不减，压缩后 vf > idx 的调用不再带这个块 → 上界用 bisect_right(vfs, idx)。
    """
    lo = bisect.bisect_right(d["ctxns"], idx)
    hi = bisect.bisect_right(d.get("vfs", [0] * len(d["calls"])), idx)
    return range(lo, max(lo, hi))


def block_use_counts(d):
    """一次线性扫描算出每个块被多少调用携带，等价于逐块 len(block_users(d, i))。

    调用 k 携带的可见块区间是 [vf_k, ctxN_k)；对区间做差分再求前缀和，
    复杂度 O(块数 + 调用数)，比逐块两次二分 O(B log C) 快一个量级
    （本机最大会话 3906 块：3.6 ms vs 43 ms）。全库块排名与统计页复用。
    """
    n = len(d["ctx"])
    diff = [0] * (n + 1)
    for c in d["calls"]:
        hi = min(c["ctxN"], n)
        lo = min(c.get("vf", 0), hi)
        if lo < hi:
            diff[lo] += 1
            diff[hi] -= 1
    out = [0] * n
    run = 0
    for i in range(n):
        run += diff[i]
        out[i] = run
    return out


_MODEL_MAP = {"key": None, "val": {}}


def model_display_map():
    """全库统一的「归一键 -> 展示名」，让调用级 API 与会话索引口径一致。

    scan_all() 已按 providerData.model 分组、全库投票选出唯一展示名；但
    gen_trace 的逐调用记录里 model 仍是随客户端变化的原始写法（同一模型会
    出现三种大小写）。若 API 直接吐原始值，分析程序按 model 分组就会重新
    裂成多份。索引刷新会生成新的 list，故用 (id, len) 作缓存键自动失效。
    """
    ss = STORE.sessions()
    key = (id(ss), len(ss))
    if _MODEL_MAP["key"] != key:
        m = {}
        for s in ss:
            for name in (s.get("models") or {}):
                k = G.canon_model(name)
                if k:
                    m[k] = name
            if s.get("model"):
                k = G.canon_model(s["model"])
                if k and k not in m:
                    m[k] = s["model"]
        _MODEL_MAP["key"] = key
        _MODEL_MAP["val"] = m
    return _MODEL_MAP["val"]


def display_model(name):
    """把逐调用的原始模型名换成全库统一展示名；查不到就原样返回。

    gen_trace 对没有 providerData 模型名的调用兜底成字面量 "?"，网页里显示
    无妨，但给分析程序会变成一个假类别；API 层统一收敛成空串（见 docs/API.md）。
    """
    if not name or name == "?":
        return ""
    return model_display_map().get(G.canon_model(name), name or "")


def call_brief(c):
    """调用列表用的轻量字典，不含上下文与输出正文"""
    r = (c["hit"] / c["pin"]) if c["pin"] else None
    return {"seq": c["seq"], "ts": c["ts"], "model": display_model(c["model"]),
            "tool": c["tool"],
            "pin": c["pin"], "hit": c["hit"], "miss": c["pin"] - c["hit"],
            "pout": c["pout"], "credit": c.get("credit") or 0, "turn": c.get("turn", 0),
            "rate": r, "ctxN": c["ctxN"], "vf": c.get("vf", 0),
            "outputs": len(c["out"])}


# role=user 消息的来源分类，与 gen_trace.USER_KIND_LABEL 一致。
# WorkBuddy 把后台任务通知 / 压缩摘要 / 系统提醒也写成 user 消息，
# 所以「一轮」不等于「用户发的一条消息」。
TURN_KIND = OrderedDict([
    ("user", ("用户消息", "ku", "你输入的提问或指令")),
    ("task", ("后台任务", "kt", "后台任务状态通知，不是你输入的")),
    ("compact", ("压缩摘要", "kc", "上下文压缩产生的历史摘要，不是你输入的")),
    ("injected", ("系统注入", "ki", "只有系统提醒，没有你的正文")),
    ("other", ("内部消息", "ko", "其他内部消息")),
])


def turn_chip(kind):
    label, cls, why = TURN_KIND.get(kind, TURN_KIND["other"])
    return '<span class="chip %s" title="%s">%s</span>' % (cls, esc(why), esc(label))


def turn_brief(d, t):
    cs = [d["calls"][i] for i in d["by_turn"].get(t["i"], [])]
    pin = sum(c["pin"] for c in cs)
    hit = sum(c["hit"] for c in cs)
    first = next((c for c in cs if c["pin"]), None)
    return {"i": t["i"], "q": t["q"],
            "disp": t.get("disp") or t["q"], "kind": t.get("kind") or "other",
            "calls": len(cs), "pin": pin, "hit": hit,
            "rate": (hit / pin) if pin else 0,
            "pout": sum(c["pout"] for c in cs),
            "credit": sum(c["credit"] for c in cs),
            "inj": t.get("inj") or 0,
            "cold": bool(first and (first["hit"] / first["pin"]) < 0.5)}


def _pick_src(src):
    """按平台筛会话；src 为空表示全部。"""
    ss = STORE.sessions()
    if not src:
        return ss
    return [s for s in ss if (s.get("src") or "workbuddy") == src]


def global_stats(ss=None):
    ss = STORE.sessions() if ss is None else ss
    out = {"sessions": len(ss), "workspaces": len(set(s["ws"] for s in ss)),
           "calls": 0, "turns": 0, "blocks": 0, "chars": 0,
           "visible_peak_sum": 0, "compactions": 0,
           "pin": 0, "hit": 0, "pout": 0, "credit": 0, "compose": Counter(),
           "wb_miss": 0,
           "cost": 0.0, "unpriced": 0,
           "cold": [], "top_calls": [], "top_blocks": [], "costly_blocks": []}
    for s in ss:
        d = STORE.get(s["id"])
        st = d["stats"]
        for k in ("calls", "turns", "blocks", "chars", "pin", "hit", "pout", "credit"):
            out[k] += st[k]
        # 积分只有 WorkBuddy 侧有（Codex/ZCode 没有这个字段），单价分母也必须
        # 只取「有积分的会话」的未命中量，否则混入外部平台会把 unit_miss 拉低。
        if st["credit"]:
            out["wb_miss"] += st["miss"]
        out["visible_peak_sum"] += st["visible_peak"]
        out["compactions"] += st["compactions"]
        out["compose"].update(st["compose"])
        for i in d["cold"]:
            c = d["calls"][i]
            out["cold"].append({"sid": s["id"], "ws": s["ws"], "seq": c["seq"],
                                "ts": c["ts"], "pin": c["pin"], "hit": c["hit"],
                                "rate": c["hit"] / c["pin"], "credit": c["credit"]})
        # 排序键改成「按刊例价估算的成本」：WorkBuddy 的 credit 只在自家数据上有值，
        # 接多平台后 Codex/ZCode 没有积分字段，继续按 credit 排会让它们永远沉底。
        ranked = []
        for c in d["calls"]:
            cost = pricing.cost_cny(c["model"], c["pin"], c["hit"], c["pout"])
            out["cost"] += cost or 0.0
            out["unpriced"] += 1 if cost is None else 0
            ranked.append((cost or 0.0, c))
        for cost, c in sorted(ranked, key=lambda x: -x[0])[:5]:
            out["top_calls"].append({"sid": s["id"], "ws": s["ws"], "seq": c["seq"],
                                     "credit": c["credit"], "cost": cost, "pin": c["pin"],
                                     "rate": (c["hit"] / c["pin"]) if c["pin"] else 0,
                                     "tool": c["tool"]})
        for i, b in sorted(enumerate(d["ctx"]), key=lambda x: -x[1]["n"])[:3]:
            out["top_blocks"].append({"sid": s["id"], "idx": i, "chars": b["n"],
                                      "kind": b["k"], "label": b["l"]})
        # 「最贵的块」与「最大的块」口径不同：一个块哪怕不大，只要被成百上千次
        # 调用反复带上，累计输入量就很可观。cost = 估算 token × 被携带次数。
        # block_users 是二分查找，逐块算一次即可；估算 token 解析期已存好。
        uses = block_use_counts(d)
        costs = []
        for i, b in enumerate(d["ctx"]):
            # 旧缓存若没有 e，用字符数 /3 粗估（O(1)），避免为兜底扫长块全文。
            est = b.get("e") or (b["n"] // 3)
            u = uses[i]
            costs.append((est * u, i, est, u, b))
        for cost, i, est, u, b in sorted(costs, reverse=True)[:3]:
            out["costly_blocks"].append({"sid": s["id"], "idx": i,
                                         "kind": b["k"], "label": b["l"],
                                         "est": est, "users": u, "cost": cost})
    out["rate"] = (out["hit"] / out["pin"]) if out["pin"] else 0
    out["miss"] = out["pin"] - out["hit"]
    # 实测单价：积分 / 未命中输入（分母只含真正有积分的平台）
    billed_credit = out["credit"]
    out["unit_miss"] = (billed_credit / out["wb_miss"] * 10000) if out["wb_miss"] else 0
    out["unit_cost_miss"] = (out["cost"] / out["miss"] * 10000) if out["miss"] else 0
    out["cold"].sort(key=lambda x: -x["pin"])
    out["top_calls"].sort(key=lambda x: -(x["cost"] or 0))
    out["top_calls"] = out["top_calls"][:20]
    out["top_blocks"].sort(key=lambda x: -x["chars"])
    out["top_blocks"] = out["top_blocks"][:20]
    out["costly_blocks"].sort(key=lambda x: -x["cost"])
    out["costly_blocks"] = out["costly_blocks"][:20]
    out["compose"] = dict(out["compose"])
    return out


def search(q, sid=None, scope="all", limit=SEARCH_MAX, src=None):
    """跨会话全文搜索。用 re 直接在原文上扫，不复制一份小写副本。"""
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
    targets = [STORE.by_id(sid)] if sid else _pick_src(src)
    for s in targets:
        d = STORE.get(s["id"])
        if scope in ("all", "blocks"):
            for i, b in enumerate(d["ctx"]):
                m = rx.search(b["t"])
                if not m:
                    continue
                lo, hi = max(0, m.start() - 60), min(len(b["t"]), m.end() + 90)
                part = m.start() // PART_CHARS
                hits.append({"kind": "block", "sid": s["id"], "ws": s["ws"],
                             "idx": i, "label": b["l"], "btype": b["k"],
                             "chars": b["n"], "pos": m.start(), "part": part,
                             "snippet": b["t"][lo:hi],
                             "href": "/s/%s/b/%d?%s" %
                                     (s["id"], i, urlencode({"part": part, "q": q}))})
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


def hl_text(text, term, max_hits=HL_MAX):
    """在**原文**上按命中切段，再逐段 esc。

    先 esc 整段再跑正则会把 &amp; 之类实体从中间切开，拼出坏 HTML；
    切段后每段独立转义就不会。返回 (HTML, 实际高亮处数)。
    """
    if not term:
        return esc(text), 0
    rx = re.compile(re.escape(term), re.I)
    out, pos, n = [], 0, 0
    for m in rx.finditer(text):
        if n >= max_hits:
            break
        out.append(esc(text[pos:m.start()]))
        out.append("<mark>%s</mark>" % esc(m.group(0)))
        pos = m.end()
        n += 1
    out.append(esc(text[pos:]))
    return "".join(out), n


def block_hits(text, term):
    """某块里关键词的全部命中位置（封顶 FIND_MAX_HITS）与它们落在哪些分片。"""
    if not term:
        return [], []
    rx = re.compile(re.escape(term), re.I)
    pos, parts = [], set()
    for m in rx.finditer(text):
        pos.append(m.start())
        parts.add(m.start() // PART_CHARS)
        if len(pos) >= FIND_MAX_HITS:
            break
    return pos, sorted(parts)


FIND_CACHE = OrderedDict()   # (term, sid) -> (ts, (rows, warn, sessions, ms))


def find_blocks(term, sid=None, refresh=False):
    """按块聚合的精确检索：一个块一行，记下全部命中位置与分片分布。

    不建倒排索引，仍是流式正则扫描；结果按 (关键词, 会话) 缓存在进程内，
    翻页 / 回看 / 点进块页都不会重扫全库原文。调用方需持有 CALC_LOCK。
    """
    term = (term or "").strip()
    if len(term) < 2:
        return [], "关键词至少 2 个字", 0, 0
    if len(term) > 200:
        return [], "关键词过长", 0, 0
    key = (term, sid or "")
    now = time.time()
    if not refresh and key in FIND_CACHE:
        ts, cached = FIND_CACHE[key]
        if now - ts < FIND_CACHE_TTL:
            FIND_CACHE.move_to_end(key)
            return cached
        del FIND_CACHE[key]
    t0 = time.time()
    rx = re.compile(re.escape(term), re.I)
    rows = []
    targets = [STORE.by_id(sid)] if sid else STORE.sessions()
    for s in targets:
        d = STORE.get(s["id"])
        for i, b in enumerate(d["ctx"]):
            pos = []
            capped = False
            for m in rx.finditer(b["t"]):
                pos.append(m.start())
                if len(pos) >= FIND_MAX_HITS:
                    capped = True
                    break
            if not pos:
                continue
            parts = sorted(set(p // PART_CHARS for p in pos))
            lo, hi = max(0, pos[0] - 60), min(len(b["t"]), pos[0] + len(term) + 90)
            rows.append({
                "kind": "block", "sid": s["id"], "ws": s["ws"], "idx": i,
                "label": b["l"], "btype": b["k"], "chars": b["n"],
                "count": len(pos), "capped": capped,
                "positions": pos[:5], "parts": parts[:FIND_MAX_PARTS],
                "parts_total": len(parts), "pos": pos[0],
                "snippet": b["t"][lo:hi],
                "href": "/s/%s/b/%d?%s" %
                        (s["id"], i, urlencode({"part": parts[0], "q": term})),
            })
    warn = ("部分块命中超过 %d 处，只记录前 %d 处位置" % (FIND_MAX_HITS, FIND_MAX_HITS)
            if any(r["capped"] for r in rows) else "")
    result = (rows, warn, len(targets), int((time.time() - t0) * 1000))
    FIND_CACHE[key] = (now, result)
    FIND_CACHE.move_to_end(key)
    while len(FIND_CACHE) > FIND_CACHE_MAX:
        FIND_CACHE.popitem(last=False)
    return result


SHELL_TOOLS = ("Bash", "Shell", "execute_command")
SHELL_SKIP = {"sudo", "env", "time", "nohup", "command", "exec", "eval", "builtin"}
SHELL_CTRL = {"cd", "export", "set", "source", ".", "for", "if", "while",
              "case", "do", "done", "then", "else", "fi", "esac", "[", "{"}
SHELL_DECOR = {"echo", "printf", ":"}


def prog_of(cmd):
    """从 shell 命令摘要里 best-effort 抽「实际程序名」。

    跳过 cd/export/控制流前缀段（本机 1345 条命令以 `cd <dir> &&` 开头），
    以及后面还有实质命令的装饰性 echo 段。非 shell 内容由调用方过滤。
    """
    if not cmd or "://" in cmd[:12]:
        return ""
    segs = [x.strip() for x in re.split(r";|&&|\|\||\||\n", cmd.strip()) if x.strip()]
    for n, seg in enumerate(segs):
        toks = seg.split()
        while toks and (re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", toks[0])
                        or toks[0] in SHELL_SKIP):
            toks = toks[1:]
        if not toks or toks[0] in SHELL_CTRL:
            continue
        p = os.path.basename(toks[0]).lstrip("(\\")
        if not p or p[0] in "-'\"$#" or len(p) > 40:
            continue
        if p in SHELL_DECOR and n < len(segs) - 1:
            continue
        return p
    return "(shell 控制流)"


def trend_rows(d, granularity="call"):
    """逐调用的可见上下文演化，返回 (rows, max_chars, max_delta)。

    用「按类型字数前缀和」把每次调用的构成压到 O(1)：否则每次调用都遍历
    ctx[0:ctxN] 会退化成 O(C^2)。不新增索引、不落盘。
    """
    ctx, pref, calls = d["ctx"], d["pref"], d["calls"]
    acc = {k: [0] * (len(ctx) + 1) for k in ORDER}
    for i, b in enumerate(ctx):
        for k in acc:
            acc[k][i + 1] = acc[k][i]
        if b["k"] in acc:
            acc[b["k"]][i + 1] += b["n"]
    cold = set(d["cold"])
    compact_seqs = set()
    for i, c in enumerate(calls):
        if i and c.get("vf", 0) != calls[i - 1].get("vf", 0):
            compact_seqs.add(c["seq"])
    sid = d["s"]["id"]
    rows = []
    for i, c in enumerate(calls):
        n, lo = c["ctxN"], min(c.get("vf", 0), c["ctxN"])
        rows.append({
            "seq": c["seq"], "turn": c["turn"], "blocks": n - lo,
            "chars": pref[n] - pref[lo], "pin": c["pin"], "hit": c["hit"],
            "rate": c["hit"] / c["pin"] if c["pin"] else 0,
            "credit": c.get("credit") or 0, "ts": c.get("ts") or 0,
            "comp": {k: acc[k][n] - acc[k][lo] for k in ORDER},
            "cold": c["seq"] in cold,
            "compact": c["seq"] in compact_seqs,
            "href": "/s/%s/c/%d" % (sid, i + 1),
        })
    if granularity == "turn":
        by = {}
        for r in rows:
            by[r["turn"]] = r
        rows = list(by.values())
    prev = None
    for r in rows:
        if prev is None:
            r["delta"] = {"blocks": 0, "chars": 0, "rate": 0.0, "credit": 0.0}
        else:
            r["delta"] = {"blocks": r["blocks"] - prev["blocks"],
                          "chars": r["chars"] - prev["chars"],
                          "rate": r["rate"] - prev["rate"],
                          "credit": round(r["credit"] - prev["credit"], 3)}
        prev = r
    mx = max((r["chars"] for r in rows), default=1) or 1
    mxd = max((abs(r["delta"]["chars"]) for r in rows), default=1) or 1
    return rows, mx, mxd


def compact_rows(d):
    """压缩事件校验表：真实账单 vs 修正前/修正后的可见字数，逐条并列。

    只读取 rawUsage 的真实值 + pref 前缀和，不做任何估算；方向一致性由调用方判断。
    """
    calls, pref, ctx = d["calls"], d["pref"], d["ctx"]
    sid = d["s"]["id"]
    out = []
    for k, cp in enumerate(d.get("compacts", []), 1):
        at = cp["at"]
        # 压缩后第一条调用：第一个 vf >= at 的位置；真实 usage 取之后首个 pin>0 的
        first_i = next((i for i, c in enumerate(calls) if c.get("vf", 0) >= at), None)
        before_i = cp["before_seq"] - 1
        prev_billed = next((i for i in range(min(before_i, len(calls) - 1), -1, -1)
                            if calls[i]["pin"]), None)
        next_billed = next((i for i in range(first_i or 0, len(calls))
                            if calls[i]["pin"]), None)
        b = calls[prev_billed] if prev_billed is not None else None
        a = calls[next_billed] if next_billed is not None else None
        def vis(c, fixed):
            n, lo = c["ctxN"], (c.get("vf", 0) if fixed else 0)
            return pref[n] - pref[lo]
        rec = {
            "n": k, "at_block": at, "summary_chars": cp["chars"],
            "kind": cp["kind"], "is_summary": cp["is_summary"], "ts": cp["ts"],
            "before_seq": (b["seq"] if b else None), "after_seq": (a["seq"] if a else None),
            "before": ({"pin": b["pin"], "hit": b["hit"],
                        "rate": b["hit"] / b["pin"] if b["pin"] else 0,
                        "credit": b.get("credit") or 0,
                        "chars_old": vis(b, False), "chars_new": vis(b, True)} if b else None),
            "after": ({"pin": a["pin"], "hit": a["hit"],
                       "rate": a["hit"] / a["pin"] if a["pin"] else 0,
                       "credit": a.get("credit") or 0,
                       "chars_old": vis(a, False), "chars_new": vis(a, True)} if a else None),
            "block_href": "/s/%s/b/%d" % (sid, at),
        }
        if b and a and b["pin"] and a["pin"]:
            rec["pin_delta_pct"] = 100 * (a["pin"] - b["pin"]) / b["pin"]
            rec["chars_old_delta_pct"] = (100 * (rec["after"]["chars_old"] -
                                                 rec["before"]["chars_old"]) /
                                          max(rec["before"]["chars_old"], 1))
            rec["chars_new_delta_pct"] = (100 * (rec["after"]["chars_new"] -
                                                 rec["before"]["chars_new"]) /
                                          max(rec["before"]["chars_new"], 1))
            # 方向一致：修正后的可见字数与真实 pin 同号变化
            rec["consistent"] = (rec["pin_delta_pct"] * rec["chars_new_delta_pct"] > 0)
        out.append(rec)
    # 连续两次压缩、中间没有调用时，前一次的摘要立刻被后一次覆盖。
    # 数据忠实保留，但页面要标出来，避免看起来像算重了。
    for i, r in enumerate(out):
        r["superseded"] = (i + 1 < len(out) and
                           out[i + 1]["before_seq"] == r["before_seq"])
    return out


def tool_stats(sid=None, src=None):
    """按工具名聚合 token / 积分，按 shell 程序名聚合执行次数。"""
    targets = [STORE.by_id(sid)] if sid else _pick_src(src)
    tools, progs = {}, {}
    total_calls, total_credit = 0, 0.0
    for s in targets:
        d = STORE.get(s["id"])
        for c in d["calls"]:
            name = c["tool"] or "最终回答"
            t = tools.setdefault(name, {"tool": name, "calls": 0, "credit": 0.0,
                                        "pin": 0, "hit": 0, "pout": 0,
                                        "sessions": set(), "last_ts": 0})
            t["calls"] += 1
            t["credit"] += c.get("credit") or 0
            t["pin"] += c["pin"]
            t["hit"] += c["hit"]
            t["pout"] += c["pout"]
            t["sessions"].add(s["id"])
            t["last_ts"] = max(t["last_ts"], _ms(c.get("ts")))
            total_calls += 1
            total_credit += c.get("credit") or 0
        for b in d["ctx"]:
            if b["k"] != "tool_result" or not b["c"]:
                continue
            if not any(x in b["l"] for x in SHELL_TOOLS):
                continue
            p = prog_of(b["c"])
            if not p:
                continue
            g = progs.setdefault(p, {"prog": p, "runs": 0, "chars": 0})
            g["runs"] += 1
            g["chars"] += b["n"]
    for t in tools.values():
        t["session_count"] = len(t["sessions"])
        del t["sessions"]
        t["rate"] = t["hit"] / t["pin"] if t["pin"] else 0
        t["credit"] = round(t["credit"], 2)
        t["last"] = fmt_ts(t["last_ts"], "%m-%d %H:%M", "")
    return {"tools": sorted(tools.values(), key=lambda x: -x["calls"]),
            "progs": sorted(progs.values(), key=lambda x: -x["runs"]),
            "sessions": len(targets), "total_calls": total_calls,
            "total_credit": round(total_credit, 2),
            "shell_runs": sum(g["runs"] for g in progs.values())}


TOOL_CACHE = OrderedDict()
TOOL_CACHE_TTL = 60


def tool_stats_cached(sid=None, refresh=False, src=None):
    """全库遍历较慢且结果稳定，缓存 60 秒（与会话索引同 TTL）。"""
    key = (sid or "") + "|" + (src or "")
    now = time.time()
    if not refresh and key in TOOL_CACHE:
        ts, val = TOOL_CACHE[key]
        if now - ts < TOOL_CACHE_TTL:
            TOOL_CACHE.move_to_end(key)
            return val
    with CALC_LOCK:
        val = tool_stats(sid, src)
    TOOL_CACHE[key] = (now, val)
    TOOL_CACHE.move_to_end(key)
    while len(TOOL_CACHE) > 8:
        TOOL_CACHE.popitem(last=False)
    return val


def tool_calls(sid, tool, offset=0, limit=40, src=None):
    """某个工具的全部调用行（可跨会话），按积分降序，供点击下钻。"""
    rows = []
    for s in ([STORE.by_id(sid)] if sid else _pick_src(src)):
        d = STORE.get(s["id"])
        for i, c in enumerate(d["calls"]):
            if (c["tool"] or "最终回答") != tool:
                continue
            rows.append({"sid": s["id"], "ws": s["ws"], "seq": c["seq"],
                         "turn": c["turn"], "credit": c.get("credit") or 0,
                         "pin": c["pin"], "hit": c["hit"], "ts": c.get("ts") or 0,
                         "href": "/s/%s/c/%d" % (s["id"], i + 1)})
    rows.sort(key=lambda x: -x["credit"])
    return rows[offset:offset + limit], len(rows)


# ---------------------------------------------------------------- HTML 外观

# 设计令牌：分类色、语义色与主题。行内 style / SVG 一律引用变量，
# 深浅色只在 CSS 末尾的 media 里切换一次；估算值统一带 ≈ 前缀。
CSS = """
:root{
/* 让仍由浏览器绘制的控件（滚动条等）跟随页面明暗；日期 / 下拉已改为自绘 */
color-scheme:light;
--bg:#fff;--fg:#2C2C2A;--panel:#F7F6F2;--panel2:#FAFAF7;--track:#F1EFE8;
--line:#E3E1D9;--line2:#EDEBE3;--border:#D3D1C7;--muted:#5F5E5A;--faint:#888780;--dim:#B4B2A9;
/* 主色是低饱和墨青，与五类数据色分开一档；文字链接用近墨灰，
   hover 才浮出墨青与细下划线，避免整页亮蓝/蓝紫的廉价感。 */
--accent:#2E7D74;--accent-d:#245F58;--accent-bg:#E8F2F0;--accent-fg:#1E5550;
--link:#3E4B4D;--link-h:#1D6C64;--rule:#C4D5D1;
--neg:#A32D2D;--neg2:#E24B4A;--neg-bg:#FCEBEB;--credit:#7A5326;
--warn:#C08A2E;--warn-bg:#F8EFE0;--warn-fg:#8A6220;--ok-bg:#E1F0EC;--ok-fg:#1F6155;
--part-bg:#ECEEF2;--part-fg:#59627A;--mark:#F2DFA0;
--p0:#1FA39A;--p1:#EF9F27;--p2:#3B82D6;
--p3:#8A63D2;--p4:#D26A9B;
--shadow:0 1px 2px #0000000a,0 2px 8px #00000008;
/* 交互缓动：起步利落、收尾缓，hover / 点击反馈共用这条曲线 */
--ease:cubic-bezier(.22,.68,.24,1);
/* 入场缓动：交互曲线起步过猛（前 20% 就走完大半），用在 0.5s 的入场
   动画上会看成「啪一下就结束」。入场改用整段都有可见变化的标准曲线。 */
--ease-in:cubic-bezier(.4,0,.2,1)
}
*{box-sizing:border-box}
body{margin:0;padding:0 0 84px;font:14px/1.65 system-ui,-apple-system,"Noto Sans CJK SC",sans-serif;color:var(--fg);background:var(--bg)}
/* 全局兜底：提示条 / 折叠内容等内联 <a> 原先落回浏览器默认亮蓝，
   统一为近墨灰 + 淡色细下划线；下面各组件再用更高特异性覆盖。 */
a{color:var(--link);text-decoration-color:var(--rule);text-underline-offset:3px}
a:hover{color:var(--link-h)}
.wrap{max-width:1120px;margin:0 auto;padding:0 26px}
header{background:var(--panel);border-bottom:1px solid var(--line);padding:16px 0 13px;margin-bottom:20px}
.crumb{font-size:11.5px;color:var(--faint);margin-bottom:5px}
.crumb a{color:var(--link);text-decoration:none;text-underline-offset:3px}
.crumb a:hover{color:var(--link-h);text-decoration:underline;text-decoration-color:var(--rule)}
.crumb .sep{color:var(--dim);margin:0 6px}
h1{font-size:19px;font-weight:500;margin:0 0 3px}
/* 标题与小字同一行：小字靠右，省掉一行高度；窄屏自动折回标题下方 */
.hdrow{display:flex;align-items:baseline;gap:16px;flex-wrap:wrap}
.hdrow h1{margin:0}
.sub{color:var(--muted);font-size:12px;margin-left:auto;text-align:right;max-width:62%}
@media(max-width:720px){.sub{margin-left:0;text-align:left;max-width:none}}
/* 首页：平台切换与大标题同栏。这里自带一份分段控件样式，不依赖 CSS_INDEX
   片段是否加载；其余页面不渲染 .platbar，不受影响。 */
.sub.sub-wide{margin-left:0;text-align:left;max-width:none;margin-top:5px}
.platbar{display:inline-flex;align-items:center;gap:8px;margin-left:auto;flex-wrap:wrap}
.platbar .tb-lab{font-size:10.5px;letter-spacing:.06em;color:var(--faint)}
.platbar .tb-seg{display:inline-flex;background:var(--track);border-radius:999px;padding:2px;
 flex-wrap:wrap;justify-content:flex-end}
.platbar .tb-seg a,.platbar .tb-seg .off{padding:4px 10px;border-radius:999px;font-size:10.5px;
 color:var(--muted);white-space:nowrap}
.platbar .tb-seg a{text-decoration:none;
 transition:background-color .28s var(--ease),color .28s var(--ease)}
.platbar .tb-seg a:hover{color:var(--link-h)}
.platbar .tb-seg a.on{background:var(--bg);color:var(--fg);font-weight:600;box-shadow:var(--shadow)}
/* 平台多、其中两个全称偏长：常规宽度一律显示全称，只有很窄的视口才换短名，
   免得把「大标题 + 平台」这一栏挤成两行；hover 的 title 始终给全称。 */
.platbar .pshort{display:none}
@media(max-width:900px){
 .platbar .pfull{display:none}
 .platbar .pshort{display:inline}
 .platbar .tb-seg a,.platbar .tb-seg .off{padding:4px 8px}
}
/* 本机没有该平台的会话：不可点，靠 title 说明原因（无 X 会话记录） */
.platbar .tb-seg .off{color:var(--faint);opacity:.55;cursor:not-allowed;user-select:none}
@media(max-width:720px){.platbar{margin-left:0}}
.badge{display:inline-block;font-size:10.5px;color:var(--ok-fg);background:var(--ok-bg);border-radius:9px;padding:0 8px;line-height:17px;margin-left:8px;vertical-align:2px}
h2{font-size:13px;font-weight:500;margin:24px 0 9px;padding-bottom:6px;border-bottom:1px solid var(--line);color:var(--fg)}
h2 small{font-weight:400;color:var(--dim);font-size:11.5px;margin-left:8px}
h3{font-size:12.5px;font-weight:500;margin:18px 0 7px;color:var(--fg)}
/* KPI 是一条连续数字带：1px 网格间隙露出底色当分隔线，换行也自动对齐，
   不再是一排各自带阴影的小方块。首页 hero 再覆盖成无边界版本。
   列宽不用等分：由 kpi() 按每格「文字宽度估算」算出 fr 表（--kcols），
   两位数的「会话」不会占掉和九位 token 一样的位置。 */
.kpi{display:grid;grid-template-columns:var(--kcols,repeat(6,minmax(0,1fr)));gap:1px;
 background:var(--line2);border:1px solid var(--line);border-radius:12px;overflow:hidden;margin-bottom:16px}
.kpi div{background:var(--panel2);padding:10px 14px;min-width:0}
.kpi b{display:block;font-size:18px;font-weight:550;line-height:1.35;letter-spacing:-.02em;
 font-variant-numeric:tabular-nums;white-space:nowrap}
.kpi span{font-size:10.5px;letter-spacing:.04em;color:var(--muted);display:block;
 overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
@media(max-width:980px){.kpi{grid-template-columns:repeat(auto-fit,minmax(140px,1fr))}}
@media(max-width:640px){.kpi{grid-template-columns:repeat(2,minmax(0,1fr))}}
.compose{display:flex;height:19px;border-radius:5px;overflow:hidden;background:var(--track);margin-bottom:6px}
.compose i{display:block;height:100%;transform-origin:left center;animation:tt-bar .62s var(--ease-in) backwards}
@keyframes tt-bar{from{transform:scaleX(.02)}}
.legend{font-size:11px;color:var(--muted);margin-bottom:12px;display:flex;gap:12px;flex-wrap:wrap}
.legend span{display:inline-flex;align-items:center;gap:5px}
.legend i{display:inline-block;width:9px;height:9px;border-radius:2px}
.legend b{display:inline-block;width:9px;height:9px;border-radius:2px;margin-right:4px;vertical-align:-1px}
.note{background:var(--accent-bg);border-left:2px solid var(--accent);padding:9px 13px;border-radius:0 7px 7px 0;font-size:12px;color:var(--accent-fg);margin-bottom:16px}
.warn{background:var(--warn-bg);border-left:2px solid var(--warn);padding:9px 13px;border-radius:0 7px 7px 0;font-size:12px;color:var(--credit);margin-bottom:16px}
.fine{color:var(--faint);font-size:11px}
/* 检索区：无外框、单行——匹配方式 / 范围在左，输入框吃掉剩余宽度，按钮贴右。
   窄屏放不下时 sb-main 整体折到下一行，不会把输入框压成一条缝。 */
form.searchbox{display:flex;flex-direction:row;align-items:center;gap:10px;
 flex-wrap:wrap;background:transparent;border:0;padding:0;margin:0 0 18px}
.sb-tools{display:flex;align-items:center;gap:9px;flex-wrap:wrap;flex:none}
.sb-main{display:flex;gap:8px;align-items:center;flex:1 1 340px;min-width:0;flex-wrap:nowrap}
form.searchbox input[type=text]{flex:1;min-width:220px;padding:11px 16px;
 border:1px solid transparent;border-radius:11px;font:13px inherit;
 background:var(--track);color:var(--fg);box-shadow:inset 0 1px 2px #0000000a;
 transition:border-color .28s var(--ease),background-color .28s var(--ease)}
form.searchbox input[type=text]::placeholder{color:var(--faint)}
form.searchbox input[type=text]:focus{outline:none;border-color:var(--accent);background:var(--bg)}
form.searchbox select{appearance:none;-webkit-appearance:none;-moz-appearance:none;
 padding:6px 26px 6px 11px;border:1px solid transparent;border-radius:999px;
 background-color:var(--track);background-repeat:no-repeat;background-position:right 9px center;
 background-image:url("data:image/svg+xml;utf8,%3Csvg xmlns='http://www.w3.org/2000/svg' width='9' height='6' viewBox='0 0 9 6'%3E%3Cpath d='M1 1l3.5 3.5L8 1' fill='none' stroke='%238a8a86' stroke-width='1.4' stroke-linecap='round'/%3E%3C/svg%3E");
 color:var(--muted);font:11px inherit;cursor:pointer}
form.searchbox select:hover{border-color:var(--line)}
form.searchbox select:focus{outline:none;border-color:var(--accent);color:var(--fg)}
/* 自绘下拉：原生 <select> 的弹层由浏览器绘制，黑边 / 直角改不掉；
   details + 链接菜单零脚本，圆角、阴影、选中态都可控。 */
.sb-lab{font-size:10.5px;color:var(--faint);font-weight:500;letter-spacing:.03em}
.pick{position:relative;display:inline-block}
.pick>summary{display:flex;align-items:center;list-style:none;cursor:pointer;
 padding:6px 28px 6px 12px;border:1px solid transparent;border-radius:999px;
 background:var(--track);color:var(--muted);font-size:11px;position:relative;user-select:none}
.pick>summary::-webkit-details-marker{display:none}
.pick>summary::after{content:"";position:absolute;right:11px;top:50%;width:6px;height:6px;
 border-right:1.4px solid currentColor;border-bottom:1.4px solid currentColor;
 transform:translateY(-70%) rotate(45deg);transition:transform .24s var(--ease)}
.pick[open]>summary::after{transform:translateY(-30%) rotate(-135deg)}
.pick>summary:hover{border-color:var(--line);color:var(--fg)}
.pick[open]>summary{border-color:var(--accent);color:var(--fg);background:var(--bg)}
.pickmenu{position:absolute;z-index:30;top:calc(100% + 5px);left:0;
 min-width:max(100%,150px);padding:5px;background:var(--panel);
 border:1px solid var(--line);border-radius:11px;
 box-shadow:0 10px 28px #00000024,0 2px 6px #00000014;
 display:flex;flex-direction:column;gap:1px}
.pickmenu a{padding:7px 11px;border-radius:7px;color:var(--fg);
 text-decoration:none;font-size:12px;white-space:nowrap}
.pickmenu a:hover{background:var(--accent-bg);color:var(--accent-fg)}
.pickmenu a.on{background:var(--accent);color:var(--bg);font-weight:500}
@keyframes pick-in{from{opacity:0;transform:translateY(-5px)}to{opacity:1;transform:none}}
.pick[open] .pickmenu{animation:pick-in .28s var(--ease-in) backwards}
.modepick{display:inline-flex;gap:2px;background:var(--track);border-radius:999px;padding:3px}
.modepick label{position:relative;cursor:pointer}
.modepick input{position:absolute;opacity:0;width:0;height:0}
.modepick label>span{display:block;padding:5px 14px;border-radius:999px;font-size:11px;
 color:var(--muted);transition:background-color .28s var(--ease),color .28s var(--ease),box-shadow .28s var(--ease)}
.modepick input:checked + span{background:var(--bg);color:var(--fg);font-weight:600;box-shadow:var(--shadow)}
.modepick input:focus-visible + span{outline:2px solid var(--accent);outline-offset:2px}
form.searchbox button{background:var(--accent);color:var(--bg);border:1px solid var(--accent);
 padding:10px 22px;border-radius:11px;font:12px inherit;font-weight:500}
form.searchbox button:hover{background:var(--link-h);border-color:var(--link-h);color:var(--bg)}
/* 自绘日期选择：原生 <input type=date> 的日历弹层由浏览器绘制，
   那圈直角黑边是浏览器 UI 层，color-scheme 只能改配色、改不掉边框。
   改成 details + 链接日历：零脚本，圆角 / 阴影 / 选中态完全可控。 */
.datepick{position:relative;display:inline-block}
.datepick[open]{z-index:40}
.datepick>summary{list-style:none;cursor:pointer;padding:6px 12px;
 border:1px solid transparent;border-radius:999px;background:var(--track);
 color:var(--muted);font-size:11px;white-space:nowrap;user-select:none;
 font-variant-numeric:tabular-nums;line-height:1.5}
.datepick>summary::-webkit-details-marker{display:none}
.datepick>summary:hover{border-color:var(--line);color:var(--fg)}
.datepick[open]>summary{border-color:var(--accent);color:var(--fg);background:var(--bg)}
.calpop{position:absolute;z-index:40;top:calc(100% + 6px);left:0;width:226px;
 padding:10px;background:var(--panel);border:1px solid var(--line);
 border-radius:11px;box-shadow:0 10px 28px #00000024,0 2px 6px #00000014;
 animation:pick-in .24s var(--ease-in) backwards}
.calhd{display:flex;align-items:center;justify-content:space-between;margin-bottom:8px}
.calhd b{font-size:11.5px;font-weight:600}
.calhd a{display:inline-flex;align-items:center;justify-content:center;
 width:22px;height:22px;border-radius:6px;color:var(--muted);text-decoration:none;font-size:15px}
.calhd a:hover{background:var(--accent-bg);color:var(--accent-fg)}
.calgrid{display:grid;grid-template-columns:repeat(7,1fr);gap:2px}
.calw,.cald{display:flex;align-items:center;justify-content:center;height:26px;
 font-size:11px;border-radius:6px}
.calw{color:var(--faint);font-size:10px}
.cald{color:var(--fg);text-decoration:none;font-variant-numeric:tabular-nums}
.cald:hover{background:var(--accent-bg);color:var(--accent-fg)}
.cald.out{color:var(--dim);opacity:.55}
.cald.today{box-shadow:inset 0 0 0 1px var(--accent)}
.cald.on{background:var(--accent);color:var(--bg);font-weight:600}
.cald.on:hover{background:var(--accent-d);color:var(--bg)}
.calft{display:flex;justify-content:space-between;margin-top:8px;padding-top:7px;
 border-top:1px solid var(--line2)}
.calft a{font-size:11px;color:var(--link);text-decoration:none}
.calft a:hover{color:var(--link-h)}
.sb-sep{color:var(--faint);font-size:11px}
button,.btn{display:inline-block;padding:6px 13px;border:1px solid var(--border);border-radius:7px;background:var(--bg);color:var(--fg);font:12px inherit;text-decoration:none;cursor:pointer}
button:hover,.btn:hover{border-color:var(--accent);color:var(--link-h);background:var(--accent-bg)}
.btn.on{background:var(--accent);color:var(--bg);border-color:var(--accent)}
.tabs{display:flex;gap:6px;margin:14px 0;flex-wrap:wrap}
.row{display:grid;align-items:center;gap:9px;padding:7px 11px;border:1px solid var(--line);border-radius:7px;margin-bottom:4px;background:var(--bg);color:var(--fg);font-size:12px}
.row:hover{border-color:var(--accent);background:var(--panel);color:var(--fg)}
.row .grow{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.row a,.row a:visited{color:var(--link);text-decoration:none;text-underline-offset:3px}
.row a:hover{color:var(--link-h);text-decoration:underline;text-decoration-color:var(--rule)}
.row .num{font-size:11.5px;color:var(--muted);font-variant-numeric:tabular-nums;text-align:right;white-space:nowrap}
.row .num.credit{color:var(--credit);font-weight:500}
.row.bad{border-left:3px solid var(--neg2)}
.row.warn2{border-left:3px solid var(--warn)}
.idx{color:var(--faint);font-size:11.5px;min-width:38px;font-variant-numeric:tabular-nums}
.bar{height:9px;background:var(--track);border-radius:3px;overflow:hidden;display:flex;min-width:60px}
.bar i{display:block;height:100%}
.bar .h{background:var(--accent)}.bar .m{background:var(--warn)}
a:focus-visible,button:focus-visible,summary:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
.trow{display:grid;grid-template-columns:56px 44px 56px 82px 76px 64px 68px 68px;gap:7px;padding:5px 9px;border-bottom:1px solid var(--line2);font-size:11.5px}
.trow.hd{color:var(--muted);font-weight:500;background:var(--panel)}
.trow .num{text-align:right;font-variant-numeric:tabular-nums}
.chip{display:inline-block;font-size:10.5px;padding:0 7px;border-radius:9px;line-height:17px;white-space:nowrap}
.chip.hit{background:var(--accent-bg);color:var(--accent-d)}.chip.miss{background:var(--warn-bg);color:var(--warn-fg)}
.chip.part{background:var(--part-bg);color:var(--part-fg)}.chip.none{background:var(--track);color:var(--faint)}
.chip.cold{background:var(--neg-bg);color:var(--neg)}
.chip.t{background:var(--track);color:var(--muted)}
/* 轮来源标签：真人输入用实心主色，其余按来源淡底区分 */
.chip.ku{background:var(--accent);color:var(--bg);font-weight:600}
.chip.kt{background:var(--warn-bg);color:var(--warn-fg)}
.chip.kc{background:var(--part-bg);color:var(--part-fg)}
.chip.ki,.chip.ko{background:var(--track);color:var(--muted)}
/* 会话列表：子代理会话与其派生主会话 */
.chip.ks{background:var(--part-bg);color:var(--part-fg)}
.chip.km{background:var(--accent-bg);color:var(--accent-fg)}
/* 块类型标签：与全库画像图例 / 上下文趋势带共用 --p0..--p4，
   tool_result / injected / user / tool_call / assistant 各一色，深浅主题自动跟随。
   color-mix 只把 16% 的主色混进面板底色做背景，文字用 72% 主色混近墨色，
   保证浅色主题下也够对比；前面留 var(--track) 兜底不支持 color-mix 的浏览器。 */
.chip.bk0{background:var(--track);background:color-mix(in srgb,var(--p0) 16%,var(--panel));
 color:var(--muted);color:color-mix(in srgb,var(--p0) 72%,var(--fg))}
.chip.bk1{background:var(--track);background:color-mix(in srgb,var(--p1) 16%,var(--panel));
 color:var(--muted);color:color-mix(in srgb,var(--p1) 72%,var(--fg))}
.chip.bk2{background:var(--track);background:color-mix(in srgb,var(--p2) 16%,var(--panel));
 color:var(--muted);color:color-mix(in srgb,var(--p2) 72%,var(--fg))}
.chip.bk3{background:var(--track);background:color-mix(in srgb,var(--p3) 16%,var(--panel));
 color:var(--muted);color:color-mix(in srgb,var(--p3) 72%,var(--fg))}
.chip.bk4{background:var(--track);background:color-mix(in srgb,var(--p4) 16%,var(--panel));
 color:var(--muted);color:color-mix(in srgb,var(--p4) 72%,var(--fg))}
/* 多选种类下拉：勾标记让「已选中的多类」一眼可辨，不再像单选 */
.pickmenu a.on::before{content:"✓";margin-right:6px;font-weight:600}
.row.subagent{border-left:2px solid var(--p3)}
/* 首页子代理树：父行下缩进挂载，纯 details 折叠，零脚本 */
.subtree{margin:2px 0 7px 16px;padding-left:14px;border-left:1px solid var(--line2)}
.subtree>summary{list-style:none;cursor:pointer;display:flex;align-items:center;gap:8px;font-size:10.5px;color:var(--faint);padding:3px 0 5px}
.subtree>summary::-webkit-details-marker{display:none}
.subtree>summary .st-n:before{content:"▸ ";color:var(--p3)}
.subtree[open]>summary .st-n:before{content:"▾ "}
.subtree>summary .st-h{margin-left:auto}
.subtree[open]>summary .st-h{font-size:0}
.subtree[open]>summary .st-h:after{content:"收起";font-size:10px}
.subtree .row.subagent{margin-bottom:3px;background:var(--panel2)}
.subtree .row .branch{color:var(--p3);font-weight:600;margin-right:6px}
.row .grow>.chip{margin-right:7px;vertical-align:1px}
.note .chip{margin-right:9px;vertical-align:1px}
.cmd{display:block;font:11px/1.5 ui-monospace,"Noto Sans Mono CJK SC",monospace;color:var(--muted);word-break:break-all;margin-top:3px}
.pager{display:flex;gap:6px;align-items:center;flex-wrap:wrap;margin:16px 0 6px;font-size:12px;color:var(--muted)}
.pager .info{margin:0 8px;font-variant-numeric:tabular-nums}
.pager .btn{min-width:64px;text-align:center}
.pager .btn.off{opacity:.35;pointer-events:none}
pre.text{margin:12px 0;padding:13px 15px;background:var(--panel);border:1px solid var(--line);border-radius:8px;
  font:12px/1.7 ui-monospace,"Noto Sans Mono CJK SC",monospace;white-space:pre-wrap;word-break:break-word;overflow:auto;max-height:70vh}
mark{background:var(--mark);color:var(--fg);padding:0 1px;border-radius:2px}
.snip{font:11.5px/1.6 ui-monospace,"Noto Sans Mono CJK SC",monospace;color:var(--muted);
  background:var(--panel);border-radius:5px;padding:6px 9px;margin-top:4px;word-break:break-word}
.split{display:flex;align-items:center;gap:8px;font-size:11px;color:var(--warn-fg);margin:10px 0}
.split:before,.split:after{content:"";flex:1;height:1px;background:var(--warn);opacity:.5}
table{width:100%;border-collapse:collapse;font-size:12px}
th,td{text-align:left;padding:6px 9px;border-bottom:1px solid var(--line2);font-variant-numeric:tabular-nums}
th{color:var(--muted);font-weight:500;font-size:11.5px;background:var(--panel)}
td.r,th.r{text-align:right}
footer{margin-top:26px;padding-top:10px;border-top:1px solid var(--line2);font-size:11px;color:var(--dim);display:flex;gap:14px;flex-wrap:wrap}
footer a{color:var(--faint);text-decoration:none}
footer a:hover{color:var(--link-h)}
details.grp{margin-bottom:9px;border:1px solid var(--line);border-radius:9px;overflow:hidden}
details.grp>summary{padding:9px 13px;cursor:pointer;list-style:none;background:var(--panel2);font-size:12.5px}
details.grp>summary::-webkit-details-marker{display:none}
details.grp>summary small{color:var(--dim);font-weight:400;margin-left:8px}
details.grp .in{padding:8px 11px 3px}
code{font:11.5px ui-monospace,monospace;background:var(--track);padding:1px 4px;border-radius:3px}
.apirow{display:grid;grid-template-columns:minmax(260px,1fr) 2fr;gap:10px;padding:7px 10px;border-bottom:1px solid var(--line2);font-size:12px}
.apirow code{background:none;padding:0;color:var(--link)}
/* 跨文档视图过渡：同源跳转整页淡入淡出，纯 CSS 声明，不支持的浏览器直接忽略。
   root 只动 opacity——整页快照纹理做位移要在集显上重采样两层全屏位图，
   是「切页发涩」的主要开销；位移感留给少量页内元素表达。
   入场一律换用 --ease-in：交互曲线起步过猛，0.5s 的过渡会看成 100ms 硬切。 */
@view-transition{navigation:auto}
::view-transition-old(root){animation:vt-out .2s var(--ease-in) both}
::view-transition-new(root){animation:vt-in .46s var(--ease-in) both}
@keyframes vt-out{to{opacity:0}}
@keyframes vt-in{from{opacity:.35}}
/* KPI 局部命名：跨页时数字区独立淡入，不与整页一起糊成一片。 */
.kpi{view-transition-name:trace-kpi}
::view-transition-group(trace-kpi){animation-duration:.46s}
::view-transition-old(trace-kpi){animation:vt-fade .2s ease both}
::view-transition-new(trace-kpi){animation:vt-kpi .46s var(--ease-in) both}
@keyframes vt-fade{to{opacity:0}}
@keyframes vt-kpi{from{opacity:.4}to{opacity:1}}
/* 页内入场：错峰层次只留给数字带、导航卡、会话分组三处。
   实测首页 34 个活动动画与首次布局争主线程，触发 250~320ms 长帧，
   把动画前一半整段吃掉；削到约 12 个后帧率与可见动画时长都明显恢复。
   用 backwards 而非 both：动画一结束就释放合成层，不长期占着显存。 */
@keyframes tt-in{from{opacity:.78;transform:translateY(8px)}to{opacity:1;transform:none}}
.kpi>div,.navcard,details.grp{animation:tt-in .52s var(--ease-in) backwards}
.navcard:nth-child(2){animation-delay:.05s}.navcard:nth-child(3){animation-delay:.1s}
.navcard:nth-child(4){animation-delay:.15s}
details.grp:nth-of-type(2){animation-delay:.05s}
details.grp:nth-of-type(3){animation-delay:.1s}
details.grp:nth-of-type(n+4){animation-delay:.14s}
.kpi>div:nth-child(2){animation-delay:.05s}.kpi>div:nth-child(3){animation-delay:.1s}
.kpi>div:nth-child(4){animation-delay:.15s}.kpi>div:nth-child(5){animation-delay:.2s}
.kpi>div:nth-child(6){animation-delay:.25s}.kpi>div:nth-child(n+7){animation-delay:.3s}
.row,.btn,button,.chip,footer a,.crumb a{transition-property:border-color,background-color,color,transform,opacity;
 transition-duration:.32s;transition-timing-function:var(--ease)}
.btn:active,button:active{transform:translateY(1px)}
@supports (interpolate-size:allow-keywords){
 details.grp{interpolate-size:allow-keywords}
 details.grp::details-content{block-size:0;overflow:hidden;transition:block-size .48s var(--ease),content-visibility .48s var(--ease) allow-discrete}
 details.grp[open]::details-content{block-size:auto}
}
/* 返回上一层：链接从面包屑倒数第二项自动推导，长内容页也始终可见 */
.back{position:fixed;right:20px;bottom:20px;z-index:20;display:inline-flex;align-items:center;gap:8px;
 padding:9px 16px;border-radius:999px;background:var(--fg);color:var(--bg);border:1px solid var(--line);
 font-size:12px;font-weight:500;text-decoration:none;box-shadow:0 4px 16px #0000002e;opacity:.94;
 transition:transform .32s var(--ease),opacity .32s var(--ease)}
.back:hover{opacity:1;transform:translateY(-2px);color:var(--bg)}
.back:active{transform:translateY(0)}
.back .ar{font-size:15px;line-height:1}
@media print{.back{display:none}*{animation:none!important;transition:none!important}}
@media(prefers-reduced-motion:reduce){
 *{animation:none!important;transition:none!important}
 ::view-transition-group(*),::view-transition-old(*),::view-transition-new(*){animation:none!important}
}
@media(prefers-color-scheme:dark){:root{
color-scheme:dark;
--bg:#15171A;--fg:#E4E6E8;--panel:#1C1F23;--panel2:#202429;--track:#262A30;
--line:#33383F;--line2:#2B3036;--border:#3A4048;--muted:#A7ACB3;--faint:#838B93;--dim:#666E77;
--accent:#6FC2B4;--accent-d:#9EDBD0;--accent-bg:#1A302D;--accent-fg:#A9DDD4;
--link:#C4CECD;--link-h:#8AD6C8;--rule:#3A4B48;
--neg:#E87777;--neg2:#E26363;--neg-bg:#3A1F1F;--credit:#D9A441;
--warn:#D9A441;--warn-bg:#332A18;--warn-fg:#E3BC72;--ok-bg:#173029;--ok-fg:#7FCFB2;
--part-bg:#262B35;--part-fg:#AAB6CC;--mark:#6B5417;
--p0:#4FCFC2;--p1:#E0AE4B;--p2:#6BA9EE;
--p3:#B292F0;--p4:#E88FB8;
--shadow:0 1px 2px #00000040,0 2px 8px #00000030
}mark{color:#F5E6B8}}
"""

# 图表样式按页面需要注入：列表 / 检索 / 全文页只带基础 CSS，
# 不会为环形图、热力图或块地图的样式买单。判定只是一次子串扫描，微秒级。
CSS_VIZ = """
.viz{background:var(--panel);border:1px solid var(--line);border-radius:9px;padding:12px 14px;margin:0 0 16px;box-shadow:var(--shadow)}
/* 全局统计页：两张各占整行、内容都挤在左边的卡片并排成左右两栏。
   等高铺满（stretch）：早先用 align-items:start 让各自按内容定高，结果右栏
   换成 7 行日历后比左栏高出一截，上下边框错开，看着就是「没对齐」。 */
.viz2{display:grid;grid-template-columns:1fr 1fr;gap:14px;align-items:stretch;margin:0 0 16px}
.viz2>.viz{margin-bottom:0;min-width:0}
/* 回退必须和定义同在 CSS_VIZ 里：CSS_INDEX 只在首页注入，/stats 页窄屏会漏。 */
@media(max-width:900px){.viz2{grid-template-columns:1fr}}
.viz-hd{display:flex;align-items:baseline;gap:9px;flex-wrap:wrap;margin:0 0 10px}
.viz-hd b{font-size:12.5px;font-weight:600}.viz-hd small{font-size:10.5px;color:var(--faint)}
.viz-hd .fine{margin-left:auto}
.empty{padding:16px;text-align:center;color:var(--faint);font-size:11.5px;background:var(--track);border-radius:6px}
/* 悬浮说明：祖先链路不能有任何 overflow 裁切。.hm 早期写了 overflow-x:auto，
   按 CSS 规范会把 overflow-y 一并算成 auto，气泡上半截被容器直接切掉。 */
[data-tip]{position:relative}
[data-tip]:hover,[data-tip]:focus-visible{z-index:6}
/* 气泡宽度必须 max-content：绝对定位元素的收缩宽度按「包含块宽度 - left」算，
   挂在 19px 的小格子上会被压成两个字一行的竖条（之前误判成容器裁切）。 */
[data-tip]:hover::after,[data-tip]:focus-visible::after{content:attr(data-tip);position:absolute;left:50%;bottom:calc(100% + 6px);
 transform:translateX(-50%);z-index:50;background:var(--fg);color:var(--bg);font-size:10.5px;line-height:1.45;
 padding:5px 8px;border-radius:5px;white-space:pre-line;width:max-content;max-width:250px;min-width:90px;
 box-shadow:var(--shadow);pointer-events:none}
/* 首列 / 末列的格子让气泡贴边，避免横向越出卡片；最上面一行（周一）改为向下弹，
   否则贴顶那一行会顶出卡片上沿。用 nth-of-type 而不是 nth-child：星期标签是
   <b>，不参与 i 的同类计数，7 行与 HM_ROWS 一致。 */
.hm i:nth-of-type(-n+7)::after{left:0;transform:none}
.hm i:nth-last-of-type(-n+7)::after{left:auto;right:0;transform:none}
.hm i:nth-of-type(7n+1)::after{bottom:auto;top:calc(100% + 6px)}
.donut-row{display:flex;align-items:center;gap:22px;flex-wrap:wrap}
.donut-wrap{position:relative;width:112px;height:112px;flex:none}
.donut{width:112px;height:112px;display:block;animation:tt-pop .62s var(--ease-in) backwards}
.donut circle{fill:none;stroke-width:5.2}
.donut .trk{stroke:var(--track)}
/* 环的唤起：底盘先轻微放大浮起，各段按自己的起始角度错峰「长」出来，
   读起来是一圈顺时针扫过，而不是整张图一起淡入。 */
.donut circle:not(.trk){animation:tt-arc .72s var(--ease-in) backwards}
@keyframes tt-pop{from{opacity:.55;transform:scale(.93)}to{opacity:1;transform:none}}
@keyframes tt-arc{from{stroke-dasharray:0 100}}
.donut-wrap .ctr{position:absolute;inset:0;display:flex;flex-direction:column;align-items:center;justify-content:center;pointer-events:none}
.donut-wrap .ctr b{font-size:16px;font-weight:600;line-height:1.2;font-variant-numeric:tabular-nums;white-space:nowrap}
.donut-wrap .ctr span{font-size:10px;color:var(--faint)}
.slice{flex:1;min-width:200px}
.slice .sl{display:flex;align-items:center;gap:7px;padding:3px 0;font-size:11.5px;border-bottom:1px solid var(--line2)}
.slice .sl i{width:9px;height:9px;border-radius:2px;flex:none}
.slice .sl .g{flex:1;color:var(--muted)}
.slice .sl b{font-weight:500;font-variant-numeric:tabular-nums}
.slice .sl em{font-style:normal;color:var(--faint);font-size:10.5px;min-width:46px;text-align:right;font-variant-numeric:tabular-nums}
.reserve{display:flex;height:14px;border-radius:4px;overflow:hidden;background:var(--track);margin:7px 0 4px}
.reserve .used{background:var(--accent)}
.reserve .retired{background:repeating-linear-gradient(45deg,var(--dim) 0 4px,var(--track) 4px 8px)}
/* 折线图的绘制：polyline 带 pathLength="100"，CSS 用 dashoffset 把路径从
   左往右「长」出来；面积延后淡入并轻微上浮，读起来像笔尖描过一遍，而不是
   整张图被裁开。归一化长度让纯 CSS 就能精确描线，不需要测量路径。
   只动合成属性（dashoffset / opacity / transform），不触发重排。 */
.spark{width:100%;height:38px;display:block}
.spark polyline{fill:none;stroke:var(--accent);stroke-width:1.6;vector-effect:non-scaling-stroke;
 stroke-dasharray:100;animation:tt-draw .88s var(--ease-in) backwards}
.spark polygon{fill:var(--accent);opacity:.11;animation:tt-area .72s .18s var(--ease-in) backwards}
/* 空档：没有会话的那几桶沿底部用虚线连过去，与「用量真的掉到 0」区分开。
   dasharray 用 viewBox 单位（这条不设 pathLength），不做描线动画、直接显示。 */
.spark polyline.gap{stroke-dasharray:1.8 1.4;stroke-width:1.3;opacity:.62;animation:none}
@keyframes tt-draw{from{stroke-dashoffset:100}to{stroke-dashoffset:0}}
@keyframes tt-area{from{opacity:0;transform:translateY(4px)}to{opacity:.11;transform:none}}
/* 首页「按时间」折线：viewBox 纵向 80px，渲染高由 .ovline 给定，折线不再被
   横拉压扁；其上的热区列复用 [data-tip] 气泡（零 JS），悬停出焦点竖线。
   容器定位在绝对层，动画交给里面的 polyline，不再整块禁用。 */
.ovline{position:relative;height:64px;margin-top:2px}
.ovline .spark{position:absolute;inset:0;height:100%}
/* 三平台三条线的共用图例 */
.ovlegend{display:flex;align-items:center;gap:10px;flex-wrap:wrap;font-size:10px;
 color:var(--muted);margin-top:5px}
.ovlegend .lg{display:inline-flex;align-items:center;gap:4px;white-space:nowrap}
.ovlegend .lg i{width:11px;height:2px;border-radius:2px;flex:none}
.ovline .hz{position:absolute;top:0;bottom:0;cursor:default}
.ovline .hz i{position:absolute;left:50%;width:0;height:0;border-left:1px dashed var(--accent);
 opacity:0;transition:opacity .12s}
.ovline .hz:hover i{opacity:.55;height:100%;top:0}
"""

CSS_HM = """
/* GitHub 式贡献图：7 行 = 周一..周日，26 列 = 最近 26 周，每格一天。
   第一列 18px 留给星期标签（一 / 三 / 五），其余列 1fr 均分，格子 aspect-ratio:1
   跟着容器长到约 20px。26 周 × 20px + 25 × 4px 间距 + 标签列 ≈ 640px，
   .hmw 就封顶 640px 居中：超宽单栏也不会把格子撑成马赛克。
   行数必须显式声明，否则 grid-auto-flow:column 下每格各占一列、横向冲出卡片。
   align-items:start 让 aspect-ratio 生效（默认 stretch 会把格子拉成横条）。
   容器不能设 overflow：祖先链路上任何 overflow 都会把悬浮气泡裁掉（踩过一次）。 */
.hmw{--hmc:26;--hmg:4px;max-width:640px;margin:0 auto}
.hm{display:grid;grid-auto-flow:column;grid-template-rows:repeat(7,auto);
 grid-template-columns:18px repeat(var(--hmc),minmax(0,1fr));gap:var(--hmg);align-items:start}
.hm-mon{display:grid;grid-template-columns:18px repeat(var(--hmc),minmax(0,1fr));
 gap:var(--hmg);margin:0 0 5px}
.hm-mon span{grid-row:1;font-size:9.5px;line-height:1.2;color:var(--faint);white-space:nowrap}
.hm b.wk{font-size:9px;font-weight:400;line-height:1;color:var(--faint);
 align-self:center;display:flex;align-items:center;white-space:nowrap}
.hm i{aspect-ratio:1;border-radius:3px;background:var(--track);display:block;
 transition:transform .16s var(--ease-in),box-shadow .16s var(--ease-in)}
/* 色阶用 color-mix 把 accent 混进面板底色，而不是给整格加 opacity：
   opacity 会连带把格子里的 ::after 气泡一起变淡（踩过），混色不会。
   每条前面留一个 var(--track) 兜底：不认 color-mix 的浏览器退化成灰格子，
   而不是整片透明。 */
.hm i.h1{background:var(--track);background:color-mix(in srgb,var(--accent) 26%,var(--panel))}
.hm i.h2{background:var(--track);background:color-mix(in srgb,var(--accent) 46%,var(--panel))}
.hm i.h3{background:var(--track);background:color-mix(in srgb,var(--accent) 68%,var(--panel))}
.hm i.h4{background:var(--track);background:color-mix(in srgb,var(--accent) 92%,var(--panel))}
/* 预留：若以后改成非整周窗口，补位格仍不占视觉（当前 7 × N 恒整除，不产生） */
.hm i.pad{background:none;pointer-events:none}
.hm i:hover{transform:scale(1.18);box-shadow:0 2px 7px rgba(0,0,0,.18);position:relative;z-index:3}
.hm-lg{display:flex;gap:4px;align-items:center;font-size:10px;color:var(--faint);margin-top:9px}
.hm-lg i{width:10px;height:10px;border-radius:2px;background:var(--track)}
.hm-lg i.l1{background:var(--track);background:color-mix(in srgb,var(--accent) 26%,var(--panel))}
.hm-lg i.l2{background:var(--track);background:color-mix(in srgb,var(--accent) 46%,var(--panel))}
.hm-lg i.l3{background:var(--track);background:color-mix(in srgb,var(--accent) 68%,var(--panel))}
.hm-lg i.l4{background:var(--track);background:color-mix(in srgb,var(--accent) 92%,var(--panel))}
/* 26 列在 520px 以下会被挤到 14px，缩间距让格子尽量保住 */
@media(max-width:520px){.hmw{--hmg:3px}}
"""

CSS_MAP = """
.map{width:100%;height:auto;display:block;border-radius:6px;background:var(--track)}
.map rect{rx:3;stroke:var(--bg);stroke-width:1.5}
.map a:hover rect,.map a:focus-visible rect{stroke:var(--fg);stroke-width:2}
.map text{fill:var(--fg);font:10px system-ui,sans-serif;pointer-events:none}
.map text.sub{opacity:.72;font-size:9px}
.map-cap{display:flex;gap:12px;flex-wrap:wrap;font-size:10.5px;color:var(--faint);margin-top:8px}
"""

CSS_TREND = """
.chart{position:relative;padding-left:46px;margin:14px 0 6px}
.chart-y{position:absolute;left:0;top:0;bottom:0;width:42px;display:flex;flex-direction:column;justify-content:space-between;
 font-size:10px;color:var(--faint);text-align:right;font-variant-numeric:tabular-nums;padding-bottom:2px}
.trend{position:relative;display:flex;align-items:flex-end;gap:2px;height:172px;border-bottom:1px solid var(--line);
 background:repeating-linear-gradient(to top,transparent 0,transparent 24.6%,var(--line2) 24.6%,var(--line2) 25%)}
.trend a{flex:1;min-width:3px;height:100%;display:flex;flex-direction:column-reverse;justify-content:flex-start;
 text-decoration:none;position:relative;transform-origin:bottom;animation:tt-rise .55s var(--ease-in) backwards}
.trend a i{display:block;width:100%;min-height:0;border-radius:2px 2px 0 0;background:var(--track);
 background:linear-gradient(to top,var(--p0) 0,var(--p0) var(--s0),var(--p1) var(--s0),var(--p1) var(--s1),var(--p2) var(--s1),var(--p2) var(--s2),var(--p3) var(--s2),var(--p3) var(--s3),var(--p4) var(--s3),var(--p4) var(--s4))}
.trend a:hover,.trend a:focus-visible{outline:1px solid var(--accent);outline-offset:1px}
.trend a .mk{position:absolute;top:-3px;left:50%;transform:translateX(-50%);font-size:9px;line-height:1;color:var(--neg2)}
.trend a .mk.c{color:var(--warn-fg)}
.trend .ovl{position:absolute;inset:0;width:100%;height:100%;pointer-events:none}
/* 命中率折线在柱子起势之后再描出来，否则两条动画同时起步会互相抢注意力 */
.trend .ovl polyline{fill:none;stroke:var(--fg);stroke-width:1.2;opacity:.42;
 vector-effect:non-scaling-stroke;stroke-dasharray:100;
 animation:tt-draw .92s .3s var(--ease-in) backwards}
.trend .ovl polyline{fill:none;stroke:var(--fg);stroke-width:1.2;opacity:.42;vector-effect:non-scaling-stroke}
@keyframes tt-rise{from{transform:scaleY(.02);opacity:.35}to{transform:scaleY(1);opacity:1}}
"""

# 命中块折叠：只在按调用浏览 / 调用详情这类有块清单的页面注入。
CSS_FOLD = """
/* 折叠行：默认是「一枚安静的胶囊」，展开后才拉出带边框的内容区，
   箭头随之旋转。mini 版贴附在调用行下方，不另起一张卡片。 */
.hitfold{margin:0 0 6px;border-radius:10px}
.hitfold>summary{list-style:none;cursor:pointer;display:inline-flex;align-items:center;gap:10px;
 width:auto;padding:7px 15px;font-size:11.5px;border:1px solid var(--line);border-radius:999px;
 background:var(--panel2);color:var(--muted);
 transition:background-color .3s var(--ease),border-color .3s var(--ease),color .3s var(--ease),box-shadow .3s var(--ease),border-radius .3s var(--ease)}
.hitfold>summary::-webkit-details-marker{display:none}
.hitfold>summary:before{content:"";width:0;height:0;flex:none;
 border-left:5px solid var(--accent);border-top:4px solid transparent;border-bottom:4px solid transparent;
 transition:transform .34s var(--ease)}
.hitfold[open]>summary:before{transform:rotate(90deg)}
.hitfold>summary .hf-dot{display:none}
.hitfold>summary b{font-weight:600;color:var(--fg)}
.hitfold>summary .hf-n{color:var(--muted);font-variant-numeric:tabular-nums;font-size:10.5px;
 padding:1px 8px;border-radius:999px;background:var(--track)}
.hitfold>summary small{margin-left:4px;color:var(--faint);font-size:10.5px}
.hitfold[open]>summary small{margin-left:auto}
.hitfold>summary:hover{background:var(--accent-bg);border-color:var(--accent);color:var(--accent-fg)}
.hitfold[open]>summary{display:flex;width:100%;padding:9px 13px;
 border-radius:10px 10px 0 0;background:var(--accent-bg);border-color:var(--accent)}
.hitfold .in{padding:8px 10px 2px;background:var(--bg);border:1px solid var(--line);
 border-top:0;border-radius:0 0 10px 10px}
.hitfold.mini{margin:-2px 0 8px}
.hitfold.mini>summary{display:inline-flex;width:auto;padding:4px 12px;border-radius:999px;
 background:var(--track);border-color:transparent;font-size:10.5px}
.hitfold.mini[open]>summary{width:auto;padding:4px 12px;border-radius:999px}
.hitfold.mini>summary:before{border-left-width:4px;border-top-width:3.5px;border-bottom-width:3.5px}
.hitfold.mini[open]>summary{border-radius:999px;background:var(--accent-bg)}
.hitfold.mini .in{display:inline-block;border:0;background:transparent;padding:6px 12px 0;font-size:11px}
@supports (interpolate-size:allow-keywords){
 .hitfold{interpolate-size:allow-keywords}
 .hitfold::details-content{block-size:0;overflow:hidden;
  transition:block-size .4s var(--ease),content-visibility .4s var(--ease) allow-discrete}
 .hitfold[open]::details-content{block-size:auto}
}
"""

# 首页全库画像：只有索引页带这些规则。
CSS_INDEX = """
/* 首页概览：数字条与三张图合成一块连续面板，消除「六个小方块 + 一张大白卡」的割裂。
   只覆盖 .hero 内部，其他页面的 .kpi / .viz 保持原样。 */
.hero{position:relative;background:linear-gradient(160deg,var(--panel2),var(--panel));
 border:1px solid var(--line);border-radius:14px;overflow:hidden;margin:0 0 18px;box-shadow:var(--shadow)}
.hero:before{content:"";position:absolute;left:0;right:0;top:0;height:2px;
 background:linear-gradient(90deg,var(--p0),var(--p1),var(--p2),var(--p3),var(--p4));opacity:.55}
.hero .kpi{display:grid;grid-template-columns:var(--kcols,repeat(6,minmax(0,1fr)));gap:0;margin:0;
 background:transparent;border:0;border-bottom:1px solid var(--line);border-radius:0;
 padding:16px 4px 18px}
.hero .kpi>div{background:transparent;border:0;border-right:1px solid var(--line2);
 border-radius:0;min-width:0;padding:2px 18px;box-shadow:none}
.hero .kpi>div:last-child{border-right:0}
.hero .kpi b{font-size:21px;font-weight:550;letter-spacing:-.025em;line-height:1.3;color:var(--fg)}
.hero .kpi span{font-size:10.5px;letter-spacing:.05em;color:var(--faint)}
.hero .viz{background:transparent;border:0;border-radius:0;box-shadow:none;margin:0;padding:16px 18px 18px}
.hero .viz-hd{margin-bottom:14px}
.hero .viz-hd b{font-size:12px;font-weight:600;letter-spacing:.02em}
.ovgrid{display:grid;grid-template-columns:1fr 1fr 1fr;gap:0}
.ovcell{min-width:0;padding:0 18px;border-right:1px solid var(--line2);display:flex;flex-direction:column}
.ovcell:first-child{padding-left:0}
.ovcell:last-child{border-right:0;padding-right:0}
.ovt{display:flex;align-items:center;gap:6px;font-size:10.5px;font-weight:600;
 letter-spacing:.06em;color:var(--faint);margin-bottom:12px}
.ovt:before{content:"";width:5px;height:5px;border-radius:50%;background:var(--accent);opacity:.75;flex:none}
.ovgrid .donut-row{gap:10px}
.ovgrid .donut-wrap,.ovgrid .donut{width:84px;height:84px}
.ovgrid .donut-wrap .ctr b{font-size:13px}
.ovgrid .slice{min-width:0;flex:1}
.ovgrid .sl{font-size:10.5px;padding:2px 0}
.ovgrid .sl em{min-width:36px}
/* 三列底部说明共用一条基线：列高不一致时由留白吸收，而不是在某一列下方留洞 */
.ovaxis{display:flex;justify-content:space-between;font-size:10px;color:var(--faint);margin-top:2px}
.wsrow{display:grid;grid-template-columns:minmax(80px,1fr) 72px 28px;gap:7px;align-items:center;font-size:10.5px;padding:2px 0}
.wsrow.top{font-weight:600;color:var(--fg);margin-top:3px}
.wsrow.sub{color:var(--muted);padding-left:9px}
.wsname{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.wsbar{height:6px;background:var(--track);border-radius:3px;overflow:hidden}
.wsbar i{display:block;height:100%;background:var(--accent);opacity:.72}
.wsrow.top .wsbar i{opacity:1}
.wsn{text-align:right;color:var(--faint);font-variant-numeric:tabular-nums}
/* 递归关系默认收成一行摘要，展开才铺开；箭头旋转与 hover 沿用全站缓动 */
.wsfold>summary{list-style:none;cursor:pointer;display:flex;align-items:center;gap:8px;
 padding:8px 11px;border:1px solid var(--line);border-radius:8px;background:var(--panel2);
 font-size:10.5px;color:var(--muted);
 transition:border-color .28s var(--ease),background-color .28s var(--ease),color .28s var(--ease)}
.wsfold>summary::-webkit-details-marker{display:none}
.wsfold>summary:before{content:"";flex:none;width:0;height:0;border-left:5px solid var(--faint);
 border-top:4px solid transparent;border-bottom:4px solid transparent;transition:transform .28s var(--ease)}
.wsfold[open]>summary:before{transform:rotate(90deg)}
.wsfold>summary:hover{border-color:var(--accent);color:var(--fg)}
.wsfold-h{margin-left:auto;color:var(--faint);font-size:10px}
.wsfold[open] .wsfold-h{font-size:0}
.wsfold[open] .wsfold-h:after{content:"收起";font-size:10px}
.wsfold .in{padding-top:9px}
.wsrow.more{color:var(--faint);padding-left:9px;font-size:10px}
/* 首页工具条：文字导航（细分隔）+ 右侧排序分段控件 + 元信息，一条横带三个分区。
   取代原先两行大小不一的 pill 按钮堆，与下方会话列表用一条细线分开。 */
.toolbar{display:flex;align-items:center;gap:16px;flex-wrap:wrap;
 padding:2px 2px 12px;margin:0 0 14px;border-bottom:1px solid var(--line2)}
.tb-nav{display:flex;align-items:center;flex-wrap:wrap}
.tb-nav a{position:relative;padding:5px 13px;font-size:11.5px;color:var(--muted);
 text-decoration:none;border-radius:6px;
 transition:color .28s var(--ease),background-color .28s var(--ease)}
.tb-nav a:first-child{padding-left:2px}
.tb-nav a + a:before{content:"";position:absolute;left:0;top:50%;transform:translateY(-50%);
 width:1px;height:11px;background:var(--line)}
.tb-nav a:hover{color:var(--link-h);background:var(--accent-bg)}
/* 外链（同机用量监视）：只在对端探测通过时才出现，颜色比站内导航轻一点 */
.tb-nav a.tb-ext{color:var(--link)}
.tb-ext:hover{color:var(--link-h)}
.tb-sort{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-left:auto}
.tb-lab{font-size:10.5px;letter-spacing:.06em;color:var(--faint)}
.tb-seg{display:inline-flex;background:var(--track);border-radius:999px;padding:2px}
.tb-seg a{padding:4px 13px;border-radius:999px;font-size:11px;color:var(--muted);
 text-decoration:none;transition:background-color .28s var(--ease),color .28s var(--ease)}
.tb-seg a:hover{color:var(--link-h)}
.tb-seg a.on{background:var(--bg);color:var(--fg);font-weight:600;box-shadow:var(--shadow)}
.tb-clear{font-size:11px;color:var(--muted);text-decoration:none;padding:4px 11px;
 border:1px solid var(--line);border-radius:999px}
.tb-clear:hover{color:var(--link-h);border-color:var(--accent)}
.tb-meta{font-size:10.5px;color:var(--faint);white-space:nowrap}
@media(max-width:900px){
 .hero .kpi{grid-template-columns:repeat(3,1fr)}
 .hero .kpi>div:nth-child(3){border-right:0}
 .hero .kpi>div:nth-child(-n+3){border-bottom:1px solid var(--line2);padding-bottom:10px;margin-bottom:8px}
 .ovgrid{grid-template-columns:1fr}
 .ovcell{border-right:0;border-top:1px solid var(--line2);padding:14px 0 0;margin-top:14px}
 .ovcell:first-child{border-top:0;margin-top:0;padding-top:0}
 .tb-sort{margin-left:0}.tb-meta{display:none}
}
"""

# 会话页入口卡片与分段控件：只在有 navgrid 的页面注入。
CSS_NAV = """
/* 入口卡片：无边框、柔和渐变底，只靠左侧分类色细条区分，hover 时色条展开。 */
.navgrid{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin:14px 0 10px}
.navcard{position:relative;display:flex;flex-direction:column;gap:4px;padding:14px 16px 13px;
 border:0;border-radius:12px;background:linear-gradient(150deg,var(--panel),var(--panel2));
 text-decoration:none;color:var(--fg);overflow:hidden;
 transition:transform .3s var(--ease),box-shadow .3s var(--ease)}
.navcard:before{content:"";position:absolute;left:0;top:16px;bottom:16px;width:2px;border-radius:2px;
 background:var(--nc,var(--accent));opacity:.85;transition:top .34s var(--ease),bottom .34s var(--ease),opacity .34s var(--ease)}
.navcard:hover{transform:translateY(-2px);box-shadow:var(--shadow)}
.navcard:hover:before{top:0;bottom:0;opacity:1}
.navcard b{font-size:12px;font-weight:600;letter-spacing:.01em}
.navcard span{font-size:10.5px;color:var(--muted);line-height:1.45}
.navcard i{position:absolute;right:14px;top:12px;font-style:normal;font-size:13px;
 color:var(--faint);opacity:.5;transition:transform .3s var(--ease),opacity .3s var(--ease),color .3s var(--ease)}
.navcard:hover i{transform:translate(2px,-2px);opacity:1;color:var(--link-h)}
/* 轮来源筛选：胶囊链接组，只在会话页按轮浏览出现 */
.tkfilter{display:flex;gap:6px;flex-wrap:wrap;margin:14px 0 8px}
.tkfilter a{padding:4px 12px;border-radius:999px;font-size:11px;color:var(--muted);
 text-decoration:none;background:var(--track);
 transition:background-color .28s var(--ease),color .28s var(--ease)}
.tkfilter a:hover{color:var(--link-h)}
.tkfilter a.on{background:var(--accent);color:var(--bg);font-weight:600}
.segrow{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin:12px 0 4px;padding-bottom:10px;border-bottom:1px solid var(--line2)}
.seg{display:inline-flex;background:var(--track);border-radius:999px;padding:3px}
.seg a{padding:5px 15px;border-radius:999px;font-size:11.5px; color:var(--muted);text-decoration:none;
 transition:background-color .28s var(--ease),color .28s var(--ease)}
.seg a:hover{color:var(--link-h)}
.seg a.on{background:var(--bg);color:var(--fg);font-weight:600;box-shadow:var(--shadow)}
.seglink{margin-left:auto;font-size:11px;color:var(--faint);text-decoration:none}
.seglink:hover{color:var(--link-h)}
@media(max-width:820px){.navgrid{grid-template-columns:repeat(2,1fr)}}
"""

# 首页「会话 · 子代理关系」：只有存在子代理会话时才注入。


def crumb(items):
    out = []
    for i, (label, href) in enumerate(items):
        if i:
            out.append('<span class="sep">/</span>')
        out.append('<a href="%s">%s</a>' % (esc(href), esc(label)) if href
                   else '<span>%s</span>' % esc(label))
    return '<div class="crumb">' + "".join(out) + "</div>"


def back_link(crumbs, footer=False):
    """父级 = 面包屑里从倒数第二项往前找的第一个带链接的层级；首页不渲染。

    有些页面中间一级是没有链接的纯文字（例如不带 ref 的块全文页，
    「调用 #N」无处可去），这时应继续往回退到会话概览，而不是整个返回入口消失。
    所有视图都不用各自加「返回」按钮，也不用浏览器回退：悬浮胶囊在长内容页
    始终可见，页脚再给一份纯文字链接作为无 fixed 定位时的兜底。
    """
    for label, href in reversed(crumbs[:-1]):
        if not href:
            continue
        name = str(label)
        text = "返回 " + cut(name, 12)
        if footer:
            return '<a href="%s">← %s</a>' % (esc(href), esc(text))
        return ('<a class="back" href="%s" title="%s">'
                '<span class="ar" aria-hidden="true">←</span><span>%s</span></a>' %
                (esc(href), esc("返回上一层：" + name), esc(text)))
    return ""


# ---------------------------------------------------------------- 同机工具互链
#
# 零脚本页面没法由浏览器判断「对面那个服务还在不在」，死链又比没有入口更烦。
# 所以由服务端探一次 loopback：只连本机、不走 http_proxy、150 ms 超时，
# 结果缓存 MONITOR_TTL 秒（成功失败都缓存），翻页时不会每页都去连一次。
_MON = {"at": 0.0, "ok": False, "url": ""}
_MON_LOCK = threading.Lock()


def monitor_url():
    """返回可点击的用量监视地址；没在跑就返回空串（调用方据此不渲染入口）。"""
    if not MONITOR_URL:
        return ""
    now = time.time()
    with _MON_LOCK:
        if _MON["url"] == MONITOR_URL and now - _MON["at"] < MONITOR_TTL:
            return MONITOR_URL if _MON["ok"] else ""
    ok = False
    try:
        u = urlsplit(MONITOR_URL)
        conn = http.client.HTTPConnection(u.hostname or "127.0.0.1",
                                          u.port or 80, timeout=0.15)
        try:
            conn.request("GET", "/api/ping")
            resp = conn.getresponse()
            ok = resp.status == 200 and b'"ok"' in resp.read(160)
        finally:
            conn.close()
    except (OSError, http.client.HTTPException, ValueError):
        ok = False
    with _MON_LOCK:
        _MON.update(at=now, ok=ok, url=MONITOR_URL)
    return MONITOR_URL if ok else ""


def monitor_link(cls="tb-ext"):
    """工具条 / 页脚里的外链。只有探测到对方在跑才返回 HTML。"""
    url = monitor_url()
    if not url:
        return ""
    return ('<a class="%s" href="%s" target="_blank" rel="noopener" '
            'title="打开 WorkBuddy 用量监视：今日消耗、生成速度、缓存命中率">'
            '实时用量 ↗</a>' % (cls, esc(url)))


_CSS_MIN = {}


def _squeeze_css(css):
    """去掉注释、把连续空白折成一个空格。

    只动字符串之外的内容：选择器、声明、@media 的空格一个不改，因此不会
    破坏 `content:"…"`、`calc(100% - 1px)` 这类需要精确空白的写法。
    样式片段在进程内只有几种组合，压缩结果按原串缓存，运行期只多一次字典查找。
    """
    hit = _CSS_MIN.get(css)
    if hit is not None:
        return hit
    out, i, n = [], 0, len(css)
    quote = ""
    while i < n:
        c = css[i]
        if quote:
            out.append(c)
            if c == "\\" and i + 1 < n:
                out.append(css[i + 1])
                i += 2
                continue
            if c == quote:
                quote = ""
            i += 1
            continue
        if c in "\"'":
            quote = c
            out.append(c)
            i += 1
            continue
        if c == "/" and i + 1 < n and css[i + 1] == "*":
            j = css.find("*/", i + 2)
            i = n if j < 0 else j + 2
            continue
        if c in " \t\r\n":
            j = i
            while j < n and css[j] in " \t\r\n":
                j += 1
            out.append(" ")
            i = j
            continue
        out.append(c)
        i += 1
    res = "".join(out).replace("; }", "}").replace(";} ", "}").replace(";} ", "}")
    res = res.replace("} ", "}")
    _CSS_MIN[css] = res
    return res


def page(title, crumbs, body, sub="", extra_head="", need=(), topbar=""):
    """need 显式声明本页需要的图表样式片段（"viz"/"hm"/"map"/"chart"/
    "fold"/"nav"/"index"），与 body 探测取并集。

    探测靠扫 body 里的 class 子串，只在「图形一定有输出」时可靠：空库时
    index_overview() 返回空串，.ovgrid 不出现，而 .hero / .toolbar 照常渲染，
    整页就会裸样式。这类边界由调用方显式声明，探测继续兜住其余页面。
    """
    need = set(need or ())
    css = CSS
    if "viz" in need or 'class="viz' in body:
        css += CSS_VIZ
    if "hm" in need or 'class="hm"' in body:
        css += CSS_HM
    if "map" in need or 'class="map"' in body:
        css += CSS_MAP
    if "chart" in need or 'class="chart"' in body:
        css += CSS_TREND
    if "fold" in need or 'class="hitfold' in body:
        css += CSS_FOLD
    if "nav" in need or 'class="navgrid"' in body:
        css += CSS_NAV
    if "index" in need or 'class="ovgrid"' in body:
        css += CSS_INDEX
    head = ('<!DOCTYPE html><html lang="zh-CN"><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            '<title>%s · Token 轨迹</title><style>%s</style>' % (esc(title), _squeeze_css(css)))
    back = back_link(crumbs)
    h1 = '<h1>%s<span class="badge">完整模式 · 零截断</span></h1>' % esc(title)
    if topbar:
        # 平台切换与大标题同栏：标题在左、切换在右；说明文字另起一行左对齐，
        # 免得和两个 margin-left:auto 抢同一行的剩余空间。
        hd = ('<div class="hdrow">%s%s</div>' % (h1, topbar) +
              ('<div class="sub sub-wide">%s</div>' % esc(sub) if sub else ''))
    else:
        # 其余页面保持原来的「标题 + 右侧说明」单行布局，零影响。
        hd = ('<div class="hdrow">' + h1 +
              ('<div class="sub">%s</div>' % esc(sub) if sub else '') + '</div>')
    doc = (head + '<header><div class="wrap">' + crumb(crumbs) + hd +
           '</div></header><div class="wrap">' + extra_head + body +
           '<footer><span>本页 HTML @@KB@@ KB</span>' +
           '<span>数据只读 · 全文由后端按需提供 · 无需 JavaScript</span>' +
           back_link(crumbs, footer=True) +
           '<a href="/">索引</a><a href="/stats">全局统计</a>'
           '<a href="/rank">筛选与排序</a><a href="/api">API</a>' +
           monitor_link("ext") + '</footer>' +
           back + '</div></html>')
    # 页脚口径与 80 KB 硬闸对齐：算整页响应字节（含内联 CSS 与 header），
    # 不是只算 body。替换会改变长度（"99.9" 与 "100.0" 差 2 字节），迭代到
    # 数字稳定；6 轮足够覆盖位数变化的边界，真出现来回跳时最多少报 0.1 KB。
    kb = "0.0"
    for _ in range(6):
        out = doc.replace("@@KB@@", kb)
        nxt = "%.1f" % (len(out.encode("utf-8")) / 1024.0)
        if nxt == kb:
            break
        kb = nxt
    return out


def clamp_p(q, total, size):
    """与 pager() 同步夹取页码：p 越界时不出现「页码显示最后一页但列表为空」。"""
    pages = max(1, (total + size - 1) // size)
    return max(0, min(qint(q, "p"), pages - 1))


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


def kpi_cols(n):
    """数字带的列数：优先让每行填满，避免最后一行只剩一两格、右边空一片。

    6 列 ≈ 178 px / 7 列 ≈ 152 px（1120 容器内），足够容纳 9 位带千分位的数字
    加单位；8 项拆成 4×2 而不是挤成一行 8 格。列宽由 kpi_weights 再细分。
    """
    if n <= 7:
        return max(1, n)
    if n % 7 == 0:
        return 7
    if n % 6 == 0:
        return 6
    if n % 4 == 0:
        return 4
    return min(6, n)


def _text_px(s, fs):
    """粗估一段文字的显示宽度（px）：CJK 记 1em，西文 0.56em，逗号/空格更窄。

    只用来分配 KPI 列宽，不需要精确：误差在几十 px 内，靠后面的底盘吸收。
    """
    w = 0.0
    for ch in s:
        if ch == " ":
            w += fs * .28
        elif ord(ch) < 128:
            w += fs * (.30 if ch in ",.:·%|" else .56)
        else:
            w += fs
    return w


def kpi_weights(items, cols, fs=18, pad=32):
    """按内容宽度给每列一个 fr 权重，替代「每格一样宽」。

    「会话 11」只要 60 px，而「177,353,380 token」要 160 px；等分的结果就是
    左边一片空白、右边数字贴着边框。这里以每列最宽的那格为基准，加一个
    固定底盘（保证短格不贴边），再压到 2.6 倍以内（避免一个长数字吃掉半行）。
    同一列的多行取最大值，所以 12 项 6 列的两行也仍然对齐。
    """
    need = [0.0] * cols
    for i, (k, v) in enumerate(items):
        n = max(_text_px(str(v), fs), _text_px(str(k), 10.5)) + pad
        need[i % cols] = max(need[i % cols], n)
    raw = [n + 46.0 for n in need]
    lo = min(raw) or 1.0
    return " ".join("minmax(0,%.2ffr)" % min(r / lo, 2.6) for r in raw)


def kpi(items, fs=18, pad=32):
    """KPI 数字带。fs/pad 由调用方按版面给：首页 hero 用 21px 字号、18px 内边距。"""
    cols = kpi_cols(len(items))
    return ('<div class="kpi" style="--kcols:%s">' % kpi_weights(items, cols, fs, pad)
            + "".join('<div><b>%s</b><span>%s</span></div>' % (esc(v), esc(k))
                      for k, v in items) + '</div>')


# ---------------------------------------------------------------- 可视化原语
#
# 全部是「数字 -> CSS/SVG 属性字符串」的纯函数：不引入 <script>、不引入图片、
# 不新增索引、不缓存大对象。数据一律来自已解析会话或已缓存的会话索引，
# 因此只在被请求的那一屏产生开销，且复杂度对块数 / 调用数是线性的。
# 字数是原文统计、token 来自 API usage，二者不同口径，页面统一用 ≈ 标注估算项。

def search_box(action, term="", mode="text", scope="all", placeholder="关键词", extra=""):
    """统一搜索框：「按字符匹配 / 按块匹配」由 v_search 在服务端分流。

    零 JS 下 form 的 action 是固定的，所以用 mode 参数让 /search 决定交给
    全文检索还是按块检索，而不是靠脚本改 action。
    """
    # 分段开关用 radio + label：无需 id（同页可能有多个搜索框）、无需脚本，
    # 提交时照样带 mode=text|blocks，由 v_search 在服务端分流。
    mode_pick = ('<span class="modepick" role="group" aria-label="匹配方式">'
                 '<label><input type="radio" name="mode" value="text" %s><span>按字符匹配</span></label>'
                 '<label><input type="radio" name="mode" value="blocks" %s><span>按块匹配</span></label>'
                 '</span>' % ("checked" if mode == "text" else "",
                              "checked" if mode == "blocks" else ""))
    scope_pick = ""
    if mode == "text":
        scope_pick = ('<span class="modepick" role="group" aria-label="检索范围">' +
                      "".join(
                          '<label><input type="radio" name="scope" value="%s" %s>'
                          '<span>%s</span></label>' %
                          (k, "checked" if scope == k else "", lab)
                          for k, lab in (("all", "全部"), ("blocks", "上下文块"),
                                         ("outputs", "模型输出"), ("turns", "提问")))
                      + '</span>')
    return ('<form class="searchbox" method="get" action="%s">%s'
            '<span class="sb-tools">%s%s</span>'
            '<span class="sb-main"><input type="text" name="q" value="%s" placeholder="%s">'
            '<button type="submit">%s</button></span></form>' %
            (esc(action), extra, mode_pick, scope_pick, esc(term), esc(placeholder),
             "检索" if mode == "blocks" else "搜索"))


def plat_switch(ss_all, src, path, keep=()):
    """平台切换控件：本机没有该平台会话时置灰，不做死链。

    path 是当前页路径，keep 是链接里需要保留的其余查询参数（排序、检索词…）。
    """
    if not any((s.get("src") or "workbuddy") != "workbuddy" for s in ss_all):
        return ""
    have = Counter(s.get("src") or "workbuddy" for s in ss_all)
    items = []
    for k, full, short, _color in (("", "全部", "全部", ""),) + PLATFORMS:
        n = have.get(k, 0) if k else sum(have.values())
        title = ("%s · %d 个会话" % (full, n) if k
                 else "全部平台 · %d 个会话" % n)
        # 宽屏给全称，窄屏由 CSS 换成短名；hover 的 title 始终是全称。
        lab = ('<span class="pfull">%s</span><span class="pshort">%s</span>'
               % (esc(full), esc(short)))
        if k and not have.get(k):
            items.append('<span class="off" title="无 %s 会话记录">%s</span>'
                         % (esc(full), lab))
            continue
        qs = ([("src", k)] if k else []) + [(kk, vv) for kk, vv in keep if vv]
        href = path + ("?" + urlencode(qs) if qs else "")
        items.append('<a class="%s" href="%s" title="%s">%s</a>'
                     % ("on" if src == k else "", esc(href), esc(title), lab))
    return ('<div class="platbar"><span class="tb-lab">平台</span>'
            '<span class="tb-seg">%s</span></div>' % "".join(items))


def kh(n):
    """紧凑数字：图表内的轴标签与块标签，避免长数字撑破小矩形。"""
    n = int(n or 0)
    if n >= 1000000:
        return "%.1fM" % (n / 1000000.0)
    if n >= 10000:
        return "%.1fw" % (n / 10000.0)
    if n >= 1000:
        return "%.1fk" % (n / 1000.0)
    return str(n)


def compose_bar(comp):
    """上下文构成：SVG 环 + 明细行。环用 stroke-dasharray 纯数学画，五段共 5 个 circle。"""
    total = sum(comp.values()) or 1
    parts = [(k, comp[k]) for k in ORDER if comp.get(k)]
    if not parts:
        return '<div class="empty">这个范围内没有可统计的上下文块</div>'
    arcs, rows, acc = [], [], 0.0
    for k, v in parts:
        length = v / total * 100.0
        gap = min(0.6, length * 0.2)
        seg = max(0.0, length - gap)
        # rotate 写进属性而非 CSS：每段独立定位，避免 CSS transform 覆盖 SVG transform
        arcs.append('<circle cx="21" cy="21" r="15.9155" style="stroke:%s" '
                    'stroke-dasharray="%.3f %.3f" transform="rotate(%.2f 21 21)"/>' %
                    (PALETTE[k], seg, 100 - seg, -90 + acc * 3.6))
        rows.append('<div class="sl"><i style="background:%s"></i>'
                    '<span class="g">%s</span><b>≈ %s 字</b><em>%.1f%%</em></div>' %
                    (PALETTE[k], esc(TYPES[k]), nfmt(v), v / total * 100))
        acc += length
    svg = ('<svg class="donut" viewBox="0 0 42 42" role="img" '
           'aria-label="上下文文本构成环形图"><circle class="trk" cx="21" cy="21" r="15.9155"/>' +
           "".join(arcs) + '</svg>')
    return ('<div class="viz"><div class="viz-hd"><b>进入上下文的文本构成</b>'
            '<small>按字符数 · 非官方 tokenizer</small></div>'
            '<div class="donut-row"><div class="donut-wrap">%s'
            '<div class="ctr"><b>%s</b><span>字 · 5 类</span></div></div>'
            '<div class="slice">%s</div></div></div>' %
            (svg, kh(total), "".join(rows)))


def reserve_bar(st):
    """归档原文里「仍可能被带上」与「压缩后已退役」的比例，斜纹表示已退役。"""
    total = st["chars"] or 1
    retired = min(st.get("retired_chars", 0), total)
    live = total - retired
    return ('<div class="reserve"><i class="used" style="width:%.2f%%"></i>'
            '<i class="retired" style="width:%.2f%%"></i></div>'
            '<div class="legend"><span><i style="background:var(--accent)"></i>归档活跃 ≈ %s 字</span>'
            '<span><i style="background:var(--dim)"></i>已退役 ≈ %s 字</span>'
            '<span>可见峰值 ≈ %s 字</span></div>' %
            (live / total * 100, retired / total * 100,
             kh(live), kh(retired), kh(st.get("visible_peak", 0))))


def visible_series(d, buckets=48):
    """可见上下文字数序列：直接读 _derive 的前缀和与单调序列，O(C/step)，不建新索引。"""
    calls, pref = d["calls"], d["pref"]
    n = len(calls)
    if not n:
        return []
    step = max(1, n // buckets)
    out = []
    for i in range(0, n, step):
        c = calls[i]
        lo = min(c.get("vf", 0), c["ctxN"])
        out.append(pref[c["ctxN"]] - pref[lo])
    if (n - 1) % step:
        c = calls[-1]
        out.append(pref[c["ctxN"]] - pref[min(c.get("vf", 0), c["ctxN"])])
    return out


def sparkline_svg(series):
    """整段会话的可见上下文走势：一个 polygon + 一个 polyline，无 DOM 列表。"""
    if len(series) < 2:
        return '<div class="empty">调用数不足，暂无走势</div>'
    mx = max(series) or 1
    pts = " ".join("%.2f,%.2f" % (i * 100.0 / (len(series) - 1), 36 - v / mx * 33)
                   for i, v in enumerate(series))
    return ('<svg class="spark" viewBox="0 0 100 38" preserveAspectRatio="none" '
            'role="img" aria-label="可见上下文字数走势">'
            '<polygon points="0,38 %s 100,38"/>'
            '<polyline pathLength="100" points="%s"/></svg>' % (pts, pts))


HM_ROWS = 7          # 周一..周日，与 GitHub 贡献图一致
HM_WEEKS = 26        # 半年。7 × 26 = 182 天正好整除 7 行，不用补位格；
                     # 26 列 × 20px + 标签列 ≈ 640px，正好是卡片能容下的宽度


def activity_heatmap(ss, weeks=HM_WEEKS):
    """活动日历：按每次调用的真实发生日分色，只消费已缓存的会话索引。

    scan_all 已把每个会话的调用按日拆进 days（YYYY-MM-DD -> [输入 token, 调用数]），
    这里逐日聚合，不再把整个会话压到 mtime（文件最后修改日）那一天——长会话
    常跨多天，按 mtime 会让前几天全空、最后一天虚高。老索引 / 外部构造的
    sessions 没有 days 时退回 mtime + calls 兜底。

    排版是 7 行 × 26 列的 GitHub 式贡献图：列 = 周，行 = 周一..周日，每格一天。
    网格按周对齐（最后一列是本周，第一列是 25 周前的周一），第一列额外留给
    「一 / 三 / 五」星期标签；其余列 1fr 均分，格子 aspect-ratio:1 跟着容器长到
    约 20px。列号随时间单调递增，顶部按月打刻度。7 行是星期语义，20px 的格子
    靠 26 周把宽度摊开——早先 7 行 × 12px × 10 周只有 147px 宽，整块贴在卡片
    左边，右边空一大片。窗口天数恒为 7 的倍数，因此不需要补位格；本周剩下的
    未来日期留空即可，看起来就是 GitHub 末尾那几格空白。
    """
    agg = Counter()
    for s in ss:
        bins = s.get("days")
        if bins:
            for day, pair in bins.items():
                agg[day] += pair[1]
        else:
            day = fmt_ts(s["mtime"], "%Y-%m-%d", "")
            if day:
                agg[day] += s["calls"]
    # 用 date 运算而不是 +i*86400：有夏令时的地区跨切换日不会错位一格。
    today = datetime.date.today()
    # 周一对齐：weekday() 周一是 0，本周日 = today + (6 - weekday)。
    end = today + datetime.timedelta(days=6 - today.weekday())
    days = weeks * HM_ROWS
    start = end - datetime.timedelta(days=days - 1)
    # 色阶只按窗口内的日期取峰值，否则窗口外某天的历史高值会把整张图压淡。
    window = [(start + datetime.timedelta(days=i)).strftime("%Y-%m-%d")
              for i in range(days)]
    mx = max([agg.get(d, 0) for d in window], default=0) or 1
    cols = -(-days // HM_ROWS)          # = weeks，换周数不用手改
    cells = []
    marks = []                          # [(起始列, 月份标签)]，用来画顶部刻度
    active = 0
    last_mon = ""
    for i, day in enumerate(window):
        col = i // HM_ROWS              # 周序，单调不减，月份刻度才对得上
        mon = day[5:7]
        if mon != last_mon:
            marks.append((col, "%d月" % int(mon)))
            last_mon = mon
        v = agg.get(day, 0)
        if not v:
            cells.append("<i></i>")
            continue
        active += 1
        lv = 1 if v / mx <= .25 else (2 if v / mx <= .5 else (3 if v / mx <= .75 else 4))
        cells.append('<i class="h%d" data-tip="%s · %s 次调用"></i>' %
                     (lv, esc(day), nfmt(v)))
    # 星期标签和格子同处一个 grid：先占满第一列的 7 行，格子自然从第二列开始。
    # 用 <b> 而不是 <i>，气泡贴边规则才能用 nth-of-type 只数格子。
    wk_html = "".join('<b class="wk">%s</b>' % (x or "")
                      for x in ("一", "", "三", "", "五", "", ""))
    # 月份刻度：每段从本月起始列占到下月起始列；+2 是让开第一列星期标签。
    mon_html = ""
    if len(marks) > 1:
        parts = []
        for k, (col, label) in enumerate(marks):
            endc = marks[k + 1][0] if k + 1 < len(marks) else cols
            parts.append('<span style="grid-column:%d/span %d">%s</span>'
                         % (col + 2, max(1, endc - col), label))
        mon_html = '<div class="hm-mon">%s</div>' % "".join(parts)
    # 「N 天有活动」只数窗口里有色的格子：按真实日期分桶后，窗口外的历史
    # 日期会进 agg，用 len(agg) 会标出比画面更多的天数。
    return ('<div class="viz"><div class="viz-hd"><b>会话活动日历</b>'
            '<small>最近 %d 周 · 每格一天，按当日调用数分色</small></div>'
            '<div class="hmw" style="--hmc:%d">%s<div class="hm">%s%s</div>'
            '<div class="hm-lg">少 <i></i><i class="l1"></i>'
            '<i class="l2"></i><i class="l3"></i><i class="l4"></i> 多'
            '<span style="margin-left:auto">%d 天有活动</span></div></div></div>' %
            (weeks, cols, mon_html, wk_html, "".join(cells), active))


# 子代理会话是独立的 agent-* jsonl。首选的父子关系键是子会话记录里的
# rootRequestId —— 它等于派发该子代理的那次 Agent 调用的 conversationRequestId，
# scan_all 已按会话收集成 agent_reqs。字段缺失（老记录、跨项目根）时再退回
# 「子会话首条时间 ↔ 父会话 Agent 调用时间」的 5 秒就近窗口。
SUBAGENT_WINDOW_MS = 5000


def _ms(v):
    """时间戳归一：jsonl 里 timestamp 可能是 int / float / 字符串 / None。

    统一成 float 毫秒，解析不了或非正值一律返回 0 —— 0 表示「没有可用时间」，
    不参与任何就近匹配（否则 first_ts=0 会被挂到同样是 0 的调用上）。
    """
    try:
        x = float(v)
    except (TypeError, ValueError):
        return 0.0
    return x if x > 0 else 0.0


def subagent_relations(ss):
    """返回 (父会话 id -> [子代理会话], 未匹配的子代理)。

    先用 rootRequestId 精确匹配（O(子代理数)），匹配不到再做时间窗兜底
    （O(子代理数 × 未命中父会话的 Agent 调用数)）。父会话也可以是子代理，
    嵌套派发因此能挂到最近的一层，而不是全部拍平到主会话上。
    """
    subs = [s for s in ss if s.get("is_sub")]
    mains = [s for s in ss if not s.get("is_sub")]
    by_req = {}
    for m in ss:
        for rid in m.get("agent_reqs") or ():
            by_req.setdefault(rid, m)
    timed = [m for m in mains if m.get("agent_calls")]
    rel, unmatched = OrderedDict(), []
    for sub in subs:
        best = by_req.get(sub.get("root") or "")
        if best is not None and best["id"] == sub["id"]:
            best = None
        if best is None and timed:
            ts = _ms(sub.get("first_ts"))      # 0 = 没有可用时间，不做就近匹配
            delta = None
            if ts:
                for m in timed:
                    for at in m["agent_calls"]:
                        at = _ms(at)
                        if not at:
                            continue
                        d = abs(ts - at)
                        if d <= SUBAGENT_WINDOW_MS and (delta is None or d < delta):
                            best, delta = m, d
        if best is None:
            unmatched.append(sub)
        else:
            rel.setdefault(best["id"], []).append(sub)
    return rel, unmatched



def _series_polys(active, buckets, n, mx, H=80, TOP=4):
    """把每个平台的按桶序列画成「实线 + 空档虚线」。

    连续有数据的桶连成实线；跨越没有会话的桶时，从前一点落到 0、沿底部
    走过去、再升到下一个点，这段用虚线 —— 读起来是「这里没有会话」，
    而不是「用量真的掉到 0」。单点段画成圆点，polyline 画不出一个点。
    """
    def y_of(v):
        return H - v / mx * (H - TOP - 2)

    def x_of(i):
        return i * 100.0 / (n - 1)

    out = []
    for idx, (k, _name, color) in enumerate(active):
        vals = buckets[k]
        delay = idx * 0.13
        i = 0
        while i < n:
            if vals[i] > 0:
                j = i
                while j < n and vals[j] > 0:
                    j += 1
                if j - i == 1:
                    out.append('<circle cx="%.2f" cy="%.2f" r="1.5" fill="%s"/>'
                               % (x_of(i), y_of(vals[i]), color))
                else:
                    pts = " ".join("%.2f,%.2f" % (x_of(x), y_of(vals[x]))
                                   for x in range(i, j))
                    out.append('<polyline pathLength="100" points="%s" '
                               'style="stroke:%s;stroke-width:1.7;animation-delay:%.2fs"/>'
                               % (pts, color, delay))
                i = j
            else:
                j = i
                while j < n and vals[j] == 0:
                    j += 1
                # 多个平台的空档线都贴底，全画在 y=H 会叠成一条；按平台错开 1.6
                base = H - 1.6 * (idx % 3)
                seg = []
                if i > 0:
                    seg.append("%.2f,%.2f" % (x_of(i - 1), y_of(vals[i - 1])))
                seg.append("%.2f,%.1f" % (x_of(i), base))
                seg.append("%.2f,%.1f" % (x_of(j - 1), base))
                if j < n:
                    seg.append("%.2f,%.2f" % (x_of(j), y_of(vals[j])))
                out.append('<polyline class="gap" points="%s" style="stroke:%s"/>'
                           % (" ".join(seg), color))
                i = j
    return "".join(out)


def _platform_charts(ss, total, raw_total):
    """「全部」视图：按平台拼接环 + 三平台三色折线。"""
    # 1) 按平台：输入 token 拼接环。三个平台各占一段，不再把 WorkBuddy 的
    #    模型分布和 Codex/ZCode 混进同一个「跨平台全量聚合模型」环。
    #    颜色与下面的折线、图例共用同一组变量，两套主题各有取值。
    platforms = [(k, full, color) for k, full, _s, color in PLATFORMS]
    by_src = Counter()
    for s in ss:
        by_src[s.get("src") or "workbuddy"] += s["pin"]
    segs = [(k, name, color, by_src[k])
            for k, name, color in platforms if by_src.get(k)]
    residual = raw_total - sum(v for _, _, _, v in segs)
    if residual > 0:
        segs.append(("other", "其他", "var(--p4)", residual))
    fracs = [val / total * 100.0 for _, _, _, val in segs]
    # 弧宽对 <1.2% 的段做视觉保底：Codex 实测只占 0.2%，严格按比例会细到
    # 看不见，「三段拼接」就退化成两段。图例仍给真实占比，弧宽只是可读性
    # 补偿；保底后整体等比归一，环始终闭合。
    vis = [max(f, 1.2) for f in fracs]
    vsum = sum(vis) or 1.0
    vis = [v * 100.0 / vsum for v in vis]
    arcs, legend, acc = [], [], 0.0
    for (_key, name, color, val), frac, vfrac in zip(segs, fracs, vis):
        seg = max(0.0, vfrac - min(.6, vfrac * .2))
        arcs.append('<circle cx="21" cy="21" r="15.9155" style="stroke:%s;animation-delay:%.2fs" '
                    'stroke-dasharray="%.3f %.3f" transform="rotate(%.2f 21 21)"/>' %
                    (color, .03 + acc / 100.0 * .42, seg, 100 - seg, -90 + acc * 3.6))
        legend.append('<div class="sl"><i style="background:%s"></i>'
                      '<span class="g">%s</span><b>%s</b><em>%.1f%%</em></div>' %
                      (color, esc(name), kh(val), frac))
        acc += vfrac
    donut = ('<svg class="donut" viewBox="0 0 42 42" role="img" '
             'aria-label="按平台的输入 token 构成">'
             '<circle class="trk" cx="21" cy="21" r="15.9155"/>' +
             "".join(arcs) + '</svg>')

    # 2) 按时间 × 按平台：三条不同颜色的折线，共用同一纵轴（全平台峰值）。
    #    窗口自适应：默认近 14 天按天；若这 14 天里不足两个平台有数据
    #    （Codex / ZCode 的会话往往更旧），退到近 12 周按周，保证三色对比图
    #    真的有三条线，而不是只剩 WorkBuddy 一条。
    today = datetime.date.today()
    keys = [k for k, _, _ in platforms] + ["other"]

    def collect(unit, n):
        """返回 (每桶起始日, {平台: [每桶输入 token]})。"""
        if unit == "day":
            labels = [today - datetime.timedelta(days=n - 1 - i) for i in range(n)]
        else:
            labels = [today - datetime.timedelta(days=(n - i) * 7 - 1) for i in range(n)]
        idx = dict((d.strftime("%Y-%m-%d"), i) for i, d in enumerate(labels)) if unit == "day" else None
        out = dict((k, [0.0] * n) for k in keys)

        def slot(day):
            if unit == "day":
                return idx.get(day)
            try:
                age = (today - datetime.date.fromisoformat(day)).days
            except (ValueError, TypeError):
                return None
            return n - 1 - age // 7 if 0 <= age < n * 7 else None

        for s in ss:
            k = s.get("src") or "workbuddy"
            if k not in out:
                k = "other"
            bins = s.get("days")
            pairs = bins.items() if bins else [(fmt_ts(s["mtime"], "%Y-%m-%d", ""), (s["pin"], 0))]
            for day, pair in pairs:
                j = slot(day)
                if j is not None:
                    out[k][j] += pair[0]
        return labels, out

    # 窗口左界跟着「最老的那个平台最后一次出会话」走：只在两个月前跑过几次的
    # 平台（典型是 DSH / Codex），固定近 14 天会让它整条线落在窗口外，看起来
    # 像没有数据。跨度 ≤14 天按天，否则按周，上限 26 周（半年外的会话不纳入）。
    last_day = {}
    for s in ss:
        # 空会话（只有元数据、0 次调用）不参与：它们的文件 mtime 可能很新，
        # 会把平台真实的最后活跃日往后推，窗口反而盖不到真正有数据的那天。
        if not s.get("calls"):
            continue
        k = s.get("src") or "workbuddy"
        if k not in keys:
            k = "other"
        bins = s.get("days") or {}
        ds = [d for d in bins.keys() if d] or [fmt_ts(s["mtime"], "%Y-%m-%d", "")]
        ds = [d for d in ds if d]
        if ds:
            last_day[k] = max(max(ds), last_day.get(k, ""))
    anchor = min(last_day.values()) if last_day else today.strftime("%Y-%m-%d")
    try:
        span = (today - datetime.date.fromisoformat(anchor)).days
    except (ValueError, TypeError):
        span = 0
    if span <= 13:
        # 最少 7 天：只在今天跑过一次时也有上下文，不至于整张图只剩一个点；
        # 前 6 天会以空档虚线的形式落到 0，一眼看出「之前没有会话」。
        unit, n = "day", max(7, min(14, span + 1))
    else:
        unit, n = "week", max(2, min(26, span // 7 + 1))
    day_list, buckets = collect(unit, n)
    active = [(k, name, color) for k, name, color in platforms if any(buckets[k])]
    if any(buckets["other"]):
        active.append(("other", "其他", "var(--p4)"))
    mx = max((v for k in buckets for v in buckets[k]), default=0) or 1
    # 视图纵向 80（横向仍 100），按容器真实宽高拉伸；每条线 inline 指定颜色，
    # 覆盖 .spark polyline 的 var(--accent) 默认色。
    H, TOP = 80, 4
    polys = _series_polys(active, buckets, n, mx, H, TOP)
    unit_cn = "天" if unit == "day" else "周"
    svg = ('<svg class="spark" viewBox="0 0 100 %d" preserveAspectRatio="none" '
           'role="img" aria-label="按%s、按平台的输入 token 走势">' %
           (H, unit_cn) + polys + '</svg>')
    # 悬停：图上叠一层 HTML 热区列（零 JS，复用 [data-tip] 气泡），一次列出
    # 该桶三个平台各自的输入量；焦点竖线按当天最高点同比例换算。
    step = 100.0 / n
    cols = []
    for i in range(n):
        top = max((buckets[k][i] for k in buckets), default=0)
        y = H - top / mx * (H - TOP - 2)
        tip = day_list[i].strftime("%m-%d") + (" 起 7 天" if unit == "week" else "") + "".join(
            "&#10;%s %s token" % (name, kh(buckets[k][i]))
            for k, name, _ in active)
        cols.append('<div class="hz" style="left:%.2f%%;width:%.2f%%" '
                    'data-tip="%s"><i style="top:%.2f%%"></i></div>' %
                    (i * step, step, tip, y / H * 100.0))
    line_legend = ('<div class="ovlegend">' + "".join(
        '<span class="lg"><i style="background:%s"></i>%s</span>' %
        (color, esc(name))
        for _k, name, color in active) + '</div>')
    line = '<div class="ovline">%s%s</div>%s' % (svg, "".join(cols), line_legend)
    axis = ('<div class="ovaxis"><span>%s</span><span>单平台峰值 %s token/%s</span>'
            '<span>%s</span></div>' %
            (day_list[0].strftime("%m-%d"), kh(mx), unit_cn,
             "今天" if unit == "day" else "最近 7 天"))
    ovt2 = "按时间 × 平台 · 近 %d %s" % (n, unit_cn)
    return donut, legend, line, axis, ovt2


def _workspace_cell(ss, total):
    """三列共用的工作区递归树，跟随当前视图的会话集合。"""
    # 工作区：按路径最后两段聚成「父目录 → 工作区」两层树
    leaf = OrderedDict()
    for s in ss:
        parts = [p for p in re.split(r"[\\/]+", s["ws"]) if p]
        parent = parts[-2] if len(parts) >= 2 else (parts[0] if parts else "?")
        name = parts[-1] if parts else "?"
        a = leaf.setdefault(parent + "/" + name, [parent, name, 0.0, 0])
        a[2] += s["pin"]
        a[3] += 1
    parents = OrderedDict()
    for parent, name, pin, n in sorted(leaf.values(), key=lambda x: -x[2]):
        parents.setdefault(parent, []).append((name, pin, n))
    tree = []
    shown_leaf = 0
    for parent, kids in list(parents.items())[:8]:
        ppin = sum(k[1] for k in kids)
        tree.append('<div class="wsrow top"><span class="wsname">%s</span>'
                    '<span class="wsbar"><i style="width:%.1f%%"></i></span>'
                    '<span class="wsn">%d</span></div>' %
                    (esc(cut(parent, 18)), min(ppin / total * 100, 100),
                     sum(k[2] for k in kids)))
        for name, pin, n in kids[:4]:
            tree.append('<div class="wsrow sub"><span class="wsname">└ %s</span>'
                        '<span class="wsbar"><i style="width:%.1f%%"></i></span>'
                        '<span class="wsn">%d</span></div>' %
                        (esc(cut(name, 20)), min(pin / total * 100, 100), n))
        shown_leaf += min(len(kids), 4)
        if len(kids) > 4:
            tree.append('<div class="wsrow sub more">└ 其余 %d 个…</div>' % (len(kids) - 4))
    if len(parents) > 8:
        tree.append('<div class="wsrow more">另有 %d 个父目录…</div>' % (len(parents) - 8))
    # 递归关系收进展开卡片：默认只留一行摘要，避免把整列拉得比另外两列长一截
    ws_cell = ('<details class="wsfold"><summary>'
               '<span class="wsfold-t">%d 个 parent 目录 · %d 个工作区 · %d 个会话</span>'
               '<span class="wsfold-h">展开</span></summary>'
               '<div class="in">%s</div></details>' %
               (len(parents), len(leaf), len(ss), "".join(tree)))
    return ws_cell


def _model_charts(ss, total, raw_total):
    """单平台视图：接多平台前的口径——按模型输入 token 环 + 按天单条折线。"""
    by_model = Counter()
    for s in ss:
        mm = s.get("models") or {}
        if mm:
            by_model.update(mm)
        else:
            by_model[s.get("model") or "未知模型"] += s["pin"]
    by_model = Counter(G.merge_model_counter(by_model))
    residual = raw_total - sum(by_model.values())
    if residual > 0:
        by_model["未知模型"] += residual
    models = sorted(by_model.items(), key=lambda x: -x[1])
    big = [(nm, v) for nm, v in models if v / total * 100.0 >= 1.0][:5]
    if len(big) < len(models):
        rest = models[len(big):]
        models = big + [("其他 %d 个模型" % len(rest), sum(v for _, v in rest))]
    else:
        models = big
    usable = bool(by_model) and not (len(by_model) == 1 and "未知模型" in by_model)
    colors = ("var(--p0)", "var(--p1)", "var(--p2)", "var(--p3)",
              "var(--p4)", "var(--accent)")
    arcs, legend, acc = [], [], 0.0
    for i, (name, val) in enumerate(models):
        frac = val / total * 100.0
        seg = max(0.0, frac - min(.6, frac * .2))
        color = colors[i % len(colors)]
        arcs.append('<circle cx="21" cy="21" r="15.9155" style="stroke:%s;animation-delay:%.2fs" '
                    'stroke-dasharray="%.3f %.3f" transform="rotate(%.2f 21 21)"/>' %
                    (color, .03 + acc / 100.0 * .42, seg, 100 - seg, -90 + acc * 3.6))
        legend.append('<div class="sl"><i style="background:%s"></i>'
                      '<span class="g">%s</span><b>%s</b><em>%.1f%%</em></div>' %
                      (color, esc(cut(name, 20)), kh(val), frac))
        acc += frac
    donut = ('<svg class="donut" viewBox="0 0 42 42" role="img" '
             'aria-label="按模型的输入 token 构成">'
             '<circle class="trk" cx="21" cy="21" r="15.9155"/>' +
             "".join(arcs) + '</svg>')

    # 近 14 天按天，单条折线 + 面积（原口径）。右界不写死「今天」：这个平台
    # 如果最后一次会话在两周前，固定窗口会让整张图全 0，改成以最后活跃日为锚点。
    today = datetime.date.today()
    lastds = []
    for s_ in ss:
        bins = s_.get("days") or {}
        ds = [d for d in bins.keys() if d] or [fmt_ts(s_["mtime"], "%Y-%m-%d", "")]
        lastds += [d for d in ds if d]
    right = today
    if lastds:
        try:
            right = min(today, max(datetime.date.fromisoformat(d) for d in lastds))
        except (ValueError, TypeError):
            right = today
    days = 14
    day_list = [right - datetime.timedelta(days=days - 1 - i) for i in range(days)]
    day_idx = dict((d.strftime("%Y-%m-%d"), i) for i, d in enumerate(day_list))
    buckets = [0.0] * days
    for s in ss:
        bins = s.get("days")
        if bins:
            for day, pair in bins.items():
                k = day_idx.get(day)
                if k is not None:
                    buckets[k] += pair[0]
        else:
            k = day_idx.get(fmt_ts(s["mtime"], "%Y-%m-%d", ""))
            if k is not None:
                buckets[k] += s["pin"]
    mx = max(buckets) or 1
    H, TOP = 80, 4
    pts = " ".join("%.2f,%.2f" % (i * 100.0 / (days - 1), H - v / mx * (H - TOP - 2))
                   for i, v in enumerate(buckets))
    svg = ('<svg class="spark" viewBox="0 0 100 %d" preserveAspectRatio="none" '
           'role="img" aria-label="按天输入 token 走势">'
           '<polygon points="0,%d %s 100,%d"/>'
           '<polyline pathLength="100" points="%s"/></svg>' %
           (H, H, pts, H, pts))
    step = 100.0 / days
    cols = []
    for i, v in enumerate(buckets):
        d = day_list[i].strftime("%m-%d")
        y = H - v / mx * (H - TOP - 2)
        cols.append('<div class="hz" style="left:%.2f%%;width:%.2f%%" '
                    'data-tip="%s&#10;输入 %s token"><i style="top:%.2f%%"></i></div>' %
                    (i * step, step, d, kh(v), y / H * 100.0))
    line = '<div class="ovline">%s%s</div>' % (svg, "".join(cols))
    axis = ('<div class="ovaxis"><span>%s</span><span>峰值 %s token/天</span>'
            '<span>%s</span></div>' %
            (day_list[0].strftime("%m-%d"), kh(mx),
             "今天" if right == today else right.strftime("%m-%d")))
    return donut, (legend if usable else []), line, axis, "按时间 · 输入 token"


def _donut_cell(donut, legend, total, empty_msg):
    """环 + 图例的外壳；环心按字数收字号，保证长数字不压到环上。"""
    if not legend:
        return '<div class="empty">%s</div>' % empty_msg
    ctr_txt = kh(total)
    ctr_fs = max(9.0, min(13.0, 13.0 * 6.0 / max(1, len(ctr_txt))))
    return ('<div class="donut-row"><div class="donut-wrap">%s'
            '<div class="ctr"><b style="font-size:%.1fpx">%s</b>'
            '<span>token</span></div></div>'
            '<div class="slice">%s</div></div>' %
            (donut, ctr_fs, ctr_txt, "".join(legend)))


def _overview_shell(cell, ovt1, ovt2, line, axis, ws_cell, note):
    return ('<div class="viz"><div class="viz-hd"><b>全库画像</b>'
            '<small>%s</small></div>'
            '<div class="ovgrid">'
            '<div class="ovcell"><div class="ovt">%s</div>%s</div>'
            '<div class="ovcell"><div class="ovt">%s</div>%s%s</div>'
            '<div class="ovcell"><div class="ovt">工作区 · 递归关系</div>%s</div>'
            '</div></div>' % (note, ovt1, cell, ovt2, line, axis, ws_cell))


def index_overview(ss, focus=""):
    """首页画像分发：全部 = 平台拼接环 + 三色线；单平台 = 模型环 + 按天单线。

    只消费 STORE.sessions() 已缓存的索引字段，不解析会话全文、不新增一次
    全库遍历。调用方在「全部」时传全量 sessions，筛到某平台时传该平台
    的 sessions，环 / 线 / 工作区树自然跟随当前视图。
    """
    if not ss:
        return ""
    raw_total = sum(s["pin"] for s in ss)
    total = raw_total or 1
    ws_cell = _workspace_cell(ss, total)
    if focus:
        donut, legend, line, axis, ovt2 = _model_charts(ss, total, raw_total)
        cell = _donut_cell(donut, legend, total, "会话记录未写模型名，此图暂不可用")
        label = PLAT_NAME.get(focus, focus)
        return _overview_shell(cell, "按模型 · 输入 token", ovt2, line, axis,
                               ws_cell, "输入 token 口径 · %s 平台内 · 只读索引" % esc(label))
    donut, legend, line, axis, ovt2 = _platform_charts(ss, total, raw_total)
    cell = _donut_cell(donut, legend, total, "暂无输入 token 记录，此图暂不可用")
    return _overview_shell(cell, "按平台 · 输入 token", ovt2, line, axis,
                           ws_cell, "输入 token 口径 · 按平台拆分 · 只读索引")


def _tile(items, x, y, w, h, out, depth=0):
    """交替方向二分铺砖：按权重把矩形切成两块，长边优先，得到接近方形的分块。
    比 squarify 更短、更可预测，且不依赖浮点最优化。"""
    if not items:
        return
    if len(items) == 1 or depth > 12 or w < 1 or h < 1:
        for v, payload in items:
            out.append((payload, x, y, w, h))
        return
    total = sum(v for v, _ in items) or 1
    acc, k = 0.0, 0
    for k, (v, _) in enumerate(items):
        acc += v
        if acc >= total / 2.0:
            break
    cut = k + 1
    ratio = acc / total
    if w >= h:
        w1 = w * ratio
        _tile(items[:cut], x, y, w1, h, out, depth + 1)
        _tile(items[cut:], x + w1, y, w - w1, h, out, depth + 1)
    else:
        h1 = h * ratio
        _tile(items[:cut], x, y, w, h1, out, depth + 1)
        _tile(items[cut:], x, y + h1, w, h - h1, out, depth + 1)


def block_treemap(d, sid, limit=24, kind=None):
    """上下文块地图：面积 ∝ 字数，颜色 = 块类型，同色深浅 = 块内相对大小。
    只铺最大的 limit 块并把面积归一到这 N 块，其余不画、只在说明里给出覆盖率
    （否则「其他」会占掉大半画布、把有效信息压成一条边）；点块直跳已有全文路由。
    kind 非空时只铺一类，等价于零脚本的类别钻取。"""
    ctx = d["ctx"]
    if not ctx:
        return '<div class="empty">这个会话没有上下文块</div>'
    ranked = sorted(((b["n"], i, b) for i, b in enumerate(ctx)
                     if kind is None or b["k"] == kind), reverse=True)
    if not ranked:
        return '<div class="empty">这个类型下没有上下文块</div>'
    picked = ranked[:limit]
    items = [(max(n, 1), (n, i, b)) for n, i, b in picked]
    laid = []
    _tile(items, 0, 0, 800, 300, laid)
    covered = sum(n for n, _, _ in picked)
    total_arch = d["stats"]["chars"] or 1
    body = []
    for (n, idx, b), x, y, w, h in laid:
        k = b["k"]
        # 同类深浅二次编码：越大的块越实，避免小格抢视觉重心
        alpha = .46 + .44 * (min(n / (picked[0][0] or 1), 1.0) ** .5)
        label = TYPES.get(k, k)
        tip = "#%d · %s · %s 字" % (idx, label, nfmt(n))
        color = PALETTE.get(k, PALETTE["tool_result"])
        inner = ('<rect x="%.1f" y="%.1f" width="%.1f" height="%.1f" '
                 'style="fill:%s;fill-opacity:%.2f"/>' % (x, y, w, h, color, alpha))
        if w >= 58 and h >= 38:
            inner += ('<text x="%.1f" y="%.1f">%s</text>' %
                      (x + 5, y + 15, esc(cut(label, max(4, int(w / 10))))))
            inner += ('<text class="sub" x="%.1f" y="%.1f">≈ %s 字</text>' %
                      (x + 5, y + 28, kh(n)))
        elif w >= 34 and h >= 16:
            inner += ('<text class="sub" x="%.1f" y="%.1f">%s</text>' %
                      (x + 4, y + 12, kh(n)))
        body.append('<a href="/s/%s/b/%d"><title>%s · 点开看全文</title>%s</a>' %
                    (esc(sid), idx, esc(tip), inner))
    legend = "".join('<span><i style="background:%s"></i>%s</span>' %
                     (PALETTE[k], esc(TYPES[k])) for k in ORDER)
    return ('<svg class="map" viewBox="0 0 800 300" role="img" '
            'aria-label="上下文块地图，面积与字数成正比">%s</svg>'
            '<div class="map-cap"><div class="legend">%s</div>'
            '<span>· 面积 ∝ 字数，颜色 = 类型，深浅 = 相对大小</span>'
            '<span>· 最大的 %d 块占归档原文 %.1f%%；点块看不截断全文</span></div>' %
            ("".join(body), legend, len(picked), covered / total_arch * 100))


def gradient_segments(r):
    """只输出 5 个累计百分比作为 CSS 变量；渐变本体常驻 .trend a i。
    一根柱 = 1 个 <i> + 约 70 字节，而不是 5 个 <i> + 约 250 字节。"""
    total = r["chars"] or 1
    acc, out = 0.0, []
    for j, k in enumerate(ORDER):
        acc += r["comp"][k]
        out.append("--s%d:%.2f%%" % (j, min(acc / total * 100.0, 100.0)))
    return ";".join(out)


def trend_overlay(rows):
    """命中率折线：单个 SVG polyline 叠在柱上，40 个点约 400 字节。"""
    n = len(rows)
    if n < 2:
        return ""
    pts = " ".join("%.2f,%.2f" % ((i + .5) * 100.0 / n, 100 - r["rate"] * 100)
                   for i, r in enumerate(rows))
    return ('<svg class="ovl" viewBox="0 0 100 100" preserveAspectRatio="none" '
            'aria-hidden="true"><polyline pathLength="100" points="%s"/></svg>' % pts)


def rate_bar(c, width=True):
    r = (c["hit"] / c["pin"]) if c["pin"] else None
    if r is None:
        return '<span class="fine">并列 · 无独立 usage</span>'
    return ('<span class="bar" style="%s"><i class="h" style="width:%.2f%%"></i>'
            '<i class="m" style="width:%.2f%%"></i></span>' %
            ("flex:1" if width else "width:100px", r * 100, (1 - r) * 100))


def cache_hit_span(d, i):
    """第 i 次调用的缓存命中前缀 [lo, hi]（绝对块号，含端点），O(log B)。

    口径与 call_state / call_outline 完全一致：按可见区间内的相对字数二分。
    不逐块遍历，所以「按调用浏览」给 40 行各算一次也不增加可感知开销。
    hi 这一块可能正好跨缓存边界（part），展开后按真实状态 chip 显示。
    """
    c = d["calls"][i]
    n = min(c["ctxN"], len(d["ctx"]))
    lo = min(c.get("vf", 0), n)
    if not c["pin"] or n <= lo:
        return lo, None
    pref = d["pref"]
    b = bisect.bisect_left(
        pref, pref[lo] + (pref[n] - pref[lo]) * c["hit"] / c["pin"], lo, n + 1)
    return lo, max(lo, min(n - 1, b - 1))


def block_row_html(d, i, sid, seq, j, st):
    """上下文块清单里的一行（抽出来供折叠 / 展开两种路径共用）。"""
    b = d["ctx"][j]
    # 行内保持「X 字」不挤，时间 / 估算 token / 携带次数放 title 悬停可见；
    # 块全文页的 KPI 里则三项都直接展示。
    tip = ("%s · ≈ %s token · 被带上 %s 次" %
           (fmt_ts(b.get("ts"), "%Y-%m-%d %H:%M", "时间未知"),
            nfmt(b.get("e") or G.est_tokens(b["t"])),
            nfmt(len(block_users(d, j)))))
    return ('<a class="row" title="%s" href="/s/%s/b/%d?ref=%d" '
            'style="grid-template-columns:52px 64px 92px 1fr 84px 62px">'
            '<span class="idx">#%d</span><span class="chip %s">%s</span>'
            '%s'
            '<span class="grow"><b style="font-weight:500">%s</b>%s</span>'
            '<span class="num">%s 字</span><span class="num" style="color:var(--link-h)">全文 →</span></a>' %
            (esc(tip), esc(sid), j, seq, j, st, G.CHIP[st], type_chip(b["k"]),
             esc(b["l"]), G.cmd_html(b, 140), nfmt(b["n"])))


def hit_fold_html(lo, hi, rows):
    """把连续命中块折成一行「#lo–#hi 块缓存命中」，点开逐块查看。原生 details，零 JS。"""
    return ('<details class="hitfold"><summary><span class="hf-dot"></span>'
            '<b>#%d–#%d 块缓存命中</b><span class="hf-n">%d 块</span>'
            '<small>点开逐块查看</small></summary><div class="in">%s</div></details>' %
            (lo, hi, hi - lo + 1, "".join(rows)))


def call_hit_fold(d, i, sid):
    """「按调用浏览」列表里的折叠摘要：只算区间，不展开块行。

    零 JS 下 <details> 内容仍占 HTML 字节，每页 40 行必须写得很短；
    逐块清单交给调用详情（那里同样有折叠），不在这里重复铺开。
    """
    lo, hi = cache_hit_span(d, i)
    if hi is None or hi < lo:
        return ""
    return ('<details class="hitfold mini"><summary><span class="hf-dot"></span>'
            '<b>#%d–#%d 块缓存命中</b><span class="hf-n">%d 块</span></summary>'
            '<div class="in"><a href="/s/%s/c/%d">展开逐块清单 →</a></div></details>' %
            (lo, hi, hi - lo + 1, esc(sid), i + 1))


def call_row(d, c, sid, show_turn=True):
    r = (c["hit"] / c["pin"]) if c["pin"] else None
    cls = "bad" if (r is not None and r < 0.5) else ("warn2" if (r is not None and r < 0.8) else "")
    pct = "%.1f%%" % (r * 100) if r is not None else "—"
    return ('<a class="row %s" href="/s/%s/c/%d" style="grid-template-columns:40px 90px 1fr 78px 78px 78px 66px 60px 92px">'
            '<span class="idx">#%d</span>%s'
            '<span class="grow"><b style="font-weight:500">%s</b>%s</span>'
            '<span class="num">输入 %s</span><span class="num">未命中 %s</span>'
            '<span class="num">输出 %s</span><span class="num">%s</span>'
            '<span class="num credit">%.2f 分</span><span class="num">%s</span></a>' %
            (cls, esc(sid), c["seq"], c["seq"], rate_bar(c),
             esc(c["tool"] or "—"),
             ('<span class="chip t" style="margin-left:6px">轮 %d</span>' % c["turn"]) if show_turn and c["turn"] else "",
             nfmt(c["pin"]), nfmt(c["pin"] - c["hit"]), nfmt(c["pout"]), pct,
             c.get("credit") or 0,
             '<span style="color:var(--link-h)">详情 →</span>'))


def turn_row(d, t, sid):
    b = turn_brief(d, t)
    return ('<a class="row" href="/s/%s/t/%d" style="grid-template-columns:52px 1fr 68px 92px 72px 90px 74px 62px">'
            '<span class="idx">轮 %d</span>'
            '<span class="grow">%s<b style="font-weight:500">%s</b>%s</span>'
            '<span class="num">%d 次</span><span class="num">输入 %s</span>'
            '<span class="num">命中 %s</span><span class="num">输出 %s</span>'
            '<span class="num credit">%.2f 分</span>'
            '<span class="num" style="color:var(--link-h)">展开 →</span></a>' %
            (esc(sid), b["i"], b["i"], turn_chip(b["kind"]),
             esc(b["disp"] or "（只有系统注入）"),
             '<span class="chip cold" style="margin-left:6px">冷启动</span>' if b["cold"] else "",
             b["calls"], nfmt(b["pin"]), "%.1f%%" % (b["rate"] * 100),
             nfmt(b["pout"]), b["credit"]))


# ---------------------------------------------------------------- HTML 视图

def human_size(n):
    """字节数转可读体积：首页「源文件」这类会随数据增长的量，不写死。"""
    n = float(n or 0)
    if n >= 1024 ** 3:
        return "%.1f GB" % (n / 1024 ** 3)
    if n >= 1024 ** 2:
        return "%.0f MB" % (n / 1024 ** 2)
    return "%.0f KB" % (n / 1024.0)


def v_index(q):
    ss_all = STORE.sessions(refresh=qstr(q, "refresh") == "1")
    ws = qstr(q, "ws")
    src = qstr(q, "src")
    sort = qstr(q, "sort", "mtime")
    # 平台筛选：不传 = 全部（WorkBuddy 与 Codex/ZCode 合并视图）
    ss = [s for s in ss_all if not src or (s.get("src") or "workbuddy") == src]
    shown = [s for s in ss if not ws or s["ws"] == ws]
    if sort == "credit":
        shown = sorted(shown, key=lambda s: -s["credit"])
    elif sort == "calls":
        shown = sorted(shown, key=lambda s: -s["calls"])
    else:
        shown = sorted(shown, key=lambda s: -s["mtime"])

    # 首页也分页：会话列表随使用线性增长，全量渲染迟早会顶破 80 KB 硬闸
    # （实测 132 个会话 94.8 KB）。分页后单页体积与总会话数无关。
    total_shown = len(shown)
    pg = clamp_p(q, total_shown, PAGE_SESSIONS)
    lo = pg * PAGE_SESSIONS
    page_items = shown[lo:lo + PAGE_SESSIONS]

    total_cr = sum(s["credit"] for s in ss)
    total_calls = sum(s["calls"] for s in ss)
    has_ext = any((s.get("src") or "workbuddy") != "workbuddy" for s in ss_all)
    total_cost = 0.0
    if has_ext:
        total_cost = sum(s.get("cost") or 0.0 for s in ss)
    kpis = [("会话", nfmt(len(ss))), ("工作区", nfmt(len(set(s["ws"] for s in ss)))),
            ("模型调用", nfmt(total_calls))]
    if has_ext:
        # 积分只有 WorkBuddy 有；外部平台按刊例价估算，两套账并列但各自标清口径
        kpis.append(("积分·WorkBuddy", "%.1f" % total_cr))
        kpis.append(("估算成本", "¥%.2f" % total_cost))
    else:
        kpis.append(("累计积分", "%.1f" % total_cr))
    kpis += [("累计输入 token", nfmt(sum(s["pin"] for s in ss))),
             ("累计输出 token", nfmt(sum(s["pout"] for s in ss)))]
    # 「全部」用全量会话画平台拼接环 + 三色线；切到单个平台则回到该平台
    # 自己的按模型环 + 单条按天折线。KPI 与会话列表始终跟随筛选。
    body = ('<div class="hero">' + kpi(kpis, fs=21, pad=42) +
            index_overview(ss_all if not src else ss, focus=src) + '</div>')
    # 子代理关系用全量 shown 算（父在第 1 页、子在第 2 页时也能标出归属）。
    # 同一页内的父子在会话列表里收编成「父行 → 缩进子树」，不再各贴一个孤立
    # chip 平铺在不同工作区分组里；跨页 / 未定位的行内注明父级。
    rel, _unmatched = subagent_relations(shown)
    sub_count = {k: len(v) for k, v in rel.items()}
    page_ids = set(s["id"] for s in page_items)
    parent_of, children_of, nested_ids = {}, {}, set()
    for p, kids in rel.items():
        for k in kids:
            parent_of[k["id"]] = p
            if p in page_ids and k["id"] in page_ids:
                children_of.setdefault(p, []).append(k)
                nested_ids.add(k["id"])
    ptitle = dict((s["id"], s["title"]) for s in shown)
    meta_warn = ""
    if any(not s.get("meta_ok", True) for s in ss):
        meta_warn = ('<div class="warn">workbuddy.db 未读到：会话标题与工作区已退回路径兜底；'
                     '模型名、token 与积分来自会话记录，不受影响。详情见服务端终端输出。</div>')
    body += search_box("/search", "", "text", "all",
                       "搜上下文块 / 输出 / 提问原文（跨全部会话）")
    # 一条分区工具条：左侧文字导航（细分隔线）、右侧排序分段控件，
    # 取代原先两行大小不一的 pill 按钮堆。窄屏时分区自动折行。
    sort_links = "".join(
        '<a class="%s" href="/?sort=%s">%s</a>' %
        ("on" if sort == k else "", k, lab)
        for k, lab in (("mtime", "最近修改"), ("credit", "积分"), ("calls", "调用数")))
    clear_ws = (('<a class="tb-clear" href="/">清除工作区筛选：%s ✕</a>' % esc(ws))
                if ws else "")
    # 平台切换挂到大标题同一栏（header 里的 .platbar），不再挤在下方工具条。
    # 只在检测到外部源时出现，单平台用户看到的界面不变。
    # 平台切换挂到大标题同一栏（header 里的 .platbar）。
    # 本机没有某个平台的会话时（没装、或还没写过记录），那一项不再可点：
    # 渲染成置灰的 span 并挂 title 说明，避免点进去看到一页空白。
    plat = plat_switch(ss_all, src, "/",
                       keep=(("sort", "" if sort == "mtime" else sort),))
    src_bytes = 0
    for s in ss:
        try:
            src_bytes += os.path.getsize(s["path"])
        except OSError:
            pass            # 单个文件被清理/占用时只少算它，不让整页退回「体积未知」
    body += ('<div class="toolbar"><nav class="tb-nav">'
             '<a href="/tools">工具与命令</a>'
             '<a href="/stats">全局统计</a>'
             '<a href="/rank">筛选与排序</a>'
             '<a href="/api">API 文档</a>'
             '<a href="/?refresh=1">刷新</a>%s</nav>'
             '<div class="tb-sort">%s<span class="tb-lab">排序</span>'
             '<span class="tb-seg">%s</span></div>'
             '<span class="tb-meta">源文件 %s · 只读</span></div>' %
             (monitor_link(), clear_ws, sort_links,
              human_size(src_bytes) if src_bytes else "体积未知"))

    groups = OrderedDict()
    for s in page_items:
        if s["id"] in nested_ids:
            continue                      # 已收编到父会话的子树，不再平铺一次
        groups.setdefault(s["ws"], []).append(s)
    # 分组标题按「该工作区的全部会话」统计，不能拿本页切片算：分页条写「共 60」，
    # 标题写「本页 40 个会话 · 40.0 分」，同一屏两套口径。工作区被分页切开时
    # 才追加「本页 N」，说明为什么这一组只显示了一部分。
    ws_stat = {}
    for s in shown:
        a = ws_stat.setdefault(s["ws"], [0, 0.0])
        a[0] += 1
        a[1] += s["credit"]

    def session_row(s, tag, rowcls, note="", branch=False, title=None):
        """会话列表一行；note 追加在第二行说明里，branch 画树状连接符。"""
        t = fmt_ts(s["mtime"], "%m-%d %H:%M", "")
        br = '<span class="branch">└</span>' if branch else ""
        label = title if title is not None else s["title"]
        src = s.get("src") or "workbuddy"
        if src != "workbuddy":
            # 外部平台没有积分字段，用刊例价估算成本代替，避免整列显示 0.0 分
            name = PLAT_SHORT.get(src, src)
            cost = s.get("cost")
            money = ("≈¥%.2f" % cost) if cost is not None else "—"
            tag = ('<span class="chip ks" title="%s">%s</span>'
                   % (esc(PLAT_NAME.get(src, src)), esc(name))) + tag
        else:
            money = "%.1f 分" % s["credit"]
        return ('<a class="%s" href="/s/%s" style="grid-template-columns:1fr 96px 108px 92px 92px 82px 70px">'
                '<span class="grow">%s%s<b style="font-weight:500">%s</b>'
                '<span class="fine" style="display:block">%s · %s%s</span></span>'
                '<span class="num">%d 轮</span><span class="num">%d 次调用</span>'
                '<span class="num">输入 %s</span><span class="num">输出 %s</span>'
                '<span class="num credit">%s</span>'
                '<span class="num">%s</span></a>' %
                (rowcls, esc(s["id"]), br, tag, esc(label[:70]),
                 esc(s["id"][:8]), esc(s["model"] or "?"), note,
                 s["turns"], s["calls"], "%.1fM" % (s["pin"] / 1e6),
                 "%.0fk" % (s["pout"] / 1000), money, t))

    def sub_row(k, parent, nested):
        tag = ('<span class="chip ks" title="子代理会话 · %s">子代理 · %s</span>' %
               (esc(k["agent"] or "?"), esc(k["agent"] or "?")))
        title = "派生子代理" if k["title"] == "(无标题)" else None
        if nested:
            note = ' · 派生自「%s」' % esc(cut(parent["title"], 20))
            return session_row(k, tag, "row subagent child", note, True, title)
        par = parent_of.get(k["id"])
        if par:
            note = ' · 派生自「%s」（父级不在本页）' % esc(cut(ptitle.get(par, "?"), 18))
        else:
            note = " · 未能定位父级"
        return session_row(k, tag, "row subagent", note, False, title)

    def sub_tree(kids, parent, total_n):
        more = (" · 另有 %d 个在其它页" % (total_n - len(kids))) if total_n > len(kids) else ""
        rows = "".join(sub_row(k, parent, True) for k in kids)
        return ('<details class="subtree"%s><summary>'
                '<span class="st-n">由本会话派生%s</span>'
                '<span class="st-h">展开</span></summary>'
                '<div class="in">%s</div></details>' %
                (" open" if len(kids) <= 3 else "", more, rows))

    for name in sorted(groups, key=lambda w: -max(x["mtime"] for x in groups[w])):
        items = groups[name]
        cards = []
        for s in items:
            kids = children_of.get(s["id"]) or []
            if s.get("is_sub"):
                cards.append(sub_row(s, None, False))
            else:
                n = sub_count.get(s["id"], 0)
                tag = ('<span class="chip km" title="派生了 %d 个子代理会话">%d 个子代理</span>' %
                       (n, n)) if n else ""
                cards.append(session_row(s, tag, "row"))
                if kids:
                    cards.append(sub_tree(kids, s, n))
        n_all, cr_all = ws_stat.get(name) or (len(items), sum(s["credit"] for s in items))
        part = "" if len(items) >= n_all else " · 本页 %d" % len(items)
        n_kids = sum(len(children_of.get(s["id"]) or []) for s in items)
        kid_note = " · 含 %d 个子代理" % n_kids if n_kids else ""
        body += ('<details class="grp" open><summary>%s<small>%d 个会话 · %.1f 分%s%s</small></summary>'
                 '<div class="in">%s</div></details>' %
                 (esc(name), n_all, cr_all, part, kid_note, "".join(cards)))
    if total_shown > PAGE_SESSIONS:
        body += pager("/", q, pg, PAGE_SESSIONS, total_shown, "个会话")
    # 元数据降级提示放在列表之后：它不影响会话列表本身，不该抢占首屏
    body += meta_warn
    return page("会话索引", [("索引", "")], body,
                sub="%d 个会话 · 点任一会话进入全量轨迹（不裁调用、不裁文本）" % total_shown,
                need=("index",), topbar=plat)


def v_session(q, sid):
    d = STORE.get(sid)
    s, st = d["s"], d["stats"]
    p = qint(q, "p")
    view = qstr(q, "view", "turns")
    crumbs = [("索引", "/"), (d["title"][:30], "")]
    body = kpi([("对话轮次", nfmt(st["turns"])), ("模型调用", nfmt(st["calls"])),
                ("上下文块", nfmt(st["blocks"])), ("归档原文", nfmt(st["chars"]) + " 字"),
                ("可见峰值", nfmt(st["visible_peak"]) + " 字"),
                ("压缩次数", nfmt(st["compactions"])),
                ("累计输入 token", nfmt(st["pin"])), ("缓存命中", "%.1f%%" % (st["rate"] * 100)),
                ("未命中（计费）", nfmt(st["miss"])), ("输出 token", nfmt(st["pout"])),
                ("累计积分", "%.2f" % st["credit"]), ("冷启动断点", nfmt(st["cold"]))])
    if st["compactions"]:
        body += ('<div class="warn">本会话发生过 <b>%d 次上下文压缩</b>：'
                 '历史被摘要替换，%s 字旧块已退役（仍可查全文，但不再计入后续调用）。'
                 '<b>归档原文</b>是全部块总字数，<b>可见峰值</b>才是单次请求真正带上的最大值。'
                 '<a href="/s/%s/compact">打开压缩事件校验页核对</a></div>' %
                 (st["compactions"], nfmt(st["retired_chars"]), esc(sid)))
    body += compose_bar(st["compose"])
    body += ('<div class="viz"><div class="viz-hd"><b>上下文规模与走势</b>'
             '<small>归档 / 退役 · 可见字数走势按调用降采样 · ≈ 为原文统计</small></div>' +
             reserve_bar(st) + sparkline_svg(visible_series(d)) + '</div>')
    if st.get("skipped_lines"):
        body += ('<div class="warn">源文件有 %d 行无法解析（多半是 WorkBuddy 写入中途被读到），'
                 '已跳过；本页统计与上下文不含这些行。刷新页面可重试。</div>' % st["skipped_lines"])
    body += ('<div class="note">这个会话的 %s 次调用、%s 个上下文块、%s 字原文<b>全部可查</b>：'
             '按轮 / 按调用翻页，点任意一次调用看它当时带上的完整块列表，'
             '再点任意一块看<b>不截断</b>的全文（超长块自动分片）。</div>' %
             (nfmt(st["calls"]), nfmt(st["blocks"]), nfmt(st["chars"])))
    biggest = max((b["n"] for b in d["ctx"]), default=0)
    hint = ("含 %.1f 万字的长块" % (biggest / 1e4)) if biggest >= 1e4 else "含长块"
    body += search_box("/s/%s/search" % sid, "", "text", "all",
                       "只在这个会话里搜（%s）" % hint)
    body += ('<div class="navgrid">'
             '<a class="navcard" style="--nc:var(--p0)" href="/s/%s/map"><i>↗</i><b>上下文块地图</b>'
             '<span>面积 ∝ 字数 · 点块看全文</span></a>'
             '<a class="navcard" style="--nc:var(--p2)" href="/s/%s/trend"><i>↗</i><b>上下文趋势</b>'
             '<span>逐调用构成 / 净变化 / 命中率</span></a>'
             '<a class="navcard" style="--nc:var(--p1)" href="/s/%s/tools"><i>↗</i><b>工具与命令</b>'
             '<span>按工具看积分与调用频次</span></a>'
             '<a class="navcard" style="--nc:var(--accent)" href="/s/%s/compact"><i>↗</i><b>压缩事件校验</b>'
             '<span>真实账单与修正前后口径</span></a></div>' % ((esc(sid),) * 4))
    body += ('<div class="segrow"><span class="fine">浏览方式</span>'
             '<span class="seg"><a class="%s" href="/s/%s?view=turns">按轮浏览</a>'
             '<a class="%s" href="/s/%s?view=calls">按调用浏览</a></span>'
             '<a class="seglink" href="/api/s/%s">这个会话的 JSON ↗</a></div>' %
             ("on" if view == "turns" else "", esc(sid),
              "on" if view == "calls" else "", esc(sid), esc(sid)))

    if view == "calls":
        total = len(d["calls"])
        p = clamp_p(q, total, PAGE_CALLS)
        lo = p * PAGE_CALLS
        body += '<h2>全部 %s 次调用 <small>每页 %d 条，一次都不省</small></h2>' % (nfmt(total), PAGE_CALLS)
        for k in range(lo, min(lo + PAGE_CALLS, total)):
            body += call_row(d, d["calls"][k], sid)
            body += call_hit_fold(d, k, sid)
        pq = dict(q)
        pq["view"] = ["calls"]
        body += pager("/s/%s" % sid, pq, p, PAGE_CALLS, total, "次调用")
    else:
        # 按来源筛选：session 里一条 role=user 消息就是一轮，其中混着后台任务通知、
        # 压缩摘要与系统提醒。默认全看，可只看「用户消息」。
        kind = qstr(q, "kind", "all")
        if kind != "all" and kind not in TURN_KIND:
            kind = "all"
        all_turns = d["turns"]
        counts = Counter(t.get("kind") or "other" for t in all_turns)
        flt = ['<a class="%s" href="/s/%s?kind=all">全部 %s</a>' %
               ("on" if kind == "all" else "", esc(sid), nfmt(len(all_turns)))]
        for k, (lab, _, why) in TURN_KIND.items():
            if counts.get(k):
                flt.append('<a class="%s" href="/s/%s?kind=%s" title="%s">%s %d</a>' %
                           ("on" if kind == k else "", esc(sid), k, esc(why), esc(lab), counts[k]))
        body += '<div class="tkfilter">' + "".join(flt) + '</div>'
        body += ('<div class="fine" style="margin:0 0 12px">只有「用户消息」是你输入的内容；'
                 '后台任务通知、压缩摘要与系统提醒由客户端写入，也各占一轮。</div>')
        shown_turns = all_turns if kind == "all" else [
            t for t in all_turns if (t.get("kind") or "other") == kind]
        total = len(shown_turns)
        p = clamp_p(q, total, PAGE_TURNS)
        lo = p * PAGE_TURNS
        title = ("全部 %s 轮" % nfmt(total) if kind == "all" else
                 "「%s」%s 轮" % (TURN_KIND[kind][0], nfmt(total)))
        body += '<h2>%s <small>每页 %d 轮%s</small></h2>' % (
            title, PAGE_TURNS, " · 已筛选" if kind != "all" else "")
        for t in shown_turns[lo:lo + PAGE_TURNS]:
            body += turn_row(d, t, sid)
        body += pager("/s/%s" % sid, q, p, PAGE_TURNS, total, "轮")
        if d["cold"]:
            body += '<h3>缓存冷启动断点 <small>上一次 &gt;80% 命中、这一次 &lt;50%</small></h3>'
            for i in d["cold"][:10]:
                c = d["calls"][i]
                body += ('<a class="row bad" href="/s/%s/c/%d" style="grid-template-columns:56px 1fr 100px 92px 82px">'
                         '<span class="idx">#%d</span><span class="grow">轮 %d · 命中率从 &gt;80%% 掉到 %.1f%%</span>'
                         '<span class="num">输入 %s</span><span class="num">未命中 %s</span>'
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
        body += kpi([("调用次数", nfmt(tb["calls"])), ("输入 token", nfmt(tb["pin"])),
                     ("命中率", "%.1f%%" % (tb["rate"] * 100)), ("输出 token", nfmt(tb["pout"])),
                     ("积分", "%.2f" % tb["credit"]), ("系统注入", nfmt(tb["inj"]) + " 字")])
        kind = tb["kind"]
        heads = {"user": "你这一轮问的是：",
                 "task": "后台任务通知（不是你输入的）：",
                 "compact": "上下文压缩摘要（不是你输入的）：",
                 "injected": "系统注入（不是你输入的）：",
                 "other": "内部消息（不是你输入的）："}
        body += ('<div class="note">%s<b>%s</b> %s</div>' %
                 (turn_chip(kind), heads.get(kind, heads["other"]), esc(tb["disp"])))
        if kind != "user":
            body += ('<div class="fine" style="margin:-8px 0 16px">WorkBuddy 会把后台任务通知、'
                     '压缩摘要与系统提醒也以 user 消息写入会话记录，所以它们各占一轮；'
                     '带「用户消息」标签的才是你输入的内容。</div>')
        if tb["cold"]:
            body += '<div class="warn">这一轮首次调用命中率 &lt;50%，是缓存冷启动：整段历史按全价重算。</div>'
    # 一轮可能含几百次调用：不分页会把单页撑过 80 KB 硬闸（长会话实测 89 KB）。
    p = clamp_p(q, len(idxs), PAGE_CALLS)
    lo = p * PAGE_CALLS
    body += '<h2>这一轮的 %s 次调用 <small>每页 %d 条，一次都不省</small></h2>' % (
        nfmt(len(idxs)), PAGE_CALLS)
    for i in idxs[lo:lo + PAGE_CALLS]:
        body += call_row(d, d["calls"][i], sid, show_turn=False)
    body += pager("/s/%s/t/%d" % (sid, turn), q, p, PAGE_CALLS, len(idxs), "次调用")
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
    body = kpi([("接口输入 token", nfmt(c["pin"])), ("缓存命中 token", nfmt(c["hit"])),
                ("未命中 token", nfmt(c["pin"] - c["hit"])),
                ("命中率", "%.1f%%" % (r * 100) if r is not None else "—"),
                ("输出 token", nfmt(c["pout"])), ("积分", "%.2f" % (c.get("credit") or 0)),
                ("可见上下文", "%s 块 / %s 字" % (nfmt(o["n"]), kh(o["chars"]))),
                ("缓存边界", "#%s" % (o["boundary"] + 1) if o["boundary"] is not None else "—")])
    if o["compacted"]:
        body += ('<div class="warn">这次调用之前发生过<b>上下文压缩</b>：'
                 '前 %s 个块已被摘要替换，不再计入本次可见上下文，'
                 '但仍可点开查全文（历史原文没有删除）。'
                 '<a href="/s/%s/compact">看本会话的压缩事件</a></div>' %
                 (nfmt(o["lo"]), esc(sid)))
    body += ('<div class="note">工具 <b>%s</b> · 模型 %s · %s<br>'
             '下面列出这次请求<b>实际带上的全部 %s 个块</b>，每行徽章标注命中 / 未命中 / 跨边界，'
             '颜色只作辅助。<b>连续命中块默认折叠成一行「#a–#b 块缓存命中」，'
             '点一下再逐块展开</b>；点任意一行看该块不截断的全文。</div>' %
             (esc(c["tool"] or "最终回答"), esc(c["model"]),
              fmt_ts(c["ts"]),
              nfmt(o["n"])))

    filt = []
    for k, lab in (("all", "全部"), ("miss", "只看未命中"), ("hit", "只看命中"), ("part", "跨边界")):
        filt.append('<a class="btn %s" href="/s/%s/c/%d?state=%s">%s</a>' %
                    ("on" if state == k else "", esc(sid), seq, k, lab))
    body += '<div class="tabs">' + "".join(filt) + '</div>'

    if state == "all":
        chosen = list(range(o["lo"], o["hi"]))
    else:
        chosen = [j for j in range(o["lo"], o["hi"]) if call_state(d, i, j) == state]
    p = clamp_p(q, len(chosen), PAGE_BLOCKS)
    lo = p * PAGE_BLOCKS
    page_idx = chosen[lo:lo + PAGE_BLOCKS]
    body += '<h2>上下文块 <small>共 %s 个，当前筛选 %s 个 · 每页 %d</small></h2>' % (
        nfmt(o["n"]), nfmt(len(chosen)), PAGE_BLOCKS)
    # 连续的缓存命中块默认折成一行「#a–#b 块缓存命中」，点开逐块查看；
    # 用户主动选「只看命中」时全部展开，不再折叠。
    fold = state == "all"
    span_lo, span_hi = cache_hit_span(d, i) if fold else (0, None)
    plain, folded = [], []
    for j in page_idx:
        st = call_state(d, i, j)
        row = block_row_html(d, i, sid, seq, j, st)
        if span_hi is not None and span_lo <= j <= span_hi:
            folded.append((j, row))
        else:
            plain.append(row)
    if folded:
        body += hit_fold_html(folded[0][0], folded[-1][0], [r for _, r in folded])
    body += "".join(plain)
    body += pager("/s/%s/c/%d" % (sid, seq), q, p, PAGE_BLOCKS, len(chosen), "块")

    if c["out"]:
        body += '<h2>本次输出 <small>不占输入上下文；每段全文可分片查看</small></h2>'
        for k, seg in enumerate(c["out"]):
            body += ('<a class="row" href="/s/%s/o/%d?seg=%d" style="grid-template-columns:70px 1fr 100px 70px">'
                     '<span class="chip t">%s</span><span class="grow">%s</span>'
                     '<span class="num">%s 字</span><span class="num" style="color:var(--link-h)">全文 →</span></a>' %
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
    term = qstr(q, "q").strip()
    text = b["t"]
    total_parts = max(1, (len(text) + PART_CHARS - 1) // PART_CHARS)
    part = min(part, total_parts - 1)
    chunk = text[part * PART_CHARS:(part + 1) * PART_CHARS]
    if raw:
        return 200, "text/plain; charset=utf-8", chunk.encode("utf-8")

    qenc = urlencode({"q": term}) if term else ""

    def blk_url(p, keep_term=True):
        u = "/s/%s/b/%d?part=%d&ref=%d" % (sid, idx, p, ref)
        if keep_term and qenc:
            u += "&" + qenc
        return esc(u)

    hit_pos, hit_parts = block_hits(text, term)
    users = block_users(d, idx)
    crumbs = [("索引", "/"), (d["title"][:20], "/s/%s" % sid),
              ("调用 #%d" % ref if ref else "上下文块", "/s/%s/c/%d" % (sid, ref) if ref else ""),
              ("块 #%d 全文" % idx, "")]
    # 估算 token 在解析期已存进块（gen_trace.est_tokens）；旧索引没有该字段时
    # 用当前全文现算一次兜底。≈ 表示非官方 tokenizer，口径见 README 已知限制。
    est = b.get("e") or G.est_tokens(text)
    body = kpi([("写入时间", fmt_ts(b.get("ts"), "%m-%d %H:%M", "时间未知")),
                ("原文长度", nfmt(b["n"]) + " 字"),
                ("估算 token", "≈ " + nfmt(est)),
                ("类型", TYPES.get(b["k"], b["k"])),
                ("分片", "%d / %d" % (part + 1, total_parts)),
                ("本片字数", nfmt(len(chunk))),
                ("被带上", "%s 次调用" % nfmt(len(users)))])
    if b["c"]:
        body += '<div class="note"><b>产生这个结果的原始指令</b><span class="cmd">$ %s</span></div>' % esc(b["c"])
    body += ('<div class="tabs"><a class="btn" href="%s">本片纯文本</a>' %
             esc("/s/%s/b/%d?raw=1&part=%d" % (sid, idx, part)) +
             '<a class="btn" href="%s">这一片的 JSON</a>' %
             esc("/api/s/%s/blocks/%d?offset=%d&limit=%d" %
                 (sid, idx, part * PART_CHARS, PART_CHARS)) +
             '<a class="btn %s" href="%s">← 上一片</a>' %
             ("off" if part == 0 else "", blk_url(part - 1)) +
             '<a class="btn %s" href="%s">下一片 →</a>' %
             ("off" if part >= total_parts - 1 else "", blk_url(part + 1)) + '</div>')

    if term:
        body += ('<div class="warn">关键词 <b>%s</b>：本块共 <b>%s</b> 处命中，'
                 '分布在 %d 个分片。点片号直接跳过去，命中处已高亮。</div>' %
                 (esc(term), nfmt(len(hit_pos)), len(hit_parts)))
        if hit_parts:
            links = "".join('<a class="btn %s" href="%s">片 %d</a>' %
                            ("on" if p == part else "", blk_url(p), p + 1)
                            for p in hit_parts[:FIND_MAX_PARTS])
            nxt = next((p for p in hit_parts if p > part), None)
            if nxt is not None:
                links += '<a class="btn" href="%s">下一处命中 →</a>' % blk_url(nxt)
            body += '<div class="tabs">%s</div>' % links
    html_chunk, hl_n = hl_text(chunk, term)
    body += '<pre class="text">%s</pre>' % html_chunk
    if term and hl_n >= HL_MAX:
        body += ('<div class="warn">本片命中超过 %d 处，只高亮前 %d 处；'
                 '用上面的分片导航或 API 继续看。</div>' % (HL_MAX, HL_MAX))
    if total_parts > 1:
        body += ('<div class="fine">这是第 %d/%d 片（每片 %s 字）。全文共 %s 字，'
                 '一片都不缺，用「下一片」翻完，或用 API 一次取全文。</div>' %
                 (part + 1, total_parts, nfmt(PART_CHARS), nfmt(len(text))))

    body += '<h3>哪些调用带上了这个块 <small>共 %s 次，列最近 12 次</small></h3>' % nfmt(len(users))
    for k in list(users)[-12:][::-1]:
        c = d["calls"][k]
        body += ('<a class="row" href="/s/%s/c/%d" style="grid-template-columns:64px 1fr 100px 80px">'
                 '<span class="idx">#%d</span><span class="grow">轮 %d · %s</span>'
                 '<span class="num">输入 %s</span><span class="num credit">%.2f 分</span></a>' %
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
    # 搜索框选「按块匹配」时服务端直接转交 v_find：form action 固定也能分流，零 JS。
    if qstr(q, "mode", "text") == "blocks":
        return v_find(q, sid)
    term = qstr(q, "q")
    scope = qstr(q, "scope", "all")
    src = "" if sid else qstr(q, "src")
    d = STORE.get(sid) if sid else None
    t0 = time.time()
    with CALC_LOCK:
        hits, warn = search(term, sid=sid, scope=scope, src=src)
    ms = int((time.time() - t0) * 1000)
    action = "/s/%s/search" % sid if sid else "/search"
    crumbs = [("索引", "/")] + ([(d["title"][:24], "/s/%s" % sid)] if d else []) + [("搜索", "")]
    hidden = ('<input type="hidden" name="src" value="%s">' % esc(src)) if src else ""
    body = search_box(action, term, "text", scope, "全文关键词 · 可切换「按块匹配」",
                      extra=hidden)
    body += ('<div class="fine" style="margin:-8px 0 14px">切到「按块匹配」会按块聚合，'
             '给出块内命中总数并直跳命中分片。</div>')
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
    return page("全文搜索", crumbs, body,
                sub="服务端逐行扫描全库原文，不建索引、不落盘",
                topbar="" if sid else plat_switch(STORE.sessions(), src, "/search"))


def v_find(q, sid=None):
    """按块聚合的精确检索：列出所有命中块，并直接跳到命中所在分片。"""
    term = qstr(q, "q")
    p = qint(q, "p")
    d = STORE.get(sid) if sid else None
    with CALC_LOCK:
        rows, warn, nsessions, ms = find_blocks(
            term, sid=sid, refresh=qstr(q, "refresh") == "1")
    total = len(rows)
    p = clamp_p(q, total, FIND_MAX_BLOCKS)
    lo = p * FIND_MAX_BLOCKS
    shown = rows[lo:lo + FIND_MAX_BLOCKS]
    # 表单统一提交到 /search 由 mode 分流；分页仍走 /find，避免每次翻页重走分流。
    action = "/s/%s/search" % sid if sid else "/search"
    base = "/s/%s/find" % sid if sid else "/find"
    crumbs = [("索引", "/")] + ([(d["title"][:24], "/s/%s" % sid)] if d else []) + [("块检索", "")]
    body = search_box(action, term, "blocks", placeholder="关键词：列出所有命中的上下文块")
    body += ('<div class="note">按<b>块</b>聚合：一个块只列一行，给出块内命中总数与它们落在'
             '哪些 %s 字分片。点片号直接跳到该片并高亮，超长块不用手翻几十页。'
             '与<a href="%s">全文搜索</a>共用同一份原文，不建索引、不落盘。</div>' %
             (nfmt(PART_CHARS), esc("/s/%s/search" % sid if sid else "/search")))
    body += ('<div class="fine">结果按关键词缓存 %d 秒，翻页与回看不重扫；源文件有新增会话时'
             '点<a href="%s">强制重检</a>。首次检索含解析会话全文，约几秒。</div>' %
             (FIND_CACHE_TTL,
              esc(base + ("?" + urlencode({"q": term, "refresh": "1"}) if term else ""))))
    if warn:
        body += '<div class="warn">%s</div>' % esc(warn)
    if term:
        body += '<h2>命中 %s 个块 <small>共 %s 处 · 扫描 %d 个会话 · %d ms</small></h2>' % (
            nfmt(total), nfmt(sum(r["count"] for r in rows)), nsessions, ms)
        max_chips = 8
        for r in shown:
            furl = "/s/%s/b/%d?" % (r["sid"], r["idx"])
            chips = "".join(
                '<a class="btn" href="%s">片 %d</a>' %
                (esc(furl + urlencode({"part": pt, "q": term})), pt + 1)
                for pt in r["parts"][:max_chips])
            if r["parts_total"] > max_chips:
                chips += '<a class="btn" href="%s">全部 %d 片 →</a>' % (
                    esc(r["href"]), r["parts_total"])
            sn, _ = hl_text(r["snippet"], term, max_hits=20)
            body += ('<div class="row" style="display:block;padding:9px 12px">'
                     '%s '
                     '<a href="%s"><b style="font-weight:500">块 #%d · %s</b></a> '
                     '<span class="fine">%s 字 · 命中 %s 处 · 首处 %s · %s</span>'
                     '<div class="tabs" style="margin-top:6px">%s</div>'
                     '<div class="snip">…%s…</div></div>' %
                     (type_chip(r["btype"]), esc(r["href"]),
                      r["idx"], esc(r["label"]), nfmt(r["chars"]), nfmt(r["count"]),
                      nfmt(r["pos"]), esc(r["ws"]), chips, sn))
        body += pager(base, q, p, FIND_MAX_BLOCKS, total, "个块")
    else:
        body += '<div class="note">输入至少 2 个字开始检索。</div>'
    return page("块检索", crumbs, body, sub="按块聚合 · 命中分片直跳 · 零脚本")


def v_tools(q, sid=None):
    """工具与命令聚合：按工具看 token / 积分，按 shell 程序看执行频次。"""
    raw_q = qstr(q, "q")
    term = raw_q.strip().lower()
    tool = qstr(q, "tool").strip()
    view = qstr(q, "view", "tools")
    if view not in ("tools", "progs"):
        view = "tools"
    p = qint(q, "p")
    src = "" if sid else qstr(q, "src")
    st = tool_stats_cached(sid=sid, refresh=qstr(q, "refresh") == "1", src=src)
    base = "/s/%s/tools" % sid if sid else "/tools"
    d = STORE.get(sid) if sid else None
    crumbs = [("索引", "/")] + ([(d["title"][:24], "/s/%s" % sid)] if d else []) + [("工具与命令", "")]
    body = ('<form class="searchbox" method="get" action="%s">'
            '<input type="text" name="q" value="%s" placeholder="筛选工具名 / 程序名">'
            '<button type="submit">筛选</button></form>' % (base, esc(raw_q)))
    body += kpi([("工具种类", nfmt(len(st["tools"]))),
                 ("总调用", nfmt(st["total_calls"])),
                 ("Shell 执行", nfmt(st["shell_runs"])),
                 ("程序种类", nfmt(len(st["progs"]))),
                 ("累计积分", "%.2f" % st["total_credit"]),
                 ("范围", "%d 个会话" % st["sessions"])])
    body += ('<div class="note">工具统计来自每次调用的 usage 与积分；程序名只从 '
             '<b>Bash / Shell 类工具</b>的命令摘要里 best-effort 提取：跳过 '
             '<code>cd / export / 控制流</code> 前缀段与装饰性 <code>echo</code>，'
             '取第一个实际程序。URL、搜索词、生图描述等不参与；'
             '纯控制流命令归入「(shell 控制流)」。点工具名可下钻到它的全部调用。</div>')
    if tool:
        all_rows, total = tool_calls(sid, tool, 0, 10 ** 9)
        p = clamp_p(q, total, PAGE_CALLS)
        rows = all_rows[p * PAGE_CALLS:(p + 1) * PAGE_CALLS]
        body += '<h2>工具 %s 的调用 <small>共 %s 次 · 按积分降序</small></h2>' % (
            esc(tool), nfmt(total))
        for r in rows:
            body += ('<a class="row" href="%s" style="grid-template-columns:74px 1fr 70px 92px 88px 82px">'
                     '<span class="idx">#%d</span><span class="grow">%s</span>'
                     '<span class="num">轮 %d</span><span class="num">输入 %s</span>'
                     '<span class="num credit">%.2f 分</span><span class="num">%s</span></a>' %
                     (esc(r["href"]), r["seq"], esc(r["ws"]), r["turn"], nfmt(r["pin"]),
                      r["credit"],
                      fmt_ts(r["ts"], "%m-%d %H:%M", "")))
        body += pager(base, q, p, PAGE_CALLS, total, "次调用")
    else:
        body += '<div class="tabs">' + "".join(
            '<a class="btn %s" href="%s">%s</a>' %
            ("on" if view == v else "",
             esc(base + "?" + urlencode({"view": v, "q": raw_q})), lab)
            for v, lab in (("tools", "按工具"), ("progs", "按程序"))) + '</div>'
        if view == "tools":
            rows = [x for x in st["tools"] if term in x["tool"].lower()]
            p = clamp_p(q, len(rows), PAGE_CALLS)
            mx = max((x["calls"] for x in st["tools"]), default=1) or 1
            body += '<h2>工具调用 <small>共 %s 种</small></h2>' % nfmt(len(rows))
            for x in rows[p * PAGE_CALLS:(p + 1) * PAGE_CALLS]:
                body += ('<div class="row" style="grid-template-columns:142px 1fr 76px 84px 88px 68px 82px">'
                         '<span class="grow"><a href="%s">%s</a></span>'
                         '<span class="bar"><i class="h" style="width:%.1f%%"></i></span>'
                         '<span class="num">%s 次</span><span class="num">命中 %.1f%%</span>'
                         '<span class="num credit">%.2f 分</span>'
                         '<span class="num">%d 会话</span><span class="num">%s</span></div>' %
                         (esc(base + "?" + urlencode({"tool": x["tool"]})), esc(x["tool"]),
                          x["calls"] / mx * 100, nfmt(x["calls"]), x["rate"] * 100,
                          x["credit"], x["session_count"], esc(x["last"])))
        else:
            rows = [x for x in st["progs"] if term in x["prog"].lower()]
            p = clamp_p(q, len(rows), PAGE_CALLS)
            mx = max((x["runs"] for x in st["progs"]), default=1) or 1
            body += '<h2>Shell 程序 <small>共 %s 种</small></h2>' % nfmt(len(rows))
            for x in rows[p * PAGE_CALLS:(p + 1) * PAGE_CALLS]:
                body += ('<div class="row" style="grid-template-columns:180px 1fr 88px 110px">'
                         '<span class="grow">%s</span>'
                         '<span class="bar"><i class="h" style="width:%.1f%%"></i></span>'
                         '<span class="num">%s 次</span>'
                         '<span class="num">结果 %s 字</span></div>' %
                         (esc(x["prog"]), x["runs"] / mx * 100,
                          nfmt(x["runs"]), nfmt(x["chars"])))
        body += pager(base, q, p, PAGE_CALLS, len(rows), "种")
    return page("工具与命令", crumbs, body,
                sub="工具 usage 聚合 · Shell 程序 best-effort 提取",
                topbar="" if sid else plat_switch(STORE.sessions(), src, "/tools"))


def v_map(q, sid):
    """上下文块地图：按需页面，块排序只在这一屏发生，不影响其它路由的体积与耗时。"""
    d = STORE.get(sid)
    st = d["stats"]
    kind = qstr(q, "k")
    if kind not in ORDER:
        kind = ""
    limit = qint(q, "n", 24, 4, 48)
    base = "/s/%s/map" % sid
    crumbs = [("索引", "/"), (d["title"][:24], "/s/%s" % sid), ("上下文块地图", "")]
    biggest = max((b["n"] for b in d["ctx"]), default=0)
    tabs = '<div class="tabs"><a class="btn %s" href="%s">全部类型</a>' % (
        "on" if not kind else "", esc(base))
    for k in ORDER:
        tabs += '<a class="btn %s" href="%s">%s</a>' % (
            "on" if kind == k else "", esc(base + "?" + urlencode({"k": k})), esc(TYPES[k]))
    tabs += '</div>'
    body = kpi([("上下文块", nfmt(st["blocks"])),
                ("归档原文", nfmt(st["chars"]) + " 字"),
                ("最大单块", nfmt(biggest) + " 字"),
                ("本页铺砖", "%d 块" % limit)])
    body += tabs
    body += ('<div class="viz"><div class="viz-hd"><b>上下文块地图</b>'
             '<small>面积 ∝ 原文字符数 · 点块看不截断全文</small></div>' +
             block_treemap(d, sid, limit, kind or None) + '</div>')
    body += ('<div class="note"><b>口径与边界</b>：面积是块的原文字符数，不是官方 token 归因；'
             '本图按「归档全文」口径统计，<b>不等于某次请求真正带上的上下文</b>。'
             '要看单次调用的可见块与缓存边界，去<a href="/s/%s/trend">上下文趋势</a>点柱，'
             '或直接打开该次调用的块清单。WorkBuddy 会话记录不含逐消息 token 拆分、'
             '工具 schema 体积与上下文替换（surfaceOp）事件，因此无法像 dsh 那样'
             '重建任意历史时刻的上下文快照；这里只做可得数据范围内的近似，缺失项一律不补造。</div>'
             % esc(sid))
    body += ('<div class="tabs"><a class="btn" href="/s/%s">← 会话概览</a>'
             '<a class="btn" href="/s/%s/trend">上下文趋势</a></div>' % (esc(sid), esc(sid)))
    return page("上下文块地图", crumbs, body,
                sub="按需渲染 · 零脚本钻取 · 字数为原文统计")


def v_trend(q, sid):
    """逐调用上下文演化：纯 CSS 色带柱 + SVG 折线 + 数值表，零 JavaScript。"""
    d = STORE.get(sid)
    g = qstr(q, "g", "call")
    if g not in ("call", "turn"):
        g = "call"
    mode = qstr(q, "mode", "total")
    if mode not in ("total", "delta"):
        mode = "total"
    p = qint(q, "p")
    rows, mx, mxd = trend_rows(d, g)
    p = clamp_p(q, len(rows), PAGE_CALLS)
    shown = rows[p * PAGE_CALLS:(p + 1) * PAGE_CALLS]
    base = "/s/%s/trend" % sid
    crumbs = [("索引", "/"), (d["title"][:24], "/s/%s" % sid), ("上下文趋势", "")]

    def switch(key, val, lab):
        cur = {"g": g, "mode": mode}
        cur[key] = val
        return '<a class="btn %s" href="%s">%s</a>' % (
            "on" if (g if key == "g" else mode) == val else "",
            esc(base + "?" + urlencode(cur)), lab)

    body = ('<div class="tabs">%s%s<span class="fine" style="align-self:center;margin-left:8px">模式</span>%s%s</div>' %
            (switch("g", "call", "按调用"), switch("g", "turn", "按轮"),
             switch("mode", "total", "累计组成"), switch("mode", "delta", "净变化")))
    body += kpi([("采样点", nfmt(len(rows))),
                 ("最大可见字", nfmt(mx)),
                 ("最大单步变化", nfmt(mxd)),
                 ("冷启动断点", nfmt(len(d["cold"])))])
    body += ('<div class="note">每根柱是一次%s的<b>可见上下文</b>（≈ 字数为原文统计，'
             '不含系统提示与工具 schema，非官方 tokenizer）。'
             '累计模式把 %d 类内容压成<b>一条色带</b>，净变化模式墨青为增长、红色为回落；'
             '高度按本页最大值归一化（翻页时刻度随本页数据重标定，跨页比高低请看左侧刻度），'
             '左侧为刻度、背景横线为 25%% 网格。'
             '细折线是缓存命中率（顶 = 100%%）。悬停看数值，点柱或表格行直达该次调用的完整块清单。'
             '顶部 ● 标记命中率骤降的冷启动调用，✂ 标记上下文压缩：'
             '那一柱可见字数会断崖下降，是历史被摘要替换，不是缓存失效。</div>' %
             ("模型调用" if g == "call" else "轮（取该轮最后一次调用）", len(ORDER)))
    body += '<div class="legend">' + "".join(
        '<span><b style="background:%s"></b>%s</span>' % (PALETTE[k], esc(TYPES[k]))
        for k in ORDER) + '<span><b style="background:var(--fg);opacity:.45"></b>命中率折线</span></div>'
    # 一根柱 = 一个 <i> + 一条 linear-gradient；网格线交给 .trend 的背景，刻度只输出文字。
    # 纵轴按「本页」重标定：全局峰值常出现在很后面的调用，若用它归一化，
    # 前几页的柱子会被压成底部一条边，看不出构成。KPI 仍保留全局峰值。
    if mode == "total":
        scale = max((r["chars"] for r in shown), default=1) or 1
    else:
        scale = max((abs(r["delta"]["chars"]) for r in shown), default=1) or 1
    ticks = "".join('<span>%s</span>' % kh(scale * f) for f in (1, .75, .5, .25, 0))
    body += '<div class="chart"><div class="chart-y">%s</div><div class="trend">' % ticks
    for r in shown:
        if mode == "total":
            h = r["chars"] / scale * 100
            extra = gradient_segments(r)
            tip = "#%d · %s 字 · 命中 %.1f%%" % (
                r["seq"], nfmt(r["chars"]), r["rate"] * 100)
        else:
            dv = r["delta"]["chars"]
            h = abs(dv) / scale * 100 if dv else 0
            extra = "background:%s" % ("var(--accent)" if dv >= 0 else "var(--neg2)")
            tip = "#%d · Δ %s%s 字" % (
                r["seq"], "+" if dv >= 0 else "-", nfmt(abs(dv)))
        segs = '<i style="height:%.3f%%;%s"></i>' % (max(h, .6), extra)
        mark = ""
        if r.get("compact"):
            mark = '<span class="mk c">✂</span>'
            tip += " · 此处发生上下文压缩"
        elif r["cold"]:
            mark = '<span class="mk">●</span>'
        body += '<a href="%s" title="%s">%s%s</a>' % (esc(r["href"]), esc(tip), segs, mark)
    body += trend_overlay(shown) + '</div></div>'
    body += ('<div class="trow hd"><span>#调用</span><span>轮</span><span>块数</span>'
             '<span class="num">可见字</span><span class="num">Δ 字</span>'
             '<span class="num">命中</span><span class="num">积分</span>'
             '<span class="num">Δ 积分</span></div>')
    for r in shown:
        dv, dc = r["delta"]["chars"], r["delta"]["credit"]
        body += ('<a class="trow" href="%s" style="text-decoration:none;color:inherit">'
                 '<span>#%d%s</span><span>%d</span><span>%s</span>'
                 '<span class="num">%s</span>'
                 '<span class="num" style="color:%s">%s%s</span>'
                 '<span class="num">%.1f%%</span><span class="num">%.2f</span>'
                 '<span class="num">%s%.2f</span></a>' %
                 (esc(r["href"]), r["seq"],
                  " ◆" if r.get("compact") else (" ●" if r["cold"] else ""), r["turn"],
                  nfmt(r["blocks"]), nfmt(r["chars"]),
                  "var(--accent)" if dv >= 0 else "var(--neg)",
                  "+" if dv >= 0 else "-", nfmt(abs(dv)),
                  r["rate"] * 100, r["credit"], "+" if dc >= 0 else "", dc))
    body += pager(base, q, p, PAGE_CALLS, len(rows),
                  "次调用" if g == "call" else "轮")
    return page("上下文趋势", crumbs, body, sub="逐调用演化 · 累计组成 / 净变化 · 零脚本")


def v_compact(q, sid):
    """压缩事件校验页：把真实账单与修正前/后统计并列，供人工核对。"""
    d = STORE.get(sid)
    st = d["stats"]
    rows = compact_rows(d)
    crumbs = [("索引", "/"), (d["title"][:30], "/s/%s" % sid), ("压缩事件", "")]
    body = kpi([("压缩次数", nfmt(len(rows))),
                ("归档原文", nfmt(st["chars"]) + " 字"),
                ("可见峰值", nfmt(st["visible_peak"]) + " 字"),
                ("末次可见", nfmt(st["visible_last"]) + " 字"),
                ("已退役历史", nfmt(st["retired_chars"]) + " 字"),
                ("真实累计输入", nfmt(st["pin"]) + " token"),
                ("真实累计积分", "%.2f" % st["credit"])])
    body += ('<div class="note"><b>怎么核对</b>：WorkBuddy 压缩会把历史替换成一条摘要，'
             '真实 <code>rawUsage.prompt_tokens</code> 应骤降。'
             '<b>修正前</b>我们把压缩前的旧块继续累加，可见字数反而上涨；'
             '<b>修正后</b>按 <code>[vf, ctxN)</code> 只算摘要之后的内容，'
             '应与真实输入<b>同方向下降</b>。'
             '「真实账单」三列是接口原值，未经任何估算；'
             '点「调用」可看该次请求实际带上的块清单。</div>')
    if not rows:
        body += '<p class="none">这个会话没有发生过上下文压缩，所有可见区间都是 [0, ctxN)。</p>'
        return page("压缩事件", crumbs, body, sub="%s · 未发生压缩" % d["title"][:30])
    bad = [r["n"] for r in rows if r.get("consistent") is False]
    body += ('<div class="%s"><b>方向自检：%d / %d 个压缩点一致</b>%s。'
             '一致 = 修正后可见字数与真实输入 token 同号变化（单位不同，只比方向，不比幅度）。</div>' %
             ("note" if not bad else "warn", len(rows) - len(bad), len(rows),
              "" if not bad else "；不一致的第 %s 个事件需人工复核" % ",".join(map(str, bad))))

    for r in rows:
        t = fmt_ts(r["ts"])
        body += ('<h2>压缩 #%d <small>%s · %s · 摘要块 #%d / %s 字</small></h2>' %
                 (r["n"], esc(t), esc(r["kind"]), r["at_block"], nfmt(r["summary_chars"])))
        if r.get("superseded"):
            body += ('<div class="warn">紧随其后又有一次压缩，这份摘要立刻被覆盖，'
                     '中间没有发生任何模型调用，因此下面两处的对照数字相同 —— '
                     '是真实的连续压缩，不是重复计算。</div>')
        body += ('<div class="tabs"><a class="btn" href="%s">看摘要块全文</a>' %
                 esc(r["block_href"]) +
                 (('<a class="btn" href="/s/%s/c/%d">压缩前调用 #%d</a>' %
                   (esc(sid), r["before_seq"], r["before_seq"])) if r["before_seq"] else "") +
                 (('<a class="btn" href="/s/%s/c/%d">压缩后调用 #%d</a>' %
                   (esc(sid), r["after_seq"], r["after_seq"])) if r["after_seq"] else "") +
                 '<a class="btn" href="/s/%s/trend">看趋势断崖</a></div>' % esc(sid))
        body += ('<div class="trow hd" style="grid-template-columns:150px 1fr 1fr 1fr">'
                 '<span>指标</span><span>压缩前</span><span>压缩后</span><span>变化</span></div>')
        def line(label, bv, av, pct, good):
            return ('<div class="trow" style="grid-template-columns:150px 1fr 1fr 1fr">'
                    '<span>%s</span><span class="num">%s</span><span class="num">%s</span>'
                    '<span class="num" style="color:%s">%s</span></div>' %
                    (label, bv, av, "var(--accent)" if good else "var(--neg)", pct))
        if r["before"] and r["after"]:
            b, a = r["before"], r["after"]
            body += line("真实输入 token（接口原值）", nfmt(b["pin"]), nfmt(a["pin"]),
                         "%+.0f%%" % r["pin_delta_pct"], True)
            body += line("真实缓存命中率", "%.1f%%" % (b["rate"] * 100),
                         "%.1f%%" % (a["rate"] * 100),
                         "%+.0f pp" % ((a["rate"] - b["rate"]) * 100), True)
            body += line("真实积分 credit", "%.2f" % b["credit"], "%.2f" % a["credit"],
                         "%+.2f" % (a["credit"] - b["credit"]), True)
            body += line("修正前 可见字数（旧口径）", nfmt(b["chars_old"]), nfmt(a["chars_old"]),
                         "%+.0f%%" % r["chars_old_delta_pct"], False)
            body += line("修正后 可见字数 [vf,ctxN)", nfmt(b["chars_new"]), nfmt(a["chars_new"]),
                         "%+.0f%%" % r["chars_new_delta_pct"], True)
        else:
            body += '<p class="none">压缩点附近没有带 usage 的调用，无法比对。</p>'
    body += ('<h2>说明与边界</h2><div class="note">'
             '压缩是 WorkBuddy 客户端行为，本工具只读 jsonl，'
             '通过 <code>providerData.isCompacted</code> 标记识别，不猜文本。'
             '旧块<b>没有删除</b>，仍可全文检索与查看，只是不再计入后续调用。'
             '可见字数是字符数、真实输入是 token，两者不可换算，'
             '本页只校验变化方向。缓存边界（命中/未命中切分）仍是估算：'
             '接口只给命中总数，不给切分位置。</div>')
    return page("压缩事件", crumbs, body,
                sub="%s · %d 次压缩 · 真实账单与统计口径对照" % (d["title"][:30], len(rows)))


def v_stats(q):
    t0 = time.time()
    src = qstr(q, "src")
    ss_all = STORE.sessions()
    ss = ss_all if not src else [s for s in ss_all
                                 if (s.get("src") or "workbuddy") == src]
    with CALC_LOCK:
        st = global_stats(ss)
        compact_sid = next((s["id"] for s in ss
                            if STORE.get(s["id"])["compacts"]), "")
    ms = int((time.time() - t0) * 1000)
    body = kpi([("会话", nfmt(st["sessions"])), ("工作区", nfmt(st["workspaces"])),
                ("模型调用", nfmt(st["calls"])), ("对话轮次", nfmt(st["turns"])),
                ("上下文块", nfmt(st["blocks"])), ("归档原文", nfmt(st["chars"]) + " 字"),
                ("可见峰值合计", nfmt(st["visible_peak_sum"]) + " 字"),
                ("压缩次数", nfmt(st["compactions"])),
                ("累计输入 token", nfmt(st["pin"])), ("缓存命中率", "%.1f%%" % (st["rate"] * 100)),
                ("未命中 token", nfmt(st["miss"])), ("输出 token", nfmt(st["pout"])),
                ("累计积分", "%.2f" % st["credit"]),
                ("积分/万未命中", "%.3f" % st["unit_miss"]),
                ("按刊例估算", "¥%.2f" % st["cost"]),
                ("估算/万未命中", "¥%.3f" % st["unit_cost_miss"])])
    # 两张卡片内容都偏窄，各占整行时右边大片留白；并排成左右两栏（窄屏回落单列）。
    body += ('<div class="viz2">' + compose_bar(st["compose"]) +
             activity_heatmap(ss) + '</div>')
    body += ('<div class="note">上面每个数字都是<b>全量</b>算出来的（%d ms，遍历 %d 个会话全文），'
             '不是抽样、不是截断后的静态页统计。<b>归档原文</b>是全部块的总字数，'
             '包含已被压缩替换、但保留下来供全文检索的历史；'
             '<b>可见峰值合计</b>是各会话单次请求真实带上量的峰值之和。'
             '发生压缩的会话，后者会明显小于前者%s。</div>' %
             (ms, st["sessions"],
              ('，<a href="/s/%s/compact">可在压缩事件页逐条核对</a>' % esc(compact_sid))
              if compact_sid else ''))
    body += '<h2>最贵的 20 次调用 <small>按刊例价估算 · 非账单</small></h2>'
    for x in st["top_calls"]:
        body += ('<a class="row bad" href="/s/%s/c/%d" style="grid-template-columns:64px 1fr 104px 84px 84px">'
                 '<span class="idx">#%d</span><span class="grow">%s · %s</span>'
                 '<span class="num">输入 %s</span><span class="num">命中 %.1f%%</span>'
                 '<span class="num credit">¥%.3f</span></a>' %
                 (esc(x["sid"]), x["seq"], x["seq"], esc(x["ws"]), esc(x["tool"] or "最终回答"),
                  nfmt(x["pin"]), x["rate"] * 100, x["cost"]))
    body += '<h2>最大的 20 个上下文块 <small>点进去分片看全文</small></h2>'
    for x in st["top_blocks"]:
        body += ('<a class="row" href="/s/%s/b/%d" style="grid-template-columns:64px 1fr 110px 76px">'
                 '<span class="idx">#%d</span><span class="grow">%s · %s</span>'
                 '<span class="num">%s 字</span><span class="num" style="color:var(--link-h)">全文 →</span></a>' %
                 (esc(x["sid"]), x["idx"], x["idx"], TYPES.get(x["kind"], x["kind"]),
                  esc(x["label"]), nfmt(x["chars"])))
    body += ('<h2>成本最高的 20 个块 <small>≈ 估算 token × 被携带次数 · '
             '不是真实账单，只衡量重复携带量</small></h2>')
    for x in st["costly_blocks"]:
        body += ('<a class="row bad" href="/s/%s/b/%d" '
                 'style="grid-template-columns:64px 1fr 106px 82px 92px">'
                 '<span class="idx">#%d</span><span class="grow">%s · %s</span>'
                 '<span class="num">≈ %s token</span><span class="num">× %s 次</span>'
                 '<span class="num credit">≈ %s</span></a>' %
                 (esc(x["sid"]), x["idx"], x["idx"], esc(TYPES.get(x["kind"], x["kind"])),
                  esc(x["label"]), nfmt(x["est"]), nfmt(x["users"]), kh(x["cost"])))
    body += '<h2>缓存冷启动断点 <small>共 %d 次；按当次输入量排序，列前 30</small></h2>' % len(st["cold"])
    for x in st["cold"][:30]:
        ts = fmt_ts(x["ts"], "%m-%d %H:%M", "?")
        body += ('<a class="row bad" href="/s/%s/c/%d" style="grid-template-columns:64px 1fr 104px 84px 84px 66px">'
                 '<span class="idx">#%d</span><span class="grow">%s · %s</span>'
                 '<span class="num">输入 %s</span><span class="num">命中 %.1f%%</span>'
                 '<span class="num credit">%.2f 分</span><span class="num">%s</span></a>' %
                 (esc(x["sid"]), x["seq"], x["seq"], esc(x["ws"]), esc(x["sid"][:8]),
                  nfmt(x["pin"]), x["rate"] * 100, x["credit"], ts))
    body += ('<div class="tabs"><a class="btn" href="/api/stats">同一份数据的 JSON</a>'
             '<a class="btn" href="/">← 回索引</a></div>')
    return page("全局统计", [("索引", "/"), ("全局统计", "")], body,
                sub="跨会话全量聚合 · 每次访问现算，不缓存结论",
                topbar=plat_switch(ss_all, src, "/stats"))


# ------------------------------------------------ 逐块 / 逐调用排行（不做分组）
RANK_WHAT = ("block", "call")
RANK_SORT = {"block": ("token", "count", "product"),
             "call": ("token", "in", "out")}
_RANK_CACHE = {}


def rank_rows(what):
    """把每一个块 / 每一次调用本身作为一行，不做会话或日期聚合。

    块行 = (sid, 会话标题, 块号, 块对象, 估算 token, 被携带次数)；
    调用行 = (sid, 会话标题, 调用对象)。行内只放引用与已算好的整数，
    展示 HTML 只为当前页拼装——全库 1.5 万块在排序阶段不生成任何字符串。
    """
    ss = STORE.sessions()
    key = (what, id(ss), len(ss))
    hit = _RANK_CACHE.get(key)
    # 只把 id 存进 key 是不够的：旧索引列表一旦被回收，新列表可能分配到同一
    # 地址，刷新后就会误命中旧结果。缓存 value 持有 ss 本身可杜绝 id 复用，
    # `is ss` 再挡一层，失效判定不依赖分配器的偶然行为。
    if hit is not None and hit[0] is ss:
        return hit[1]
    rows = []
    if what == "block":
        for s in ss:
            d = STORE.get(s["id"])
            uses = block_use_counts(d)
            title = s["title"] or s["id"]
            for i, b in enumerate(d["ctx"]):
                est = b.get("e") or (b["n"] // 3)
                # 日期 / 归一键随行预计算：筛选时只做字符串比较，不再逐行调
                # day_key（15k 次 localtime）或 canon_model（8k 次正则）。
                rows.append((s["id"], title, i, b, est, uses[i],
                             G.day_key(b.get("ts"))))
    else:
        for s in ss:
            d = STORE.get(s["id"])
            title = s["title"] or s["id"]
            for c in d["calls"]:
                rows.append((s["id"], title, c, G.day_key(c.get("ts")),
                             G.canon_model(c.get("model"))))
    if len(_RANK_CACHE) > 8:
        _RANK_CACHE.clear()
    _RANK_CACHE[key] = (ss, rows)
    return rows


def rank_sort_key(what, sort):
    """降序靠取负；同值按块号 / 轮次稳定收尾，翻页时行序不会抖动。"""
    if what == "block":
        if sort == "count":
            return lambda r: (-r[5], -r[4], r[0], r[2])
        if sort == "product":
            return lambda r: (-(r[4] * r[5]), -r[4], r[0], r[2])
        return lambda r: (-r[4], -r[5], r[0], r[2])
    if sort == "in":
        return lambda r: (-r[2]["pin"], r[0], r[2]["seq"])
    if sort == "out":
        return lambda r: (-r[2]["pout"], r[0], r[2]["seq"])
    return lambda r: (-(r[2]["pin"] + r[2]["pout"]), r[0], r[2]["seq"])


def rank_metric(what, sort, r):
    """当前排序键对应的原始值，用于算占比。"""
    if what == "block":
        if sort == "count":
            return r[5]
        if sort == "product":
            return r[4] * r[5]
        return r[4]
    c = r[2]
    if sort == "in":
        return c["pin"]
    if sort == "out":
        return c["pout"]
    return c["pin"] + c["pout"]


def rank_filter(what, rows, kinds, dfrom, dto, exclude_compact=False):
    """按种类 / 时间筛选排行行，各维可单独或任意组合。

    kinds 支持单个字符串（兼容旧调用）或逗号分隔的多值 / 集合：块按块类型（k）
    筛选，调用按模型归一键筛选；多选是「任一类命中即保留」，不是交集。
    exclude_compact 为真时排除标题以「压缩摘要」开头的块——它们是上下文压缩
    产生的历史摘要，单块几十万字，会把按 token 排行的前几名全占掉。
    时间按对象时间戳的本地日期做闭区间比较，任一端为空表示该侧不限。
    日期与归一键已在 rank_rows() 里预计算，这里只做字符串比较。
    """
    if isinstance(kinds, str):
        kinds = set(x for x in kinds.split(",") if x)
    else:
        kinds = set(kinds or ())
    if kinds:
        if what == "block":
            rows = [r for r in rows if r[3]["k"] in kinds]
        else:
            rows = [r for r in rows if r[4] in kinds]
    if exclude_compact and what == "block":
        rows = [r for r in rows
                if not str(r[3].get("l") or "").startswith("压缩摘要")]
    if dfrom or dto:
        di = 6 if what == "block" else 3
        rows = [r for r in rows
                if r[di] and (not dfrom or r[di] >= dfrom)
                and (not dto or r[di] <= dto)]
    return rows


_RANK_SORTED = {}


def rank_sorted(what, sort):
    """按 (对象, 排序键) 缓存已排序的行，筛选只是在有序列表上过滤、不必重排。

    15 879 块按乘积排序一次约 98 ms；缓存后筛选请求只剩一次 O(n) 过滤。
    与 rank_rows() 一样用索引对象身份做失效判定。
    """
    ss = STORE.sessions()
    key = (what, sort)
    hit = _RANK_SORTED.get(key)
    if hit is not None and hit[0] is ss:
        return hit[1]
    rows = sorted(rank_rows(what), key=rank_sort_key(what, sort))
    if len(_RANK_SORTED) > 8:
        _RANK_SORTED.clear()
    _RANK_SORTED[key] = (ss, rows)
    return rows


def v_rank(q):
    what = qstr(q, "what", "block")
    if what not in RANK_WHAT:
        what = "block"
    sorts = RANK_SORT[what]
    sort = qstr(q, "sort", sorts[0])
    if sort not in sorts:
        sort = sorts[0]          # 白名单：非法值一律回落，不拼进 HTML / 排序键

    # ---- 筛选：种类多选（块类型 / 模型归一键）+ 时间闭区间 + 压缩摘要开关 ----
    # k 可以是单个值，也可以是逗号分隔的多个值；单值 URL 与旧版完全兼容。
    valid_kinds = (list(ORDER) if what == "block"
                   else sorted(model_display_map()))
    picked = set(x for x in qstr(q, "k", "").split(",") if x in valid_kinds)
    kinds = [x for x in valid_kinds if x in picked]   # 按规范顺序输出，URL 稳定
    kind = ",".join(kinds)
    show_compact = qstr(q, "cx", "") == "1"

    def _norm_date(s):
        s = (s or "").strip()
        if not s:
            return ""
        try:
            return datetime.datetime.strptime(s, "%Y-%m-%d").strftime("%Y-%m-%d")
        except ValueError:
            return ""

    dfrom = _norm_date(qstr(q, "from", ""))
    dto = _norm_date(qstr(q, "to", ""))

    rows = rank_filter(what, rank_sorted(what, sort), kind, dfrom, dto,
                       exclude_compact=(what == "block" and not show_compact))
    size = PAGE_BLOCKS
    p = clamp_p(q, len(rows), size)
    shown = rows[p * size:(p + 1) * size]
    mx = max((rank_metric(what, sort, r) for r in rows), default=0) or 1

    filt = {}
    if kind:
        filt["k"] = kind
    if dfrom:
        filt["from"] = dfrom
    if dto:
        filt["to"] = dto
    if show_compact and what == "block":
        filt["cx"] = "1"

    def url(what_, sort_, extra=None):
        d = {"what": what_, "sort": sort_}
        if extra:
            d.update(extra)
        return esc("/rank?" + urlencode(d))

    def tab_obj(k2):
        # 切换对象时丢掉对象专属的种类，保留时间范围，排序回到新对象默认键
        d = {a: b for a, b in filt.items() if a != "k"}
        return url(k2, RANK_SORT[k2][0], d)

    def tab_sort(sk):
        return url(what, sk, filt)

    def tabs(opts, cur, href_fn):
        return "".join('<a class="btn %s" href="%s">%s</a>' %
                       ("on" if v == cur else "", href_fn(v), lab)
                       for v, lab in opts)

    if what == "block":
        kind_opts = [("", "全部种类")] + [(x, TYPES[x]) for x in ORDER]
        kind_label = "种类"
    else:
        kind_opts = ([("", "全部模型")] +
                     sorted(((a, b) for a, b in model_display_map().items()),
                            key=lambda x: x[1].lower()))
        kind_label = "模型"

    def kind_url(val):
        # 多选：点一次把该类加入集合，再点一次移出；「全部种类」清空集合。
        # 仍是纯链接，零脚本；时间范围、压缩摘要开关、排序都原样保留。
        d = {a: b for a, b in filt.items() if a != "k"}
        cur = set(kinds)
        if not val:
            cur.clear()
        elif val in cur:
            cur.discard(val)
        else:
            cur.add(val)
        ks = ",".join(x for x in valid_kinds if x in cur)
        if ks:
            d["k"] = ks
        return url(what, sort, d)

    if not kinds:
        cur_kind = kind_opts[0][1]
    elif len(kinds) == 1:
        cur_kind = next((lab for v, lab in kind_opts if v == kinds[0]), kinds[0])
    else:
        cur_kind = ("已选 %d 类" if what == "block" else "已选 %d 个模型") % len(kinds)
    kind_menu = "".join(
        '<a class="%s" href="%s">%s</a>' %
        ("on" if ((not v and not kinds) or (v and v in kinds)) else "",
         kind_url(v), esc(lab))
        for v, lab in kind_opts)
    # 原生 <select> 的弹层由浏览器绘制，黑色直角边框无法用 CSS 去掉；
    # 改成 details + 链接菜单：零脚本，圆角 / 阴影 / 选中态完全可控。
    pick_html = ('<details class="pick"><summary>%s</summary>'
                 '<div class="pickmenu">%s</div></details>' %
                 (esc(cur_kind), kind_menu))
    # 自绘日历：原生 <input type=date> 的日历弹层同样由浏览器绘制，那圈
    # 直角黑边在浏览器 UI 层，color-scheme 只能改配色、改不掉边框。改成
    # details + 链接日历，零脚本；每个日期都是带完整筛选条件的链接。
    today = datetime.date.today()
    cal_raw = qstr(q, "cal", "")
    cal_field = qstr(q, "calfield", "")
    if cal_field not in ("from", "to"):
        cal_field = ""
    try:
        cy, cm = (int(x) for x in cal_raw.split("-")[:2])
        datetime.date(cy, cm, 1)
    except (ValueError, TypeError):
        cy, cm = today.year, today.month
    first_wd = datetime.date(cy, cm, 1).weekday()          # 周一 = 0
    days_in = (datetime.date(cy + (cm == 12), cm % 12 + 1, 1)
               - datetime.timedelta(days=1)).day
    prev_last = (datetime.date(cy, cm, 1) - datetime.timedelta(days=1)).day
    prev_ym = "%04d-%02d" % (cy - (cm == 1), 12 if cm == 1 else cm - 1)
    next_ym = "%04d-%02d" % (cy + (cm == 12), 1 if cm == 12 else cm + 1)

    def date_href(field, val):
        d = dict(filt)
        if val:
            d[field] = val
        else:
            d.pop(field, None)
        return url(what, sort, d)

    def cal_href(ym, field):
        d = dict(filt)
        d["cal"] = ym
        d["calfield"] = field
        return url(what, sort, d)

    def date_pick(field, label, cur):
        cells = ['<span class="cald out">%d</span>' % (prev_last - first_wd + 1 + i)
                 for i in range(first_wd)]
        for day in range(1, days_in + 1):
            ds = "%04d-%02d-%02d" % (cy, cm, day)
            cls = "cald"
            if ds == cur:
                cls += " on"
            if ds == today.isoformat():
                cls += " today"
            cells.append('<a class="%s" href="%s">%d</a>' %
                         (cls, date_href(field, ds), day))
        tail = (7 - len(cells) % 7) % 7
        cells.extend('<span class="cald out">%d</span>' % i
                     for i in range(1, tail + 1))
        week = "".join('<span class="calw">%s</span>' % w for w in "一二三四五六日")
        # 切月份是链接跳转；带 cal/calfield 回来时让对应弹层保持展开，
        # 连点几个月不用反复打开。
        opened = " open" if (cal_field == field and cal_raw) else ""
        return ('<details class="datepick"%s><summary>%s</summary>'
                '<div class="calpop">'
                '<div class="calhd"><a href="%s" aria-label="上个月">‹</a>'
                '<b>%d 年 %d 月</b><a href="%s" aria-label="下个月">›</a></div>'
                '<div class="calgrid">%s%s</div>'
                '<div class="calft"><a href="%s">今天</a><a href="%s">清除</a></div>'
                '</div></details>' %
                (opened, esc(cur or label),
                 cal_href(prev_ym, field), cy, cm, cal_href(next_ym, field),
                 week, "".join(cells),
                 date_href(field, today.isoformat()), date_href(field, "")))

    # 压缩摘要开关：默认排除，避免几十万字的历史摘要霸占 token 排行前几名。
    # 同样是纯链接，cx=1 时把开关状态塞进 filt，翻页 / 排序 / 日历都不丢。
    cx_html, cx_hidden = "", ""
    if what == "block":
        d = dict(filt)
        if show_compact:
            d.pop("cx", None)
        else:
            d["cx"] = "1"
        cx_href = esc("/rank?" + urlencode(dict({"what": what, "sort": sort}, **d)))
        cx_html = ('<a class="btn %s" href="%s" '
                   'title="上下文压缩产生的历史摘要，单块可达几十万字">%s</a>' %
                   ("on" if show_compact else "", cx_href,
                    "隐藏压缩摘要" if show_compact else "显示压缩摘要"))
        if show_compact:
            cx_hidden = '<input type="hidden" name="cx" value="1">'

    filt_html = ('<form class="searchbox" method="get" action="/rank">'
                 '<input type="hidden" name="what" value="%s">'
                 '<input type="hidden" name="sort" value="%s">'
                 '<input type="hidden" name="k" value="%s">%s'
                 '<input type="hidden" name="from" value="%s">'
                 '<input type="hidden" name="to" value="%s">'
                 '<span class="sb-tools">'
                 '<b class="sb-lab">%s</b>%s'
                 '<b class="sb-lab">时间</b>'
                 '%s<span class="sb-sep">至</span>%s%s'
                 '</span>'
                 '<span class="sb-main"><button type="submit">筛选</button>'
                 '<a class="btn" href="%s">清空</a></span></form>' %
                 (esc(what), esc(sort), esc(kind), cx_hidden, esc(dfrom), esc(dto),
                  esc(kind_label), pick_html,
                  date_pick("from", "起始日期", dfrom),
                  date_pick("to", "结束日期", dto), cx_html,
                  esc("/rank?" + urlencode({"what": what, "sort": sort}))))
    n_label = (("筛选后块数" if filt else "块总数") if what == "block"
               else ("筛选后调用数" if filt else "调用总数"))
    pager_q = {"what": [what], "sort": [sort]}
    for a, b in filt.items():
        pager_q[a] = [b]

    # 筛选栏放最上面：自绘日历向下展开约 300px，若夹在 KPI / 说明 / 两行
    # tabs 之后，弹层会越过视口底部，末行与「今天 / 清除」被截掉。
    body = filt_html

    if what == "block":
        arch = sum(r[4] for r in rows)
        carried = sum(r[4] * r[5] for r in rows)
        uses = sum(r[5] for r in rows)
        body += kpi([(n_label, nfmt(len(rows))),
                    ("归档 token 合计", "≈ " + kh(arch)),
                    ("承载 token 合计", "≈ " + kh(carried)),
                    ("被携带合计", nfmt(uses)),
                    ("当前排序", {"token": "块 token", "count": "被携带次数",
                                 "product": "token × 次数"}[sort])])
        body += ('<div class="note">每一行就是一个<b>上下文块本身</b>，不是按会话 / '
                 '日期聚合出来的桶。<b>块 token</b> 是这块原文的估算量（<b>≈ 估算</b>，'
                 '非官方 tokenizer、非真实账单）；<b>被携带</b> 是它被后续请求带进上下文的'
                 '次数；<b>乘积</b> = 块 token × 被携带次数，也就是「成本最高的块」的口径。'
                 '「种类」可<b>多选组合</b>（在下拉里逐项点选 / 取消），「时间」按块'
                 '<b>写入</b>的本地日期闭区间筛选，两者可任意叠加。'
                 '默认已排除「压缩摘要」块（上下文压缩产生的历史摘要，单块几十万字，'
                 '会把按 token 排行的前几名全占掉），需要时点筛选栏的开关纳入。</div>')
        body += ('<div class="tabs"><b>对象</b>%s</div>' %
                 tabs((("block", "上下文块"), ("call", "调用")), what, tab_obj))
        body += ('<div class="tabs"><b>排序</b>%s</div>' %
                 tabs((("token", "按块 token"), ("count", "按被携带次数"),
                       ("product", "按乘积")), sort, tab_sort))
        if not rows:
            body += '<div class="note">没有匹配当前筛选条件的块，换个种类或时间范围试试。</div>'
        else:
            cols = "52px 1fr 150px 118px 84px 76px 92px 48px"
            body += ('<div class="row" style="grid-template-columns:%s">'
                     '<span class="idx">#</span><span class="grow">块</span>'
                     '<span>来源会话</span><span>写入时间</span>'
                     '<span class="num">块 token</span><span class="num">被携带</span>'
                     '<span class="num">乘积</span><span class="num">占比</span></div>' % cols)
            for n, r in enumerate(shown, p * size + 1):
                sid, title, i, b, est, u, _day = r
                body += ('<div class="row" style="grid-template-columns:%s">'
                         '<span class="idx">#%d</span>'
                         '<span class="grow"><a href="/s/%s/b/%d">'
                         '%s'
                         '<b style="font-weight:500">%s</b></a>%s</span>'
                         '<span><a href="/s/%s" title="%s">%s</a></span>'
                         '<span>%s</span>'
                         '<span class="num">≈ %s</span><span class="num">%s</span>'
                         '<span class="num credit">%s</span>'
                         '<span class="num">%.0f%%</span></div>' %
                         (cols, n, esc(sid), i, type_chip(b["k"]),
                          esc(b["l"]), G.cmd_html(b, 72),
                          esc(sid), esc(title), esc(title[:26]),
                          esc(fmt_ts(b.get("ts"), "%Y-%m-%d %H:%M", "时间未知")),
                          kh(est), nfmt(u), kh(est * u),
                          rank_metric(what, sort, r) / mx * 100))
            body += pager("/rank", pager_q, p, size, len(rows), "块")
        sub = ("逐块排行 · 每行一个上下文块 · 支持种类 / 时间筛选 · "
               "按 token / 被携带次数 / 乘积排序 · 零脚本")
    else:
        total = sum(r[2]["pin"] + r[2]["pout"] for r in rows)
        tin = sum(r[2]["pin"] for r in rows)
        tout = sum(r[2]["pout"] for r in rows)
        body += kpi([(n_label, nfmt(len(rows))),
                    ("token 合计", kh(total)),
                    ("输入 / 输出", "%s / %s" % (kh(tin), kh(tout))),
                    ("当前排序", {"token": "合计 token", "in": "输入 token",
                                 "out": "输出 token"}[sort])])
        body += ('<div class="note">每一行就是<b>一次调用本身</b>。token 取自接口 '
                 '<code>rawUsage</code> 的真实用量，不做估算；<b>输入 + 输出 = 合计</b>。'
                 '一次调用只有一个「次数」，所以没有次数 / 乘积排序，改为在合计 / 输入 / '
                 '输出三个 token 口径间切换。「模型」与「时间」可任意组合，时间按调用'
                 '<b>发生</b>的本地日期闭区间筛选。'
                 '</div>')
        body += ('<div class="tabs"><b>对象</b>%s</div>' %
                 tabs((("block", "上下文块"), ("call", "调用")), what, tab_obj))
        body += ('<div class="tabs"><b>排序</b>%s</div>' %
                 tabs((("token", "按合计"), ("in", "按输入"),
                       ("out", "按输出")), sort, tab_sort))
        if not rows:
            body += '<div class="note">没有匹配当前筛选条件的调用，换个模型或时间范围试试。</div>'
        else:
            cols = "52px 1fr 150px 128px 82px 82px 92px 48px"
            body += ('<div class="row" style="grid-template-columns:%s">'
                     '<span class="idx">#</span><span class="grow">会话 · 轮次</span>'
                     '<span>模型</span><span>时间</span>'
                     '<span class="num">输入</span><span class="num">输出</span>'
                     '<span class="num">合计</span><span class="num">占比</span></div>' % cols)
            for n, r in enumerate(shown, p * size + 1):
                sid, title, c, _day, _canon = r
                body += ('<div class="row" style="grid-template-columns:%s">'
                         '<span class="idx">#%d</span>'
                         '<span class="grow"><a href="/s/%s" title="%s">%s</a> '
                         '<a href="/s/%s/c/%d">#%d</a></span>'
                         '<span>%s</span><span>%s</span>'
                         '<span class="num">%s</span><span class="num">%s</span>'
                         '<span class="num credit">%s</span>'
                         '<span class="num">%.0f%%</span></div>' %
                         (cols, n, esc(sid), esc(title), esc(title[:34]),
                          esc(sid), c["seq"], c["seq"],
                          esc(c.get("model") or "—"),
                          esc(fmt_ts(c.get("ts"), "%Y-%m-%d %H:%M", "时间未知")),
                          nfmt(c["pin"]), nfmt(c["pout"]), nfmt(c["pin"] + c["pout"]),
                          rank_metric(what, sort, r) / mx * 100))
            body += pager("/rank", pager_q, p, size, len(rows), "次")
        sub = ("逐调用排行 · 每行一次调用 · 支持模型 / 时间筛选 · "
               "按合计 / 输入 / 输出 token 排序 · 零脚本")

    body += ('<div class="tabs"><a class="btn" href="/stats">← 全局统计</a>'
             '<a class="btn" href="/">← 回索引</a></div>')
    return page("筛选与排序", [("索引", "/"), ("筛选与排序", "")], body, sub=sub)


def v_api_doc(q):
    api_rows = [(r["route"], r["doc"]) for r in ROUTE_SPEC if r["kind"] == "api"]
    body = ('<div class="note">所有接口只读、返回 JSON，统一信封 '
            '<code>{"ok":true,"data":...,"meta":{...}}</code>；出错为 '
            '<code>{"ok":false,"error":"..."}</code> 加对应 HTTP 状态码。'
            '分页参数统一 <code>offset/limit</code>，<code>meta</code> 里给 <code>total</code> '
            '和下一页的 <code>next</code>。跨域默认关闭，只监听本机。</div>')
    body += '<h2>接口清单</h2>'
    for path, desc in api_rows:
        body += '<div class="apirow"><code>GET %s</code><span>%s</span></div>' % (
            esc(path), esc(desc))
    body += ('<div class="apirow"><code>POST /shutdown</code>'
             '<span>停止服务（<code>start.sh stop</code> 用；校验 Host/Origin/Sec-Fetch-Site，'
             '配置令牌时还需鉴权）</span></div>')
    body += ('<h2>调用示例</h2><pre class="text">'
             'curl -s "http://127.0.0.1:8765/api/sessions" | python3 -m json.tool\n'
             'curl -s "http://127.0.0.1:8765/api/s/&lt;sid&gt;/calls?limit=5"\n'
             'curl -s "http://127.0.0.1:8765/api/s/&lt;sid&gt;/blocks/42?offset=0&amp;limit=1000"\n'
             'curl -s "http://127.0.0.1:8765/api/search?q=缓存冷启动"\n'
             'curl -s "http://127.0.0.1:8765/api/s/&lt;sid&gt;/blocks/42/find?q=缓存"\n'
             'curl -s "http://127.0.0.1:8765/api/s/&lt;sid&gt;/compact"</pre>')
    body += ('<h2>压缩感知的统计口径</h2><div class="note">'
             '每次调用的可见上下文是 <code>[vf, ctxN)</code>：<code>vf</code> 来自 '
             '<code>providerData.isCompacted</code> 标记，压缩前的旧块退役。'
             '块本身<b>不删除</b>，索引 / 分片 / URL 全部不变，仍可全文检索。'
             '「归档原文」= 全部块总字数，「可见峰值 / 末次可见」= 单次请求实际带上的量；'
             '<code>/api/s/&lt;sid&gt;/calls/&lt;seq&gt;</code> 返回 '
             '<code>vf</code> 与 <code>context.visible_from/visible_to</code>。'
             '真实 <code>pin/hit/credit</code> 始终取自 <code>rawUsage</code>，未做任何估算。</div>')
    body += '<h2>HTML 路由</h2>'
    for r in ROUTE_SPEC:
        if r["kind"] == "html":
            body += '<div class="apirow"><code>%s</code><span>%s</span></div>' % (
                esc(r["route"]), esc(r["doc"]))
    body += ('<div class="note">以上两张表、HTML 路由分发与 '
             '<code>scripts/selftest.py</code> 共用同一份 <code>ROUTE_SPEC</code> 声明；'
             '新增端点只需在声明里加一行。</div>')
    return page("API 文档", [("索引", "/"), ("API", "")], body,
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


# ------------------------------------------------ 分析宽表（给外部程序取数用）
ANALYTICS_FIELDS = (
    "sid", "seq", "ts", "date", "model", "tool",
    "pin", "hit", "miss", "pout", "credit", "rate", "turn", "ws",
    "is_sub", "root",
)
ANALYTICS_MAX_LIMIT = 500     # 与 API_MAX_LIMIT 对齐，避免一次序列化过大


def _parse_bound(raw, end=False):
    """from/to 边界：YYYY-MM-DD（服务器本地时区）或 Unix 秒 / 毫秒数字。"""
    raw = (raw or "").strip()
    if not raw:
        return None
    if re.match(r"^\d{4}-\d{2}-\d{2}$", raw):
        try:
            t = time.mktime(time.strptime(raw, "%Y-%m-%d"))
        except (ValueError, OverflowError, OSError):
            raise ValueError("invalid date: " + raw)
        return (t + 86400) * 1000.0 - 1 if end else t * 1000.0
    try:
        n = float(raw)
    except ValueError:
        raise ValueError("invalid time bound: " + raw)
    if not (0 < n < 1e14):
        raise ValueError("time bound out of range: " + raw)
    return n if n > 1e11 else n * 1000.0


def _session_may_hit(s, lo, hi):
    """会话级粗筛：[first_ts, mtime] 是保守超集，只会少算不会漏算。

    first_ts 是会话首条记录时间、mtime 是文件最后修改时间；脏 / 越界时间戳
    一律返回 True，交给调用级用 ts 精筛，保证正确性优先于剪枝率。
    """
    if lo is None and hi is None:
        return True
    try:
        a = float(s.get("first_ts") or 0)
        b = float(s.get("mtime") or 0) * 1000.0
    except (TypeError, ValueError, OverflowError):
        return True
    if a <= 0 or b <= 0:
        return True
    if b < a:
        a, b = b, a
    return not ((hi is not None and a > hi) or (lo is not None and b < lo))


def analytics_calls(q):
    """跨会话扁平调用宽表，返回 (信封, HTTP 状态码)。

    只读内存索引；过滤下推到会话级，且只物化 [offset, offset+limit) 这一段，
    因此序列化开销随 limit 走而不是随全库调用数走。total 是过滤后的命中数。
    """
    try:
        lo = _parse_bound(qstr(q, "from"))
        hi = _parse_bound(qstr(q, "to"), end=True)
    except ValueError as e:
        return {"ok": False, "error": str(e)}, 400
    if lo is not None and hi is not None and lo > hi:
        return {"ok": False, "error": "from must not be later than to"}, 400

    raw = qstr(q, "fields").strip()
    if raw:
        cols = [x.strip() for x in raw.split(",") if x.strip()]
        bad = [x for x in cols if x not in ANALYTICS_FIELDS]
        if bad:
            return ({"ok": False,
                     "error": "unknown fields: %s (allowed: %s)"
                              % (",".join(bad), ",".join(ANALYTICS_FIELDS))}, 400)
    else:
        cols = list(ANALYTICS_FIELDS)

    lim = min(ANALYTICS_MAX_LIMIT, qint(q, "limit", 200, 1))
    off = qint(q, "offset", 0, 0)
    mf = G.canon_model(qstr(q, "model").strip())
    wf = qstr(q, "ws").strip().lower()
    tf = qstr(q, "tool").strip().lower()
    sid_f = qstr(q, "sid").strip()          # 精确匹配单个会话 id
    refresh = qstr(q, "refresh") == "1"

    # refresh=1 会同时失效会话全文缓存（见 Store.sessions），让宽表与
    # /api/sessions 看到同一份最新 jsonl；分页链接不再携带它，避免每页重扫。
    sessions = STORE.sessions(refresh=refresh)
    total, rows = 0, []
    for s in sessions:
        if sid_f and s["id"] != sid_f:
            continue
        if not _session_may_hit(s, lo, hi):
            continue
        ws = s.get("ws") or ""
        if wf and wf not in ws.lower():
            continue
        d = STORE.get(s["id"])
        for c in d["calls"]:
            ts = c.get("ts") or 0
            try:
                tsf = float(ts)
            except (TypeError, ValueError):
                tsf = 0.0
            if (lo is not None and tsf < lo) or (hi is not None and tsf > hi):
                continue
            model = display_model(c.get("model"))
            if mf and G.canon_model(model) != mf:
                continue
            tool = c.get("tool") or ""
            if tf and tf not in tool.lower():
                continue
            if off <= total < off + lim:
                pin = c.get("pin") or 0
                hit = c.get("hit") or 0
                full = {
                    "sid": s["id"], "seq": c.get("seq"), "ts": ts,
                    "date": fmt_ts(ts, "%Y-%m-%d", ""), "model": model,
                    "tool": tool, "pin": pin, "hit": hit, "miss": pin - hit,
                    "pout": c.get("pout") or 0, "credit": c.get("credit") or 0,
                    "rate": (hit / pin) if pin else None,
                    "turn": c.get("turn", 0), "ws": ws,
                    "is_sub": bool(s.get("is_sub")),
                    "root": s.get("root") or "",
                }
                rows.append(dict((k, full[k]) for k in cols))
            total += 1

    q_next = dict((k, v) for k, v in q.items() if k != "refresh")
    resp = paged(rows, total, off, lim, "/api/analytics/calls", q_next)
    latest = max((x.get("mtime") or 0) for x in sessions) if sessions else 0
    resp["meta"].update({"ts_unit": "unix_ms", "date_tz": "server local time",
                         "fields": cols,
                         "model_key": "providerData.model（全库统一展示名）",
                         "indexed_at": round(STORE._index_at, 3),
                         "source_mtime_max": round(latest, 3),
                         "data_as_of": fmt_ts(latest * 1000, "%Y-%m-%d %H:%M:%S", ""),
                         "refreshed": refresh})
    return resp, 200


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
            "monitor": ("up" if monitor_url() else "down") if MONITOR_URL else "disabled",
            "monitor_url": MONITOR_URL,
        }))
    if path == "/api/sessions":
        return json_bytes(env([dict(s) for s in STORE.sessions()], total=len(STORE.sessions())))
    if path == "/api/stats":
        with CALC_LOCK:
            return json_bytes(env(global_stats()))
    if path == "/api/analytics/calls":
        obj, st = analytics_calls(q)
        return json_bytes(obj, st)
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

    if path == "/api/find":
        sid = qstr(q, "sid") or None
        if sid:
            try:
                STORE.by_id(sid)
            except KeyError:
                return json_bytes({"ok": False, "error": "unknown session: " + sid}, 404)
        with CALC_LOCK:
            rows, warn, nsessions, ms = find_blocks(
                qstr(q, "q"), sid=sid, refresh=qstr(q, "refresh") == "1")
        off = qint(q, "offset")
        lim = min(API_MAX_LIMIT, qint(q, "limit", FIND_MAX_BLOCKS, 1))
        resp = paged(rows[off:off + lim], len(rows), off, lim, "/api/find", q)
        resp["meta"].update({"scan_ms": ms, "sessions": nsessions, "note": warn,
                             "total_hits": sum(r["count"] for r in rows)})
        return json_bytes(resp)

    if path == "/api/tools":
        sid = qstr(q, "sid") or None
        if sid:
            try:
                STORE.by_id(sid)
            except KeyError:
                return json_bytes({"ok": False, "error": "unknown session: " + sid}, 404)
        off = qint(q, "offset")
        lim = min(API_MAX_LIMIT, qint(q, "limit", 50, 1))
        tool = qstr(q, "tool").strip()
        if tool:
            rows, total = tool_calls(sid, tool, off, lim)
            return json_bytes(paged(rows, total, off, lim, "/api/tools", q))
        st = tool_stats_cached(sid=sid, refresh=qstr(q, "refresh") == "1")
        key = "progs" if qstr(q, "view", "tools") == "progs" else "tools"
        data = st[key]
        resp = paged(data[off:off + lim], len(data), off, lim, "/api/tools", q)
        resp["meta"].update({"sessions": st["sessions"],
                             "total_calls": st["total_calls"],
                             "total_credit": st["total_credit"],
                             "shell_runs": st["shell_runs"]})
        return json_bytes(resp)

    m = re.match(r"^/api/s/([0-9A-Za-z_-]+)(/.*)?$", path)
    if not m:
        return json_bytes({"ok": False, "error": "unknown endpoint: " + path}, 404)
    sid, rest = m.group(1), m.group(2) or ""
    try:
        d = STORE.get(sid)
    except KeyError:
        return json_bytes({"ok": False, "error": "unknown session: " + sid}, 404)

    if rest == "/trend":
        g = qstr(q, "g", "call")
        if g not in ("call", "turn"):
            g = "call"
        rows, mx, mxd = trend_rows(d, g)
        off = qint(q, "offset")
        lim = min(API_MAX_LIMIT, qint(q, "limit", PAGE_CALLS, 1))
        resp = paged(rows[off:off + lim], len(rows), off, lim,
                     "/api/s/%s/trend" % sid, q)
        resp["meta"].update({"granularity": g, "max_chars": mx,
                             "max_delta": mxd, "cold": d["cold"]})
        return json_bytes(resp)

    if rest == "/compact":
        rows = compact_rows(d)
        return json_bytes(env(
            {"events": rows, "stats": {k: d["stats"][k] for k in
             ("chars", "visible_peak", "visible_last", "retired_chars",
              "compactions", "pin", "hit", "credit")},
             "consistent": sum(1 for r in rows if r.get("consistent"))},
            total=len(rows),
            note="可见字数是字符数、pin 是 token，只比方向不比幅度；旧块未删除，仅不计入后续调用"))

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
            idxs = list(range(o["lo"], o["hi"])) if state == "all" else [
                j for j in range(o["lo"], o["hi"]) if call_state(d, seq - 1, j) == state]
            rows = []
            for j in idxs[off:off + lim]:
                b = d["ctx"][j]
                rows.append({"idx": j, "state": call_state(d, seq - 1, j),
                             "kind": b["k"], "label": b["l"], "chars": b["n"],
                             "cmd": b["c"], "text_url": "/api/s/%s/blocks/%d" % (sid, j)})
            return json_bytes(paged(rows, len(idxs), off, lim,
                                    "/api/s/%s/calls/%d/context" % (sid, seq), q))
        return json_bytes(env({
            "call": call_brief(c), "model": display_model(c["model"]),
            "tool": c["tool"],
            "context": {"blocks": o["n"], "chars": o["chars"], "cache_ratio": o["ratio"],
                        "visible_from": o["lo"], "visible_to": o["hi"],
                        "compacted": o["compacted"],
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
    mm = re.match(r"^/blocks/(\d+)/find$", rest)
    if mm:
        idx = int(mm.group(1))
        if idx < 0 or idx >= len(d["ctx"]):
            return json_bytes({"ok": False, "error": "block out of range"}, 404)
        term = qstr(q, "q").strip()
        # 与 /api/search、/api/find 保持一致：短词/超长词是 200 + note，不是错误
        if len(term) < 2 or len(term) > 200:
            return json_bytes(env([], total=0, term=term, idx=idx,
                                  note=("关键词至少 2 个字" if len(term) < 2 else "关键词过长")))
        text = d["ctx"][idx]["t"]
        pos, parts = block_hits(text, term)
        off = qint(q, "offset")
        lim = min(API_MAX_LIMIT, qint(q, "limit", 100, 1))
        hits = [{"pos": p, "part": p // PART_CHARS,
                 "html_url": "/s/%s/b/%d?%s" %
                             (sid, idx, urlencode({"part": p // PART_CHARS, "q": term}))}
                for p in pos[off:off + lim]]
        resp = paged(hits, len(pos), off, lim,
                     "/api/s/%s/blocks/%d/find" % (sid, idx), q)
        resp["meta"].update({"term": term, "idx": idx, "total_chars": len(text),
                             "parts": parts, "capped": len(pos) >= FIND_MAX_HITS})
        return json_bytes(resp)
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

def H(pattern, view, route, doc, sample):
    """声明一个 HTML 路由：pattern/view 给分发，route/doc 给文档，sample 给自检。"""
    return dict(kind="html", pattern=pattern, view=view, route=route,
                doc=doc, sample=sample)


def A(route, doc, sample):
    """声明一个只读 JSON 端点。"""
    return dict(kind="api", route=route, doc=doc, sample=sample)


# 单一事实源：HTML 分发、/api 文档页、scripts/selftest.py 三者都读这里。
# 新增端点只需在此加一行；sample 里的 {sid} 由自检脚本替换成真实会话 id。
ROUTE_SPEC = [
    H(r"^/$", lambda q, m: v_index(q), "GET /", "会话索引，按工作区分组", "/"),
    H(r"^/stats$", lambda q, m: v_stats(q), "GET /stats", "跨会话全量聚合、最贵调用 / 最贵块与最大块", "/stats"),
    H(r"^/rank$", lambda q, m: v_rank(q),
      "GET /rank?what=&sort=&k=&from=&to=&p=",
      "逐块 / 逐调用排行：每行一个块或一次调用，可按种类 / 模型与时间筛选，按 token / 次数 / 乘积排序",
      "/rank?what=block&sort=product&k=tool_result"),
    H(r"^/api$", lambda q, m: v_api_doc(q), "GET /api", "API 文档", "/api"),
    H(r"^/search$", lambda q, m: v_search(q), "GET /search?q=&scope=", "全文搜索（跨会话）", "/search?q=token"),
    H(r"^/find$", lambda q, m: v_find(q), "GET /find?q=&p=", "按块精确检索（跨会话）", "/find?q=token"),
    H(r"^/tools$", lambda q, m: v_tools(q), "GET /tools?tool=&view=&q=&p=", "工具与命令聚合（跨会话）", "/tools"),
    H(r"^/s/([0-9A-Za-z_-]+)/search$", lambda q, m: v_search(q, m.group(1)),
      "GET /s/<sid>/search?q=", "会话内全文搜索", "/s/{sid}/search?q=token"),
    H(r"^/s/([0-9A-Za-z_-]+)/find$", lambda q, m: v_find(q, m.group(1)),
      "GET /s/<sid>/find?q=&p=", "会话内按块精确检索", "/s/{sid}/find?q=token"),
    H(r"^/s/([0-9A-Za-z_-]+)/tools$", lambda q, m: v_tools(q, m.group(1)),
      "GET /s/<sid>/tools?tool=&view=", "会话内工具与命令聚合", "/s/{sid}/tools"),
    H(r"^/s/([0-9A-Za-z_-]+)/trend$", lambda q, m: v_trend(q, m.group(1)),
      "GET /s/<sid>/trend?g=&mode=&p=", "逐调用上下文趋势（累计组成 / 净变化）", "/s/{sid}/trend"),
    H(r"^/s/([0-9A-Za-z_-]+)/map$", lambda q, m: v_map(q, m.group(1)),
      "GET /s/<sid>/map?k=&n=", "上下文块地图（面积 ∝ 字数，点块跳全文）", "/s/{sid}/map"),
    H(r"^/s/([0-9A-Za-z_-]+)/compact$", lambda q, m: v_compact(q, m.group(1)),
      "GET /s/<sid>/compact", "压缩事件：真实账单与修正前后统计对照", "/s/{sid}/compact"),
    H(r"^/s/([0-9A-Za-z_-]+)/calls$", lambda q, m: v_session(dict(q, view=["calls"]), m.group(1)),
      "GET /s/<sid>/calls&p=", "会话按调用分页", "/s/{sid}/calls"),
    H(r"^/s/([0-9A-Za-z_-]+)/t/(\d+)$", lambda q, m: v_turn(q, m.group(1), int(m.group(2))),
      "GET /s/<sid>/t/<n>", "单轮的全部调用", "/s/{sid}/t/1"),
    H(r"^/s/([0-9A-Za-z_-]+)/c/(\d+)$", lambda q, m: v_call(q, m.group(1), int(m.group(2))),
      "GET /s/<sid>/c/<seq>?state=&p=", "单次调用的完整上下文块清单", "/s/{sid}/c/1"),
    H(r"^/s/([0-9A-Za-z_-]+)/b/(\d+)$", lambda q, m: v_block(q, m.group(1), int(m.group(2))),
      "GET /s/<sid>/b/<idx>?part=&q=", "块全文分片（raw=1 纯文本；q= 高亮并列出命中片）", "/s/{sid}/b/0"),
    H(r"^/s/([0-9A-Za-z_-]+)/o/(\d+)$", lambda q, m: v_output(q, m.group(1), int(m.group(2))),
      "GET /s/<sid>/o/<seq>?seg=&part=", "该次调用的输出全文分片", "/s/{sid}/o/1"),
    H(r"^/s/([0-9A-Za-z_-]+)$", lambda q, m: v_session(q, m.group(1)),
      "GET /s/<sid>?view=turns|calls&p=", "会话概览（按轮 / 按调用分页）", "/s/{sid}"),

    A("/api/health", "状态、pid、rss_mb、会话数、缓存数、write_files、monitor", "/api/health"),
    A("/api/sessions", "全部会话摘要（新增 hit / miss / rate）", "/api/sessions"),
    A("/api/stats", "跨会话全量聚合", "/api/stats"),
    A("/api/analytics/calls?from=&to=&model=&ws=&tool=&sid=&fields=&refresh=&offset=&limit=",
      "跨会话扁平调用宽表：sid 精筛、is_sub/root 列、refresh=1 强制重扫并在 meta 给出数据新鲜度（详见 docs/API.md）",
      "/api/analytics/calls?limit=3"),
    A("/api/s/<sid>", "单会话全量统计 + 每一轮摘要", "/api/s/{sid}"),
    A("/api/s/<sid>/calls?offset=&limit=&turn=", "调用列表（分页；不含正文）", "/api/s/{sid}/calls?limit=3"),
    A("/api/s/<sid>/calls/<seq>", "单次调用：usage、缓存边界、输出段索引", "/api/s/{sid}/calls/1"),
    A("/api/s/<sid>/calls/<seq>/context?offset=&limit=&state=", "上下文块清单（分页 / 按状态筛选）", "/api/s/{sid}/calls/1/context?limit=3"),
    A("/api/s/<sid>/blocks/<idx>?offset=&limit=&full=1", "块全文（Range 语义，full=1 一次取完）", "/api/s/{sid}/blocks/0?limit=100"),
    A("/api/s/<sid>/blocks/<idx>/users?offset=&limit=", "哪些调用带上了这个块", "/api/s/{sid}/blocks/0/users?limit=3"),
    A("/api/s/<sid>/outputs/<seq>?seg=&offset=&limit=", "输出全文（分段 + 分片）", "/api/s/{sid}/outputs/1?seg=0&limit=100"),
    A("/api/s/<sid>/turns/<n>", "某一轮的全部调用", "/api/s/{sid}/turns/1"),
    A("/api/search?q=&sid=&scope=&limit=", "全文搜索，返回片段与跳转链接（块结果带 part）", "/api/search?q=token&limit=3"),
    A("/api/find?q=&sid=&offset=&limit=", "按块精确检索：命中块、命中数、命中分片", "/api/find?q=token&limit=3"),
    A("/api/s/<sid>/blocks/<idx>/find?q=&offset=&limit=", "单块内全部命中位置与所在分片", "/api/s/{sid}/blocks/0/find?q=token&limit=3"),
    A("/api/tools?sid=&view=&tool=&offset=&limit=", "工具聚合 / Shell 程序频次，带 tool 时下钻", "/api/tools?limit=3"),
    A("/api/s/<sid>/trend?g=&offset=&limit=", "逐调用上下文演化与相邻 delta", "/api/s/{sid}/trend?limit=3"),
    A("/api/s/<sid>/compact", "压缩事件：真实 pin/hit/credit 与修正前后可见字数对照", "/api/s/{sid}/compact"),
]

HTML_ROUTES = [(r["pattern"], r["view"]) for r in ROUTE_SPEC if r["kind"] == "html"]


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
                    "/stats", "/api/stats", "/search", "/api/search",
                    "/find", "/api/find", "/tools", "/api/tools"):
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
    ap.add_argument("--cache", type=int, default=CACHE_SESSIONS,
                    help="内存中保留几个已解析会话；0（默认）按会话数自适应，"
                         "上限 %d。设为小于会话数的值会让 /stats 每次重新解析"
                         % CACHE_MAX)
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
    if not a.warm:
        # 后台预热全部会话：不阻塞监听，首页先走；/stats、/rank 这类全量页
        # 几秒后即热。与请求并发解析同一会话时由 Store._loading 事件去重，
        # 不会重复解析，也不改变零写盘契约。
        def _warm_bg():
            t0 = time.time()
            try:
                for s in STORE.sessions():
                    STORE.get(s["id"])
                print("后台预热完成 %.2fs" % (time.time() - t0), file=sys.stderr)
            except Exception:
                pass
        threading.Thread(target=_warm_bg, daemon=True,
                         name="warm-sessions").start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
    print("已停止")


if __name__ == "__main__":
    main()
