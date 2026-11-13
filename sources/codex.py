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
- `.zst` 走 `sources/_zstd.py`（ctypes 直连系统 libzstd，跨平台、支持多帧），
  不引入第三方包；库和命令行都不可用时只跳过 .zst 文件，不影响 .jsonl；
- 逐条 token_usage_record 的 usage 直接当单次增量（实测 5 个会话
  sum(usage) == max(total)，差分只会重复计数）；
- ctxN 按**行序**取「该条 usage 记录出现前已出现的块数」，不用时间戳，
  避免同一毫秒内 assistant 回答被算进本次请求；
- tool 归属按行序取「本次 usage 之后、下次 usage 之前」的 function_call，
  对应这次回答调用的工具；没有则视为最终回答。
"""
import collections
import datetime
import glob
import json
import os
import re
import time

try:                                  # 作为包导入（gen_trace / trace_server）
    from ._zstd import decompress as _zdec
except ImportError:                   # 直接运行本文件自测
    from _zstd import decompress as _zdec

CODEX_HOME = os.environ.get("CODEX_HOME", os.path.expanduser("~/.codex"))


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


def parse(path, keep_ctx=True):
    """解析单个 rollout，返回会话摘要 + ctx + calls，字段与 gen_trace 对齐。

    keep_ctx=False 供 scan() 用：只数块、只留少量 user 文本做标题，不构造
    完整块列表。块本身占内存也占时间（每条都要估 token），扫一遍全库只为
    出摘要时没必要付这份成本。
    """
    meta, model = {}, ""
    ctx, calls, head = [], [], []
    nblocks = 0
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
                          "ts": ts, "model": model, "ctxN": nblocks,
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
                if keep_ctx:
                    ctx.append({"k": k, "l": kind_label[0], "n": len(txt), "t": txt,
                                "c": str(pl.get("name") or "")[:120], "ts": ts,
                                "e": _est(txt), "src": "codex"})
                elif len(head) < 8 and k == "user":
                    head.append({"k": "user", "t": txt})
                nblocks += 1
            if pt in ("function_call", "custom_tool_call"):
                pending.append(pl.get("name") or "?")
            continue

        if t == "event_msg" and pt == "token_count":
            info = pl.get("info") or {}
            tot = info.get("total_token_usage") or {}
            last = info.get("last_token_usage") or {}
            if tot or last:      # 实测存在 info:null 的空事件
                calls.append({"_legacy": (tot, last), "ts": ts, "model": model,
                              "ctxN": nblocks, "turn": "", "tool": ""})
            continue

    # 末尾残留的 pending 属于「还没落 usage 记录的调用」（如 turn_aborted），
    # 不能回填给上一条，否则工具名会错位一次。

    calls = _normalize_calls(calls)

    sid = meta.get("sid") or os.path.basename(path).replace("rollout-", "").split(".")[0]
    cwd = meta.get("cwd") or ""
    title = _title(ctx if keep_ctx else head, cwd)
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


def _merge_calls(parts):
    """同一 session_id 的多个 rollout 是 resume 出的时间分片，按时间合并。

    resume 会重放历史，重叠的调用按 (ts, pin, pout, model) 去重——同一次
    请求在重放里会得到完全相同的四元组，而两次真实请求撞在同一毫秒且
    token 数完全相同的概率可以忽略。
    """
    out, seen = [], set()
    for cs in parts:
        for c in cs:
            k = (c["ts"], c["pin"], c["pout"], c["model"])
            if k in seen:
                continue
            seen.add(k)
            out.append(c)
    out.sort(key=lambda c: c["ts"] or 0)
    for i, c in enumerate(out):
        c["seq"] = i + 1
    return out


# 文件级解析缓存：path → ((mtime_ns, size), 轻量解析结果)。
# 日常只有正在写入的那一两个 rollout 会变，其余直接命中，全库扫描不会
# 每次都把 100+ MB 历史重新解压一遍。
_PARSED = {}


def _parse_cached(p):
    try:
        st = os.stat(p)
    except OSError:
        return None
    key = (st.st_mtime_ns, st.st_size)
    hit = _PARSED.get(p)
    if hit and hit[0] == key:
        return hit[1]
    r = parse(p, keep_ctx=False)     # 只要摘要，不构造块列表
    _PARSED[p] = (key, r)
    if len(_PARSED) > 4096:          # 防止长期运行堆积已删除的路径
        live = set(session_files())
        for k in [k for k in _PARSED if k not in live]:
            _PARSED.pop(k, None)
    return r


def scan(home=None):
    """与 gen_trace.scan_file() 同构的会话摘要列表。

    同一 sid 可能对应多个 rollout（codex 每次 resume 新开一个文件并重放
    历史），逐个出摘要会让同一次调用被重复计入、会话列表里也出现重复项，
    所以先按 sid 分组，合并成一条会话再汇总。
    """
    groups = collections.OrderedDict()
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
        g = groups.get(r["sid"])
        if g is None:
            groups[r["sid"]] = {"r": r, "parts": [r["calls"]],
                                "file": p, "mtime": mt, "files": 1}
        else:
            g["parts"].append(r["calls"])
            g["files"] += 1
            g["mtime"] = max(g["mtime"], mt)
            if len(r["calls"]) > len(g["r"]["calls"]):
                g["r"] = r          # 块级上下文取最完整的那份

    out = []
    for sid, g in groups.items():
        r = g["r"]
        cs = _merge_calls(g["parts"])
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
            "path": "codex://" + sid, "id": "codex-" + sid,
            "ai": r["title"], "turns": len(turns), "calls": len(billed),
            "credit": 0, "pin": pin, "pout": pout, "hit": hit,
            "miss": pin - hit, "rate": (hit / pin) if pin else 0,
            "models": models, "votes": {k: {k: v} for k, v in models.items()},
            "days": days, "agent": "codex",
            "is_sub": 0, "root": "",
            "first_ts": cs[0]["ts"] if cs else 0,
            "agent_calls": [], "agent_reqs": [], "skipped": r["bad"], "meta_ok": True,
            "ws": r["cwd"] or "(未知工作区)", "src": "codex",
            "raw_calls": len(cs), "file": g["file"],
            "mtime": g["mtime"], "parts": g["files"],
        })
    return out


def build(path):
    """伪路径 codex://<sid> → (ctx, calls, meta, turns)。

    同一 sid 的多个分片里取 calls 最多的那份：块级详情必须与 calls 同源，
    把不同分片的块拼在一起会让 ctxN 错位。会话级统计走 scan() 的合并口径。
    """
    sid = path.split("://", 1)[1]
    files = session_files()
    # 文件名里带 sid 的前 8 位（uuid 足够唯一），先缩小到 1–3 个文件再解析，
    # 否则每次打开会话都要把上百个 rollout 全解一遍。
    cand = [p for p in files if sid[:8] in os.path.basename(p)] or files
    best = None
    for p in cand:
        try:
            r = parse(p)
        except Exception:
            continue
        if r["sid"] == sid and (best is None or len(r["calls"]) > len(best["calls"])):
            best = r
    if best is not None:
        r = best
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
