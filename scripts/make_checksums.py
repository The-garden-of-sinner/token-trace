#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成 Release 附件的完整性清单 dist/SHA256SUMS。

    python3 -B scripts/make_checksums.py

只列本次真正上传的附件：当前版本的 .deb 与 TokenTrace.bat。dist/ 里会留着
历史版本的 deb，全部列进去会让 `sha256sum -c` 去找 Release 页上并不存在的文件。
.bash / .bat 这类纯文本脚本无法做 Windows Authenticode 签名，
所以由本清单提供可核对的哈希；release.sh 会把它作为第三个附件上传。
校验：Linux `sha256sum -c SHA256SUMS`；Windows `Get-FileHash <文件> -Algorithm SHA256`。
"""
import hashlib
import os

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DIST = os.path.join(BASE, "dist")
OUT = os.path.join(DIST, "SHA256SUMS")


def main():
    with open(os.path.join(BASE, "scripts", "VERSION"), encoding="utf-8") as f:
        version = f.read().strip()
    want = {"TokenTrace.bat", "token-trace_%s_all.deb" % version}
    files = sorted(p for p in os.listdir(DIST)
                   if p in want and os.path.isfile(os.path.join(DIST, p)))
    missing = want - set(files)
    if missing:
        raise SystemExit("dist/ 缺少本次发布产物：%s（先构建 deb 与 bat）"
                         % ", ".join(sorted(missing)))
    lines = ["# token-trace release assets - SHA256",
             "# Linux: sha256sum -c SHA256SUMS"]
    for name in files:
        with open(os.path.join(DIST, name), "rb") as f:
            lines.append("%s  %s" % (hashlib.sha256(f.read()).hexdigest(), name))
    with open(OUT, "w", encoding="ascii", newline="\n") as f:
        f.write("\n".join(lines) + "\n")
    print("SHA256SUMS: %d 个产物 -> %s" % (len(files), OUT))
    for line in lines[2:]:
        print("  " + line)


if __name__ == "__main__":
    main()
