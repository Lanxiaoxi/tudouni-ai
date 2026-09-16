"""一次工具调用的全程追踪：每一步实际发出去的载荷、历史、盘上的东西。

它用真的 Agent / ContextManager / ArtifactStore / Renderer，只把模型换成脚本，
并且在**模型被调用的那一刻**把载荷抄一份下来 —— 这里所有数字都是从真实对象上量
的，不是手写的示意图。

跑：

```sh
uv run python scripts/walkthrough_context.py
```

它跑完八段：新会话 → 请求 1（那时还不知道文件内容）→ 工具返回后正文落盘、历史里
换成一句引用 → 请求 2（文件正文在这一份里）→ 会话文件逐行 → 又聊两轮 →
把预算压到 700/400 看降级真的发生 → 盘上最终留下什么。

**为什么它是个脚本而不是文档里的一段**：这段行为里"哪一步看到什么"最容易讲错
（写说明时我自己就错过两次 —— 把索引那一格说成了正文、把请求 1 说成已经带了
文件内容）。脚本量出来的数字不会漂，改完之后重跑一遍就知道有没有说错。

临时工作区建在 `.tmpctx/`（已 ignore），跑完自己删掉。
"""

import json
import os
import pathlib
import shutil
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "tests"))
sys.path.insert(0, str(REPO))

WORK = REPO / ".tmpctx" / "walkthrough"
shutil.rmtree(WORK, ignore_errors=True)
WORK.mkdir(parents=True)

from agent_runtime.agents import Agent  # noqa: E402
from agent_runtime.context import (  # noqa: E402
    ArtifactStore, ContextBudget, ContextManager, default_processor,
)
from agent_runtime.models.base import ChatModel  # noqa: E402
from agent_runtime.models.types import ModelResponse  # noqa: E402
from agent_runtime.security import PermissionPolicy  # noqa: E402
from agent_runtime.state import JsonSessionStore, Session  # noqa: E402
from agent_runtime.tools.builtin.filesystem import ListFilesArgs  # noqa: E402
from agent_runtime.tools.tool import RiskLevel, Tool, ToolRegistry  # noqa: E402
from fakes import usage  # noqa: E402

FILE = "".join(f"line{i:03d}: 这是第 {i} 行，里面有一些中文和 code_{i}\n"
               for i in range(1, 121))


def rule(title):
    print()
    print("=" * 78)
    print(title)
    print("=" * 78)


def size(text):
    return f"{len(text)} 字符"


# Agent 自己拼载荷，这里从它手上接一份 —— 打出来的就是真发出去的那一份。
ORIGINAL_PAYLOAD = Agent._payload
RECORDED = []


def traced_payload(self, session, max_steps, step):
    payload = ORIGINAL_PAYLOAD(self, session, max_steps, step)
    RECORDED.append(payload)
    return payload


Agent._payload = traced_payload


def show_payload(payload, *, what):
    print(f"{what} —— 一共 {len(payload)} 条：\n")
    for i, m in enumerate(payload):
        content = m.get("content") or ""
        head = content[:52].replace("\n", "\\n")
        tail = "…" if len(content) > 52 else ""
        note = ""
        if m.get("tool_calls"):
            # 历史里的形状是 OpenAI 那一份（function.name），不是适配器归一化后的
            names = [c.get("function", {}).get("name") for c in m["tool_calls"]]
            note = f"  → 调 {names}"
        print(f"  [{i}] {m['role']:9} {size(content):12} {head}{tail}{note}")


class ScriptedModel(ChatModel):
    """每轮都：第一步读文件，第二步给出答复。"""

    def __init__(self):
        self.n = 0

    def complete(self, messages, tools=None):
        self.n += 1
        if self.n % 2 == 1:
            return ModelResponse(
                content=None,
                tool_calls=[{"id": f"call_{self.n}", "name": "read_file",
                             "arguments": json.dumps({"path": "main.py"})}],
                usage=usage(prompt=312, cached=256, completion=24),
            )
        return ModelResponse(content="main.py 一共 120 行，是个示例文件。",
                             usage=usage(prompt=180, cached=0, completion=18))

    @property
    def model(self):
        return "demo-model"


store = ArtifactStore(WORK / "artifacts")
manager = ContextManager(store, budget=ContextBudget(max_tokens=8000))
registry = ToolRegistry()
registry.register(Tool(
    name="read_file", description="读文件", risk=RiskLevel.LOW,
    args_model=ListFilesArgs, handler=lambda **kw: FILE, parallel_safe=True,
))
session = Session.new("demo", workspace=WORK)
agent = Agent(ScriptedModel(), registry, PermissionPolicy({RiskLevel.LOW}),
              context=manager, processor=default_processor())
saved = JsonSessionStore(WORK / "sessions")
agent.on_checkpoint = saved.save

# ---------------------------------------------------------------- 0
rule("0. 起点：新会话")
print(f"session.messages：{len(session.messages)} 条")
for i, m in enumerate(session.messages):
    print(f"  [{i}] {m['role']:9} {size(m['content'])}")
print(f"\nContext 里的条目：{len(manager.items())} 条   盘上的 Artifact：{len(store)} 份")
print(f"工作区里那份 main.py：{size(FILE)}")

# ---------------------------------------------------------------- 1
rule("1. 用户说『读一下 main.py』，Agent 走两步")
print("这一步里发生的事，按顺序：")
print("  a) session.messages.append(user)              —— 用户这句话进历史")
print("  b) mark_context_messages()                    —— 给 stable/pinned 立标记")
print("  c) _payload(step=0) → 请求 1                   ——  模型返回 read_file 调用")
print("  d) 读磁盘 → ToolResultProcessor → Artifact     —— 正文落盘")
print("  e) messages.append(tool 引用)                  —— 历史里只留一句引用")
print("  f) _payload(step=1) → 请求 2                   —— 模型据此收尾")
RECORDED.clear()
answer = agent.run(session, "读一下 main.py")
print(f"\n答案：{answer}")

# ---------------------------------------------------------------- 2
rule("2. 请求 1：模型看到的载荷（此时还不知道文件内容）")
show_payload(RECORDED[0], what="请求 1")
print("\n只有 system + 用户那句话 + 一条临时提示（步数）。文件内容还不在任何地方。")

# ---------------------------------------------------------------- 3
rule("3. 工具返回之后：正文落盘，历史里换成一句引用")
print("磁盘上：")
for f in sorted((WORK / "artifacts" / "refs").glob("*")):
    print(f"  {f.name}   {size(f.read_text(encoding='utf-8'))}")
art = store.all()[0]
print("\n这份 Artifact 的索引（manifest.json 里那一格）：")
print("  " + json.dumps(art.to_json(), ensure_ascii=False, indent=2).replace("\n", "\n  "))
tool_msg = [m for m in session.messages if m["role"] == "tool"][0]
print("\nsession.messages 里那条 tool 消息：")
print(f"  content      {tool_msg['content']}")
print(f"  artifact_id  {tool_msg['artifact_id']}")
print(f"  → 引用 {size(tool_msg['content'])}；正文 {size(FILE)}")

# ---------------------------------------------------------------- 4
rule("4. 请求 2：模型看到的载荷（文件正文在这一份里）")
show_payload(RECORDED[1], what="请求 2")
print(f"\n[3] 那条 tool 的内容是 Renderer 从 ArtifactStore 现取的（档位 full），")
print(f"    {size(RECORDED[1][3]['content'])} —— 和原来的文件正文逐字节一致。")

# ---------------------------------------------------------------- 5
rule("5. 落盘：会话文件里每一行是什么")
print(f"{WORK / 'sessions' / 'demo.jsonl'}\n")
for line in (WORK / "sessions" / "demo.jsonl").read_text(encoding="utf-8").splitlines():
    rec = json.loads(line)
    kind, raw = rec.get("t"), len(json.dumps(rec, ensure_ascii=False))
    if kind == "msg":
        m = rec["m"]
        extra = f"  artifact_id={m['artifact_id']}" if m.get("artifact_id") else ""
        print(f"  {kind:5} {m['role']:9} 这一行 {raw:>5} 字节{extra}")
    elif kind == "ctx":
        items = rec["c"]["items"]
        print(f"  {kind:5} version={rec['c']['version']}  {len(items)} 条  "
              f"这一行 {raw} 字节")
        for it in items:
            print(f"        · {it['artifact_id']}  {it['representation']:6}"
                  f" zone={it['zone']:8} pinned={it['pinned']}")
    else:
        print(f"  {kind:5} {json.dumps(rec, ensure_ascii=False)[:66]}")

# ---------------------------------------------------------------- 6
rule("6. 又聊两轮：历史变长了，但先看『引用今天长什么样』")
RECORDED.clear()
agent.run(session, "它一共几行？")
agent.run(session, "再看一眼 main.py 的前几行")
print("第三轮（『它一共几行？』）的请求：\n")
show_payload(RECORDED[0], what="第三轮请求 1")
print("\n第三轮的请求 2：\n")
show_payload(RECORDED[1], what="第三轮请求 2")
print("\n注意 [3] 那条 tool —— 它还是完整正文。历史里存的是引用，而模型每一步")
print("拿到的都是现渲染出来的正文：这两件事是分开的。")

# ---------------------------------------------------------------- 7
rule("7. 把窗口调小到 700 token：降级真的发生")
print("先把预算压到 700，跑一轮 —— 中间每一步都量一次这条 tool 实际发出多少：\n")
manager.budget = ContextBudget(max_tokens=700, reserve=0, headroom=0.0)
RECORDED.clear()
agent.run(session, "再读一次")
payload = RECORDED[0]
for i, m in enumerate(payload):
    content = m.get("content") or ""
    if m["role"] == "tool":
        print(f"  [{i}] tool  这一轮发给模型的是 {size(content)}：\n")
        print("\n".join("        " + ln for ln in content.splitlines()[:6]))
        if content.count("\n") > 6:
            print(f"        …（还有 {content.count(chr(10)) - 6} 行）")
    else:
        head = content[:44].replace("\n", "\\n")
        print(f"  [{i}] {m['role']:9} {size(content):12} {head}")
item = manager.items()[0]
print(f"\nContext 里那一条：档位 {item.representation.value}  options={item.options}")
print(f"估算体积 {manager.last_estimate} token（限额 {manager.budget.effective_limit}）")

print("\n再把预算压到 400，并把每一步都走完（`fit()` 一次只降一档）：\n")
manager.budget = ContextBudget(max_tokens=400, reserve=0, headroom=0.0)
from agent_runtime.context.renderer import ContextRenderer  # noqa: E402
renderer = ContextRenderer(store, manager)
for step in range(1, 6):
    changed = manager.fit(renderer.render_item)
    out = renderer.render_item(item) or ""
    print(f"  第 {step} 次 fit：{item.representation.value:8} "
          f"{size(out):12} {out.splitlines()[0] if out else '（空）'}")
    if not changed:
        print("  → 已经塞得下了，停手。")
        break

print("\n**只降不升**：把预算调回 8000，再问一次『要不要降级』——")
before = item.representation.value
manager.budget = ContextBudget(max_tokens=8000)
changed = manager.fit(renderer.render_item)
print(f"  档位 {before} → {item.representation.value}   "
      f"fit() 返回 {len(changed)} 条改动（0 = 没动它）")
print("  预算松下来不会把它弹回 full —— 弹回去就是每轮一个不同的 prompt，")
print("  而 provider 的前缀缓存按最长公共前缀算（见 budget.py）。")

# ---------------------------------------------------------------- 8
rule("8. 盘上最终留下了什么")
for path in sorted(WORK.rglob("*")):
    if path.is_file():
        print(f"  {path.stat().st_size:>7}  {path.relative_to(WORK).as_posix()}")
print("\n对比：如果正文进历史，光那一份 7.4KB 的正文在会话文件里会被重复写进")
print("每一次落盘、并在每一次请求里重发。现在它只在这儿有一份。")

os.chdir(REPO)
shutil.rmtree(WORK, ignore_errors=True)
# 连 `.tmpctx/` 这个外壳一起收掉：留着它会让"跑完了吗"看起来像没跑完
# （上一次跑留下的空目录和这次刚建的那个长得一模一样）。
shutil.rmtree(REPO / ".tmpctx", ignore_errors=True)
Agent._payload = ORIGINAL_PAYLOAD
print("\n（临时目录已删）")
