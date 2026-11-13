#!/usr/bin/env python3
"""Antigravity 数据源，只读。

数据：`$AG_HOME/conversations/<uuid>.db`（SQLite，默认 `~/.gemini/antigravity`）。
每个会话一个库，表与 protobuf 字段（由本机 14 个真实会话库实测）：

- `gen_metadata(idx, data)`：每次模型调用一条遥测记录
      f1.f19            模型名
      f1.f4.f2          uncached 输入
      f1.f4.f3          输出
      f1.f4.f5          cached 输入
      f1.f4.f9 / f10    thinking / 正文 token
    `pin = uncached + cached`，`hit = cached`，`pout = 输出`。
- `steps(idx, step_type, metadata, step_payload)`：
      metadata.f1       秒 + 纳秒时间戳，与 gen_metadata.idx 共享索引空间
      step_type 14      用户消息，正文在 step_payload.f19.f2
      step_type 15      模型回答，正文在 step_payload.f20.f3
      step_type 132     工具调用，名 / 参数在 step_payload.f5.f4.{f2,f3}
- `annotations/<uuid>.pbtxt`：`title:"..."` 是会话标题（13/14 有）。
- `trajectory_metadata_blob.data`：含 `file:///...` 工作区 URI。

口径边界：`gen_metadata.idx` 与 `step_type=15` 的 idx **并非一一对应**
（实测 1425 条 / 1421 步，交集只有 691），所以逐调用统计只认 `gen_metadata`，
时间戳按共享的 idx 空间从 `steps.metadata` 取（实测覆盖 100%），块数 `ctxN`
按 idx 顺序用二分推进作近似。助手正文与工具调用来自 `steps`，不强行绑定到
某一次调用；`tool` 留空，不宣称精确到单次调用的工具归属。
"""
import bisect
import glob
import json
import os
import re
import sqlite3
import time

AG_HOME = os.environ.get("AG_HOME", os.path.expanduser("~/.gemini/antigravity"))
_PREFIX = "antigravity://"
_PARSED = {}          # path -> ((mtime_ns, size), 轻量结果)


def session_files(home=None):
    return sorted(glob.glob(os.path.join(home or AG_HOME, "conversations", "*.db")))


# ---------------------------------------------------------------- protobuf
# 只需 varint / 定长 / length-delimited 三种 wire type；截断或损坏数据逐字段跳过，
# 不因一条坏记录丢掉整个会话。
def _dvarint(d, p):
    r = s = 0
    while True:
        b = d[p]
        p += 1
        r |= (b & 0x7f) << s
        s += 7
        if not b & 0x80:
            return r, p


def _proto(d):
    p, out = 0, {}
    while p < len(d):
        try:
            tag, p = _dvarint(d, p)
            wire, fld = tag & 7, tag >> 3
            if not fld:
                break
            if wire == 0:
                v, p = _dvarint(d, p)
                out.setdefault(fld, []).append(("v", v))
            elif wire == 1:
                out.setdefault(fld, []).append(("b", d[p:p + 8]))
                p += 8
            elif wire == 2:
                n, p = _dvarint(d, p)
                if p + n > len(d):
                    break
                out.setdefault(fld, []).append(("b", d[p:p + n]))
                p += n
            elif wire == 5:
                out.setdefault(fld, []).append(("b", d[p:p + 4]))
                p += 4
            else:
                break
        except (IndexError, KeyError, ValueError):
            break
    return out


def _sub(fs, fld):
    e = fs.get(fld)
    return _proto(e[0][1]) if e and e[0][0] == "b" else {}


def _var(fs, fld, default=0):
    e = fs.get(fld)
    return e[0][1] if e and e[0][0] == "v" else default


def _str(fs, fld, default=""):
    e = fs.get(fld)
    if not e or e[0][0] != "b":
        return default
    return e[0][1].decode("utf-8", "replace")


def _gen_record(blob):
    f = _proto(blob)
    e = f.get(1)
    if not e or e[0][0] != "b":
        return None
    f1 = _proto(e[0][1])
    f4 = _sub(f1, 4)
    uncached, out, cached = _var(f4, 2), _var(f4, 3), _var(f4, 5)
    return {"model": _str(f1, 19) or "unknown", "pin": uncached + cached,
            "hit": cached, "pout": out, "think": _var(f4, 9),
            "text": _var(f4, 10)}


def _ts_map(con):
    """idx -> Unix 秒（float）。实测 gen_metadata 全部 idx 都能取到时间。"""
    out = {}
    for idx, meta in con.execute("select idx, metadata from steps"):
        if not meta:
            continue
        p = _proto(bytes(meta))
        e = p.get(1)
        if not e or e[0][0] != "b":
            continue
        t = _proto(e[0][1])
        sec = _var(t, 1)
        if sec:
            out[idx] = sec + _var(t, 2) / 1e9
    return out


def _step_text(pl, fld, sub_fld):
    e = _proto(pl).get(fld)
    if not e or e[0][0] != "b":
        return ""
    return _str(_proto(e[0][1]), sub_fld).strip()


def _tool(pl):
    e = _proto(pl).get(5)
    if not e or e[0][0] != "b":
        return "", ""
    inner = _sub(_proto(e[0][1]), 4)
    return _str(inner, 2), _str(inner, 3)


def _title(sid, home=None):
    p = os.path.join(home or AG_HOME, "annotations", sid + ".pbtxt")
    try:
        with open(p, encoding="utf-8", errors="replace") as f:
            m = re.search(r'title\s*:\s*"((?:[^"\\]|\\.)*)"', f.read())
        if m:
            return m.group(1).replace('\\"', '"').replace("\\\\", "\\")[:80]
    except OSError:
        pass
    return sid[:8]


def _workspace(con):
    try:
        row = con.execute("select data from trajectory_metadata_blob where id='main'").fetchone()
        if row and row[0]:
            m = re.search(rb"file:///[^\x00-\x1f\" ]{1,300}", bytes(row[0]))
            if m:
                return m.group(0).decode("utf-8", "replace")[7:]
    except (sqlite3.Error, TypeError):
        pass
    return ""


def _est(s):
    """与 gen_trace.est_tokens 同口径：CJK 1 字 ≈ 1 token，其余 4 字符 ≈ 1 token。"""
    s = s or ""
    cjk = len(re.findall(r"[\u2e80-\u9fff\uac00-\ud7af\uf900-\ufaff]", s))
    return cjk + (len(s) - cjk + 3) // 4


def _block(k, label, txt, c, ts):
    return {"k": k, "l": label, "n": len(txt), "t": txt, "c": c,
            "ts": ts, "e": _est(txt), "src": "antigravity"}


def parse(path, keep_ctx=True):
    """解析单个会话库，返回 {sid, title, cwd, ctx, calls, bad, block_idxs, user_idxs}。"""
    sid = os.path.basename(path)[:-3] if path.endswith(".db") else os.path.basename(path)
    con = sqlite3.connect("file:%s?mode=ro" % path, uri=True, timeout=3)
    try:
        ts_map = _ts_map(con)
        cwd = _workspace(con)
        gen = {}
        bad = 0
        for idx, blob in con.execute("select idx, data from gen_metadata order by idx"):
            try:
                r = _gen_record(bytes(blob or b""))
            except Exception:
                r = None
            if r:
                gen[idx] = r
            else:
                bad += 1

        ctx, block_idxs, user_idxs = [], [], []
        for idx, st, payload in con.execute(
                "select idx, step_type, step_payload from steps order by idx"):
            pl = bytes(payload or b"")
            ts = int(ts_map.get(idx, 0) * 1000)
            txt = ""
            if st == 14:                       # 用户消息
                txt = _step_text(pl, 19, 2)
                user_idxs.append(idx)
                if txt and keep_ctx:
                    ctx.append(_block("user", "你的输入", txt, "", ts))
            elif st == 15:                     # 模型回答
                txt = _step_text(pl, 20, 3)
                if txt and keep_ctx:
                    ctx.append(_block("assistant", "助手的回答", txt, "", ts))
            elif st == 132:                    # 工具调用
                name, args = _tool(pl)
                txt = args or name
                if txt and keep_ctx:
                    ctx.append(_block("tool_call", "调用 %s 的参数" % (name or "工具"),
                                      txt, (name or "")[:120], ts))
            if txt and keep_ctx:
                block_idxs.append(idx)

        calls = []
        for idx in sorted(gen):
            r = gen[idx]
            calls.append({
                "pin": r["pin"], "hit": r["hit"], "pout": r["pout"],
                "ts": int(ts_map.get(idx, 0) * 1000), "model": r["model"],
                "ctxN": bisect.bisect_right(block_idxs, idx) if keep_ctx else 0,
                "turn": bisect.bisect_right(user_idxs, idx) or 1,
                "tool": "", "seq": len(calls) + 1,
                "vf": 0, "credit": 0, "out": [], "src": "antigravity",
            })
        return {"sid": sid, "title": _title(sid), "cwd": cwd, "ctx": ctx,
                "calls": calls, "bad": bad, "block_idxs": block_idxs,
                "user_idxs": user_idxs}
    finally:
        con.close()


def _parse_cached(path):
    """scan() 不需要块正文，按 (mtime_ns, size) 缓存轻量结果。"""
    try:
        st = os.stat(path)
    except OSError:
        return None
    key = (st.st_mtime_ns, st.st_size)
    hit = _PARSED.get(path)
    if hit and hit[0] == key:
        return hit[1]
    r = parse(path, keep_ctx=False)
    _PARSED[path] = (key, r)
    return r


def _summary(path, r, mtime):
    cs = r["calls"]
    pin = sum(c["pin"] for c in cs)
    hit = sum(c["hit"] for c in cs)
    pout = sum(c["pout"] for c in cs)
    models, days, turns = {}, {}, set()
    for c in cs:
        models[c["model"]] = models.get(c["model"], 0) + c["pin"]
        if c["ts"]:
            d = time.strftime("%Y-%m-%d", time.localtime(c["ts"] / 1000.0))
            b = days.setdefault(d, [0, 0])
            b[0] += c["pin"]
            b[1] += 1
        turns.add(c["turn"])
    return {
        "path": _PREFIX + r["sid"], "id": "antigravity-" + r["sid"],
        "ai": r["title"], "turns": len(turns), "calls": len(cs),
        "credit": 0, "pin": pin, "pout": pout, "hit": hit,
        "miss": pin - hit, "rate": (hit / pin) if pin else 0,
        "models": models, "votes": {k: {k: v} for k, v in models.items()},
        "days": days, "agent": "Antigravity", "is_sub": 0, "root": "",
        "first_ts": cs[0]["ts"] if cs else 0,
        "agent_calls": [], "agent_reqs": [], "skipped": r["bad"], "meta_ok": True,
        "ws": r["cwd"] or "(未知工作区)", "src": "antigravity",
        "raw_calls": len(cs), "file": path, "mtime": mtime,
    }


def scan(home=None):
    out = []
    for p in session_files(home):
        try:
            r = _parse_cached(p)
            if r is None:
                continue
            mt = os.path.getmtime(p)
        except (OSError, sqlite3.Error) as e:
            print("token-trace: 跳过 %s（%s: %s）" % (p, type(e).__name__, e))
            continue
        out.append(_summary(p, r, mt))
    return out


def build(path):
    """伪路径 antigravity://<sid> → (ctx, calls, meta, turns)。"""
    sid = path.split("://", 1)[1]
    p = os.path.join(AG_HOME, "conversations", sid + ".db")
    r = parse(p, keep_ctx=True)
    cs = r["calls"]
    meta = {"aiTitle": r["title"], "_compactions": [], "_skipped_lines": r["bad"]}
    return r["ctx"], cs, meta, sorted({c["turn"] for c in cs if c["turn"]})


if __name__ == "__main__":
    ss = scan()
    print("会话 %d  调用 %d  pin %s  hit %s  pout %s" %
          (len(ss), sum(s["calls"] for s in ss),
           format(sum(s["pin"] for s in ss), ","),
           format(sum(s["hit"] for s in ss), ","),
           format(sum(s["pout"] for s in ss), ",")))
    for s in ss[:5]:
        print("  %-40s calls=%4d pin=%12s %s" %
              (s["id"][:40], s["calls"], format(s["pin"], ","), s["ai"][:26]))
    if ss:
        ctx, calls, meta, turns = build(ss[0]["path"])
        print("\n首个会话 build：ctx=%d calls=%d turns=%d title=%s" %
              (len(ctx), len(calls), len(turns), meta["aiTitle"]))
        print("前 3 次调用：", [(c["seq"], c["ctxN"], c["model"], c["pin"])
                              for c in calls[:3]])
