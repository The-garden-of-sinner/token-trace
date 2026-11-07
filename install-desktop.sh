#!/bin/bash
# 生成并安装桌面启动器：把模板里的 @EXEC@ 换成本机 start.sh 的绝对路径，
# 同时把项目图标装进用户图标目录（模板里写的是图标名 token-trace）。
# 克隆到任意目录后跑一次即可；不会修改仓库里的模板。
set -u
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC="$DIR/token-trace.desktop"
OUT="$HOME/.local/share/applications/token-trace.desktop"

mkdir -p "$(dirname "$OUT")"
# 路径含空格时必须给 Exec 加引号；同时转义 sed 替换串里的 \ | &
ESC="${DIR//\\/\\\\}"
ESC="${ESC//|/\\|}"
ESC="${ESC//&/\\&}"
sed "s|@EXEC@|\"$ESC/start.sh\"|g" "$SRC" > "$OUT"
chmod +x "$OUT"
echo "应用菜单: $OUT"

ICON_DIR="$HOME/.local/share/icons/hicolor/256x256/apps"
mkdir -p "$ICON_DIR"
cp "$DIR/assets/token-trace.png" "$ICON_DIR/token-trace.png"
echo "应用图标: $ICON_DIR/token-trace.png"

DESK="$(xdg-user-dir DESKTOP 2>/dev/null || echo "$HOME/Desktop")"
if [ -d "$DESK" ]; then
    cp "$OUT" "$DESK/Token 轨迹.desktop"
    chmod +x "$DESK/Token 轨迹.desktop"
    echo "桌面图标: $DESK/Token 轨迹.desktop"
fi
echo "Exec = \"$DIR/start.sh\""
