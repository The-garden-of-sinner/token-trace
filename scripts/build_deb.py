#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""在没有 dpkg-deb 的机器上构建等价 .deb（纯标准库，Windows 亦可运行）。

打包清单与元数据直接取自 scripts/build_deb.sh 的 heredoc，避免两处维护漂移；
本脚本只负责把内容按 ar + tar.gz 的 Debian 容器格式写出去。

    python3 -B scripts/build_deb.py [版本号]

校验：`ar t <deb>` 或 `dpkg-deb -c <deb>`；安装：`sudo dpkg -i <deb>`。
"""
import gzip
import io
import os
import re
import sys
import tarfile

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DIST = os.path.join(BASE, "dist")
SH = os.path.join(BASE, "scripts", "build_deb.sh")
VERSION = (sys.argv[1] if len(sys.argv) > 1 else
           open(os.path.join(BASE, "scripts", "VERSION"), encoding="utf-8").read().strip())
# 版本号会直接拼进输出文件名；不校验的话 `-- ../x` 之类可以把产物写出 dist/。
# deb 的 Version 字段本来也只允许这些字符。
if not re.match(r"^[0-9][0-9A-Za-z.+~-]*$", VERSION):
    raise SystemExit("非法版本号 %r：只允许数字、字母与 . + ~ -（且以数字开头）" % VERSION)
PKG = "token-trace"
ARCH = "all"
MAINTAINER = "The-garden-of-sinner <50914620+The-garden-of-sinner@users.noreply.github.com>"
HOMEPAGE = "https://github.com/The-garden-of-sinner/token-trace"


def heredoc(tag):
    """从 build_deb.sh 里取一段 heredoc 正文（保留 shell 变量，交给目标机解释）。"""
    src = open(SH, encoding="utf-8").read()
    m = re.search(r"<<'?%s'?[^\n]*\n(.*?)\n%s\n" % (tag, tag), src, re.S)
    if not m:
        raise SystemExit("build_deb.sh 里找不到 heredoc %s" % tag)
    return m.group(1) + "\n"


def tar_gz(files):
    """files: [(arcname, bytes, mode)]；uid/gid=0、mtime=0，gzip 头也不写墙钟时间。

    注意 `tarfile.open(mode="w:gz")` 只把 tar 成员时间归零，gzip 容器头里的
    MTIME 仍是当前时间 —— 同输入相隔一秒构建出来的字节就不同。这里先写裸 tar，
    再用 GzipFile(mtime=0) 压一层，注释里的「可复现构建」才成立。
    """
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.GNU_FORMAT) as tf:
        for name, data, mode in files:
            ti = tarfile.TarInfo("./" + name)
            ti.size = len(data)
            ti.mode = mode
            ti.uid = ti.gid = 0
            ti.uname = ti.gname = "root"
            ti.mtime = 0
            tf.addfile(ti, io.BytesIO(data))
    buf = io.BytesIO()
    with gzip.GzipFile(filename="", mode="wb", fileobj=buf, mtime=0) as gz:
        gz.write(raw.getvalue())
    return buf.getvalue()


def ar_member(name, data):
    """ar 成员头 60 字节 + 数据（奇数长度补 \\n 对齐到偶数字节）。"""
    head = ("%-16s%-12d%-6d%-6d%-8o%-10d`\n" %
            (name, 0, 0, 0, 0o100644, len(data))).encode("ascii")
    return head + data + (b"\n" if len(data) % 2 else b"")


def read(rel):
    with open(os.path.join(BASE, rel), "rb") as f:
        return f.read()


def main():
    launcher = heredoc("LAUNCHER")
    desktop = heredoc("DESKTOP")
    control = heredoc("CONTROL")
    postinst = heredoc("POSTINST")
    postrm = heredoc("POSTRM")

    data_files = [("usr/bin/token-trace", launcher.encode("utf-8"), 0o755)]
    for name in ("trace_server.py", "gen_trace.py", "README.md", "LICENSE"):
        data_files.append(("usr/share/token-trace/" + name, read(name), 0o644))
    docs = os.path.join(BASE, "docs")
    for name in sorted(os.listdir(docs)):
        path = os.path.join(docs, name)
        if os.path.isfile(path):
            data_files.append(("usr/share/token-trace/docs/" + name, read("docs/" + name), 0o644))
    data_files.append(("usr/share/applications/token-trace.desktop",
                       desktop.encode("utf-8"), 0o644))
    data_files.append(("usr/share/icons/hicolor/256x256/apps/token-trace.png",
                       read("assets/token-trace.png"), 0o644))

    size_kb = max(1, (sum(len(d) for _, d, _ in data_files) + 1023) // 1024)
    control = (control.replace("$PKG", PKG).replace("$VERSION", VERSION)
               .replace("$ARCH", ARCH).replace("$MAINTAINER", MAINTAINER)
               .replace("$HOMEPAGE", HOMEPAGE).replace("$SIZE_KB", str(size_kb)))
    control_files = [("control", control.encode("utf-8"), 0o644),
                     ("postinst", postinst.encode("utf-8"), 0o755),
                     ("postrm", postrm.encode("utf-8"), 0o755)]

    blob = (b"!<arch>\n"
            + ar_member("debian-binary/", b"2.0\n")
            + ar_member("control.tar.gz/", tar_gz(control_files))
            + ar_member("data.tar.gz/", tar_gz(data_files)))

    os.makedirs(DIST, exist_ok=True)
    out = os.path.join(DIST, "%s_%s_%s.deb" % (PKG, VERSION, ARCH))
    with open(out, "wb") as f:
        f.write(blob)
    print("version   : %s" % VERSION)
    print("files     : %d data + %d control" % (len(data_files), len(control_files)))
    print("installed : %d KB" % size_kb)
    print("output    : %s (%d bytes)" % (out, len(blob)))


if __name__ == "__main__":
    main()
