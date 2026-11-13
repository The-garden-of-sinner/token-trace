#!/usr/bin/env python3
"""DeepSeek Harness（DSH）数据源，只读。

会话文件：`$DSH_HOME/sessions/<workspace>/session-<uuid>/session.v4.jsonl.zstd`
  - 追加式事件流，每行一个事件；文件是**多帧 zstd**（每次 flush 一帧），
    必须逐帧解码，只解第一帧会静默丢掉后续内容；
  - 事件：`session` / `session/title` / `turn/start` / `user/message` /
    `assistant/message` / `tool/call` / `tool/result` / `turn/end` …；
  - 用量在 `assistant/message.data.usage`：
        inputTokens       未命中缓存的输入
        cacheReadTokens   命中缓存
        cacheWriteTokens  写入缓存
        outputTokens      输出
    `pin = input + cacheRead + cacheWrite`，`hit = cacheRead`，`pout = output`；
  - 模型在 `assistant/message.data.message.source.model`；
  - `session.delegationDepth > 0` 表示子代理会话。

口径边界：这里做的是**会话 / 调用 / 消息块**粒度。DSH 的 surface 级分类
（system / tools / message 的 token 归属）在它自己的 projection 里，事件流
只有原始消息，因此本模块不宣称逐 token 的 system/tools 拆分。
"""
import collections
import glob
import json
import os
import time

try:                                  # 作为包导入（gen_trace / trace_server）
    from ._zstd import decompress as _zdec
except ImportError:                   # 直接运行本文件自测
    from _zstd import decompress as _zdec

DSH_HOME = os.environ.get("DSH_HOME", os.path.expanduser("~/.dsh"))
SESSION_FILE = "session.v4.jsonl.zstd"

# 需要的事件类型。其余行（tool/result、request/header 等）体积最大且这里
# 用不上，先按子串跳过——一个 100 MB 的会话里绝大多数字节都在工具输出里，
# 逐行 json.loads 它们纯属浪费。type 字段总在行首附近，只看前 600 字符。
_WANT = ('"session"', '"assistant/message"', '"user/message"', '"turn/start"',
         '"session/title"', '"tool/call"', '"tool/result"')

# path → ((mtime_ns, size), 轻量结果)，只在文件变动时重新解压
_PARSED = {}


def session_files(home=None):
    base = home or DSH_HOME
    pat = os.path.join(base, "sessions", "*", "session-*", SESSION_FILE)
    return sorted(glob.glob(pat))


def rows(path):
    raw = open(path, "rb").read()
    if path.endswith((".zstd", ".zst")):
        raw = _zdec(raw)
    for line in raw.decode("utf-8", "replace").splitlines():
        line = line.strip()
        if not line:
            continue
        if not any(k in line[:600] for k in _WANT):
            continue
        try:
            yield json.loads(line)
        except Exception:
            yield {"_bad": 1}


def _ms(v):
    try:
        x = float(v)
    except (TypeError, ValueError):
        return 0
    return int(x) if x > 0 else 0


def _est(s):
    """与 gen_trace.est_tokens 同口径：CJK 1 字 ≈ 1 token，其余 4 字符 ≈ 1 token。"""
    import re
    cjk = len(re.findall(r"[\u2e80-\u9fff\uac00-\ud7af\uf900-\ufaff]", s or ""))
    return cjk + (len(s or "") - cjk + 3) // 4


def _text(content):
    """message.content 可能是字符串或 [{type:'text',text:'…'}]。"""
    if isinstance(content, str):
        return content
    out = []
    for p in (content or []):
        if isinstance(p, dict) and p.get("type") == "text" and isinstance(p.get("text"), str):
            out.append(p["text"])
    return "".join(out)


def _title(cands, cwd):
    for txt in cands:
        txt = (txt or "").strip()
        if not txt or (txt.startswith("<") and ">" in txt[:80]):
            continue
        first = " ".join(txt.split())
        return first[:40] + ("…" if len(first) > 40 else "")
    return os.path.basename((cwd or "").rstrip("/")) or "(无标题)"


def parse(path, keep_ctx=True):
    """解析单个会话，返回 {ctx, calls, meta, title, sid, cwd, bad, depth}。"""
    meta = {"sid": "", "cwd": "", "depth": 0, "created": 0}
    title, first_user = "", ""
    ctx, calls, head = [], [], []
    nblocks = 0
    turn_no = 0
    bad = 0
    pending = []          # 本次回答之后出现的工具名，归到上一条调用

    def close_pending():
        if pending and calls:
            prev = calls[-1]
            prev["_tools"] = (prev.get("_tools") or []) + pending[:]
        del pending[:]

    for d in rows(path):
        if d.get("_bad"):
            bad += 1
            continue
        t = d.get("type") or ""
        ts = _ms(d.get("time") or d.get("createdAt"))
        data = d.get("data") or {}

        if t == "session":
            meta["sid"] = str(d.get("id") or "")
            meta["cwd"] = d.get("cwd") or ""
            meta["depth"] = int(d.get("delegationDepth") or 0)
            meta["created"] = _ms(d.get("createdAt"))
            continue
        if t == "session/title":
            v = data.get("title")
            if isinstance(v, str) and v.strip():
                title = v.strip()
            continue
        if t == "turn/start":
            turn_no += 1
            continue

        if t == "user/message":
            txt = _text(data.get("content"))
            if not first_user and txt.strip():
                first_user = txt
            if keep_ctx:
                ctx.append({"k": "user", "l": "你的输入", "n": len(txt), "t": txt,
                            "c": "", "ts": ts, "e": _est(txt), "src": "dsh"})
            elif len(head) < 8:
                head.append(txt)
            nblocks += 1
            continue

        if t == "assistant/message":
            # 上一条回答之后的 tool/call 属于那一次请求，先结算再开新的一条
            close_pending()
            msg = data.get("message") or {}
            src = msg.get("source") or {}
            model = str(src.get("model") or "")
            txt = _text(msg.get("content"))
            u = data.get("usage") or {}
            if u:
                ip = int(u.get("inputTokens") or 0)
                op = int(u.get("outputTokens") or 0)
                cr = int(u.get("cacheReadTokens") or 0)
                cw = int(u.get("cacheWriteTokens") or 0)
                calls.append({"pin": ip + cr + cw, "hit": cr, "pout": op,
                              "ts": ts, "model": model, "ctxN": nblocks,
                              "turn": turn_no or 1, "tool": ""})
            if keep_ctx and txt:
                ctx.append({"k": "assistant", "l": "助手的回答", "n": len(txt), "t": txt,
                            "c": model[:120], "ts": ts, "e": _est(txt), "src": "dsh"})
            if txt:
                nblocks += 1
            continue

        if t == "tool/call":
            name = str(data.get("name") or data.get("tool") or "")
            pending.append(name or "?")
            if keep_ctx:
                txt = data.get("input") if isinstance(data.get("input"), str) else json.dumps(
                    data.get("input") or {}, ensure_ascii=False)
                ctx.append({"k": "tool_call", "l": "调用 %s 的参数" % name, "n": len(txt),
                            "t": txt, "c": name[:120], "ts": ts, "e": _est(txt), "src": "dsh"})
            nblocks += 1
            continue

        if t == "tool/result":
            txt = data.get("content") if isinstance(data.get("content"), str) else json.dumps(
                data.get("content") or {}, ensure_ascii=False)
            if keep_ctx:
                ctx.append({"k": "tool_result", "l": "工具结果", "n": len(txt), "t": txt,
                            "c": "", "ts": ts, "e": _est(txt), "src": "dsh"})
            nblocks += 1
            continue

    close_pending()
    for c in calls:
        names = c.pop("_tools", None)
        if names:
            seen, keep = set(), []
            for n in names:
                if n and n not in seen:
                    seen.add(n)
                    keep.append(n)
            c["tool"] = ", ".join(keep[:3])

    sid = meta["sid"] or os.path.basename(os.path.dirname(path)).replace("session-", "")
    cwd = meta["cwd"]
    cands = ([first_user] if first_user else []) + (
        head if not keep_ctx else [b["t"] for b in ctx if b["k"] == "user"])
    return {"ctx": ctx, "calls": calls, "meta": meta, "sid": sid, "cwd": cwd,
            "title": title or _title(cands, cwd),
            "bad": bad, "depth": meta["depth"]}


def _parse_cached(p):
    try:
        st = os.stat(p)
    except OSError:
        return None
    key = (st.st_mtime_ns, st.st_size)
    hit = _PARSED.get(p)
    if hit and hit[0] == key:
        return hit[1]
    r = parse(p, keep_ctx=False)
    _PARSED[p] = (key, r)
    return r


def _summary(path, r, mtime):
    cs = [c for c in r["calls"] if c["pin"]]
    pin = sum(c["pin"] for c in cs)
    hit = sum(c["hit"] for c in cs)
    pout = sum(c["pout"] for c in cs)
    models, days, turns = {}, {}, set()
    for c in cs:
        models[c["model"]] = models.get(c["model"], 0) + c["pin"]
        d = time.strftime("%Y-%m-%d", time.localtime(c["ts"] / 1000.0)) if c["ts"] else ""
        if d:
            b = days.setdefault(d, [0, 0])
            b[0] += c["pin"]
            b[1] += 1
        if c["turn"]:
            turns.add(c["turn"])
    return {
        "path": "dsh://" + r["sid"], "id": "dsh-" + r["sid"],
        "ai": r["title"], "turns": len(turns), "calls": len(cs),
        "credit": 0, "pin": pin, "pout": pout, "hit": hit,
        "miss": pin - hit, "rate": (hit / pin) if pin else 0,
        "models": models, "votes": {k: {k: v} for k, v in models.items()},
        "days": days,
        "agent": "DSH",          # 子代理由 is_sub 标注，列表里显示「子代理 · DSH」
        "is_sub": 1 if r["depth"] > 0 else 0,
        "root": "", "first_ts": r["calls"][0]["ts"] if r["calls"] else 0,
        "agent_calls": [], "agent_reqs": [], "skipped": r["bad"], "meta_ok": True,
        "ws": r["cwd"] or "(未知工作区)", "src": "dsh",
        "raw_calls": len(r["calls"]), "file": path, "mtime": mtime,
    }


def scan(home=None):
    out = []
    for p in session_files(home):
        try:
            r = _parse_cached(p)
        except Exception as e:
            print("token-trace: 跳过 %s（%s: %s）" % (p, type(e).__name__, e))
            continue
        if r is None:
            continue
        try:
            mt = os.path.getmtime(p)
        except OSError:
            mt = 0
        out.append(_summary(p, r, mt))
    return out


def build(path):
    """伪路径 dsh://<sid> → (ctx, calls, meta, turns)。"""
    sid = path.split("://", 1)[1]
    files = session_files()
    cand = [p for p in files if sid in os.path.basename(p)] or files
    for p in cand:
        try:
            r = parse(p, keep_ctx=True)
        except Exception:
            continue
        if r["sid"] != sid:
            continue
        cs = r["calls"]
        for i, c in enumerate(cs):
            c.setdefault("seq", i + 1)
            c.setdefault("out", [])
            c.setdefault("vf", 0)
            c.setdefault("credit", 0)
            c.setdefault("src", "dsh")
        turns = sorted({c["turn"] for c in cs if c["turn"]})
        meta = {"aiTitle": r["title"], "_compactions": [], "_skipped_lines": r["bad"]}
        return r["ctx"], cs, meta, turns
    raise KeyError("dsh session not found: " + sid)


if __name__ == "__main__":
    ss = scan()
    print("会话 %d  调用 %d  pin %s  hit %s  pout %s"
          % (len(ss), sum(s["calls"] for s in ss), format(sum(s["pin"] for s in ss), ","),
             format(sum(s["hit"] for s in ss), ","), format(sum(s["pout"] for s in ss), ",")))
