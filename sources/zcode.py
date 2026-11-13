#!/usr/bin/env python3
"""ZCode（Z.ai / GLM）数据源，只读。

主源：~/.zcode/cli/db/db.sqlite
  session        会话元数据（title / directory / parent_id）
  model_usage    逐次模型调用（token / 模型 / 时间 / 状态），是 calls 的唯一来源
  message + part 消息与内容块（text / tool / reasoning / file），用于还原 ctx
  tool_usage     工具执行记录（可选，用于校验工具名）
补充源：~/.zcode/cli/rollout/model-io-*.jsonl
  请求级快照，只补 db 没有的 system prompt 与工具 schema；
  覆盖不全（本机 170/1287 ≈ 13%），**不参与逐调用统计**，避免重复计数。

设计约束：
- 只读（sqlite URI mode=ro），不写任何数据目录；
- 产出字段与 gen_trace.scan_file() / build() 对齐，接入时不用改下游；
- 零输入调用（status=error/cancelled）保留，但按内核口径不计入 rate/miss 分母。
"""
import glob
import json
import os
import sqlite3
import time

ZCODE_HOME = os.environ.get("ZCODE_HOME", os.path.expanduser("~/.zcode"))


def db_path(home=None):
    return os.path.join(home or ZCODE_HOME, "cli", "db", "db.sqlite")


def connect(home=None):
    p = db_path(home)
    con = sqlite3.connect("file:%s?mode=ro" % p, uri=True, timeout=3)
    con.row_factory = sqlite3.Row
    return con


def _ms(v):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return 0


def _day(ms):
    if not (0 < ms < 1e14):
        return ""
    return time.strftime("%Y-%m-%d", time.localtime(ms / 1000.0))


def _tool_name(tools):
    """一次调用可能并行发多个工具，压成 'Bash' 或 'Bash×2'。"""
    names = [t for t in (tools or "").split(",") if t]
    if not names:
        return ""
    first = names[0]
    return first if len(names) == 1 else "%s×%d" % (first, len(names))


def calls(con, sid=None):
    """逐次模型调用。tool 通过 assistant_message_id ↔ part.message_id 精确关联。"""
    sql = """
      select m.*, (
        select group_concat(json_extract(p.data,'$.tool'))
        from part p
        where p.message_id = m.assistant_message_id
          and json_extract(p.data,'$.type') = 'tool') as tools
      from model_usage m
      %s order by m.started_at, m.attempt_index
    """ % ("where m.session_id = ?" if sid else "")
    out = []
    for r in con.execute(sql, (sid,) if sid else ()):
        out.append({
            "seq": len(out) + 1,
            "ts": _ms(r["started_at"]),
            "model": r["model_id"] or "?",
            "tool": _tool_name(r["tools"]),
            "pin": r["input_tokens"] or 0,
            "pout": r["output_tokens"] or 0,
            "hit": r["cache_read_input_tokens"] or 0,
            "ctxN": 0, "vf": 0, "out": [],
            "credit": 0,
            "turn": r["turn_id"] or 0,
            # 以下为 ZCode 独有、供前端按需展示，不参与内核计算
            "src": "zcode",
            "status": r["status"],
            "trace": r["trace_id"] or "",
            "ttft": r["time_to_first_token_ms"] or 0,
            "dur": r["duration_ms"] or 0,
            "n_tool": r["tool_call_count"] or 0,
        })
    return out


def blocks(con, sid):
    """按 message.sequence / part.sequence 还原 ctx 块。
    text 按角色分 user/assistant；tool 拆成 tool_call + tool_result；reasoning 不入 ctx
    （与 WorkBuddy 口径一致：多数实现不回传思考内容）。"""
    rows = con.execute("""
      select p.id, p.message_id, p.time_created, p.sequence as pseq, p.data as pdata,
             m.data as mdata, m.sequence as mseq, m.time_created as mts
      from part p join message m on m.id = p.message_id
      where p.session_id = ?
      order by m.time_created, p.time_created, p.rowid
    """, (sid,))
    ctx, msg_role, last_tool = [], {}, {}
    for r in rows:
        try:
            p = json.loads(r["pdata"])
        except Exception:
            continue
        if r["message_id"] not in msg_role:
            try:
                msg_role[r["message_id"]] = json.loads(r["mdata"]).get("role") or "assistant"
            except Exception:
                msg_role[r["message_id"]] = "assistant"
        role = msg_role[r["message_id"]]
        t = p.get("type")
        ts = _ms(r["time_created"])
        if t == "text":
            txt = p.get("text") or ""
            if txt:
                ctx.append({"k": "user" if role == "user" else "assistant",
                            "l": "你的输入" if role == "user" else "助手的回答",
                            "n": len(txt), "t": txt, "c": "", "ts": ts,
                            "e": _est(txt), "src": "zcode"})
        elif t == "tool":
            name = p.get("tool") or ""
            st = p.get("state") or {}
            inp = st.get("input") or {}
            args = json.dumps(inp, ensure_ascii=False, indent=1)
            cmd = inp.get("command") or inp.get("file_path") or ""
            ctx.append({"k": "tool_call", "l": "调用 %s 的参数" % name, "n": len(args),
                        "t": args, "c": str(cmd)[:120], "ts": ts, "e": _est(args),
                        "src": "zcode"})
            out = st.get("output") or ""
            if out:
                last_tool[p.get("callID") or r["id"]] = str(cmd)[:120]
                ctx.append({"k": "tool_result", "l": "工具结果 · %s" % name,
                            "n": len(out), "t": out, "c": str(cmd)[:120], "ts": ts,
                            "e": _est(out), "src": "zcode"})
        elif t == "file":
            fn = p.get("filename") or "附件"
            ctx.append({"k": "injected", "l": "附件 · %s" % fn, "n": len(fn), "t": fn,
                        "c": "", "ts": ts, "e": _est(fn), "src": "zcode"})
    return ctx


def _est(s):
    """与 gen_trace.est_tokens 同口径：CJK 1 字 ≈ 1 token，其余 4 字符 ≈ 1 token。"""
    import re
    cjk = len(re.findall(r"[\u2e80-\u9fff\uac00-\ud7af\uf900-\ufaff]", s or ""))
    return cjk + (len(s or "") - cjk + 3) // 4


def system_prompts(modelio_files, limit=None):
    """从 model-io 补 system prompt（db 的 message 表没有 system 角色）。
    按文本 hash 去重，返回 [{chars, text, requests}]。limit 限制扫描行数。"""
    import hashlib
    seen, out = {}, []
    for fp in modelio_files:
        with open(fp, errors="replace") as f:
            for i, line in enumerate(f):
                if limit and i >= limit:
                    break
                try:
                    body = (json.loads(line).get("request") or {}).get("body") or {}
                except Exception:
                    continue
                txt = "\n".join(s.get("text", "") for s in (body.get("system") or [])
                                if isinstance(s, dict))
                if not txt:
                    continue
                h = hashlib.sha1(txt.encode("utf-8", "replace")).hexdigest()
                if h in seen:
                    seen[h]["requests"] += 1
                    continue
                rec = {"chars": len(txt), "text": txt, "requests": 1,
                       "tools": len(body.get("tools") or []),
                       "tools_chars": len(json.dumps(body.get("tools") or [],
                                                     ensure_ascii=False))}
                seen[h] = rec
                out.append(rec)
    return out


def modelio_files(home=None):
    return sorted(glob.glob(os.path.join(home or ZCODE_HOME, "cli", "rollout",
                                         "model-io-*.jsonl")))


def build(con, sid):
    """伪路径 zcode://<sid> → (ctx, calls, meta, turns)。

    ctxN（本次请求带上的块数）只能按时间戳推进：db 里 message.sequence 与
    part.sequence 实测全为 0，无法做序列切分；part.time_created 完整（0 缺失）。
    用 <= 而不是 <，才能把「工具刚跑完、同一毫秒发起下一次调用」的那条结果算进去。
    """
    ctx = blocks(con, sid)
    cs = calls(con, sid)
    j = 0
    for c in cs:
        ts = c["ts"]
        while j < len(ctx) and (ctx[j]["ts"] or 0) <= ts:
            j += 1
        c["ctxN"] = j
    row = con.execute("select title from session where id = ?", (sid,)).fetchone()
    title = (row["title"] if row else "") or sid[:8]
    meta = {"aiTitle": title, "_compactions": [], "_skipped_lines": 0}
    return ctx, cs, meta, sorted({c["turn"] for c in cs if c["turn"]})


def scan(con, home=None):
    """与 gen_trace.scan_file() 同构的会话摘要列表。"""
    out = []
    for s in con.execute("select * from session where time_deleted is null "
                         "order by time_created"):
        cs = calls(con, s["id"])
        billed = [c for c in cs if c["pin"]]
        pin = sum(c["pin"] for c in billed)
        hit = sum(c["hit"] for c in billed)
        pout = sum(c["pout"] for c in billed)
        models, days, turns = {}, {}, set()
        for c in billed:      # days 与 calls/pin 同口径：零输入调用不落桶
            models[c["model"]] = models.get(c["model"], 0) + c["pin"]
            d = _day(c["ts"])
            if d:
                b = days.setdefault(d, [0, 0])
                b[0] += c["pin"]
                b[1] += 1
            if c["turn"]:
                turns.add(c["turn"])
        out.append({
            "path": "zcode://" + s["id"], "id": "zcode-" + s["id"], "ai": s["title"] or "",
            "turns": len(turns), "calls": len(billed),
            "credit": 0, "pin": pin, "pout": pout, "hit": hit,
            "miss": pin - hit, "rate": (hit / pin) if pin else 0,
            "models": models, "votes": {k: {k: v} for k, v in models.items()},
            "days": days, "agent": "zcode",
            "is_sub": 1 if s["parent_id"] else 0, "root": s["parent_id"] or "",
            "first_ts": cs[0]["ts"] if cs else 0,
            "agent_calls": [], "agent_reqs": [], "skipped": 0, "meta_ok": True,
            "ws": s["directory"] or "", "src": "zcode",
            "raw_calls": len(cs),  # 含零输入调用，便于核对索引/会话页差异
            "mtime": (max((c["ts"] for c in cs), default=0) / 1000.0)
                     or os.path.getmtime(db_path(home)),
        })
    return out


if __name__ == "__main__":
    con = connect()
    ss = scan(con)
    print(f"会话 {len(ss)}  调用 {sum(s['calls'] for s in ss)}  "
          f"pin {sum(s['pin'] for s in ss):,}  hit {sum(s['hit'] for s in ss):,}  "
          f"pout {sum(s['pout'] for s in ss):,}")
    for s in ss[:4]:
        print(f"  {s['id'][:26]:26s} calls={s['calls']:4d}/{s['raw_calls']:4d} "
              f"pin={s['pin']:>11,d} model={list(s['models'])[:2]} {s['ai'][:24]}")
    first = ss[0]["id"].split("-", 1)[1]
    ctx = blocks(con, first)
    kinds = {}
    for b in ctx:
        kinds[b["k"]] = kinds.get(b["k"], 0) + 1
    print(f"\n块类型 {kinds}  总字数 {sum(b['n'] for b in ctx):,}")
    for b in ctx[:3]:
        print(f"  [{b['k']}] {b['l'][:24]} {b['n']} 字 {b['t'][:60]!r}")
    sp = system_prompts(modelio_files(), limit=20)
    print(f"\nmodel-io system prompt 去重后 {len(sp)} 种，"
          f"平均 {sum(x['chars'] for x in sp) // max(1, len(sp)):,} 字，"
          f"工具 {[x['tools'] for x in sp]}")
