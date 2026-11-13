#!/usr/bin/env bash
# 创建 GitHub Release 并上传三个附件：.deb、自包含的单文件 Windows .bat、SHA256SUMS
#
#   ./scripts/release.sh            使用 scripts/VERSION
#   ./scripts/release.sh <版本>      指定版本号（默认读 VERSION）
#
# 前置：远端已存在同版本 tag（本脚本不隐式改 git 历史），dist/ 里有 deb。
# 每次发版都用当前源码重建 bat，避免附件落后于代码。
set -eu

DIR="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
ROOT="$(cd "$DIR/.." && pwd)"
REPO="The-garden-of-sinner/token-trace"
VERSION="${1:-$(cat "$DIR/VERSION")}"
NOTES="${RELEASE_NOTES:-}"   # 可选：本次更新说明，插到 Release 正文「本次更新」一节
DEB="$ROOT/dist/token-trace_${VERSION}_all.deb"
BAT="$ROOT/dist/TokenTrace.bat"
SUMS="$ROOT/dist/SHA256SUMS"

[ -f "$DEB" ] || { echo "找不到 $DEB，先运行 ./scripts/build_deb.sh $VERSION"; exit 1; }

# 确认 tag 已在远端，否则 GitHub 会悄悄用默认分支 HEAD 造一个不受控的 tag
if ! git -C "$ROOT" ls-remote --exit-code --tags origin "refs/tags/v$VERSION" >/dev/null 2>&1; then
    echo "远端缺少 tag v$VERSION，请先打 tag 并推送："
    echo "  git -C $ROOT tag -a v$VERSION -m 'v$VERSION'"
    echo "  git -C $ROOT push origin v$VERSION"
    exit 1
fi

echo "重建图标与单文件 Windows 启动器 ..."
/usr/bin/python3 "$DIR/make_icon.py" >/dev/null
/usr/bin/python3 "$DIR/build_win_bat.py" >/dev/null
[ -f "$BAT" ] || { echo "生成 $BAT 失败"; exit 1; }
echo "生成完整性清单 ..."
/usr/bin/python3 "$DIR/make_checksums.py" >/dev/null
[ -f "$SUMS" ] || { echo "生成 SHA256SUMS 失败"; exit 1; }

TOKEN_FILE="${XDG_CONFIG_HOME:-$HOME/.config}/github/token"
[ -f "$TOKEN_FILE" ] || { echo "缺少 $TOKEN_FILE"; exit 1; }
GH_TOKEN="$(cat "$TOKEN_FILE")"

BODY="$(/usr/bin/python3 - "$VERSION" "$NOTES" <<'PY'
import json, sys
v, notes = sys.argv[1], sys.argv[2]
text = """## 安装

从本页 **Assets** 下载附件即可，不用 clone 仓库。README 的
[一键安装](https://github.com/The-garden-of-sinner/token-trace#2-安装与发行)
有可直接复制的命令。

**Linux（Debian / Ubuntu / 麒麟等）**

```bash
sudo dpkg -i token-trace_{v}_all.deb
```

装好后从应用菜单打开「Token 轨迹」，或终端执行 `token-trace`。
`token-trace stop` 停止，`token-trace status` 查看状态，`TRACE_PORT=9000 token-trace` 换端口。

**Windows（单文件）**

下载 `TokenTrace.bat` 双击即可 —— 自包含，**不需要 clone 仓库，也不需要其他文件**。
脚本自动寻找 Python 3.8+，把内置服务释放到 `%LOCALAPPDATA%\\TokenTrace\\`，
等待就绪后打开 http://127.0.0.1:8765。重复双击只开浏览器，不会多起进程。

首次运行执行 `TokenTrace.bat shortcut` 可创建带图标的桌面 / 开始菜单快捷方式；
`help` / `version` 查看用法与版本。命令行还支持 `stop` / `status`，
`TRACE_PORT=9000` 换端口，`TRACE_PYTHON` 指定 Python 路径。

## 完整性校验

Release 附 `SHA256SUMS`。`.bat` 是纯文本脚本，Windows 不支持对它做
Authenticode 签名，因此用哈希清单核对发布产物：

```bash
sha256sum -c SHA256SUMS
```
```powershell
Get-FileHash .\\TokenTrace.bat -Algorithm SHA256
```

## 说明

- 两个产物只读本机会话数据（WorkBuddy `~/.workbuddy`、Codex `~/.codex`、ZCode `~/.zcode`），不联网、不写源文件、不需要第三方包；外部平台目录不存在时自动跳过，不影响 WorkBuddy 主源。
- 页面零 JavaScript，每次只渲染一屏；全部调用与原文均可逐条回溯。
- **Windows 版尚未在真实 Windows 机器上验证**：中文/空格用户名、`-WindowStyle` 隐藏效果、
  中文路径传参等待目标机实测。Linux deb 内的源码已在本机实跑验证。
""".format(v=v)
if notes:
    text = text.replace("## 说明", "## 本次更新\n\n" + notes + "\n\n## 说明")
print(json.dumps({"tag_name": "v" + v, "name": "v" + v, "body": text,
                  "draft": False, "prerelease": False}, ensure_ascii=False))
PY
)"

echo "创建 release v$VERSION ..."
RESP="$(curl -sS -X POST \
  -H "Authorization: Bearer $GH_TOKEN" \
  -H "Accept: application/vnd.github+json" \
  "https://api.github.com/repos/$REPO/releases" \
  -d "$BODY")"
REL_ID="$(printf '%s' "$RESP" | /usr/bin/python3 -c 'import json,sys;print(json.load(sys.stdin).get("id",""))' 2>/dev/null || true)"
if [ -z "$REL_ID" ]; then
    echo "创建失败："; printf '%s\n' "$RESP" | head -20; exit 1
fi
echo "release id = $REL_ID"

upload() {
    local file="$1" ctype="$2" name="$3" resp url
    echo "上传 $name ..."
    resp="$(curl -sS -X POST \
      -H "Authorization: Bearer $GH_TOKEN" \
      -H "Content-Type: $ctype" \
      --data-binary "@$file" \
      "https://uploads.github.com/repos/$REPO/releases/$REL_ID/assets?name=$name")"
    url="$(printf '%s' "$resp" | /usr/bin/python3 -c 'import json,sys;print(json.load(sys.stdin).get("browser_download_url",""))' 2>/dev/null || true)"
    [ -n "$url" ] || { echo "上传失败："; printf '%s\n' "$resp" | head -20; exit 1; }
    echo "  $url"
}

upload "$DEB" "application/vnd.debian.binary-package" "$(basename "$DEB")"
upload "$BAT" "application/octet-stream" "$(basename "$BAT")"
upload "$SUMS" "text/plain" "SHA256SUMS"

echo
echo "发布完成：https://github.com/$REPO/releases/tag/v$VERSION"
