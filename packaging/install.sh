#!/bin/sh
# 把 tudouni 装到这台机器上。**只装给当前用户，不需要 root。**
#
# 用法：把压缩包完整解压，在解压出来的目录里跑
#
#     ./install.sh
#
# 装到哪儿：~/.local/opt/tudouni，并在 ~/.local/bin/tudouni 放一个软链接。
# 重复运行就是覆盖升级，不会留下第二份。
#
# 想换地方就设 TUDOUNI_PREFIX，例如 `TUDOUNI_PREFIX=/opt/tudouni ./install.sh`。

set -eu

SOURCE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PREFIX="${TUDOUNI_PREFIX:-$HOME/.local}"
APPDIR="$PREFIX/opt/tudouni"
BINDIR="$PREFIX/bin"
LINK="$BINDIR/tudouni"

say() { printf '%s\n' "$*"; }

say ''
say '  tudouni 安装'
say "    来源    $SOURCE"
say "    安装到  $APPDIR"
say ''

# --- 0. 确认这个脚本旁边真的有东西 ------------------------------------------
#
# 从压缩包里直接跑（没解压）时这里会立刻说清楚"先解压"，而不是让用户对着一句
# "找不到 tudouni" 发愣。
if [ ! -f "$SOURCE/tudouni" ] || [ ! -d "$SOURCE/_internal" ]; then
    say '  这个目录里没有 tudouni / _internal。' >&2
    say '  请**先把压缩包完整解压**，再运行解压出来的那个 install.sh。' >&2
    exit 1
fi

# --- 1. 拷贝 ----------------------------------------------------------------
if [ -d "$APPDIR" ]; then
    say '  已装过一份，覆盖升级……'
    rm -rf "$APPDIR"
fi
mkdir -p "$APPDIR" "$BINDIR"
# `cp -R src/. dst/` 而不是 `cp -R src dst`：后者在 dst 已存在时会再套一层。
cp -R "$SOURCE/tudouni" "$SOURCE/_internal" "$APPDIR/"
chmod +x "$APPDIR/tudouni"
say '  [1/4] 文件已就位'

# --- 2. 随包带的 ripgrep 要有执行位 -----------------------------------------
#
# 这一条不是多余的：那个二进制是作为**数据文件**被打进产物的，在某些解压工具/
# 文件系统（zip 不带权限位、或者挂载时用了 noexec）上落地就是 644。少了它
# `rg_binary()` 返回 None，于是 `grep` 工具**不注册** —— 而界面只会在启动时
# 打一行提示，模型则会改去猜文件名。所以这里补一刀。
if [ -d "$APPDIR/_internal/agent_runtime/tools/vendor/rg" ]; then
    find "$APPDIR/_internal/agent_runtime/tools/vendor/rg" -type f -name rg -exec chmod +x {} + 2>/dev/null || true
fi
say '  [2/4] 执行位已确认'

# --- 3. 放一个软链接 --------------------------------------------------------
#
# 装到 `opt/` 里、只把链接放进 `bin/`，是为了升级时不会出现"文件正在被使用"
# 那种半新半旧的状态（`rm -rf` 换的是整个目录，链接最后才改指）。
ln -sf "$APPDIR/tudouni" "$LINK"
say '  [3/4] 软链接已建好'

# --- 4. 确认这个二进制真的能跑 ----------------------------------------------
#
# `--help` 是**唯一一个不碰工作区**的调用：它在 argparse 里就结束了，不建
# `.tudouni/`、不读配置、不需要密钥。所以拿它当"装好了没有"的判据是干净的。
# 在 `/tmp` 里跑，连"当前目录被写进东西"都不必担心。
if ! ( cd "${TMPDIR:-/tmp}" && "$APPDIR/tudouni" --help >/dev/null 2>&1 ); then
    say '  装好了，但 tudouni --help 跑不起来。' >&2
    say '  这通常意味着包不完整或者平台不对（这个包只支持 x86_64 的 Linux）。' >&2
    exit 1
fi
say '  [4/4] 装好了，二进制能跑'

# --- 收尾 -------------------------------------------------------------------
say ''
say '  下一步'
case ":$PATH:" in
    *":$BINDIR:"*)
        say "    1) 开个新终端（$BINDIR 已经在 PATH 里）"
        ;;
    *)
        say "    1) $BINDIR 不在 PATH 里。把这一行加进 ~/.profile 或 ~/.bashrc："
        say ''
        say "         export PATH=\"$BINDIR:\$PATH\""
        say ''
        say '       然后开一个新终端。'
        ;;
esac
say '    2) cd 到你自己的项目目录，然后：'
say ''
say '         tudouni --tui'
say ''
say '   第一次运行会告诉你去哪填密钥。那份文件是：'
say '         ~/.tudouni/config.json'
say ''
say '   注意：**别在你自己的用户主目录（或者 / ）下启动它。** 工作区就是你'
say '   敲命令时所在的那个目录，程序在 home 下会拒绝启动 —— 那等于把整个'
say '   主目录（含 .ssh、浏览器数据）交给它。'
say ''
