"""跨平台 zstd 解压，Codex 的 `.jsonl.zst` 与 DSH 的 `.jsonl.zstd` 共用。

追加写入的会话文件常常是**多帧拼接**（每 flush 一次就是一帧），只解第一帧
会静默丢掉后面的内容，所以这里统一走流式接口，一次调用跨全部帧。

仍然是零第三方依赖：优先 ctypes 直连系统 libzstd（Linux 的 libzstd.so.1、
macOS 的 libzstd.dylib、Windows 的 libzstd.dll / zstd.dll），找不到再退到
`zstd` 命令行，最后才试 Python 3.14 的 `compression.zstd` 与已安装的第三方包。
全都不行时抛 RuntimeError，由调用方跳过该源并打印一行原因。
"""
import ctypes
import ctypes.util
import os
import shutil
import subprocess


class _In(ctypes.Structure):
    _fields_ = [("src", ctypes.c_void_p), ("size", ctypes.c_size_t),
                ("pos", ctypes.c_size_t)]


class _Out(ctypes.Structure):
    _fields_ = [("dst", ctypes.c_void_p), ("size", ctypes.c_size_t),
                ("pos", ctypes.c_size_t)]


_NAMES = ("libzstd.so.1", "libzstd.so", "libzstd.dylib", "libzstd.dll", "zstd.dll")
_HINTS = (
    "/usr/lib/x86_64-linux-gnu/libzstd.so.1",
    "/usr/lib/aarch64-linux-gnu/libzstd.so.1",
    "/usr/lib/arm-linux-gnueabihf/libzstd.so.1",
    "/usr/lib/libzstd.so.1",
    "/usr/local/lib/libzstd.so",
    "/opt/homebrew/lib/libzstd.dylib",
    "/opt/local/lib/libzstd.dylib",
)

DEC = None          # 解析一次后缓存：callable(bytes) -> bytes，或 False 表示不可用
_REASON = ""


def _win_hints():
    """Windows 没有系统级 libzstd，沿 PATH 和几个常见安装位置找。"""
    out = []
    dirs = [d for d in os.environ.get("PATH", "").split(os.pathsep) if d]
    for base in (os.environ.get("ProgramFiles", r"C:\Program Files"),
                 os.environ.get("ProgramW6432", r"C:\Program Files")):
        dirs += [os.path.join(base, "Git", "mingw64", "bin"),
                 os.path.join(base, "Git", "usr", "bin")]
    for d in dirs:
        for f in ("libzstd.dll", "zstd.dll"):
            p = os.path.join(d, f)
            if os.path.exists(p):
                out.append(p)
    return out


def _candidates():
    seen = set()
    for n in _NAMES:
        try:
            found = ctypes.util.find_library(n)
        except Exception:
            found = None
        for c in (found, n):
            if c and c not in seen:
                seen.add(c)
                yield c
    for p in list(_HINTS) + _win_hints():
        if p and p not in seen:
            seen.add(p)
            yield p


def _bind(lib):
    lib.ZSTD_createDStream.restype = ctypes.c_void_p
    lib.ZSTD_freeDStream.argtypes = [ctypes.c_void_p]
    lib.ZSTD_freeDStream.restype = ctypes.c_size_t
    lib.ZSTD_initDStream.argtypes = [ctypes.c_void_p]
    lib.ZSTD_initDStream.restype = ctypes.c_size_t
    lib.ZSTD_decompressStream.argtypes = [ctypes.c_void_p, ctypes.POINTER(_Out),
                                          ctypes.POINTER(_In)]
    lib.ZSTD_decompressStream.restype = ctypes.c_size_t
    lib.ZSTD_isError.argtypes = [ctypes.c_size_t]
    lib.ZSTD_isError.restype = ctypes.c_uint

    def dec(raw):
        ds = lib.ZSTD_createDStream()
        if not ds:
            raise RuntimeError("ZSTD_createDStream 返回空")
        try:
            rc = lib.ZSTD_initDStream(ds)
            if lib.ZSTD_isError(rc):
                raise RuntimeError("ZSTD_initDStream 失败")
            src = ctypes.create_string_buffer(bytes(raw), len(raw))
            inb = _In(ctypes.cast(src, ctypes.c_void_p), len(raw), 0)
            chunk, out, last = 1 << 16, [], -1
            while inb.pos < inb.size:
                buf = ctypes.create_string_buffer(chunk)
                ob = _Out(ctypes.cast(buf, ctypes.c_void_p), chunk, 0)
                rc = lib.ZSTD_decompressStream(ds, ctypes.byref(ob),
                                               ctypes.byref(inb))
                if lib.ZSTD_isError(rc):
                    raise RuntimeError("zstd 解码失败（输入可能被截断）")
                if ob.pos:
                    out.append(buf.raw[:ob.pos])
                if ob.pos == 0 and inb.pos == last:
                    break       # 输入用完但解码器还要数据：文件被截断，保留已解部分
                last = inb.pos
            return b"".join(out)
        finally:
            lib.ZSTD_freeDStream(ds)

    return dec


def _cli():
    exe = shutil.which("zstd") or shutil.which("zstd.exe")
    if not exe:
        return None, ""

    def dec(raw):
        try:
            r = subprocess.run([exe, "-dc", "-q"], input=raw,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               timeout=180)
        except (OSError, subprocess.SubprocessError) as e:
            raise RuntimeError("zstd 命令行不可用：%s" % e)
        if r.returncode != 0:
            raise RuntimeError("zstd 返回 %d：%s" %
                               (r.returncode,
                                r.stderr.decode("utf-8", "replace")[:120]))
        return r.stdout

    return dec, exe


def _module():
    """最后兜底：3.14 标准库或已装的第三方包。多帧场景不保证，但聊胜于无。"""
    try:
        from compression import zstd as m          # Python 3.14+
        return (lambda raw: m.decompress(raw)), "compression.zstd"
    except Exception:
        pass
    try:
        import zstandard as m
        return (lambda raw: m.ZstdDecompressor().decompress(raw)), "zstandard"
    except Exception:
        pass
    try:
        import pyzstd as m
        return (lambda raw: m.decompress(raw)), "pyzstd"
    except Exception:
        pass
    return None, ""


def _load():
    global DEC, _REASON
    if DEC is not None:
        return DEC
    tried = []
    for c in _candidates():
        try:
            dec = _bind(ctypes.CDLL(c))
        except (OSError, AttributeError) as e:
            tried.append("%s(%s)" % (c, type(e).__name__))
            continue
        DEC = dec
        return DEC
    dec, how = _cli()
    if dec:
        DEC = dec
        return DEC
    dec, how = _module()
    if dec:
        DEC = dec
        return DEC
    DEC = False
    _REASON = ("系统里没有可用的 zstd（试过 %s）"
               % (", ".join(tried[:4]) or "ctypes 候选库"))
    return DEC


def available():
    return bool(_load())


def reason():
    _load()
    return _REASON


def decompress(raw):
    dec = _load()
    if not dec:
        raise RuntimeError(_REASON or "zstd 不可用")
    return dec(raw)
