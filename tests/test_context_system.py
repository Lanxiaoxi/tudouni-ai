"""Context 系统：Artifact / ContextItem / 预算 / 渲染。

这个功能改的是**每一轮请求的载荷从哪来**，所以这里钉的不是"没崩"，而是四条不变量：

  1. **形状不变。** `full` 档渲染出来的 tool 正文和重构之前逐字节一致 —— provider
     的 tool 配对、前缀缓存的语义都靠这条（见 `context/renderer.py` 的模块 docstring）。
  2. **正文只有一份。** 会话历史里 tool 消息只剩一句引用；全文在 ArtifactStore。
     两处都有的话，"哪一份是真的"就没有答案了。
  3. **只降不升。** 预算降过档的 Artifact 不会因为下一轮 token 又够了而弹回 full
     —— 那正是缓存抖动的来源。
  4. **降级/失联都要出声。** 模型看不到全文时必须知道（表头里写着行号）；正文真的
     取不到时更不能给一个空串（那会被读成"这个工具没输出"）。

另有一条**兼容路径**要被钉住：重构之前落盘的会话里，tool 消息存的是全文。恢复它
们不该走第二套渲染路径，而该被 `hydrate` 收成 Artifact。
"""

import json
import re

import pytest

from agent_runtime.agents import Agent
from agent_runtime.context import (
    ArtifactStore,
    ContextBudget,
    ContextManager,
    ContextRenderer,
    Representation,
    Zone,
    default_processor,
)
from agent_runtime.context import ref
from agent_runtime.context.models import ArtifactSource, ContextItem, ContextState
from agent_runtime.models.base import ChatModel
from agent_runtime.models.types import ModelResponse
from agent_runtime.security import PermissionPolicy
from agent_runtime.state import JsonSessionStore, Session
from agent_runtime.tools.builtin.filesystem import ListFilesArgs
from agent_runtime.tools.tool import RiskLevel, Tool, ToolRegistry

from fakes import Collector, usage


# --- 公共脚手架 ---------------------------------------------------------------

def build(base, **kwargs):
    """一个 store + manager + renderer 的三件套。"""
    store = ArtifactStore(base / "artifacts", **kwargs)
    manager = ContextManager(store, **{k: v for k, v in kwargs.items()
                                      if k in _MANAGER_KNOBS})
    renderer = ContextRenderer(store, manager)
    return store, manager, renderer


# `build` 认得的那些属于 manager 的旋钮（其余原样给 store）。
_MANAGER_KNOBS = {
    "range_lines", "preview_lines", "range_ratio", "preview_ratio",
    "range_chars", "preview_chars",
}


def put(store, manager, text, **kwargs):
    """收一份正文并让它进 Context，返回那个 Artifact。"""
    artifact = store.create(text, **kwargs)
    manager.add_artifact(artifact)
    return artifact


def tool_texts(session) -> list[str]:
    return [m["content"] for m in session.messages if m["role"] == "tool"]


def tool_artifacts(session) -> list[str]:
    return [m["artifact_id"] for m in session.messages if m["role"] == "tool"]


def render_all(renderer):
    """`ContextManager.fit` 要的那个"按当前档位渲染"的口子。"""
    return renderer.render_item


# --- 1. ArtifactStore ----------------------------------------------------------

def test_the_same_content_gets_the_same_id(workdir):
    """id 由正文定（内容寻址）。

    设计原则第 10 条要求 id 稳定，而随机的 id（`art_7f3a…`）会让渲染出来的 prompt
    每一轮都不同 —— 前缀缓存全废，而且症状只会体现在账单上。
    """
    store, _, _ = build(workdir)
    first = store.create("同一段内容")
    second = store.create("同一段内容")          # 同一个来源（默认空来源）⇒ 去重

    assert first.artifact_id == second.artifact_id


def test_two_events_with_the_same_content_do_not_collide(workdir):
    """但"另一个文件的内容和它一样"是**另一份信息**，所以第二个换一个 id。

    合并它们会让"这次读的是哪个文件"在历史里丢失 —— 而那份历史是事后唯一能回答
    "它当时看到的是什么"的地方。
    """
    store, _, _ = build(workdir)
    first = store.create("内容", source=ArtifactSource(tool="read_file", path="a.py"))
    second = store.create("内容", source=ArtifactSource(tool="read_file", path="b.py"))

    assert first.artifact_id != second.artifact_id
    assert second.artifact_id.startswith(first.artifact_id)
    assert second.metadata.get("path") in (None, "")       # 来源只记在 source 上


def test_deduplication_survives_a_reload(workdir):
    """**重开会话之后，同一个文件再读一次不该多出一份 Artifact。**

    第一版把去重表只留给本进程新造的那些，于是每重启一次就多一份重复的正文 ——
    而症状只是磁盘上多几个文件，看不出错。
    """
    store, _, _ = build(workdir)
    source = ArtifactSource(tool="read_file", path="a.py")
    first = store.create("内容", source=source)

    reopened = ArtifactStore(workdir / "artifacts")
    reopened.load()
    again = reopened.create("内容", source=source)

    assert again.artifact_id == first.artifact_id
    assert len(reopened) == 1


def test_a_new_store_reads_the_artifacts_back(workdir):
    """跨进程：正文在盘上，索引也在。"""
    store, _, _ = build(workdir)
    artifact = store.create("持久化的正文", type="file", metadata={"path": "a.py"})

    reopened = ArtifactStore(workdir / "artifacts")
    assert reopened.load() == []
    back = reopened.get(artifact.artifact_id)

    assert back is not None
    assert back.chars == len("持久化的正文")
    assert reopened.content(artifact.artifact_id) == "持久化的正文"


def test_a_missing_content_file_is_reported_not_swallowed(workdir):
    """正文文件被人删了 ⇒ 那份 Artifact 从索引里消失，**但那份名单要交出去**。

    "静默少了一条"是这个功能最坏的失败形态：模型看不到那次工具结果，而所有日志
    都显示一切正常。
    """
    store, _, _ = build(workdir)
    artifact = store.create("会被删掉的正文")

    for path in (workdir / "artifacts" / "refs").glob("*.txt"):
        path.unlink()

    reopened = ArtifactStore(workdir / "artifacts")
    assert reopened.load() == [artifact.artifact_id]
    assert reopened.get(artifact.artifact_id) is None


def test_the_id_never_leaks_the_content_into_the_filename(workdir):
    """id 拼进文件名，所以它必须是哈希、不是正文。

    内容寻址最自然的错法是直接拿正文当 id —— 那样一份 12 万字符的文件会变成一个
    12 万字符的**文件名**，而它在某些文件系统上直接建不出来。
    """
    store, _, _ = build(workdir)
    artifact = store.create("含换行\n和空格 的正文 / 还有斜杠")

    assert "\n" not in artifact.artifact_id
    assert "/" not in artifact.artifact_id
    assert len(artifact.artifact_id) < 40


def test_slicing_ignores_the_trailing_newline(workdir):
    """`"a\\nb\\n"` 是**两行**，不是三行。

    多算一行的话，"文件共 N 行"就是错的，而模型照着自己看到的行数去引用行号会
    永远差一行 —— 那个错误看起来像模型数错了。
    """
    store, _, _ = build(workdir)
    artifact = store.create("a\nb\n")

    snippet = store.read(artifact.artifact_id)

    assert snippet.total_lines == 2
    assert snippet.end_line == 2


# --- 2. 渲染：形状和重构之前一致 -----------------------------------------------

def test_full_shows_the_content_byte_for_byte(workdir):
    """**这条是整个重构的底线。**

    `full` 档不加任何表头：那一档的含义就是"和重构之前一模一样"。加了表头就
    等于每一轮都在原来的正文前面多送一段话，而 provider 的前缀缓存、以及所有
    "模型读到的是原文"的假设都会跟着变。
    """
    store, manager, renderer = build(workdir)
    text = "def f():\n    return 1\n"
    artifact = put(store, manager, text, type="file",
                   metadata={"path": "a.py", "lines": 2})

    assert renderer.render_item(manager.item(artifact.artifact_id)) == text


def test_range_says_which_lines_it_is_showing(workdir):
    """降级**必须写在载荷里**。

    模型看到半份代码却以为看到了全部，是这一层唯一会静默出错的地方。
    """
    store, manager, renderer = build(workdir)
    artifact = put(store, manager, "".join(f"line{i}\n" for i in range(1, 101)),
                   type="file", metadata={"path": "a.py", "lines": 100})
    item = manager.item(artifact.artifact_id)
    item.representation = Representation.RANGE
    item.options = {"start_line": 10, "end_line": 12}

    rendered = renderer.render_item(item)

    assert "第 10-12 行" in rendered
    assert "共 100 行" in rendered
    assert "line10" in rendered and "line12" in rendered
    assert "line13" not in rendered


def test_metadata_carries_no_content_at_all(workdir):
    """最后一档只剩事实：路径、行数、大小。"""
    store, manager, renderer = build(workdir)
    artifact = put(store, manager, "秘密内容" * 100, type="file",
                   metadata={"path": "a.py", "lines": 3})
    item = manager.item(artifact.artifact_id)
    item.representation = Representation.METADATA

    rendered = renderer.render_item(item)

    assert "秘密内容" not in rendered
    assert "a.py" in rendered
    assert "400" in rendered          # 字符数（"秘密内容" 4 个字符 × 100）


def test_a_tool_message_with_a_lost_artifact_is_never_empty(workdir):
    """正文丢了 ⇒ 说清"取不到了"，而不是给一个空串。

    空串会被读成"这个工具什么都没返回"，于是模型换一条完全没必要的路重做一遍。
    """
    store, manager, renderer = build(workdir)
    artifact = put(store, manager, "会丢的正文")
    message = {"role": "tool", "tool_call_id": "c1",
               "content": ref.build(artifact.artifact_id, 5, "read_file"),
               "artifact_id": artifact.artifact_id}
    for path in (workdir / "artifacts" / "refs").glob("*.txt"):
        path.unlink()
    manager.store._cache.clear()

    rendered = renderer.render_tool_content(message)

    assert rendered.strip()
    assert "取不到" in rendered


def test_an_evicted_item_says_so_instead_of_disappearing(workdir):
    """被预算挤出去的 Artifact：一句话说明它**曾经在**这里。

    直接删掉的话，模型会以为自己从没读过那个文件，于是再读一遍（那会产生一份
    新的 Artifact，于是又一次挤掉别人）。
    """
    store, manager, renderer = build(workdir)
    artifact = put(store, manager, "被挤掉的正文")
    manager.remove(artifact.artifact_id)
    message = {"role": "tool", "tool_call_id": "c1",
               "content": ref.build(artifact.artifact_id, 6, "read_file"),
               "artifact_id": artifact.artifact_id}

    rendered = renderer.render_tool_content(message)

    assert "移出" in rendered
    assert "被挤掉的正文" not in rendered


def test_a_message_without_an_artifact_passes_through(workdir):
    """老会话、或者手工拼的历史：原样照发（不猜）。"""
    store, manager, renderer = build(workdir)
    message = {"role": "tool", "tool_call_id": "c1", "content": "一段普通的工具结果"}

    assert renderer.render_tool_content(message) == "一段普通的工具结果"


# --- 3. 预算与降级 -------------------------------------------------------------

def test_degradation_goes_full_range_preview(workdir):
    """设计原则第 7 条：一步一步退，**每一步只退一档**。

    每一步只退一档（不是直接跳到最省）是有意的 —— 一次退到底会把本来读得到的
    部分也丢掉。这里钉住那条阶梯的前三档，而下面的测试钉住最后两档。

    几档的窗口由**正文自己的大小**推出来（`range` 两成、`preview` 半成），所以
    要让每一档都刚好还是超预算：4000 字符的文件配 150 token 的窗口。
    """
    store, manager, renderer = build(workdir)
    artifact = put(store, manager, "x" * 4000, type="file",
                   metadata={"path": "big.py", "lines": 400})
    manager.budget = ContextBudget(max_tokens=150, reserve=0, headroom=0.0)
    item = manager.item(artifact.artifact_id)
    seen = [item.representation.value]

    for _ in range(6):
        manager.fit(render_all(renderer))
        if seen[-1] != item.representation.value:
            seen.append(item.representation.value)
        if item.representation is Representation.PREVIEW:
            break

    assert seen == ["full", "range", "preview"]


def test_the_floor_is_metadata_and_it_can_still_be_removed(workdir):
    """最后两档：`preview → metadata → removed`。

    `metadata` 是"只剩事实"的保底档（几十个 token），所以它只有在窗口小到连那
    一行都放不下时才会被进一步摘掉 —— 那正是"窗口配错了"或"模型太小"的形状。
    这里用一个小到不合理的窗口把它逼出来，钉住的是**顺序**：先降到底、再摘掉，
    而且摘掉之后**盘上的正文一个字节都不许动**（设计原则第 7 条：先降级、其次
    删除；删的是可见性，不是数据）。
    """
    store, manager, renderer = build(workdir)
    artifact = put(store, manager, "y" * 20_000, type="file",
                   metadata={"path": "big.py", "lines": 900})
    manager.budget = ContextBudget(max_tokens=10, reserve=0, headroom=0.0)
    item = manager.item(artifact.artifact_id)
    seen = [item.representation.value]

    for _ in range(10):
        manager.fit(render_all(renderer))
        if item.removed:
            seen.append("removed")
            break
        if seen[-1] != item.representation.value:
            seen.append(item.representation.value)

    assert seen == ["full", "range", "preview", "metadata", "removed"]
    assert store.get(artifact.artifact_id) is not None
    assert store.content(artifact.artifact_id) == "y" * 20_000
    # 被摘掉之后这一条不再渲染（载荷里那句"它被移出了"由 renderer 给）
    assert renderer.render_item(item) is None


def test_every_step_of_the_ladder_actually_reduces_the_payload(workdir):
    """**每一档都必须真的把 token 降下来。**

    这条是一个实测过的 bug 留下的：第一版把 `range` 定成"最多 400 行"、`preview`
    定成 40 行，于是对一份"200 行、一行 20 个字符"的文件，降级让估算从 2016
    **涨到** 2034（多出来的是表头）—— 档位变了、日志里也对，就是没省下东西。

    所以判据不能是"档位变了"，必须是"字节数变小了"。这里同时钉住长度单调下降，
    因为它正是降级存在的理由。

    还钉住"一次一档"：假如一次 fit 就降到底，最后一步会**超出**必要地丢掉内容
    （见 `budget.fit` 的 `single_step`）。
    """
    store, manager, renderer = build(workdir)
    artifact = put(store, manager, "x" * 8000, type="file",
                   metadata={"path": "big.py", "lines": 400})
    manager.budget = ContextBudget(max_tokens=150, reserve=0, headroom=0.0)
    item = manager.item(artifact.artifact_id)

    sizes = [len(renderer.render_item(item) or "")]
    steps = []
    for _ in range(8):
        done = manager.fit(render_all(renderer))
        assert len(done) <= 1, f"一次 fit 降了 {len(done)} 档（应该一次一档）"
        if not done:
            break
        steps.append(item.representation.value)
        sizes.append(len(renderer.render_item(item) or ""))
        if item.representation is Representation.METADATA:
            break

    assert steps == ["range", "preview"]
    assert sizes[0] == 8000
    assert sizes == sorted(sizes, reverse=True), f"降级没有让载荷变小：{sizes}"
    assert sizes[-1] < 500, f"缩到 preview 之后应当只剩几百字符：{sizes}"


def test_a_capped_range_never_promises_lines_it_did_not_send(workdir):
    """表头里的行号区间**必须和内容对得上**。

    这条是实测撞出来的：`range` 档的表头写着"第 1-23 行"，而字符预算在第 23 行的
    第 14 个字符处就把内容切断了 —— 模型从这两句话里看不出那是断的，于是它会以为
    自己读到了完整的第 23 行。

    所以字符那一侧**只切在行边界上**：放不下的那一行整个不给，表头里的 `end_line`
    跟着退到最后一个真的给出去的行。代价是最后一行少掉，换来的是区间永远说得对。
    """
    store, manager, renderer = build(workdir)
    lines = "".join(f"line{i:03d}: 这一行有二十来个字符，够长\n" for i in range(1, 121))
    artifact = put(store, manager, lines, type="file",
                   metadata={"path": "main.py", "lines": 120})
    item = manager.item(artifact.artifact_id)
    item.representation = Representation.RANGE
    item.options = {"start_line": 1, "end_line": 120, "max_chars": 500}

    rendered = renderer.render_item(item)

    head, _, body = rendered.partition("\n")
    promised = re.search(r"第 (\d+)-(\d+) 行", head)
    assert promised, head
    first, last = int(promised.group(1)), int(promised.group(2))
    body_lines = body.split("\n")

    # 内容行数 == 承诺的行数，而且每一行都是**完整**的一行
    assert len(body_lines) == last - first + 1, (head, len(body_lines))
    assert body_lines == [f"line{i:03d}: 这一行有二十来个字符，够长"
                          for i in range(first, last + 1)]
    assert body_lines[-1].endswith("够长"), "最后一行被切成了半行"


def test_a_single_line_artifact_still_shrinks(workdir):
    """**一行 8 万字符的正文也要能降下来。**

    压缩过的 JSON、`''.join()` 拼出来的日志、任何没有换行的输出 —— 它们的行数
    都是 1，所以"给 20 行"就是给全文。只按行夹的实现会在这里一直降不动。
    """
    store, manager, renderer = build(workdir)
    artifact = put(store, manager, "j" * 80_000, type="file",
                   metadata={"path": "one-line.json", "lines": 1})
    manager.budget = ContextBudget(max_tokens=300, reserve=0, headroom=0.0)
    item = manager.item(artifact.artifact_id)

    for _ in range(4):
        manager.fit(render_all(renderer))

    rendered = renderer.render_item(item) or ""
    assert item.representation is not Representation.FULL
    assert len(rendered) < 3000, f"单行正文没有被真的截断：{len(rendered)} 字符"


def test_the_last_step_evicts_but_keeps_the_data(workdir):
    """降到 metadata 还塞不下 ⇒ 从 Context 里摘掉，**但盘上的正文不动**。

    这是"先降级、其次删除"里最后那一档。删的是**可见性**，不是数据 —— 模型
    需要时可以再读一次，而"它曾经在这里"这件事仍然查得到。

    要走到这一档，**metadata 本身就得太大**：一条只剩 `字符=N 路径=…` 的渲染结果
    通常只有几十个 token，一个正常的窗口永远塞得下它。所以这里把命令原文塞得
    很长（真实场景里就是它）—— 于是最后一步只剩"摘掉"。
    """
    store, manager, renderer = build(workdir)
    artifact = put(store, manager, "y" * 20_000, type="file",
                   metadata={"path": "big.py", "lines": 900,
                             "command": "q" * 4000})
    manager.budget = ContextBudget(max_tokens=60, reserve=0, headroom=0.0)
    item = manager.item(artifact.artifact_id)

    for _ in range(10):
        manager.fit(render_all(renderer))

    assert item.removed is True
    # 摘掉之后**不再渲染**（那一条占位说明由 renderer 给，见它的测试）
    assert renderer.render_item(item) is None
    assert store.get(artifact.artifact_id) is not None
    assert store.content(artifact.artifact_id) == "y" * 20_000


def test_degradation_never_climbs_back(workdir):
    """**只降不升。** 预算松下来之后档位不许弹回去。

    弹回去的代价是缓存：`full → range → full` 会让渲染出来的 prompt 每轮都不同，
    而 provider 按**最长公共前缀**计费 —— 变化的那个字节之后全部按未命中算。
    """
    store, manager, renderer = build(workdir)
    artifact = put(store, manager, "z" * 4000, type="file",
                   metadata={"path": "big.py", "lines": 200})
    manager.budget = ContextBudget(max_tokens=400, reserve=0, headroom=0.0)
    manager.fit(render_all(renderer))
    degraded = manager.item(artifact.artifact_id).representation
    assert degraded is not Representation.FULL

    manager.budget = ContextBudget(max_tokens=1_000_000)
    manager.fit(render_all(renderer))
    assert manager.item(artifact.artifact_id).representation is degraded


def test_pinned_items_are_never_touched(workdir):
    """pinned = "Token 再紧也不许动它"。

    系统提示词和本回合的任务是这一档：挤掉它们的后果不是"少看一份文件"，而是
    "模型不知道该干什么"。
    """
    store, manager, renderer = build(workdir)
    task = store.create("这个回合的任务")
    manager.add(task.artifact_id, pinned=True, zone=Zone.STABLE, priority=100)
    filler = put(store, manager, "f" * 8000, type="file",
                 metadata={"path": "big.py", "lines": 400})
    manager.budget = ContextBudget(max_tokens=400, reserve=0, headroom=0.0)

    manager.fit(render_all(renderer))

    assert manager.item(task.artifact_id).representation is Representation.FULL
    assert manager.item(filler.artifact_id).representation is not Representation.FULL


def test_the_same_input_degrades_the_same_way_twice(workdir):
    """顺序必须**完全确定** —— 同一个会话跑两次，Context 得长成一个样。

    不确定的话，两次恢复会话之后的档位不同，而"为什么这次看到的是摘要"就没有
    答案了（那不是模型的问题，是排序用了 set 之类的容器）。
    """
    results = []
    for round_index in range(2):
        store, manager, renderer = build(workdir / f"round{round_index}")
        for index in range(4):
            put(store, manager, f"内容{index}" * 500, type="file",
                metadata={"path": f"f{index}.py", "lines": 100})
        manager.budget = ContextBudget(max_tokens=3000, reserve=0, headroom=0.0)
        manager.fit(render_all(renderer))
        results.append([(i.artifact_id, i.representation.value)
                        for i in manager.items()])

    assert results[0] == results[1]


def test_an_unknown_window_turns_the_budget_off(workdir):
    """配置里没写 `context_window` ⇒ **不做任何降级**。

    一个假的上限比没有上限更坏：它会去压缩一个本来塞得下的 Context，而"为什么
    模型看不到全文"就变成一个查不出来的现象。
    """
    store, manager, renderer = build(workdir)
    artifact = put(store, manager, "w" * 100_000, type="file",
                   metadata={"path": "big.py", "lines": 5000})
    manager.budget = ContextBudget(max_tokens=None)

    assert manager.fit(render_all(renderer)) == []
    assert manager.item(artifact.artifact_id).representation is Representation.FULL
    assert manager.last_estimate > 0          # 仍然报数，只是不降级


def test_calibration_ignores_a_tiny_sample(workdir):
    """一次 200 token 的请求修不了比例 —— 那是噪音。

    拿它去修正会让后面几百 K 的请求被带偏，而"估低了"的后果是请求直接 400。
    """
    store, manager, _ = build(workdir)
    before = manager.budget._factor

    manager.budget.calibrate(estimated=180, measured=220)

    assert manager.budget._factor == before


# --- 4. 一次真实的 Agent 回合 --------------------------------------------------

class TwoStepModel(ChatModel):
    """第一步读文件，第二步收尾。"""

    def __init__(self, content: str):
        self.content = content
        self.sent = False
        self.seen: list[list[dict]] = []

    def complete(self, messages, tools=None):
        self.seen.append([dict(m) for m in messages])
        if not self.sent:
            self.sent = True
            return ModelResponse(
                content=None,
                tool_calls=[{"id": "c1", "name": "read_file",
                             "arguments": json.dumps({"path": "a.py"})}],
                usage=usage(),
            )
        return ModelResponse(content="读完了", usage=usage())


def context_agent(base, content: str, **kwargs):
    """一个**带着 Context** 跑的 Agent（和 `composition` 里的装配同构）。"""
    store = ArtifactStore(base / "artifacts")
    manager = ContextManager(store)
    registry = ToolRegistry()
    registry.register(Tool(
        name="read_file", description="读文件", risk=RiskLevel.LOW,
        args_model=ListFilesArgs, handler=lambda **kw: content,
        parallel_safe=True,
    ))
    model = TwoStepModel(content)
    session = Session.new("s")
    agent = Agent(
        model, registry, PermissionPolicy({RiskLevel.LOW}),
        context=manager, processor=default_processor(), **kwargs,
    )
    return agent, session, model, store, manager


def test_the_tool_message_holds_a_reference_not_the_text(workdir):
    """**会话历史里没有正文了。** 这是"History ≠ Context"落地的地方。"""
    content = "A" * 5000
    agent, session, model, store, manager = context_agent(workdir, content)

    agent.run(session, "读一下 a.py")

    refs = tool_texts(session)
    assert len(refs) == 1
    assert content not in refs[0]
    assert "artifact" in refs[0]
    artifact_id = tool_artifacts(session)[0]
    assert store.content(artifact_id) == content


def test_what_the_model_sees_is_still_the_whole_text(workdir):
    """**重构不改变模型看到的东西**（在 full 档下）。

    模型第二步收到的工具结果必须是完整的正文 —— 少了它，模型会以为工具没返回
    内容，于是换一条路重做。
    """
    content = "def f():\n    return 42\n"
    agent, session, model, _, _ = context_agent(workdir, content)

    agent.run(session, "读一下 a.py")

    second = model.seen[1]
    tool_messages = [m for m in second if m.get("role") == "tool"]
    assert [m["content"] for m in tool_messages] == [content]
    assert tool_messages[0]["tool_call_id"] == "c1"


def test_a_second_turn_still_sees_the_first_tool_result(workdir):
    """**跨回合**：第二次 run() 里，第一轮那次 read_file 的正文仍然发得出去。

    这条正是"引用 + 渲染"能替代"正文躺在历史里"的证明。少了它，第二次提问时
    模型就再也看不到它上一轮读过的东西了 —— 而那是最容易在重构里丢掉的一条。
    """
    content = "第一轮读到的内容"
    agent, session, model, _, _ = context_agent(workdir, content)
    agent.run(session, "读一下")
    agent.run(session, "再想想")

    # 第二轮没有再调工具，所以最后一次请求里只有第一轮留下的那一条 tool 结果 ——
    # 而它的正文必须仍然是完整的那段内容（按 Artifact 渲染出来的）。
    tool_messages = [m for m in model.seen[-1] if m.get("role") == "tool"]
    assert [m["content"] for m in tool_messages] == [content]
    # 第二次请求里那条 tool 消息在**历史**里仍然是引用（正文没被复制回去）
    assert len(tool_artifacts(session)) == 1


def test_a_repeated_read_reuses_the_same_artifact(workdir):
    """同一个文件读两次（同一段内容、同一个来源）⇒ 同一个 Artifact。

    内容寻址的回报：历史里两条 tool 消息指向同一份正文，而磁盘上只有一份。
    """
    content = "同一份内容"
    sent = {"n": 0}

    class TwoReadsModel(ChatModel):
        def complete(self, messages, tools=None):
            sent["n"] += 1
            if sent["n"] <= 2:
                return ModelResponse(
                    content=None,
                    tool_calls=[{"id": f"c{sent['n']}", "name": "read_file",
                                 "arguments": json.dumps({"path": "a.py"})}],
                    usage=usage(),
                )
            return ModelResponse(content="好了", usage=usage())

    store = ArtifactStore(workdir / "artifacts")
    manager = ContextManager(store)
    registry = ToolRegistry()
    registry.register(Tool(
        name="read_file", description="读文件", risk=RiskLevel.LOW,
        args_model=ListFilesArgs, handler=lambda **kw: content, parallel_safe=True,
    ))
    session = Session.new("s")
    agent = Agent(TwoReadsModel(), registry, PermissionPolicy({RiskLevel.LOW}),
                  context=manager, processor=default_processor())

    agent.run(session, "读一下")
    agent.run(session, "再读一次")

    ids = tool_artifacts(session)
    assert len(ids) == 2
    assert ids[0] == ids[1], "同一份内容+同一个来源应当是同一个 Artifact"
    assert len(store) == 1, "磁盘上只该有一份正文"



def test_the_task_message_is_not_degradable(workdir):
    """**本回合的任务不会因为预算被挤掉。**

    这条保证是**天然成立**的，而不是靠一条 ContextItem：预算那一层只看 Context
    里的条目（tool 消息背后那些 Artifact），而历史里的 `role="user"` 消息不参与
    降级 —— 它原样进载荷。

    这里钉住它，是因为"系统提示词和用户任务不许被挤掉"是设计原则第 19 条最硬的
    一条；实现方式换了（从"排除某几条消息"变成"它们根本不在降级集合里"），但那
    条保证必须还在。
    """
    content = "x" * 40_000
    agent, session, _, _, manager = context_agent(workdir, content)
    agent.context.budget = ContextBudget(max_tokens=60, reserve=0, headroom=0.0)

    agent.run(session, "这个回合的任务")

    # 任务那条消息还在历史里，而且它背后没有 ContextItem（所以降级碰不到它）
    users = [i for i, m in enumerate(session.messages) if m["role"] == "user"]
    assert users == [1]
    assert manager.items(), "工具结果应当进了 Context（它才是被降级的对象）"


def test_context_survives_a_resume(workdir):
    """恢复会话之后：正文还在，**档位也还在**（不能弹回 full）。"""
    content = "B" * 4000
    agent, session, _, store, manager = context_agent(workdir, content)
    agent.run(session, "读一下")

    artifact_id = tool_artifacts(session)[0]
    manager.item(artifact_id).representation = Representation.PREVIEW
    manager.item(artifact_id).options = {"preview_lines": 3}

    saved = JsonSessionStore(workdir / "sessions")
    saved.save(session)
    back = saved.load("s")

    assert back.context is not None
    item = back.context.get(artifact_id)
    assert item.representation is Representation.PREVIEW
    assert item.options == {"preview_lines": 3}

    reopened = ArtifactStore(workdir / "artifacts")
    assert reopened.load() == []
    assert reopened.content(artifact_id) == content


def test_the_session_file_does_not_grow_with_the_tool_body(workdir):
    """**这次重构的全部理由，用字节数说一遍。**

    过去：一次 `read_file` 的正文此后每一轮都重发，而会话文件里也把它抄一份。
    现在：会话文件里只有一句引用（几十字节），正文只在 ArtifactStore 里有一份。

    这里拿一份 400 KB 的"文件"跑一轮，量两样东西：

      * 会话文件里那条 tool 记录的字节数 —— 必须和正文大小无关；
      * 磁盘上那份正文 —— 必须只有一个副本，且内容逐字节相同。

    这条测试的价值在于它**不会假通过**：把 `_tool_message` 改回"正文进历史"，
    第一个断言立刻红；把正文同时写两处，第二个断言红。
    """
    content = ("def f():\n    return 1\n" * 20_000)      # 约 460 KB
    agent, session, _, store, _ = context_agent(workdir, content)
    agent.run(session, "读一下")

    saved = JsonSessionStore(workdir / "sessions")
    saved.save(session)

    # 会话文件里那一行 tool 记录
    lines = [json.loads(line) for line in
             (workdir / "sessions" / "s.jsonl").read_text(encoding="utf-8").splitlines()]
    tool_records = [r for r in lines if r.get("t") == "msg"
                    and r["m"].get("role") == "tool"]
    assert len(tool_records) == 1
    inline = len(json.dumps(tool_records[0], ensure_ascii=False))
    assert inline < 500, f"工具结果的正文还是进了历史（{inline} 字节）"

    # 正文只有一份，而且逐字节相同
    refs = list((workdir / "artifacts" / "refs").glob("*.txt"))
    assert len(refs) == 1, f"正文被写了 {len(refs)} 份"
    assert refs[0].read_text(encoding="utf-8") == content
    assert len(content.encode("utf-8")) > 100_000, "样本太小，这条测试就没意义了"


def test_the_session_file_grows_with_the_delta_not_the_history(workdir):
    """每一步的落盘量是 O(新增)，**不是 O(整个历史)**。

    这是 `state/store.py` "只追加"的收益，而它最容易被这次重构破坏：只要有人把
    正文塞回消息里，"每一步写出去的都是前几步的全部"那个二次项就回来了 ——
    而这一层的所有收益都会被它吃掉（实测过：80 步、每步 256KB 的文件，落盘量
    863MB，而最终会话只有 21MB）。

    判据取"第二步比第一步多写了多少"：两次读的是**同一份 20 万字符的正文**，
    所以两次的新增量必须一样大（都是"一条 assistant + 一条 tool"），而**不是**
    第二步比第一步大一倍（那才是把整个历史又写了一遍）。
    """
    content = "x" * 200_000
    agent, session, _, _, _ = context_agent(workdir, content)
    saved = JsonSessionStore(workdir / "sessions")
    path = workdir / "sessions" / "s.jsonl"

    agent.run(session, "读一下")
    saved.save(session)
    after_first = path.stat().st_size
    agent.run(session, "再来一次")
    saved.save(session)
    grown = path.stat().st_size - after_first

    assert grown < 8000, (
        f"第二步往会话文件里写进了 {grown} 字节 —— 正文八成又回到历史里了"
        f"（一次追加应当只有几十到几百字节）"
    )
def test_context_degraded_is_recorded_in_the_audit(workdir):
    """降级事件要进审计 —— "这一轮为什么没看到全文"只有一个出口。"""
    content = "C" * 20_000
    agent, session, _, _, _ = context_agent(workdir, content)
    collector = Collector()
    agent.on_event = collector
    agent.context.budget = ContextBudget(max_tokens=1200, reserve=0, headroom=0.0)

    agent.run(session, "读一下")

    degraded = collector.of("context_degraded")
    assert degraded, "超预算降级之后必须留下一条 context_degraded"
    assert degraded[0]["items"] >= 1
    assert degraded[0]["changes"]


# --- 5. 兼容：重构之前落盘的会话 -----------------------------------------------

def test_a_legacy_session_is_hydrated_into_artifacts(workdir):
    """老会话文件里的 tool 正文会被重新收成 Artifact，**不走第二套渲染路径**。

    这条是"重构之后旧会话照常能接着聊"的全部实现 —— 缺了它，那些会话里 tool
    消息没有 `artifact_id`，而引用取不到正文。
    """
    store, manager, renderer = build(workdir)
    legacy = [
        {"role": "system", "content": "提示词"},
        {"role": "user", "content": "读一下"},
        {"role": "assistant", "content": None,
         "tool_calls": [{"id": "c1", "type": "function",
                         "function": {"name": "read_file", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "旧格式的全文"},
    ]

    created = manager.hydrate(legacy)

    assert len(created) == 1
    assert store.get(created[0]) is not None
    assert store.content(created[0]) == "旧格式的全文"
    assert renderer.render_tool_content(legacy[3]) == "旧格式的全文"


def test_hydrating_twice_does_not_duplicate(workdir):
    """**已经收过的不再收第二次** —— 否则每次启动都会把整份历史再抄一遍。"""
    store, manager, _ = build(workdir)
    legacy = [{"role": "tool", "tool_call_id": "c1", "content": "正文"}]

    first = manager.hydrate(legacy)
    converted = dict(legacy[0], artifact_id=first[0])
    second = manager.hydrate([converted])

    assert second == []
    assert len(store) == 1


def test_a_state_round_trips_through_json():
    """ContextState 要能原样过一遍 JSON —— 它会被写进会话文件。"""
    state = ContextState(version=7)
    state.items.append(ContextItem(artifact_id="art_1"))
    state.items[0].options = {"start_line": 3, "end_line": 9}

    back = ContextState.from_json(json.loads(json.dumps(state.to_json())))

    assert back.version == 7
    assert back.items[0].artifact_id == "art_1"
    assert back.items[0].options == {"start_line": 3, "end_line": 9}


def test_an_unknown_representation_is_refused_loudly():
    """写错一个档位名 ⇒ 抛。**不静默回落 full。**

    静默回落的症状是"我配的压缩没生效"，而那件事在账单上和心理预期里都对不上。
    """
    with pytest.raises(ValueError, match="representation"):
        ContextState.from_json({"items": [{"artifact_id": "a",
                                           "representation": "summary"}]})
