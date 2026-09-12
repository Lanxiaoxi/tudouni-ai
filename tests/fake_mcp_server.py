"""测试用的 MCP server（stdio 传输）。

**它不是 mock，是一个真的子进程**：真的从 stdin 按行读 JSON-RPC、真的往 stdout
写回应。理由和 tests/fakes.py 里那句"手写的假实现，不用 mock 库"是同一个 —— 传输层
要测的东西（分帧、并发读写、进程死了怎么办、超时）只有真起一个进程才测得出来，
而用 mock 去断言"我们调了 Popen"是自证。

stdout 上**只能有协议消息**：混一行日志进去就是某些真实 server 的毛病之一
（见 tools/mcp.py 里那句"stdout 上有一行不是 JSON"），要制造那种毛病用
--stderr-noise（stderr 是允许随便写的）。
"""

import argparse
import json
import sys

TOOLS = [
    {
        "name": "echo",
        "description": "把参数原样回给你",
        "inputSchema": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
    },
    {
        "name": "boom",
        "description": "总是失败（isError）",
        "inputSchema": {"type": "object"},
    },
    {
        "name": "bad_args",
        "description": "参数不合法（JSON-RPC 错误）",
        "inputSchema": {"type": "object"},
    },
    {
        "name": "nontext",
        "description": "只回非文本内容",
        "inputSchema": {"type": "object"},
    },
    # 需要净化的名字：OpenAI 的 function name 不接受 `.`、空格、`/`。
    {
        "name": "weird.name with/slash",
        "description": "名字需要净化",
        "inputSchema": {"type": "object"},
    },
]


def send(message: dict) -> None:
    sys.stdout.write(json.dumps(message, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def result(request_id, payload) -> None:
    send({"jsonrpc": "2.0", "id": request_id, "result": payload})


def failure(request_id, code, message) -> None:
    send({"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}})


def handle(message: dict, args) -> None:
    method = message.get("method")
    request_id = message.get("id")

    if method == "initialize":
        if args.no_initialize:
            failure(request_id, -32603, "这个 server 拒绝握手")
            return
        result(request_id, {
            "protocolVersion": args.protocol_version or "2024-11-05",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "fake-mcp", "version": "0.1"},
        })
        return

    if method == "notifications/initialized":
        return          # 通知，协议上就没有回应

    if method == "tools/list":
        if args.paginate:
            # 两页：第一页带 nextCursor，第二页没有（客户端必须跟着翻）。
            if not (message.get("params") or {}).get("cursor"):
                result(request_id, {"tools": TOOLS[:2], "nextCursor": "page-2"})
            else:
                result(request_id, {"tools": TOOLS[2:]})
            return
        result(request_id, {"tools": TOOLS})
        return

    if method == "tools/call":
        params = message.get("params") or {}
        name = params.get("name")
        arguments = params.get("arguments") or {}

        if name == "echo":
            result(request_id, {"content": [
                {"type": "text", "text": json.dumps(arguments, ensure_ascii=False)},
            ]})
        elif name == "boom":
            result(request_id, {
                "content": [{"type": "text", "text": "炸了：这个工具总是失败"}],
                "isError": True,
            })
        elif name == "bad_args":
            failure(request_id, -32602, "缺少必填参数 text")
        elif name == "nontext":
            result(request_id, {"content": [
                {"type": "image", "mimeType": "image/png", "data": "iVBORw0KGgo="},
                {"type": "text", "text": "只有这一句是文本"},
            ]})
        else:
            result(request_id, {"content": [
                {"type": "text", "text": f"收到了 {name} {json.dumps(arguments, ensure_ascii=False)}"},
            ]})
        return

    if request_id is not None:
        failure(request_id, -32601, f"没有 {method} 这个方法")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-initialize", action="store_true", help="握手时报错")
    parser.add_argument("--hang", action="store_true", help="收到什么都不回（测超时）")
    parser.add_argument("--exit-now", action="store_true", help="一起来就退出")
    parser.add_argument("--paginate", action="store_true", help="tools/list 分两页")
    parser.add_argument("--stderr-noise", action="store_true", help="往 stderr 写一行日志")
    parser.add_argument("--protocol-version", default=None, help="握手时回一个别的版本号")
    args = parser.parse_args()

    if args.stderr_noise:
        print("fake-mcp: 这是 server 自己的日志（应当原样落到调用方的 stderr 上）",
              file=sys.stderr, flush=True)

    if args.exit_now:
        return 3

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(message, dict):
            continue
        if args.hang:
            # 关键：**继续读 stdin**。不读的话客户端的写会被管道缓冲挡住，
            # 那测出来的是"写阻塞"而不是"等回应超时"。
            continue
        handle(message, args)

    return 0


if __name__ == "__main__":
    sys.exit(main())
