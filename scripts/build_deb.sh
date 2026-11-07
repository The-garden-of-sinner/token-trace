#!/usr/bin/env bash
# 构建 .deb 安装包（在仓库任意位置执行均可）
#   ./scripts/build_deb.sh            使用 scripts/VERSION
#   ./scripts/build_deb.sh <版本>      指定版本号（默认读 VERSION）
set -eu

DIR="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
ROOT="$(cd "$DIR/.." && pwd)"
PKG="token-trace"
MAINTAINER="The-garden-of-sinner <50914620+The-garden-of-sinner@users.noreply.github.com>"
HOMEPAGE="https://github.com/The-garden-of-sinner/token-trace"

VERSION="${1:-$(cat "$DIR/VERSION")}"
ARCH="all"
STAGE="$ROOT/.build/${PKG}_${VERSION}_${ARCH}"
OUT="$ROOT/dist/${PKG}_${VERSION}_${ARCH}.deb"

command -v dpkg-deb >/dev/null 2>&1 || { echo "缺少 dpkg-deb"; exit 1; }

rm -rf "$ROOT/.build"
mkdir -p "$STAGE/DEBIAN" \
         "$STAGE/usr/bin" \
         "$STAGE/usr/share/$PKG" \
         "$STAGE/usr/share/applications"

# ---- 程序文件（只放运行必需的，不含 trace/ 页面与开发期产物）
install -m 644 "$ROOT/trace_server.py" "$ROOT/gen_trace.py" "$STAGE/usr/share/$PKG/"
install -m 644 "$ROOT/README.md" "$ROOT/LICENSE" "$STAGE/usr/share/$PKG/"
# README 用相对路径引用架构图，docs/ 必须一起打包，否则离线阅读会裂图
mkdir -p "$STAGE/usr/share/$PKG/docs"
install -m 644 "$ROOT/docs/"* "$STAGE/usr/share/$PKG/docs/"

# ---- 系统级启动器：幂等、自动开浏览器、支持 stop / status
cat > "$STAGE/usr/bin/$PKG" <<'LAUNCHER'
#!/bin/sh
# token-trace system launcher. Data stays in ~/.workbuddy, read-only.
set -u
PORT="${TRACE_PORT:-8765}"
URL="http://127.0.0.1:$PORT"
APPDIR=/usr/share/token-trace

alive() {
    python3 -c "import urllib.request,sys;sys.exit(0 if urllib.request.urlopen('$URL/health',timeout=1).read().strip()==b'ok' else 1)" >/dev/null 2>&1
}
open_browser() {
    if command -v xdg-open >/dev/null 2>&1; then
        xdg-open "$URL/" >/dev/null 2>&1 || true
    fi
    echo "Token Trace: $URL/"
}

case "${1:-}" in
    stop)
        if python3 -c "import urllib.request;urllib.request.urlopen(urllib.request.Request('$URL/shutdown',data=b'',method='POST'),timeout=3).read()" >/dev/null 2>&1; then
            echo "Stopped."
        else
            echo "Not running on port $PORT."
        fi
        exit 0
        ;;
    status)
        python3 -c "import urllib.request;print(urllib.request.urlopen('$URL/api/health',timeout=2).read().decode())" 2>/dev/null || echo "Not running on port $PORT."
        exit 0
        ;;
esac

if alive; then
    open_browser
    exit 0
fi

cd "$APPDIR" || exit 1
if command -v setsid >/dev/null 2>&1; then
    setsid python3 -B trace_server.py --port "$PORT" </dev/null >/dev/null 2>&1 &
else
    nohup python3 -B trace_server.py --port "$PORT" </dev/null >/dev/null 2>&1 &
fi

i=0
while [ "$i" -lt 40 ]; do
    if alive; then
        open_browser
        exit 0
    fi
    i=$((i + 1))
    sleep 0.25
done
echo "Service did not become ready on port $PORT." >&2
exit 1
LAUNCHER
chmod 755 "$STAGE/usr/bin/$PKG"

# ---- desktop（单个主分类，避免应用菜单出现两次）
cat > "$STAGE/usr/share/applications/$PKG.desktop" <<'DESKTOP'
[Desktop Entry]
Type=Application
Version=1.0
Name=Token 轨迹
Name[en]=Token Trace
GenericName=WorkBuddy Session Trace
Comment=逐次调用回溯 WorkBuddy 会话的 token 与缓存命中
Comment[en]=Trace per-call token usage and cache hits in WorkBuddy sessions
Exec=token-trace
Icon=utilities-system-monitor
Terminal=false
StartupNotify=true
Categories=Development;
Keywords=workbuddy;token;trace;cache;用量;轨迹;缓存;
DESKTOP
chmod 644 "$STAGE/usr/share/applications/$PKG.desktop"

SIZE_KB="$(du -sk "$STAGE/usr" | cut -f1)"

cat > "$STAGE/DEBIAN/control" <<CONTROL
Package: $PKG
Version: $VERSION
Section: utils
Priority: optional
Architecture: $ARCH
Depends: python3 (>= 3.8)
Installed-Size: $SIZE_KB
Maintainer: $MAINTAINER
Homepage: $HOMEPAGE
Description: WorkBuddy 会话的逐次调用 token 轨迹
 把本地会话记录还原成逐次调用的 token / 积分 / 缓存命中轨迹，
 以零 JavaScript 的按需服务端页面呈现，内容零截断。
 .
 只读 ~/.workbuddy 下的会话文件，不联网、不写盘、不需要第三方包。
 安装后从应用菜单打开「Token 轨迹」，或终端执行 token-trace。
CONTROL

cat > "$STAGE/DEBIAN/postinst" <<'POSTINST'
#!/bin/sh
set -e
if command -v update-desktop-database >/dev/null 2>&1; then
    update-desktop-database -q /usr/share/applications || true
fi
exit 0
POSTINST
chmod 755 "$STAGE/DEBIAN/postinst"

cat > "$STAGE/DEBIAN/postrm" <<'POSTRM'
#!/bin/sh
set -e
if [ "$1" = "remove" ] || [ "$1" = "purge" ]; then
    if command -v update-desktop-database >/dev/null 2>&1; then
        update-desktop-database -q /usr/share/applications || true
    fi
fi
exit 0
POSTRM
chmod 755 "$STAGE/DEBIAN/postrm"

mkdir -p "$ROOT/dist"
rm -f "$OUT"
dpkg-deb --build --root-owner-group "$STAGE" "$OUT" >/dev/null

echo "已生成：$OUT"
echo "大小：$(du -h "$OUT" | cut -f1)"
echo "安装：sudo dpkg -i $OUT"
echo "卸载：sudo apt remove $PKG"
