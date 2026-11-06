#!/usr/bin/env python3
# gen_trace.py —— 把 WorkBuddy 会话 jsonl 渲染成逐次调用的 token 轨迹页
# 用法: ./gen_trace.py [jsonl 路径] [-o 输出.html]
#   不给路径则取 ~/.workbuddy/projects/**/*.jsonl 里最近修改的一个
#
# 两个设计约束（都是踩出来的）：
# 1. 每次调用都要重建「这次请求实际看到的完整上下文」，否则看不到缓存命中了什么。
# 2. 输出必须是纯静态 HTML —— 依赖 <script> 的版本在托管预览里根本不执行。
import html
import json
import os
import re
import sqlite3
import sys
from pathlib import Path

SR = re.compile(r"<system-reminder[^>]*>(.*?)</system-reminder>", re.S)
MAX_TEXT = 3000      # 块字典里存的上限（全文）
PREVIEW = 600        # 调用内部就地展开时的预览长度
INLINE_BLOCKS = 3    # 每次调用就地展开几块，其余跳到字典
MAX_CALLS = 150      # 单页最多渲染多少次调用，超出只留最近这些
CHIP = {"hit": "命中", "miss": "未命中", "part": "跨边界", "none": "上下文"}


def trim(ctx, calls, turns, limit=MAX_CALLS):
    """超大会话只保留最近 limit 次调用，ctx 同步裁掉用不到的前缀"""
    if not limit or len(calls) <= limit:
        return ctx, calls, turns, 0
    drop = len(calls) - limit
    keep = [dict(c) for c in calls[drop:]]   # 浅拷贝，避免改到调用方持有的记录
    # 有压缩时，窗口内最早的可见起点可能早于窗口第一次调用的 ctxN（摘要块在前面），
    # 要从那里裁才能留住完整的可见区间；无压缩则沿用原来的 ctxN 起点。
    positive_vf = [c.get("vf", 0) for c in keep if c.get("vf", 0) > 0]
    start = min(keep[0]["ctxN"], min(positive_vf)) if positive_vf else keep[0]["ctxN"]
    ctx2 = ctx[start:]
    for c in keep:
        c["ctxN"] -= start
        c["vf"] = max(0, c.get("vf", 0) - start)
    keep_turns = {c["turn"] for c in keep}
    turns2 = [t for t in turns if t["i"] in keep_turns]
    return ctx2, keep, turns2, drop


def data_dir():
    """WorkBuddy 数据目录，跨平台。

    优先级：WORKBUDDY_HOME 环境变量 > ~/.workbuddy >（仅 Windows）APPDATA/LOCALAPPDATA。
    Windows 版客户端的数据目录是 %USERPROFILE%\\.workbuddy，与 Linux/macOS 同构，
    所以 Path.home() 在两边都能直接命中；后两个候选用于非默认安装。
    """
    override = os.environ.get("WORKBUDDY_HOME")
    if override:
        return Path(override)
    base = Path.home() / ".workbuddy"
    if (base / "projects").is_dir():
        return base
    if os.name == "nt":
        for var in ("APPDATA", "LOCALAPPDATA"):
            d = os.environ.get(var)
            if d and (Path(d) / "WorkBuddy" / "projects").is_dir():
                return Path(d) / "WorkBuddy"
    return base


def iter_jsonl():
    """递归列出全部会话 jsonl。用 Path.rglob，跨平台且不受路径分隔符影响。"""
    root = data_dir() / "projects"
    if not root.is_dir():
        return
    for f in sorted(root.rglob("*.jsonl")):
        if f.is_file():
            yield str(f)


def db_uri(db):
    """生成本地 sqlite 的 file: 只读 URI。

    Windows 路径含反斜杠与盘符，直接拼 "file:%s?mode=ro" 会让 SQLite 报
    unable to open database file；Path.as_uri() 会转成 file:///C:/... 并
    percent-encode 空格与非 ASCII 字符。
    """
    return Path(db).resolve().as_uri() + "?mode=ro"


def iter_rows(path, stats=None):
    """逐行产出 jsonl 记录；坏行跳过并计入 stats["skipped"]。

    WorkBuddy 以 append-only 方式写 jsonl，服务读取时末尾常是截断的半行。
    build() 与 scan_all() 共用本函数，保证两处容错口径一致。
    stats 由调用方传入，生成器结束后读取，避免为了计数把整个文件读进内存。
    """
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except (ValueError, UnicodeError):
                if stats is not None:
                    stats["skipped"] += 1


def seg_user(text):
    injected = [m.group(1).strip() for m in SR.finditer(text)]
    return SR.sub("", text).strip(), injected


def text_of(msg):
    out = []
    for k in ("content", "rawContent"):
        v = msg.get(k)
        if isinstance(v, list):
            for it in v:
                if isinstance(it, dict) and it.get("text"):
                    out.append(it["text"])
    o = msg.get("output")
    if isinstance(o, dict) and o.get("text"):
        out.append(o["text"])
    return "\n".join(out)


def clip(s):
    return s if len(s) <= MAX_TEXT else s[:MAX_TEXT] + f"\n…（共 {len(s):,} 字，此处显示前 {MAX_TEXT} 字）"


def brief(args):
    """从工具调用参数里抠出最能代表这次调用的那一项，用于结果块回显"""
    if not args:
        return ""
    d = args
    if isinstance(args, str):
        try:
            d = json.loads(args)
        except Exception:
            return args.strip().replace("\n", " ")[:120]
    if not isinstance(d, dict):
        return str(d)[:120]
    for k in ("command", "file_path", "notebook_path", "pattern", "query", "glob", "url", "path"):
        v = d.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip().replace("\n", " ")
    for v in d.values():
        if isinstance(v, str) and v.strip():
            return v.strip().replace("\n", " ")
        if isinstance(v, list) and v and isinstance(v[0], str):
            return v[0].strip().replace("\n", " ")
    return ""


def build(path, full=False):
    """返回 (ctx, calls, meta, turns)。

    ctx 是「按时间顺序进入上下文的内容块」，只放会作为历史重发给模型的东西：
      user / injected  → 用户消息
      tool_call        → 助手发出的工具调用
      tool_result      → 工具返回
      assistant        → 助手的最终回答
    reasoning 不进 ctx（多数实现不回传），只作为输出展示。

    full=False（默认）：块正文与输出截到 MAX_TEXT，供纯静态单页使用，体积可控。
    full=True：保留全文，供后端服务按需分片读取，内容零丢失。
    两种模式下 n 都是原文的真实字数，统计口径一致。

    压缩感知：WorkBuddy 的自动压缩会把历史替换成一条 <cb_summary> 的 user 消息。
    jsonl 里旧消息仍然保留（append-only），但压缩后模型只看得到摘要 + 之后的对话。
    因此不删任何块（块索引 / 分片 / URL 全部不变），只给每次调用记 vf（visible_from，
    可见区间起点块号）与 compact（该调用是否紧随压缩）。所有统计用 [vf, ctxN)。
    """
    skipped = {"skipped": 0}
    rows = list(iter_rows(path, skipped))
    ctx, calls, out_buf = [], [], []
    call_cmd = {}  # callId -> 该次调用的命令摘要，供结果块回显
    turns = []     # 一轮 = 一条用户消息触发的一串调用
    cur = None
    vf = 0         # 当前可见区间起点（最近一次压缩的摘要块号）
    compactions = []

    def keep(s):
        return s if full else clip(s)

    def push(kind, label, text, cmd=""):
        ctx.append({"k": kind, "l": label, "n": len(text), "t": keep(text), "c": cmd})

    def usage_of(d):
        prov = d.get("providerData") or {}
        ru = prov.get("rawUsage") or {}
        u = (d.get("message") or {}).get("usage") or {}
        return prov, ru, u

    def model_of(prov):
        return prov.get("requestModelName") or prov.get("model") or "?"

    def val(*pairs):
        """按 (dict, key) 顺序取第一个非 None 值；0 是合法值，不能被 or 吃掉。"""
        for src, k in pairs:
            v = src.get(k)
            if v is not None:
                return v
        return 0

    for d in rows:
        t = d.get("type")
        if t == "function_call":
            prov, ru, u = usage_of(d)
            rec = {
                "seq": len(calls) + 1,
                "ts": d.get("timestamp") or 0,
                "model": model_of(prov),
                "tool": d.get("name") or "",
                "pin": val((ru, "prompt_tokens"), (u, "input_tokens")),
                "pout": val((ru, "completion_tokens"), (u, "output_tokens")),
                "hit": val((ru, "prompt_cache_hit_tokens"), (u, "cache_read_input_tokens")),
                "ctxN": len(ctx),
                "vf": vf,
                "out": out_buf[:],
                "credit": val((ru, "credit")),
                "turn": cur["i"] if cur else 0,
            }
            calls.append(rec)
            if cur:
                cur["n"] += 1
            out_buf = []
            args = d.get("arguments")
            if isinstance(args, str) and args.strip():
                call_cmd[d.get("callId") or id(d)] = brief(args)
                try:
                    args = json.dumps(json.loads(args), ensure_ascii=False, indent=1)
                except Exception:
                    pass
                push("tool_call", f"调用 {d.get('name','')} 的参数", args)
        elif t == "reasoning":
            out_buf.append({"kind": "reasoning", "label": "思考", "text": keep(text_of(d))})
        elif t == "function_call_result":
            cmd = call_cmd.get(d.get("callId") or "", "")
            push("tool_result", f"工具结果 · {d.get('name') or ''}".strip(), text_of(d), cmd)
        elif t == "message" and d.get("role") == "user":
            plain, inj = seg_user(text_of(d))
            prov = d.get("providerData") or {}
            if prov.get("isCompacted"):
                # 压缩：历史被这条 <cb_summary> 摘要整体替换。旧块仍保留在 ctx 里
                # （全文可查、索引/分片/URL 全部不变），但之后的调用只看得到
                # 摘要块及后续内容。vf 指向摘要块自身，它不是「你的提问」。
                vf = len(ctx)
                compactions.append({
                    "at": vf,
                    "chars": len(plain) + sum(len(x) for x in inj),
                    "kind": prov.get("compactType") or "?",
                    "is_summary": bool(prov.get("isSummary")),
                    "ts": d.get("timestamp") or 0,
                    "before_seq": len(calls),   # 压缩前最后一次调用序号
                })
            cur = {"i": len(turns) + 1,
                   "q": re.sub(r"</?user_query>", "", plain or "（只有系统注入）").replace("\n", " ").strip()[:120],
                   "n": 0, "inj": sum(len(x) for x in inj)}
            turns.append(cur)
            if plain:
                push("user", "压缩摘要" if prov.get("isCompacted") else "你的输入", plain)
            for x in inj:
                push("injected", "系统注入", x)
        elif t == "message" and d.get("role") == "assistant":
            prov, ru, u = usage_of(d)
            txt = text_of(d)
            calls.append({
                "seq": len(calls) + 1,
                "ts": d.get("timestamp") or 0,
                "model": model_of(prov),
                "tool": "最终回答",
                "pin": val((ru, "prompt_tokens"), (u, "input_tokens")),
                "pout": val((ru, "completion_tokens"), (u, "output_tokens")),
                "hit": val((ru, "prompt_cache_hit_tokens"), (u, "cache_read_input_tokens")),
                "ctxN": len(ctx),
                "vf": vf,
                "out": out_buf + [{"kind": "assistant", "label": "回答", "text": keep(txt)}],
                "credit": val((ru, "credit")),
                "turn": cur["i"] if cur else 0,
            })
            if cur:
                cur["n"] += 1
            out_buf = []
            push("assistant", "助手的回答", txt)

    meta = dict(next((d for d in rows if d.get("type") == "session-meta"), {}))
    if skipped["skipped"]:
        meta["_skipped_lines"] = skipped["skipped"]
    if compactions:
        meta["_compactions"] = compactions
    return ctx, calls, meta, turns


def cmd_html(b, n=0):
    """回显这次工具调用实际执行的指令 —— 折叠状态下也可见。n=0 表示不截断"""
    c = (b.get("c") or "").strip()
    if not c:
        return ""
    s = c if not n or len(c) <= n else c[:n] + "…"
    return f'<span class="cmd">$ {html.escape(s)}</span>'


def block(b, state):
    """就地展开的块 —— 预览版，正文截到 PREVIEW，完整内容在页尾字典里"""
    t = b["t"]
    if len(t) > PREVIEW:
        t = t[:PREVIEW] + f"\n…（预览只显示前 {PREVIEW} 字，完整内容见页尾上下文块字典）"
    return (f'<details class="blk {state} {b["k"]}">'
            f'<summary><span class="hd1"><span class="chip {state}">{CHIP[state]}</span>'
            f'<b>{html.escape(b["l"])}</b><span class="sz">{b["n"]:,} 字</span>'
            f'<span class="hint2">点开看原文</span></span>{cmd_html(b, 180)}</summary>'
            f'<pre>{html.escape(t)}</pre></details>')


def block_head(b, state, idx):
    """超出同屏上限的块 —— 保留入口，正文放到页尾的块字典里，点击跳转"""
    return (f'<div class="refrow {state}">'
            f'<span class="hd1"><span class="chip {state}">{CHIP[state]}</span>'
            f'<a href="#blk-{idx}">{html.escape(b["l"])}</a>'
            f'<span class="sz">{b["n"]:,} 字</span>'
            f'<span class="hint2">跳到块字典</span></span>{cmd_html(b)}</div>')


def dict_html(ctx):
    """页尾的上下文块字典 —— 每块只出现一次，用 :target 展开，无需脚本"""
    rows = []
    for i, b in enumerate(ctx):
        rows.append(f'<div class="dict {b["k"]}" id="blk-{i}">'
                    f'<div class="dh"><a class="dg" href="#blk-{i}" title="点这里展开这一块">#{i}</a>'
                    f'<b>{html.escape(b["l"])}</b>'
                    f'<span class="sz">{b["n"]:,} 字</span>'
                    f'<a class="totop" href="#top">↑ 顶部</a></div>'
                    f'{cmd_html(b)}'
                    f'<pre>{html.escape(b["t"])}</pre></div>')
    return "".join(rows)


def body_html(ctx, c):
    """重建这次调用实际带上的完整上下文，并标出缓存边界。

    压缩后历史被替换，可见区间是 [vf, ctxN)；旧块仍留在 ctx 里供字典全文检索，
    但不再算进这次请求。块字典的编号用绝对下标，切片后要补回偏移。
    """
    lo = c.get("vf", 0)
    blocks = ctx[lo:c["ctxN"]]
    if not blocks:
        return '<p class="none">这次调用之前没有可见上下文（首轮的系统提示不在 jsonl 里，只体现在 token 计数中）。</p>'
    total = sum(b["n"] for b in blocks)
    ratio = (c["hit"] / c["pin"]) if c["pin"] else None

    parts = []
    if ratio is None:
        parts.append(f'<p class="tot">本次带上 {len(blocks)} 块 / {total:,} 字 · '
                     f'这次没有独立的 usage 记录（同一响应内的并列调用）</p>')
        cut = None
    else:
        parts.append(f'<p class="tot">本次带上 {len(blocks)} 块 / {total:,} 字 · '
                     f'缓存命中 {ratio:.1%} · 接口报输入 {c["pin"]:,} tok'
                     f'<br><span class="fine">token 含系统提示与工具定义，字数只是可见部分，两者不可直接换算</span></p>')
        cut = total * ratio

    if lo:
        parts.append('<p class="fine">发生过上下文压缩：%d 个旧块已被摘要替换，'
                     '不再计入本次可见上下文（仍可在页尾字典查全文）。</p>' % lo)
    acc, split_done, hit_n, hit_sz, miss_n = 0, False, 0, 0, 0
    hit_rows, miss_rows = [], []
    for off, b in enumerate(blocks):
        idx = lo + off
        start = acc
        acc += b["n"]
        if cut is None:
            state = "none"
        elif acc <= cut:
            state = "hit"
        elif start >= cut:
            state = "miss"
        else:
            state = "part"
        if state == "hit":
            hit_n += 1
            hit_sz += b["n"]
        else:
            if not split_done:
                hit_rows.append(f'<p class="agg">前面 {hit_n} 块 / {hit_sz:,} 字命中缓存（已折叠）</p>')
                if cut is not None:
                    hit_rows.append('<div class="split">缓存边界 · 往下是本次未命中</div>')
                split_done = True
            miss_rows.append(block(b, state) if miss_n < INLINE_BLOCKS else block_head(b, state, idx))
            miss_n += 1
    if miss_n > INLINE_BLOCKS:
        miss_rows.append(f'<p class="agg">未命中共 {miss_n} 块，前 {INLINE_BLOCKS} 块就地展开，'
                         f'其余 {miss_n - INLINE_BLOCKS} 块点标题跳到页尾「上下文块字典」看全文'
                         f'（同一块在多次调用里会重复出现，字典里只存一份）</p>')
    if not split_done:
        hit_rows.append(f'<p class="agg good">全部 {hit_n} 块 / {hit_sz:,} 字都命中缓存</p>')

    out = ""
    if c["out"]:
        out = ('<h4 class="sec">本次输出 <small>不占输入上下文</small></h4>'
               + "".join(block({"k": o["kind"], "l": o["label"], "n": len(o["text"]), "t": o["text"]}, "none")
                          for o in c["out"]))
    return "".join(parts) + "".join(hit_rows) + "".join(miss_rows) + out


def render(ctx, calls, meta, turns, src, back="", trimmed=0):
    billed = [c for c in calls if c["pin"]]
    tin = sum(c["pin"] for c in billed)
    thit = sum(c["hit"] for c in billed)
    tout = sum(c["pout"] for c in calls)
    tcredit = sum(c.get("credit") or 0 for c in calls)
    rate = thit / tin if tin else 0
    title = meta.get("aiTitle") or os.path.basename(os.path.dirname(src))

    from collections import Counter
    sz = Counter()
    for b in ctx:
        sz[b["k"]] += b["n"]
    total_sz = sum(sz.values()) or 1
    palette = {"tool_result": "#888780", "injected": "#EF9F27", "user": "#378ADD",
               "tool_call": "#B4B2A9", "assistant": "#1D9E75"}
    names = {"tool_result": "工具结果", "injected": "系统注入", "user": "你的输入",
             "tool_call": "工具调用", "assistant": "助手回答"}
    order = ["tool_result", "injected", "user", "tool_call", "assistant"]
    compose = "".join(f'<i style="width:{sz[k]/total_sz*100:.2f}%;background:{palette[k]}"></i>'
                      for k in order if sz[k])
    legend = "".join(f'<span><i style="background:{palette[k]}"></i>{names[k]} {sz[k]:,} 字</span>'
                     for k in order if sz[k])

    def call_html(c):
        r = (c["hit"] / c["pin"]) if c["pin"] else None
        cls = "aux" if r is None else ("bad" if r < 0.5 else ("warn" if r < 0.8 else ""))
        if r is None:
            bar = '<span class="bar empty"></span>'
            nums = '<span class="num in">并列调用 · 无独立 usage</span><span class="pct">—</span>'
        else:
            miss = c["pin"] - c["hit"]
            bar = (f'<span class="bar"><i class="hit" style="width:{r*100:.2f}%"></i>'
                   f'<i class="miss" style="width:{(1-r)*100:.2f}%"></i></span>')
            nums = (f'<span class="num in">in {c["pin"]:,}</span>'
                    f'<span class="num hit">命中 {c["hit"]:,}</span>'
                    f'<span class="num miss">未命中 {miss:,}</span>'
                    f'<span class="num out">out {c["pout"]:,}</span>'
                    + (f'<span class="num credit">{c.get("credit",0):.2f} 分</span>' if tcredit else "")
                    + f'<span class="pct">{r:.0%}</span>')
        return (f'<details class="call {cls}">'
                f'<summary><span class="idx">#{c["seq"]}</span>{bar}{nums}'
                f'<span class="tool">{html.escape(c["tool"])}</span></summary>'
                f'<div class="body">{body_html(ctx, c)}</div></details>')

    by_turn = {}
    for c in calls:
        by_turn.setdefault(c.get("turn", 0), []).append(c)

    rounds_html = []
    for t in turns:
        cs = by_turn.get(t["i"], [])
        pin = sum(c["pin"] for c in cs)
        pout = sum(c["pout"] for c in cs)
        cred = sum(c["credit"] for c in cs)
        hit = sum(c["hit"] for c in cs)
        rr = (hit / pin) if pin else 0
        first = next((c for c in cs if c["pin"]), None)
        cold = first is not None and (first["hit"] / first["pin"]) < 0.5
        rounds_html.append(f"""
<details class="turn">
  <summary>
    <span class="tno">轮 {t['i']}</span>
    <span class="tq">{html.escape(t['q'])}</span>
    <span class="tn">{t['n']} 次调用</span>
    {f'<span class="cold">缓存冷启动</span>' if cold else ''}
    <span class="num out">out {pout:,}</span>
    <span class="num credit">{cred:.2f} 分</span>
  </summary>
  <div class="tbody">
    <p class="tmeta">这一轮分 {t['n']} 次调用完成 · 输入 {pin:,} tok（命中 {hit:,}，{rr:.0%}）· 输出 {pout:,} tok · <b>积分 {cred:.2f}</b>
    {f'<br><span class="coldnote">首次调用只命中 {first["hit"]/first["pin"]:.0%} —— 多半是重启或换会话后缓存失效，这一次按全价计费 {first["credit"]:.2f} 分</span>' if cold else ''}
    {f'<br><span class="fine">含系统注入 {t["inj"]:,} 字</span>' if t.get("inj") else ''}</p>
    {''.join(call_html(c) for c in cs)}
  </div>
</details>""")
    if by_turn.get(0):
        rounds_html.insert(0, f"""
<details class="turn">
  <summary><span class="tno">开场</span><span class="tq">首条用户消息之前的调用</span>
    <span class="tn">{len(by_turn[0])} 次</span>
    <span class="num credit">{sum(c['credit'] for c in by_turn[0]):.2f} 分</span></summary>
  <div class="tbody">{''.join(call_html(c) for c in by_turn[0])}</div>
</details>""")

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<meta charset="utf-8">
<title>Token 轨迹 · {html.escape(title)}</title>
<style>
*{{box-sizing:border-box}}
body{{margin:0;padding:26px 30px 60px;font:14px/1.65 system-ui,-apple-system,"Noto Sans CJK SC",sans-serif;color:#2C2C2A;background:#fff}}
h1{{font-size:19px;font-weight:500;margin:0 0 4px}}
.back{{display:inline-block;font-size:12px;color:#378ADD;text-decoration:none;margin-bottom:10px}}
.back:hover{{text-decoration:underline}}
.sub{{color:#5F5E5A;font-size:12px;margin-bottom:20px}}
.trimnote{{background:#FAEEDA;border-left:2px solid #EF9F27;padding:9px 13px;border-radius:0 8px 8px 0;font-size:12px;color:#854F0B;margin-bottom:16px}}
.kpi{{display:flex;gap:9px;flex-wrap:wrap;margin-bottom:22px}}
.kpi div{{background:#F1EFE8;border-radius:10px;padding:11px 15px;min-width:126px}}
.kpi b{{display:block;font-size:19px;font-weight:500;line-height:1.3}}
.kpi span{{font-size:11px;color:#5F5E5A}}
h4.sec{{margin:16px 0 6px;font-size:12.5px;font-weight:500;color:#5F5E5A}}
h4.sec small{{font-weight:400;color:#B4B2A9;margin-left:6px}}
.compose{{display:flex;height:22px;border-radius:5px;overflow:hidden;background:#F1EFE8;margin-bottom:7px}}
.compose i{{display:block;height:100%}}
.legend{{font-size:11.5px;color:#5F5E5A;margin-bottom:18px;display:flex;gap:14px;flex-wrap:wrap}}
.legend span{{display:inline-flex;align-items:center;gap:5px}}
.legend i{{display:inline-block;width:9px;height:9px;border-radius:2px}}
.note{{background:#E6F1FB;border-left:2px solid #378ADD;padding:10px 14px;border-radius:0 8px 8px 0;font-size:12.5px;margin-bottom:18px;color:#0C447C}}
.flow{{font-size:12px;color:#5F5E5A;margin-bottom:12px}}
.call{{border:1px solid #E3E1D9;border-radius:8px;margin-bottom:5px;overflow:hidden;background:#fff;content-visibility:auto;contain-intrinsic-size:auto 40px}}
.turn{{border:1px solid #D3D1C7;border-radius:11px;margin-bottom:12px;background:#fff;content-visibility:auto;contain-intrinsic-size:auto 60px}}
.turn>summary{{display:flex;align-items:center;gap:10px;padding:11px 14px;cursor:pointer;list-style:none;flex-wrap:wrap}}
.turn>summary::-webkit-details-marker{{display:none}}
.tno{{background:#2C2C2A;color:#fff;font-size:11px;padding:1px 9px;border-radius:10px;white-space:nowrap}}
.tq{{flex:1;font-size:13px;color:#2C2C2A;min-width:170px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}}
.tn{{font-size:11.5px;color:#5F5E5A;background:#F1EFE8;padding:1px 9px;border-radius:10px;white-space:nowrap}}
.cold{{font-size:11px;color:#A32D2D;background:#FCEBEB;padding:1px 9px;border-radius:10px;white-space:nowrap}}
.coldnote{{color:#A32D2D}}
.tbody{{padding:0 12px 11px;border-top:1px solid #EFEDE5}}
.tmeta{{font-size:11.5px;color:#5F5E5A;margin:10px 0;padding:8px 11px;background:#F7F6F2;border-radius:6px;line-height:1.7}}
.call[open]{{background:#FCFCFA}}
.call.warn{{border-color:#EF9F27}} .call.bad{{border-color:#E24B4A}}
.call.aux{{border-style:dashed;background:#FAFAF7}}
.call>summary{{display:flex;align-items:center;gap:9px;padding:7px 11px;cursor:pointer;list-style:none}}
.call>summary::-webkit-details-marker{{display:none}}
.idx{{color:#888780;font-size:12px;min-width:32px}}
.bar{{flex:1;height:13px;background:#F1EFE8;border-radius:3px;overflow:hidden;display:flex;min-width:90px}}
.bar i{{display:block;height:100%}}
.bar .hit{{background:#378ADD}} .bar .miss{{background:#EF9F27}}
.num{{font-size:11.5px;color:#5F5E5A;font-variant-numeric:tabular-nums;min-width:72px;text-align:right;white-space:nowrap}}
.num.hit{{color:#185FA5}} .num.miss{{color:#BA7517}} .num.out{{color:#0F6E56}}
.num.credit{{color:#854F0B;font-weight:500}}
.pct{{font-size:12px;font-weight:500;min-width:40px;text-align:right}}
.call.warn .pct{{color:#BA7517}} .call.bad .pct{{color:#A32D2D}}
.tool{{font-size:11px;color:#888780;min-width:84px;text-align:right}}
.body{{padding:2px 14px 14px;border-top:1px solid #EFEDE5}}
.tot{{font-size:11.5px;color:#5F5E5A;margin:8px 0;padding:7px 11px;background:#F1EFE8;border-radius:6px}}
.fine{{color:#888780;font-size:11px}}
.agg{{font-size:11.5px;color:#888780;margin:6px 0;padding:5px 11px;background:#F7F6F2;border-radius:5px}}
.agg.good{{color:#185FA5;background:#E6F1FB66}}
.split{{display:flex;align-items:center;gap:8px;font-size:11px;color:#BA7517;margin:9px 0}}
.split:before,.split:after{{content:"";flex:1;height:1px;background:#EF9F27;opacity:.5}}
.blk{{border-left:3px solid #D3D1C7;margin-bottom:4px;border-radius:0 5px 5px 0;background:#fff}}
.blk>summary{{display:flex;flex-direction:column;align-items:flex-start;gap:3px;padding:6px 11px;cursor:pointer;list-style:none;font-size:11.5px;color:#888780}}
.blk>summary::-webkit-details-marker{{display:none}}
.blk>summary b{{font-weight:500;color:#444441;font-size:12px}}
.hd1{{display:flex;align-items:center;gap:8px;width:100%;flex-wrap:wrap}}
.cmd{{display:block;font:11px/1.5 ui-monospace,"Noto Sans Mono CJK SC",monospace;color:#185FA5;word-break:break-all;width:100%;text-align:left}}
.blk .sz{{font-variant-numeric:tabular-nums}}
.blk .hint2{{margin-left:auto;color:#378ADD;font-size:11px}}
.blk pre{{margin:0;padding:2px 12px 10px;font:12px/1.6 ui-monospace,"Noto Sans Mono CJK SC",monospace;white-space:pre-wrap;word-break:break-word;max-height:320px;overflow:auto}}
.blk.hit{{border-left-color:#378ADD;background:#E6F1FB22}}
.blk.miss{{border-left-color:#EF9F27;background:#FAEEDA44}}
.blk.part{{border-left-color:#7F77DD;background:#EEEDFE44}}
.blk.none{{border-left-color:#D3D1C7}}
.blk.one{{display:flex;align-items:center;gap:8px;padding:5px 11px;font-size:11.5px;color:#888780;border-left-width:3px;border-left-style:solid;background:#FAFAF8}}
.blk.one b{{font-weight:500;color:#5F5E5A;font-size:12px}}
.blk.user{{border-left-color:#378ADD}} .blk.assistant{{border-left-color:#1D9E75}}
.blk.injected{{border-left-color:#EF9F27}} .blk.reasoning{{border-left-color:#B4B2A9}}
.blk.tool_result{{border-left-color:#888780}} .blk.tool_call{{border-left-color:#B4B2A9}}
.chip{{display:inline-block;font-size:10.5px;padding:0 6px;border-radius:9px;line-height:16px;font-style:normal}}
.chip.hit{{background:#E6F1FB;color:#185FA5}} .chip.miss{{background:#FAEEDA;color:#BA7517}}
.chip.part{{background:#EEEDFE;color:#534AB7}} .chip.none{{background:#F1EFE8;color:#888780}}
.none{{color:#B4B2A9;font-size:12px;margin:6px 0}}
.refrow{{display:flex;flex-direction:column;align-items:flex-start;gap:3px;padding:5px 11px;font-size:11.5px;color:#888780;background:#FAFAF8;margin-bottom:4px;border-radius:0 5px 5px 0;border-left:3px solid #EF9F27}}
.refrow.part{{border-left-color:#7F77DD}} .refrow.none{{border-left-color:#D3D1C7}}
.refrow a{{color:#185FA5;text-decoration:none;font-weight:500;font-size:12px}}
.refrow a:hover{{text-decoration:underline}}
.refrow .sz{{font-variant-numeric:tabular-nums}}
.refrow .hint2{{margin-left:auto;color:#B4B2A9;font-size:11px}}
h2.dicth{{font-size:15px;font-weight:500;margin:34px 0 4px;padding-top:18px;border-top:1px solid #E3E1D9}}
.dictsub{{font-size:12px;color:#5F5E5A;margin-bottom:14px}}
.dict{{border-left:3px solid #D3D1C7;margin-bottom:4px;background:#fff;border-radius:0 5px 5px 0;scroll-margin-top:14px}}
.dict .dh{{display:flex;gap:8px;align-items:center;padding:6px 11px;font-size:11.5px;color:#888780;flex-wrap:wrap}}
.dict .dg{{color:#378ADD;min-width:44px;text-decoration:none;font-variant-numeric:tabular-nums;cursor:pointer}}
.dict .dg:hover{{text-decoration:underline;color:#0C447C}}
.dict .dh b{{font-weight:500;color:#444441;font-size:12px}}
.dict .cmd{{padding:0 11px 5px}}
.dict .totop{{margin-left:auto;color:#B4B2A9;font-size:11px;text-decoration:none}}
.dict pre{{display:none;margin:0;padding:2px 12px 12px;font:12px/1.6 ui-monospace,"Noto Sans Mono CJK SC",monospace;white-space:pre-wrap;word-break:break-word;max-height:420px;overflow:auto}}
.dict:target{{border-left-color:#378ADD;background:#E6F1FB1a}}
.dict:target pre{{display:block}}
.dict:target .dh b{{color:#185FA5}}
.dict.user{{border-left-color:#378ADD}} .dict.assistant{{border-left-color:#1D9E75}}
.dict.injected{{border-left-color:#EF9F27}} .dict.tool_result{{border-left-color:#888780}}
.dict.tool_call{{border-left-color:#B4B2A9}}
</style>
<a id="top"></a>
{f'<a class="back" href="{back}">← 返回索引</a>' if back else ''}
<h1>Token 轨迹 · {html.escape(title)}</h1>
<div class="sub">{html.escape(os.path.basename(src))} · {len(calls)} 次模型调用 · 模型 {html.escape(billed[0]['model'] if billed else '?')}</div>
{f'<div class="trimnote">这个会话共 {len(calls)+trimmed} 次调用，页面只渲染最近 {len(calls)} 次（更早的 {trimmed} 次已省略，避免单页过大打不开）。</div>' if trimmed else ''}
{f'<div class="trimnote">源文件有 {meta.get("_skipped_lines", 0)} 行无法解析（多半是写入中途被读到），已跳过；下方统计不含这些行。</div>' if meta.get("_skipped_lines") else ''}

<div class="kpi">
  <div><b>{len(turns)}</b><span>对话轮次</span></div>
  <div><b>{tin:,}</b><span>累计输入 token</span></div>
  <div><b>{thit:,}</b><span>其中命中缓存</span></div>
  <div><b>{tin-thit:,}</b><span>未命中（真计费）</span></div>
  <div><b>{tout:,}</b><span>累计输出 token</span></div>
  <div><b>{rate:.1%}</b><span>缓存命中率</span></div>
  {f'<div><b>{tcredit:.1f}</b><span>累计积分（{tcredit/max(1,len(billed)):.3f}/次）</span></div>' if tcredit else ''}
</div>

<h4 class="sec">进入上下文的文本构成 <small>按字符数，反映谁在占你的上下文</small></h4>
<div class="compose">{compose}</div>
<div class="legend">{legend}</div>

<div class="note">
点任意一条调用 → 展开<b>这次请求实际带上的完整上下文</b>，命中缓存的块折叠成一行汇总，
未命中的块逐条列出，中间那条黄线就是缓存边界。<br>
再点任意一个块 → 展开该块原文。命中边界按字数比例估算，块级准确、块内会有误差。<br>
每条末尾的「x.xx 分」是接口回传的 <code>credit</code> 逐次累加；本机实测单价约 0.014 积分/万 token（已含缓存折扣），
<b>积分差异基本等于用量差异</b>，不是单价变了。
</div>

<div class="flow">全部纯 HTML 折叠，不需要脚本。</div>
{''.join(rounds_html)}

<h2 class="dicth" id="dict">上下文块字典 <a href="#top" style="font-size:11px;color:#B4B2A9;font-weight:400;text-decoration:none">↑ 顶部</a></h2>
<div class="dictsub">这里每个块只存一份，点上面调用里的「跳到块字典」会定位到对应块并自动展开原文。
共 {len(ctx)} 块。</div>
{dict_html(ctx)}
</html>
"""


def scan_all():
    """扫描所有会话，返回索引条目（db 补 cwd/标题，jsonl 补统计）"""
    db = data_dir() / "workbuddy.db"
    meta = {}
    meta_ok = True
    try:
        con = sqlite3.connect(db_uri(db), uri=True)
        try:
            for r in con.execute("select id, cwd, title, custom_title, model from sessions"):
                meta[r[0]] = {"cwd": r[1] or "", "title": r[3] or r[2] or "", "model": r[4] or ""}
        finally:
            con.close()
    except Exception as e:
        meta_ok = False
        print("token-trace: 读取 %s 失败：%s: %s（标题/工作区/模型已退回兜底值）"
              % (db, type(e).__name__, e), file=sys.stderr)

    out = []
    seen = {}
    for f in iter_jsonl():
        sid = os.path.basename(f)[:-6]
        if sid in seen:
            print("token-trace: 会话 id 重复，后者会覆盖前者：%s / %s" % (seen[sid], f),
                  file=sys.stderr)
        seen[sid] = f
        turns = calls = 0
        credit = pin = pout = 0
        ai = ""
        skipped = {"skipped": 0}
        for d in iter_rows(f, skipped):
            t = d.get("type")
            if t == "message" and d.get("role") == "user":
                turns += 1
            elif t == "function_call":
                ru = (d.get("providerData") or {}).get("rawUsage") or {}
                if not ru.get("prompt_tokens"):
                    continue
                calls += 1
                pin += ru.get("prompt_tokens", 0)
                pout += ru.get("completion_tokens", 0)
                credit += ru.get("credit") or 0
            elif t == "ai-title" and not ai:
                ai = d.get("aiTitle") or ""
        info = meta.get(sid, {})
        out.append({
            "path": f, "id": sid,
            "ws": info.get("cwd") or os.path.basename(os.path.dirname(f)).replace("-", "/"),
            "title": info.get("title") or ai or "(无标题)",
            "model": info.get("model") or "",
            "mtime": os.path.getmtime(f),
            "turns": turns, "calls": calls, "credit": credit, "pin": pin, "pout": pout,
            "skipped": skipped["skipped"],
            "meta_ok": meta_ok,
        })
    out.sort(key=lambda x: (x["ws"], -x["mtime"]))
    return out


def render_index(sessions):
    import time
    groups = {}
    for s in sessions:
        groups.setdefault(s["ws"], []).append(s)
    total_cr = sum(s["credit"] for s in sessions)
    total_calls = sum(s["calls"] for s in sessions)

    blocks = []
    for ws in sorted(groups, key=lambda w: -max(x["mtime"] for x in groups[w])):
        items = groups[ws]
        cards = []
        for s in items:
            t = time.strftime("%m-%d %H:%M", time.localtime(s["mtime"]))
            cards.append(f"""
<a class="sess" href="s-{s['id']}.html">
  <b>{html.escape(s['title'][:60])}</b>
  <span class="meta">{s['turns']} 轮 · {s['calls']} 次调用 · <em>{s['credit']:.1f} 分</em>
    · {s['pin']/1e6:.1f}M in / {s['pout']/1000:.0f}k out</span>
  <span class="when">{t}</span>
</a>""")
        sum_cr = sum(s["credit"] for s in items)
        blocks.append(f"""
<section>
  <h2>{html.escape(ws)} <small>{len(items)} 个会话 · {sum_cr:.1f} 分</small></h2>
  {''.join(cards)}
</section>""")

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<meta charset="utf-8">
<title>Token 轨迹索引</title>
<style>
*{{box-sizing:border-box}}
body{{margin:0;padding:28px 32px 60px;font:14px/1.65 system-ui,-apple-system,"Noto Sans CJK SC",sans-serif;color:#2C2C2A;background:#fff;max-width:1000px}}
h1{{font-size:20px;font-weight:500;margin:0 0 4px}}
.sub{{color:#5F5E5A;font-size:12px;margin-bottom:26px}}
h2{{font-size:13px;font-weight:500;margin:26px 0 10px;padding-bottom:6px;border-bottom:1px solid #E3E1D9;color:#444441}}
h2 small{{font-weight:400;color:#B4B2A9;font-size:11.5px;margin-left:8px}}
.sess{{display:grid;grid-template-columns:1fr auto;gap:2px 14px;padding:10px 13px;border:1px solid #E3E1D9;border-radius:9px;margin-bottom:6px;text-decoration:none;color:inherit;background:#fff}}
.sess:hover{{border-color:#378ADD;background:#E6F1FB11}}
.sess b{{font-weight:500;font-size:13px;color:#2C2C2A;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}}
.sess .meta{{grid-column:1;font-size:11.5px;color:#5F5E5A}}
.sess .meta em{{color:#854F0B;font-style:normal;font-weight:500}}
.sess .when{{grid-column:2;grid-row:1/span 2;font-size:11.5px;color:#B4B2A9;align-self:center;white-space:nowrap}}
</style>
<h1>Token 轨迹索引</h1>
<div class="sub">{len(sessions)} 个会话 · {len(groups)} 个工作区 · 累计 {total_calls:,} 次调用 · {total_cr:.1f} 分</div>
{''.join(blocks)}
</html>
"""


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    base = os.path.dirname(os.path.abspath(__file__))
    out = None
    if "-o" in sys.argv:
        out = sys.argv[sys.argv.index("-o") + 1]

    if not args:
        outdir = os.path.join(base, "trace")
        os.makedirs(outdir, exist_ok=True)
        sessions = scan_all()
        limit = 0 if "--all" in sys.argv else MAX_CALLS
        ok = 0
        for s in sessions:
            try:
                ctx, calls, meta, turns = build(s["path"])
                ctx, calls, turns, dropped = trim(ctx, calls, turns, limit)
                page = render(ctx, calls, meta, turns, s["path"], back="index.html", trimmed=dropped)
                open(os.path.join(outdir, "s-%s.html" % s["id"]), "w", encoding="utf-8").write(page)
                ok += 1
            except Exception as e:
                print("  跳过 %s: %s" % (s["id"][:8], e))
        ix = os.path.join(outdir, "index.html")
        open(ix, "w", encoding="utf-8").write(render_index(sessions))
        print("%d/%d 个会话 -> %s" % (ok, len(sessions), outdir))
        print("索引: %s" % ix)
        return

    src = args[0]
    out = out or os.path.join(base, "token-trace.html")
    ctx, calls, meta, turns = build(src)
    limit = 0 if "--all" in sys.argv else MAX_CALLS
    ctx, calls, turns, dropped = trim(ctx, calls, turns, limit)
    open(out, "w", encoding="utf-8").write(render(ctx, calls, meta, turns, src, trimmed=dropped))
    print("%d 次调用 / %d 轮 / %d 个上下文块 -> %s" % (len(calls), len(turns), len(ctx), out))


if __name__ == "__main__":
    main()
