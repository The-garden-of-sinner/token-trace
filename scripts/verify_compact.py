#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""独立人工校验：不 import 本项目任何代码，直接读 jsonl 原始记录复核压缩效果。

    python3 scripts/verify_compact.py                 # 全库
    python3 scripts/verify_compact.py 9367d786       # 只看某个会话

用意：④ 修的是「我们工具的统计口径」，不该也不可能影响真实账单。
本脚本绕开 trace_server / gen_trace，从 jsonl 逐行独立重算三件事：

1. 真实值：压缩前后 pin / hit / credit，直接取 rawUsage 原字段；
2. 旧口径（错误）：累计所有进入上下文的文本，压缩点不重置 → 一路涨；
3. 新口径（修正）：遇到 isCompacted 消息就把可见累计量重置为摘要自身。

判定：新口径的变化方向必须与真实 pin 一致（同为负），旧口径应当相反。
统计范围只取会进入上下文的 user / tool_call 参数 / tool_result / assistant 文本，
是字符数、不含系统提示，与工具的「可见字数」同口径但独立实现。
只读、不写文件、不联网；退出码 0 = 全部方向一致。
"""
import glob
import json
import os
import sys

DATA = os.path.expanduser("~/.workbuddy/projects")
PREFIX = sys.argv[1] if len(sys.argv) > 1 else ""


def text_of(row):
    out = []
    for key in ("content", "rawContent"):
        v = row.get(key)
        if isinstance(v, list):
            for it in v:
                if isinstance(it, dict) and it.get("text"):
                    out.append(it["text"])
    o = row.get("output")
    if isinstance(o, dict) and o.get("text"):
        out.append(o["text"])
    return "\n".join(out)


def fmt(n):
    return format(int(n), ",")


def main():
    files = [f for f in sorted(glob.glob(os.path.join(DATA, "**", "*.jsonl"),
                                         recursive=True))
             if not PREFIX or PREFIX in os.path.basename(f)]
    if not files:
        print("没有匹配的 jsonl 文件：", DATA)
        return 1

    total = ok = 0
    for path in files:
        rows = []
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except (ValueError, UnicodeError):
                    pass

        # 逐行累积两套累计字数，并记录压缩事件所在行号
        cum_all = cum_vis = 0
        all_at, vis_at = [], []
        events = []
        for i, r in enumerate(rows):
            t = r.get("type")
            pd = r.get("providerData") or {}
            n = 0
            if t == "function_call":
                a = r.get("arguments")
                n = len(a) if isinstance(a, str) else len(json.dumps(a, ensure_ascii=False))
            elif t in ("function_call_result", "message"):
                if t == "message" and r.get("role") not in ("user", "assistant"):
                    n = 0
                else:
                    n = len(text_of(r))
            cum_all += n
            cum_vis += n
            if t == "message" and r.get("role") == "user" and pd.get("isCompacted"):
                cum_vis = n            # 历史被摘要替换：可见量重置为摘要自身
                events.append({"i": i, "chars": n, "kind": pd.get("compactType")})
            all_at.append(cum_all)
            vis_at.append(cum_vis)

        if not events:
            continue

        usage = [(i, r["providerData"]["rawUsage"]) for i, r in enumerate(rows)
                 if isinstance(r.get("providerData"), dict)
                 and isinstance(r["providerData"].get("rawUsage"), dict)
                 and r["providerData"]["rawUsage"].get("prompt_tokens")]

        print("\n=== %s  (%d 行 / %d 次压缩) ===" %
              (os.path.basename(path)[:12], len(rows), len(events)))
        for k, ev in enumerate(events, 1):
            before = [u for u in usage if u[0] < ev["i"]]
            after = [u for u in usage if u[0] > ev["i"]]
            if not before or not after:
                print("  压缩 #%d：附近无完整 usage，跳过" % k)
                continue
            ib, ub = before[-1]
            ia, ua = after[0]
            pb, pa = ub["prompt_tokens"], ua["prompt_tokens"]
            dip = 100 * (pa - pb) / pb if pb else 0
            old_b, old_a = all_at[ib], all_at[ia]
            new_b, new_a = vis_at[ib], vis_at[ia]
            d_old = 100 * (old_a - old_b) / max(old_b, 1)
            d_new = 100 * (new_a - new_b) / max(new_b, 1)
            good = (dip < 0 and d_new < 0)
            print("  压缩 #%d  %s  摘要 %s 字（行 %d）" %
                  (k, ev["kind"], fmt(ev["chars"]), ev["i"]))
            print("    真实 pin       %9s → %-9s  %+.0f%%   credit %.2f → %.2f" %
                  (fmt(pb), fmt(pa), dip, ub.get("credit") or 0, ua.get("credit") or 0))
            print("    旧口径(错)     %9s → %-9s  %+.0f%%   ← 压缩点不重置，反而上涨" %
                  (fmt(old_b), fmt(old_a), d_old))
            print("    新口径(修正)   %9s → %-9s  %+.0f%%   ← 与真实 pin 同向" %
                  (fmt(new_b), fmt(new_a), d_new))
            print("    方向一致: %s" % ("是" if good else "否（需人工复核）"))
            total += 1
            ok += good

    print("\n汇总：%d/%d 个压缩点方向一致；本脚本不依赖项目代码，"
          "数字可与 /s/<sid>/compact 页面逐条对照。" % (ok, total))
    print("真实账单字段是 jsonl 原值，任何统计修复都不应改变它们。")
    return 0 if total and ok == total else 1


if __name__ == "__main__":
    sys.exit(main())
