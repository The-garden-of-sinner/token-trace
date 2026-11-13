"""外部源桥接：把 Codex / ZCode / DSH 的会话接进 gen_trace 的 scan_all / build。

对外只暴露两个函数，形状与 gen_trace 内部契约一致：
    scan_external(force=False) -> [会话摘要 dict]
    build(path)                -> (ctx, calls, meta, turns)

会话 id 带平台前缀（codex- / zcode- / dsh-），伪路径走 <src>://<sid>，
因此下游（Store / 统计 / 搜索 / 路由）不需要知道数据来自 JSONL 还是 SQLite。

单个源不可用时只跳过该源并打印一行，不影响其他平台与 WorkBuddy 主源。
"""
import os

try:                      # 作为包导入（gen_trace / trace_server）
    from . import codex, zcode, dsh
except ImportError:       # 直接运行本文件自测
    import codex
    import zcode
    import dsh

_CACHE = {"sig": None, "out": []}
_PREFIX = ("codex://", "zcode://", "dsh://")


def _sig():
    """文件集合 + 各文件签名。sqlite 走 WAL，-wal 也必须进签名。"""
    parts = []
    for f in codex.session_files():
        try:
            st = os.stat(f)
        except OSError:
            continue
        parts.append((f, st.st_mtime_ns, st.st_size))
    for f in dsh.session_files():
        try:
            st = os.stat(f)
        except OSError:
            continue
        parts.append((f, st.st_mtime_ns, st.st_size))
    zp = zcode.db_path()
    for suf in ("", "-wal"):
        try:
            st = os.stat(zp + suf)
        except OSError:
            continue
        parts.append((zp + suf, st.st_mtime_ns, st.st_size))
    return tuple(parts)


def scan_external(force=False):
    sig = _sig()
    if not force and _CACHE["sig"] == sig:
        return _CACHE["out"]

    out = []
    try:
        out.extend(codex.scan())
    except Exception as e:
        print("token-trace: Codex 源跳过（%s: %s）" % (type(e).__name__, e))
    try:
        con = zcode.connect()
        try:
            out.extend(zcode.scan(con))
        finally:
            con.close()
    except Exception as e:
        print("token-trace: ZCode 源跳过（%s: %s）" % (type(e).__name__, e))
    try:
        out.extend(dsh.scan())
    except Exception as e:
        print("token-trace: DSH 源跳过（%s: %s）" % (type(e).__name__, e))

    _CACHE["sig"] = sig
    _CACHE["out"] = out
    return out


def handles(path):
    return isinstance(path, str) and path.startswith(_PREFIX)


def build(path):
    if path.startswith("codex://"):
        ctx, calls, meta, _ = codex.build(path)
    elif path.startswith("zcode://"):
        con = zcode.connect()
        try:
            ctx, calls, meta, _ = zcode.build(con, path.split("://", 1)[1])
        finally:
            con.close()
    elif path.startswith("dsh://"):
        ctx, calls, meta, _ = dsh.build(path)
    else:
        raise KeyError(path)
    return _turn_objects(ctx, calls, meta)


def _turn_objects(ctx, calls, meta):
    """把各源的 turn_id（字符串/整数）统一成 1..N 的轮次对象。

    下游 v_session / turn_brief 用 d["by_turn"].get(t["i"]) 反查调用，
    所以 c["turn"] 必须与 t["i"] 完全对齐，且从 1 开始连续。
    """
    tmap, out = {}, []
    for c in calls:
        tid = c.get("turn")
        if tid in (None, ""):
            tid = 1
        t = tmap.get(tid)
        if t is None:
            q = ""
            for b in reversed(ctx[:c.get("ctxN") or 0]):
                if b["k"] == "user":
                    q = " ".join(b["t"].split())[:120]
                    break
            t = {"i": len(out) + 1, "q": q, "n": 0, "inj": 0,
                 "kind": "user", "disp": q}
            tmap[tid] = t
            out.append(t)
        c["turn"] = t["i"]
        t["n"] += 1
    return ctx, calls, meta, out


if __name__ == "__main__":
    ss = scan_external(force=True)
    for s in ss:
        print("%-46s %-7s calls=%4d pin=%11s %s"
              % (s["id"][:46], s["src"], s["calls"], format(s["pin"], ","), s["ai"][:22]))
    print("合计 %d 会话 / %d 调用 / pin %s"
          % (len(ss), sum(s["calls"] for s in ss), format(sum(s["pin"] for s in ss), ",")))
    if ss:
        ctx, calls, meta, turns = build(ss[0]["path"])
        print("\n首个会话 build：ctx=%d calls=%d turns=%d title=%s"
              % (len(ctx), len(calls), len(turns), meta.get("aiTitle")))
        for c in calls[:3]:
            print("  seq=%d ctxN=%d pin=%d hit=%d tool=%s"
                  % (c["seq"], c["ctxN"], c["pin"], c["hit"], c["tool"]))
