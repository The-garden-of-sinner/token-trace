#!/bin/bash
# Token 轨迹 · 一键启动
#   双击桌面的「Token 轨迹」图标，或直接运行本脚本：
#     服务没在跑 → 后台启动 → 等就绪 → 打开浏览器
#     服务已在跑 → 直接打开浏览器（不会多起一个进程）
#   ./start.sh stop            停止服务
#   TRACE_NO_BROWSER=1 ./start.sh   只起服务、不开浏览器（无头 / 自动化用）
#
# 用 /usr/bin/python3：系统自带、带标准库即可，不依赖 WorkBuddy 内部解释器。
# -B 关掉 __pycache__，保持「运行期零写盘」。

set -u
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY=/usr/bin/python3
PORT="${TRACE_PORT:-8765}"
URL="http://127.0.0.1:${PORT}/"

alive() { curl -fsS --max-time 1 "${URL}health" >/dev/null 2>&1; }

if [ "${1:-}" = "stop" ]; then
    # 优先让服务自己退出（HTTP），不依赖 pkill —— 沙箱/容器里常看不到桌面进程
    code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 2 -X POST "${URL}shutdown" 2>/dev/null || true)"
    if [ "$code" = "200" ]; then
        echo "Token 轨迹服务已停止（$URL）"
    elif pkill -f "trace_server.py --port ${PORT}" 2>/dev/null; then
        echo "Token 轨迹服务已停止"
    else
        echo "没有正在运行的 Token 轨迹服务（端口 $PORT）"
    fi
    exit 0
fi

if alive; then
    echo "服务已在运行，直接打开：$URL"
else
    echo "正在启动 Token 轨迹服务…"
    setsid "$PY" -B "$DIR/trace_server.py" --port "$PORT" </dev/null >/dev/null 2>&1 &
    for _ in $(seq 1 40); do
        alive && break
        sleep 0.25
    done
    if ! alive; then
        echo "启动失败。手动跑一次看报错：" >&2
        echo "  $PY -B $DIR/trace_server.py --port $PORT" >&2
        exit 1
    fi
    echo "服务已就绪：$URL"
fi

if [ "${TRACE_NO_BROWSER:-0}" != "1" ]; then
    ( xdg-open "$URL" >/dev/null 2>&1 & ) || echo "浏览器打开失败，请手动访问 $URL" >&2
fi
