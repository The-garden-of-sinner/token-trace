#!/usr/bin/env python3
"""Codex CLI 数据源，只读。

主源：~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl[.zst]
  session_meta       会话元数据（session_id / cwd / cli_version / model_provider）
  turn_context       本轮模型名（可能多次，取最近一次）
  token_usage_record 新版逐次调用用量：usage 就是本次增量，**不要差分**
  event_msg/token_count
                     老版累计量，没有逐次记录时才回退做差分（本机无正样本）
  response_item      message / function_call(_output) / reasoning，用来还原 ctx

设计要点：
- `.zst` 用系统 libzstd.so.1 经 ctypes 解，不引入第三方包；
- 逐条 token_usage_record 的 usage 直接当单次增量（实测 5 个会话
  sum(usage) == max(total)，差分只会重复计数）；
- ctxN 按**行序**取「该条 usage 记录出现前已出现的块数」，不用时间戳，
  避免同一毫秒内 assistant 回答被算进本次请求；
- tool 归属按行序取「本次 usage 之后、下次 usage 之前」的 function_call，
  对应这次回答调用的工具；没有则视为最终回答。
"""
import collections
import ctypes
import datetime
import glob
import json
import os
import re
import time

CODEX_HOME = os.environ.get("CODEX_HOME", os.path.expanduser("~/.codex"))

_Z = None


def _zstd():
    """惰性加载 libzstd；缺失时返回 None，只影响 .zst 文件。"""
    global _Z
    if _Z is not None:
        return _Z or None
    try:
        z = ctypes.CDLL("libzstd.so.1")
        z.ZSTD_getFrameContentSize.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
        z.ZSTD_getFrameContentSize.restype = ctypes.c_ulonglong
        z.ZSTD_decompress.argtypes = [ctypes.c_void_p, ctypes.c_size_t,
                                      ctypes.c_void_p, ctypes.c_size_t]
        z.ZSTD_decompress.restype = ctypes.c_size_t
        z.ZSTD_isError.argtypes = [ctypes.c_size_t]
        z.ZSTD_isError.restype = ctypes.c_uint
        _Z = z
    except OSError:
        _Z = False
    return _Z or None


def _zdec(raw):
    z = _zstd()
    if z is None:
        raise RuntimeError("libzstd.so.1 不可用，无法解压 %d 字节的 .zst" % len(raw))
    n = z.ZSTD_getFrameContentSize(raw, len(raw))
    if n <= 0 or n > 1 << 31:      # -1/-2 表示未知长度或解码错误
        raise RuntimeError("zstd 帧长度异常: %d" % n)
    buf = ctypes.create_string_buffer(n)
    got = z.ZSTD_decompress(buf, n, raw, len(raw))
    if z.ZSTD_isError(got):
        raise RuntimeError("zstd 解压失败")
    return buf.raw[:got]


def _ms(v):
    """ISO8601 → 毫秒。gen_trace._ms() 只吃数字，这里必须自己转。"""
    if isinstance(v, (int, float)):
        return int(v)
    try:
        return int(datetime.datetime.fromisoformat(
            str(v).replace("Z", "+00:00")).timestamp() * 1000)
    except Exception:
        return 0


def _day(ms):
    if not (0 < ms < 1e14):
        return ""
    return time.strftime("%Y-%m-%d", time.localtime(ms / 1000.0))


def session_files(home=None):
    pat = os.path.join(home or CODEX_HOME, "sessions", "*", "*", "*", "rollout-*")
    return sorted(f for f in glob.glob(pat) if f.endswith((".jsonl", ".jsonl.zst")))


def rows(path):
    raw = open(path, "rb").read()
    if path.endswith(".zst"):
        raw = _zdec(raw)
    for line in raw.decode("utf-8", "replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            yield json.loads(line)
        except Exception:
            yield {"_bad": 1}


_BLOCK_KIND = {
    "message": None,                      # 按 role 再判
    "function_call_output": "tool_result",
    "custom_tool_call_output": "tool_result",
    "function_call": "tool_call",
    "custom_tool_call": "tool_call",
}


def _text(pl):
    """message 的 content 可能是 str 或 [{type,text}]。"""
    c = pl.get("content")
    if isinstance(c, str):
        return c
    out = []
    for it in (c or []):
        if isinstance(it, dict):
            out.append(it.get("text") or it.get("input_text") or "")
    return "".join(out)


def parse(path):
    """解析单个 rollout，返回会话摘要 + ctx + calls，字段与 gen_trace 对齐。"""
    meta, model = {}, ""
    ctx, calls = [], []
    pending = []          # 当前这次调用之后出现的工具名，等下次 usage 落账
    bad = 0
    for d in rows(path):
        if d.get("_bad"):
            bad += 1
            continue
        t = d.get("type")
        pl = d.get("payload") or {}
        pt = pl.get("type") if isinstance(pl, dict) else None
        ts = _ms(d.get("timestamp"))

        if t == "session_meta":
            meta = {"sid": pl.get("session_id") or "", "cwd": pl.get("cwd") or "",
                    "cli": pl.get("cli_version") or "",
                    "provider": pl.get("model_provider") or "",
                    "ts": ts}
            continue
        if t == "turn_context":
            model = pl.get("model") or model
            continue

        if t == "token_usage_record":
            # Codex 的行序是「assistant message → function_call → usage 记录」，
            # 也就是 usage 前紧邻的那批 function_call 属于**本次**调用，不是上一次。
            u = pl.get("usage") or {}
            calls.append({"pin": u.get("input_tokens") or 0,
                          "hit": u.get("cached_input_tokens") or 0,
                          "pout": u.get("output_tokens") or 0,
                          "ts": ts, "model": model, "ctxN": len(ctx),
                          "turn": pl.get("turn_id") or pl.get("root_turn_id") or "",
                          "tool": _tool_label(pending)})
            pending = []
            continue

        if t == "response_item":
            k = _BLOCK_KIND.get(pt)
            if pt == "message":
                role = pl.get("role") or "assistant"
                k = {"user": "user", "assistant": "assistant"}.get(role, "injected")
                # Codex 把 environment_context / recommended_plugins 也塞成
                # role=user 的 message，块分类和标题都必须把它们排除掉
                if k == "user" and re.match(r"^<[a-z_][a-z0-9_]*>", _text(pl).lstrip()):
                    k = "injected"
            if k:
                txt = _text(pl) if pt == "message" else (
                    pl.get("arguments") or pl.get("input") or "")
                if pt in ("function_call_output", "custom_tool_call_output"):
                    txt = pl.get("output") or ""
                if not isinstance(txt, str):
                    txt = json.dumps(txt, ensure_ascii=False)
                kind_label = {"user": ("你的输入", "user"),
                              "assistant": ("助手的回答", "assistant"),
                              "tool_call": ("调用 %s 的参数" % (pl.get("name") or ""), ""),
                              "tool_result": ("工具结果 · %s" % (pl.get("name") or ""), ""),
                              "injected": ("注入内容", "")}[k]
                txt = str(txt)
                ctx.append({"k": k, "l": kind_label[0], "n": len(txt), "t": txt,
                            "c": str(pl.get("name") or "")[:120], "ts": ts,
                            "e": _est(txt), "src": "codex"})
            if pt in ("function_call", "custom_tool_call"):
                pending.append(pl.get("name") or "?")
            continue

        if t == "event_msg" and pt == "token_count":
            info = pl.get("info") or {}
            tot = info.get("total_token_usage") or {}
            last = info.get("last_token_usage") or {}
            if tot or last:      # 实测存在 info:null 的空事件
                calls.append({"_legacy": (tot, last), "ts": ts, "model": model,
                              "ctxN": len(ctx), "turn": "", "tool": ""})
            continue

    # 末尾残留的 pending 属于「还没落 usage 记录的调用」（如 turn_aborted），
    # 不能回填给上一条，否则工具名会错位一次。

    calls = _normalize_calls(calls)

    sid = meta.get("sid") or os.path.basename(path).replace("rollout-", "").split(".")[0]
    cwd = meta.get("cwd") or ""
    title = _title(ctx, cwd)
    return {"meta": meta, "model": model, "ctx": ctx, "calls": calls,
            "sid": sid, "cwd": cwd, "title": title, "bad": bad}


def _tool_label(names):
    names = [n for n in names if n]
    if not names:
        return ""
    return names[0] if len(names) == 1 else "%s×%d" % (names[0], len(names))


def _normalize_calls(raw):
    """老格式（只有累计量）在这里做差分；新格式原样通过。
    差分只在「整个会话没有任何逐次记录」时启用，绝不与逐次记录混用。"""
    fresh = [c for c in raw if "_legacy" not in c]
    if fresh:
        for i, c in enumerate(fresh):
            c["seq"] = i + 1
        return fresh
    out, prev = [], {}
    for c in raw:
        tot, last = c["_legacy"]
        src = tot or last
        delta = {}
        for k, f in (("input_tokens", "pin"), ("cached_input_tokens", "hit"),
                     ("output_tokens", "pout")):
            cur = src.get(k, 0)
            delta[f] = max(0, cur - prev.get(k, 0)) if tot else cur
            prev[k] = cur
        if not any(delta.values()):
            continue
        delta.update(seq=len(out) + 1, ts=c["ts"], model=c["model"],
                     ctxN=c["ctxN"], turn=c["turn"], tool=c["tool"])
        out.append(delta)
    return out


_INJ_HEAD = ("# Files mentioned", "# AGENTS.md", "# Tools", "# Environment")


def _title(ctx, cwd):
    for b in ctx:
        txt = b["t"].strip()
        if b["k"] != "user" or not txt:
            continue
        # 注入的环境/插件/文件清单也走 role=user 的 message，标题要跳过它们
        if txt.startswith("<") and ">" in txt[:80]:
            continue
        if txt.startswith(_INJ_HEAD):
            continue
        first = " ".join(txt.split())
        return first[:40] + ("…" if len(first) > 40 else "")
    return os.path.basename((cwd or "").rstrip("/")) or "(无标题)"


def _est(s):
    """与 gen_trace.est_tokens 同口径：CJK 1 字 ≈ 1 token，其余 4 字符 ≈ 1 token。"""
    import re
    cjk = len(re.findall(r"[\u2e80-\u9fff\uac00-\ud7af\uf900-\ufaff]", s or ""))
    return cjk + (len(s or "") - cjk + 3) // 4


def scan(home=None):
    """与 gen_trace.scan_file() 同构的会话摘要列表。"""
    out = []
    for p in session_files(home):
        try:
            r = parse(p)
        except Exception as e:
            print("token-trace: 跳过 %s（%s: %s）" % (p, type(e).__name__, e))
            continue
        cs = r["calls"]
        billed = [c for c in cs if c["pin"]]
        pin = sum(c["pin"] for c in billed)
        hit = sum(c["hit"] for c in billed)
        pout = sum(c["pout"] for c in billed)
        models, days = {}, {}
        turns = set()
        for c in billed:      # days 与 calls/pin 同口径
            models[c["model"]] = models.get(c["model"], 0) + c["pin"]
            d = _day(c["ts"])
            if d:
                b = days.setdefault(d, [0, 0])
                b[0] += c["pin"]
                b[1] += 1
            if c["turn"]:
                turns.add(c["turn"])
        out.append({
            "path": "codex://" + r["sid"], "id": "codex-" + r["sid"],
            "ai": r["title"], "turns": len(turns), "calls": len(billed),
            "credit": 0, "pin": pin, "pout": pout, "hit": hit,
            "miss": pin - hit, "rate": (hit / pin) if pin else 0,
            "models": models, "votes": {k: {k: v} for k, v in models.items()},
            "days": days, "agent": "codex",
            "is_sub": 0, "root": "",
            "first_ts": cs[0]["ts"] if cs else 0,
            "agent_calls": [], "agent_reqs": [], "skipped": r["bad"], "meta_ok": True,
            "ws": r["cwd"] or "(未知工作区)", "src": "codex",
            "raw_calls": len(cs), "file": p,
            "mtime": os.path.getmtime(p),
        })
    return out


def build(path):
    """伪路径 codex://<sid> → (ctx, calls, meta, turns)。"""
    sid = path.split("://", 1)[1]
    for p in session_files():
        try:
            r = parse(p)
        except Exception:
            continue
        if r["sid"] == sid:
            cs = r["calls"]
            for i, c in enumerate(cs):
                c.setdefault("seq", i + 1)
                c.setdefault("out", [])
                c.setdefault("vf", 0)
                c.setdefault("credit", 0)
                c.setdefault("src", "codex")
            turns = sorted({c["turn"] for c in cs if c["turn"]})
            meta = {"aiTitle": r["title"], "_compactions": [], "_skipped_lines": r["bad"]}
            return r["ctx"], cs, meta, turns
    raise KeyError("codex session not found: " + sid)


if __name__ == "__main__":
    ss = scan()
    print("会话 %d  调用 %d  pin %s  hit %s  pout %s"
          % (len(ss), sum(s["calls"] for s in ss), format(sum(s["pin"] for s in ss), ","),
             format(sum(s["hit"] for s in ss), ","), format(sum(s["pout"] for s in ss), ",")))
    for s in ss:
        r = parse(s["file"])
        kinds = collections.Counter(b["k"] for b in r["ctx"])
        tools = collections.Counter(b["c"] for b in r["ctx"] if b["k"] == "tool_call")
        print("  %-38s calls=%3d pin=%9s blocks=%3d %s"
              % (s["id"][:38], s["calls"], format(s["pin"], ","), len(r["ctx"]), s["ai"][:24]))
        print("     块 %s  工具 %s" % (dict(kinds), tools.most_common(4)))
