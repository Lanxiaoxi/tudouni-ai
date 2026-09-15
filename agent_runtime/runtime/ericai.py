"""`--ericai`：启动时自动刷新 Ericsson AI 的 token。

## 它解决什么

`providers.ericai.api_key` 里存的是一把 JWT，EricSSO 签发，大约 1 小时过期。过期之后
模型请求会返回鉴权失败，而"刷新"这个动作本身不难（用缓存的 MSAL 凭据非交互换一把
新的），难在它每次都要人手动跑一遍、再把结果抄回 config —— 于是它被拖成了隔一小时
一次的手工活。

这个模块让 `tudouni --tui --ericai`（或老 CLI 直连那支）在启动时把这件事做掉：
检查 token 还新不新，不新就调用外部刷新脚本，拿到新 token **原子写回**
`~/.tudouni/config.json`，然后才进界面。

## 刷新脚本从哪来：config 的 `scripts` 段，不写死在代码里

刷新要用 `ericai` 包（Ericsson 内部包，tudouni 自己的依赖里没有）。与其把它装进来，
不如调外部进程 —— 但**脚本路径不写死在代码里**，而是放在 config 的 `scripts` 段：

```jsonc
{
  "scripts": {
    "ericai_refresh_token": "\"C:\\...\\python.exe\" \"C:\\...\\refresh_token.py\""
  }
}
```

契约（`config.example.json` 的 `$comment` 里也写着，这里是实现依据）：

  1. 值是一条**命令行**，用平台 shell 执行（Windows 上是 cmd.exe；路径带空格就自己
     用引号包起来）；
  2. 脚本要能**非交互**地拿到一把新 token —— 它自己负责把 cwd 切到能读到认证凭据的
     地方（见下面"为什么是外部脚本"）；
  3. 新 token 打在 **stdout 的最后一个非空行**：前面随便打印多少说明文字都行，最后
     一行必须是 token 本身。

## 为什么是"外部脚本"，而不是直接 import ericai

tudouni 的依赖里没有 `ericai`，也不该有：它是 Ericsson 内部包，把它装进 tudouni 等于
让这个运行时依赖一个公司内网才有的东西。而调外部进程的一条代价是**认证上下文在脚本
那一侧** —— 非交互刷新靠的是 MSAL 持久化缓存（Windows 上是 Credential Manager）＋
cwd 附近的 `.ericai_authrecord`，这两样都存在 ericAiClientDemo 那个目录里，tudouni
从任意目录启动都够不着。所以契约 2 才写"脚本自己负责"：用户写的那个脚本（比如
ericAiClientDemo 里的 refresh_token.py）第一步 `os.chdir(脚本所在目录)`，就绕开了
"tudouni 不知道凭据在哪"这件事。tudouni 只负责：调用它、读它 stdout 最后一行、写回
config。

## 失败处置：绝不拦启动

刷新失败（脚本没配、跑挂了、输出不是 JWT）一律**降级出声**：打一句说明到 stderr，
保留旧 token 继续启动。因为 token 是否真的废了，最终由模型请求报错时才知道 ——
为一把还没废的 token 让整个 TUI 起不来，是最坏的取舍。真正该停下的事（config 写坏、
providers 形状错）由 `userconfig` / `catalog` 的校验本来就拦在装配之前。
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import tempfile
import time
from pathlib import Path

from agent_runtime import userconfig

# 刷新哪条路由。EricAI 的 base_url / api_key 就住在这里。
PROVIDER = "ericai"
# scripts 段里的键名。**脚本路径只从配置来，代码里不写死任何路径。**
SCRIPT_KEY = "ericai_refresh_token"
# 剩余有效时间低于这个秒数就刷新。用户拍板采纳的默认：10 分钟。
REFRESH_THRESHOLD = 600.0
# 外部脚本最多跑多久。刷新是启动路径上的一步，不该让一个挂死的脚本拖住整个启动。
SCRIPT_TIMEOUT = 120


def decode_expiry(token: str, now: float | None = None) -> float | None:
    """从 JWT 里解出 `exp`（Unix 秒）。解不出返回 `None`。

    不依赖第三方 jwt 库（tudouni 的依赖里没有）：JWT 的 payload 就是一段 base64url
    的 JSON，自己解。`now` 参数只给测试用 —— 正常调用不传，用真实时间。
    """
    text = (token or "").strip()
    if not text:
        return None
    parts = text.split(".")
    if len(parts) != 3:
        return None
    try:
        payload = parts[1]
        # JWT 的 base64url 是**去 padding** 的，补回去再解。
        payload += "=" * (-len(payload) % 4)
        decoded = base64.urlsafe_b64decode(payload)
        data = json.loads(decoded.decode("utf-8"))
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    exp = data.get("exp")
    return float(exp) if isinstance(exp, (int, float)) and exp > 0 else None


def needs_refresh(token: str, now: float | None = None,
                  threshold: float = REFRESH_THRESHOLD) -> bool:
    """这把 token 该不该刷新。

    - 空串 / 解不出 `exp`：**按"该刷"处理**。解不出说明它现在长什么样我们不知道，
      让刷新脚本去碰一次运气，比留着它赌"可能还好"更实在（反正失败不拦启动）；
    - 剩余时间 < threshold：刷；
    - 否则：不刷。
    """
    exp = decode_expiry(token, now=now)
    if exp is None:
        return True
    current = time.time() if now is None else now
    return exp - current < threshold


def _run_script(command: str, *, timeout: float = SCRIPT_TIMEOUT) -> str:
    """执行刷新命令行，返回 **stdout 的最后一个非空行**（那就是 token）。

    契约见模块 docstring。失败一律抛 `RuntimeError`，由调用方降级处理。
    """
    try:
        proc = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"刷新脚本超时（{timeout:.0f} 秒还没跑完）") from None
    except OSError as exc:
        raise RuntimeError(f"跑刷新脚本失败：{exc}") from None

    if proc.returncode != 0:
        tail = (proc.stderr or "").strip().splitlines()
        tail = tail[-3:] if tail else [""]
        raise RuntimeError(
            f"刷新脚本退出码 {proc.returncode}（stderr 末尾：{' / '.join(tail)}）"
        )

    lines = [line.strip() for line in (proc.stdout or "").splitlines() if line.strip()]
    if not lines:
        raise RuntimeError("刷新脚本跑完了，但 stdout 上一行都没有（最后一行应是新 token）")
    return lines[-1]


def _write_back(path: Path, new_key: str) -> None:
    """把 `providers.ericai.api_key` 换成 `new_key`，**原子写回**。

    读的是**原始 JSON**（不是 `userconfig.read()` 抽出来的那几个字段）：`$comment`
    和别的顶层键都得原样保留 —— 只改 api_key 这一处，别的什么都不许动。键序由
    dict 的插入序天然保留，缩进统一成 2 格（模板本来就是 2 格）。

    原子性：先写同目录下的临时文件，再 `os.replace`。同文件系统内 replace 是原子
    的 —— 写到一半断电不会留下一份半截的 config（这个文件里装着密钥，半截文件
    等于数据丢失）。
    """
    raw = userconfig._read_json_object(path)
    providers = raw.setdefault("providers", {})
    item = providers.setdefault(PROVIDER, {})
    item["api_key"] = new_key

    fd, tmp_name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(raw, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _remaining(token: str, now: float | None = None) -> float | None:
    exp = decode_expiry(token, now=now)
    if exp is None:
        return None
    current = time.time() if now is None else now
    return exp - current


def ensure(config: userconfig.UserConfig | None = None,
           *, threshold: float = REFRESH_THRESHOLD) -> str:
    """`--ericai` 的启动动作：检查，必要时刷新，写回 config。

    返回一句给人看的话（打印到 stderr 或 stdout 由调用方定）。**任何失败都不抛异常**
    —— 返回说明文字，旧 token 原样留着，启动继续。
    """
    try:
        cfg = userconfig.read() if config is None else config
    except userconfig.UserConfigError as exc:
        return f"[ericai] 读 config 失败：{exc}（跳过刷新）"

    if not cfg.found or not cfg.providers:
        return "[ericai] config 里没有 providers —— --ericai 需要配置 providers.ericai"

    provider = cfg.providers.get(PROVIDER)
    if not isinstance(provider, dict):
        return f"[ericai] providers 里没有 {PROVIDER} 这条路由 —— --ericai 用不上"

    key = provider.get("api_key") or ""
    if not needs_refresh(key, threshold=threshold):
        remain = _remaining(key)
        mins = max(0.0, remain or 0.0) / 60
        return f"[ericai] token 还有效（剩余约 {mins:.0f} 分钟），不用刷新"

    command = (cfg.scripts.get(SCRIPT_KEY) or "").strip() if cfg.scripts else ""
    if not command:
        return (
            f"[ericai] token 需要刷新，但 config 的 scripts 段没配 {SCRIPT_KEY!r} —— "
            f"旧 token 继续用，手动刷新一次吧"
        )

    try:
        new_key = _run_script(command)
    except RuntimeError as exc:
        return f"[ericai] 刷新失败：{exc}（旧 token 继续用）"

    # 新 token 至少要能被解出 exp —— 脚本 stdout 最后一行不是 JWT 的话，宁可拒收：
    # 把一把解不出过期时间的字符串写进 config，会让人在鉴权失败时无从排查。
    new_exp = decode_expiry(new_key)
    if new_exp is None:
        return (
            f"[ericai] 脚本输出了东西，但最后一行不像 JWT（解不出 exp）—— 没写回，"
            f"旧 token 继续用"
        )
    if new_exp <= time.time():
        return (
            f"[ericai] 脚本给的 token 已经过期（可能还是旧的那把）—— 没写回，"
            f"旧 token 继续用"
        )

    try:
        _write_back(cfg.path, new_key)
    except (OSError, userconfig.UserConfigError) as exc:
        return f"[ericai] 新 token 拿到了，但写不回 config：{exc}（旧 token 继续用）"

    return f"[ericai] token 已刷新（新 token 剩余约 {(new_exp - time.time()) / 60:.0f} 分钟）"


__all__ = [
    "PROVIDER",
    "SCRIPT_KEY",
    "REFRESH_THRESHOLD",
    "SCRIPT_TIMEOUT",
    "decode_expiry",
    "ensure",
    "needs_refresh",
]