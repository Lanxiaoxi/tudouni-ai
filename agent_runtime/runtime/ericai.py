r"""`--ericai`：内置的 Ericsson AI 登录与 token 刷新。

## 它解决什么

`providers.ericai.api_key` 里存的是一把 JWT，EricSSO 签发，大约 1 小时过期。过期之后
模型请求会返回鉴权失败。这个模块让 `tudouni --tui --ericai`（或老 CLI `--ericai`）
在启动时把这件事做掉：检查 token 还新不新，不新就取一把新的 **原子写回**
`~/.tudouni/config.json`，然后才进界面。

## 不依赖 ericai 包，也不依赖任何外部脚本

这套 SSO 的本质是微软标准的 MSAL 流程（ericai 包只是给它包了层壳）。我们用
`azure-identity`（PyPI 公共包）直接实现，参数是 ericai 公开的常量（tenant / client /
scope），不 import ericai、也不调用外面的刷新脚本。`azure-identity` 在**函数内延迟
import**：只有真正跑 `--ericai` 才会加载它，没装 azure-identity 时其它路径不受影响
（`tests/test_imports.py` 那条"每个模块都能导入"也是据此成立的）。

## 登录 vs 刷新：一套流程，两级处置

- **刷新（非交互）**：已有登录会话（`%LOCALAPPDATA%\.IdentityService\EricAI.cache.nocae`
  里的 refresh token + 用户目录里的 `ericai_authrecord`）时，静默换新 token，全程不打扰；
- **登录（交互）**：缓存里没有可用的会话、或 refresh token 已失效时，走 device code
  流程 —— 打印验证链接和验证码，你在浏览器里验证后自动继续，并把新的 authrecord
  存到 `~/.tudouni/ericai_authrecord`，之后就一直静默刷新。

## 复用的关键：缓存名沿用 `EricAI.cache`

微软的持久化缓存名决定了"和谁共享登录会话"。沿用名字，就能直接复用你之前用 ericai
登录过的那份会话 —— 首次跑 `--ericai` 都不用重新登录，缓存里那把 refresh token 直接
能用。换成别的名字反而要重新登录一次。名字里带 "EricAI" 只是沿用文件名，内容是完全
标准的微软缓存，谁写谁读，这不构成对 ericai 包的依赖。

## 失败处置：绝不拦启动

刷新/登录失败一律**降级出声**：打一句说明到 stderr，保留旧 token 继续启动。因为 token
是否真的废了，最终由模型请求报错时才知道 —— 为一把还没废的 token 让整个 TUI 起不来，
是最坏的取舍。真正该停下的事（config 写坏、providers 形状错）由 `userconfig` / `catalog`
的校验本来就拦在装配之前。
"""

from __future__ import annotations

import base64
import json
import sys
import time
from pathlib import Path
from typing import Any

from agent_runtime import paths, userconfig

# 刷新哪条路由。EricAI 的 base_url / api_key 就住在这里。
PROVIDER = "ericai"

# --- EricAI 后端的公开常量（ericai 包 constants 里的同一个值，这里不再 import 它） ---
_TENANT_ID = "92e84ceb-fbfd-47ab-be52-080c6b87953f"
_CLIENT_ID = "b46aa582-485a-4a7d-b30e-552dbd790b16"
_SCOPE = f"api://{_CLIENT_ID}/API"
# 沿用 ericai 的缓存名，见模块 docstring "复用的关键"。
_CACHE_NAME = "EricAI.cache"
# authrecord 在用户级目录下的文件名（原来 ericai 放 cwd 相对路径，这里搬到 user_config_dir）。
_AUTHREC_NAME = "ericai_authrecord"

# 剩余有效时间低于这个秒数就刷新。
REFRESH_THRESHOLD = 600.0


def _authrec() -> Path:
    return paths.user_config_dir() / _AUTHREC_NAME


# --- 过期判断：JWT 自己解，不依赖第三方 jwt 库（azure 只负责拿 token） -------------


def decode_expiry(token: str, now: float | None = None) -> float | None:
    """从 JWT 里解出 `exp`（Unix 秒）。解不出返回 `None`。

    不依赖第三方 jwt 库：JWT 的 payload 就是一段 base64url 的 JSON，自己解。
    `now` 参数只给测试用 —— 正常调用不传，用真实时间。
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
      让刷新流程去碰一次运气，比留着它赌"可能还好"更实在（反正失败不拦启动）；
    - 剩余时间 < threshold：刷；
    - 否则：不刷。
    """
    exp = decode_expiry(token, now=now)
    if exp is None:
        return True
    current = time.time() if now is None else now
    return exp - current < threshold


def _remaining(token: str, now: float | None = None) -> float | None:
    exp = decode_expiry(token, now=now)
    if exp is None:
        return None
    current = time.time() if now is None else now
    return exp - current


# --- 登录 / 刷新核心（azure-identity，延迟 import） ------------------------------


def _load_authrecord(authrec: Path) -> Any:
    """读 authrecord；不存在或损坏都按"没有"处理（返回 None，交给登录流程）。"""
    from azure.identity import AuthenticationRecord

    if not authrec.is_file():
        return None
    try:
        return AuthenticationRecord.deserialize(authrec.read_text(encoding="utf-8"))
    except Exception:
        return None


def _save_authrecord(authrec: Path, record: Any) -> None:
    authrec.parent.mkdir(parents=True, exist_ok=True)
    authrec.write_text(record.serialize(), encoding="utf-8")


def _device_code_prompt(verification_uri: str, user_code: str, expires_on: Any) -> None:
    """把 device code 的登录指引打到 stderr（进界面之前，普通终端上看得到）。"""
    print("\n[ericai] 需要完成一次 Eric AI 登录（之后就一直静默刷新）：", file=sys.stderr, flush=True)
    print(f"[ericai]   打开： {verification_uri}", file=sys.stderr, flush=True)
    print(f"[ericai]   验证码： {user_code}", file=sys.stderr, flush=True)
    print(f"[ericai]   验证码有效期至 {expires_on}，完成后自动继续……", file=sys.stderr, flush=True)


def _obtain_token(authrec: Path, timeout: float = 30.0) -> str:
    """取一把新 token。先试非交互（用缓存），缓存不够就交互登录。失败抛异常由调用方降级。

    timeout: 单次操作的超时时间（秒）。超过后会抛出 TimeoutError。
    """
    import threading
    from concurrent.futures import TimeoutError as FuturesTimeoutError

    result = {}
    exc_info = {}

    def _do_obtain():
        """实际的 token 获取逻辑，在子线程中运行。"""
        try:
            from azure.identity import (
                AuthenticationRecord,
                DeviceCodeCredential,
                TokenCachePersistenceOptions,
            )
            from azure.identity._exceptions import AuthenticationRequiredError

            cache_options = TokenCachePersistenceOptions(
                name=_CACHE_NAME, allow_unencrypted_storage=True
            )
            record = _load_authrecord(authrec)

            # 1) 非交互：有 authrecord 就带上。disable_automatic_authentication=True 保证
            #    需要交互时抛 AuthenticationRequiredError，而不是擅自弹登录。
            noninteractive = DeviceCodeCredential(
                client_id=_CLIENT_ID,
                tenant_id=_TENANT_ID,
                cache_persistence_options=cache_options,
                authentication_record=record if record is not None else None,
                disable_automatic_authentication=True,
            )
            try:
                new_record = noninteractive.authenticate(scopes=[_SCOPE])
                _save_authrecord(authrec, new_record)
                result["token"] = noninteractive.get_token(_SCOPE).token
                return
            except AuthenticationRequiredError:
                pass  # 缓存不够用，落到交互登录

            # 2) 交互登录：device code 流程，在浏览器里验证。验证成功后 authrecord 更新，
            #    下次 --ericai 就能静默刷新了。
            interactive = DeviceCodeCredential(
                client_id=_CLIENT_ID,
                tenant_id=_TENANT_ID,
                cache_persistence_options=cache_options,
                prompt_callback=_device_code_prompt,
            )
            new_record = interactive.authenticate(scopes=[_SCOPE])
            _save_authrecord(authrec, new_record)
            result["token"] = interactive.get_token(_SCOPE).token
        except Exception as e:
            exc_info["exc"] = e

    t = threading.Thread(target=_do_obtain, daemon=True)
    t.start()
    t.join(timeout=timeout)

    if t.is_alive():
        raise TimeoutError(f"登录/刷新超时（>{timeout}秒），可能网络不通或服务不可用")
    if "exc" in exc_info:
        raise exc_info["exc"]
    if "token" not in result:
        raise RuntimeError("token 获取未返回结果，也未抛出异常（异常）")
    return result["token"]


# --- 写回 config：只动 api_key 一处，原子替换 ------------------------------------


def _write_back(path: Path, new_key: str) -> None:
    """把 `providers.ericai.api_key` 换成 `new_key`，**原子写回**。

    读的是**原始 JSON**（不是 `userconfig.read()` 抽出来的那几个字段）：`$comment`
    和别的顶层键都得原样保留 —— 只改 api_key 这一处，别的什么都不许动。键序由
    dict 的插入序天然保留，缩进统一成 2 格（模板本来就是 2 格）。

    原子性：先写同目录下的临时文件，再 `os.replace`。同文件系统内 replace 是原子
    的 —— 写到一半断电不会留下一份半截的 config（这个文件里装着密钥，半截文件
    等于数据丢失）。
    """
    import os
    import tempfile

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


# --- 启动动作 ------------------------------------------------------------------


def ensure(config: userconfig.UserConfig | None = None,
           *, threshold: float = REFRESH_THRESHOLD) -> str:
    """`--ericai` 的启动动作：检查，必要时刷新（缓存不够就交互登录），写回 config。

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

    # 需要新 token。
    try:
        new_key = _obtain_token(_authrec())
    except Exception as exc:
        # 登录/刷新失败。可能的原因：网络、缓存锁、用户取消登录……
        # 保留旧 token 继续启动，并把怎么修说清楚。
        return (
            f"[ericai] 登录/刷新失败：{exc}（旧 token 继续用；"
            f"可重跑本命令让登录流程再走一遍）"
        )

    # 新 token 至少要能被解出 exp —— 不是 JWT 的话，宁可拒收：
    # 把一把解不出过期时间的字符串写进 config，会让人在鉴权失败时无从排查。
    new_exp = decode_expiry(new_key)
    if new_exp is None:
        return (
            f"[ericai] 拿到的 token 解不出 exp —— 没写回，旧 token 继续用"
        )
    if new_exp <= time.time():
        return (
            f"[ericai] 拿到的 token 已经过期（可能还是旧的那把）—— 没写回，"
            f"旧 token 继续用"
        )

    try:
        _write_back(cfg.path, new_key)
    except (OSError, userconfig.UserConfigError) as exc:
        return f"[ericai] 新 token 拿到了，但写不回 config：{exc}（旧 token 继续用）"

    return f"[ericai] token 已刷新（新 token 剩余约 {(new_exp - time.time()) / 60:.0f} 分钟）"


__all__ = [
    "PROVIDER",
    "REFRESH_THRESHOLD",
    "decode_expiry",
    "ensure",
    "needs_refresh",
]