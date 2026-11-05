#!/bin/bash
# 生成并安装桌面启动器：把模板里的 @EXEC@ 换成本机 start.sh 的绝对路径。
# 克隆到任意目录后跑一次即可；不会修改仓库里的模板。
set -u
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC="$DIR/token-trace.desktop"
OUT="$HOME/.local/share/applications/token-trace.desktop"

mkdir -p "$(dirname "$OUT")"
sed "s|@EXEC@|$DIR/start.sh|g" "$SRC" > "$OUT"
chmod +x "$OUT"
echo "应用菜单: $OUT"

DESK="$(xdg-user-dir DESKTOP 2>/dev/null || echo "$HOME/Desktop")"
if [ -d "$DESK" ]; then
    cp "$OUT" "$DESK/Token 轨迹.desktop"
    chmod +x "$DESK/Token 轨迹.desktop"
    echo "桌面图标: $DESK/Token 轨迹.desktop"
fi
echo "Exec = $DIR/start.sh"
