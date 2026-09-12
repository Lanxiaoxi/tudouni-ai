"""跨进程协议：runtime 的"远程 API"。

**这一层为什么存在。** `frontends/` 里的每一个前端（CLI、TUI、将来的 Web）都只该
通过协议和 runtime 说话（决策 18）。这样做的回报是具体的：**客户端换语言、换框架、
换进程拓扑，对 runtime 完全不可见** —— 而这件事已经被"从 Ink 换成 Textual"和
"将来要加 Web"预演过两次了。

**这一层不许 import 前端**（由 `tests/test_imports.py` 的三条边界测试盯着）。

## 一、传输

`Transport`：一行 JSON ↔ 一个 dict。两端都是 UTF-8、`\n` 结尾、每条 flush。
子进程的 stdout 是**管道**不是终端，所以编码必须显式指定 —— 这一条不会因为"两侧
都是 Python"而消失。

## 二、协议服务器

`ProtocolServer` 是这一层的中心，它同时是三样东西：

  1. `Transport` 的读端循环（`serve()`）；
  2. `Runtime` 的持有者；
  3. **`Channels` 的提供者** —— 审批和提问那两条人机通道走的就是这块协议。

第 3 条解开了一个顺序上的环（见 `runtime/channels.py` 的 docstring）：
`Agent` 构造时就要 asker/questioner，而协议版的它们要能收发消息，那需要 Runtime
已经存在。所以 Runtime 只**接受**通道，`ProtocolServer` 用 `Pending` 把自己和
Runtime 接起来。

## 三、不变式

**`attach()` 之前不可能收到任何请求。** 这不是巧合：只有 runtime 会发请求，而
runtime 要等 `open_runtime(...)` 返回、再由 `attach()` 补上另一半。`Pending`
在没接上时**抛异常而不是阻塞** —— 一个静静等着的通道会把整个服务挂死，
而那种 bug 没有任何症状。
"""
