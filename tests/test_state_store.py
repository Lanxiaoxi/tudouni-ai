"""会话持久化的正确性与安全底线。

这里的每一条都对应一个实测过的故障：路径穿越、写到一半被杀、schema 漂移。
而自从落盘改成**只追加**之后（见 `state/store.py` 的模块 docstring），多了一类
要盯的东西：**"只追加"本身是一条必须被测试钉住的契约** —— 它一旦被改回整份重写，
功能测试全都还是绿的，只有写放大悄悄回到 41 倍。
"""

import json
from pathlib import Path

import pytest

from agent_runtime.agents import Agent
from agent_runtime.audit import JsonlSink
from agent_runtime.models.types import ModelResponse
from agent_runtime.security import PermissionPolicy
from agent_runtime.state import JsonSessionStore, Session
from agent_runtime.state.store import HEAD, META, MSG, SUFFIX, STATE_VERSION
from agent_runtime.tools.tool import RiskLevel

from fakes import ScriptedModel, usage


def _bytes_written_while(store: JsonSessionStore, session: Session, content: str) -> int:
    """往会话末尾加一条消息、落一次盘，返回**这次落盘往文件里写了多少字节**。

    它是"只追加"这件事唯一能观测到的量：整份重写的实现下，这个数会随历史长度
    线性上涨；只追加的实现下，它只和这一条消息有关。见下面那条测试。
    """
    path = store._path(session.session_id)
    before = path.stat().st_size
    session.messages.append({"role": "assistant", "content": content})
    store.save(session)
    return path.stat().st_size - before


def test_round_trip(workdir):
    store = JsonSessionStore(workdir)
    session = Session.new("s")
    session.messages.append({"role": "user", "content": "你好"})
    session.metadata["note"] = "随便一个值"
    store.save(session)

    back = store.load("s")
    assert back.messages == session.messages
    assert back.metadata == session.metadata


def test_no_file_until_the_first_turn(workdir, registry):
    """新建会话本身不该产生文件 —— 说了第一句话才落盘。

    这个行为不是特意写的，而是从设计里掉出来的：第一次 _checkpoint 发生在
    run() 把用户消息追加进 messages 之后。所以"聊了才存"和"开了不用不留垃圾"
    是同一个机制保证的。
    """
    store = JsonSessionStore(workdir)
    session = Session.new("s")
    agent = Agent(ScriptedModel([ModelResponse(content="你好", usage=usage())]),
                  registry, PermissionPolicy({RiskLevel.LOW}),
                  asker=lambda t, a: False, on_checkpoint=store.save)

    assert not store.exists("s")
    agent.run(session, "在吗")
    assert store.exists("s")


@pytest.mark.parametrize("bad_id", ["../../evil", "..\\..\\evil", "a/b", "", "x" * 65, "a b"])
def test_store_rejects_unsafe_session_id(workdir, bad_id):
    """session_id 会被拼进文件名，实测 "../../evil" 能把文件写到目录外。"""
    store = JsonSessionStore(workdir)
    with pytest.raises(ValueError):
        store.save(Session(session_id=bad_id))


@pytest.mark.parametrize("bad_id", ["../../evil", "a/b", ""])
def test_sink_rejects_unsafe_session_id(workdir, bad_id):
    """审计 sink 同样把 session_id 拼进文件名 —— 校验规则必须和 store 是同一份。"""
    sink = JsonlSink(workdir)
    with pytest.raises(ValueError):
        sink({"kind": "x", "session_id": bad_id})


# --- 只追加：这次改动的全部意义 -------------------------------------------
#
# 这一节盯的不是"能不能存下来"（上面那条已经盯了），而是**存的方式**。
# 改回整份重写的话，功能测试一条都不会红 —— 只有下面这两条会。

def test_a_save_writes_only_what_is_new_not_the_whole_history(workdir):
    """**核心那条。** 一次落盘写出去多少，只和"新增了什么"有关，和历史多长无关。

    这是二次项消失的判据。整份重写的版本里，这个数 = 整份历史的大小，
    于是第 i 步写 O(i)、合计 O(步数²) —— 实测 80 步写出去 863MB，而会话只有 21MB。

    判据取"同样大的一条消息、历史长短不同、增量必须一样"，而不是"增量小于某个常数"：
    后者会被消息本身的大小、metadata 的大小这些无关变量带偏，而前者是一个
    **不依赖任何阈值**的性质。
    """
    store = JsonSessionStore(workdir)
    session = Session("s", [{"role": "user", "content": "x" * 1000}])
    store.save(session)

    with_short_history = _bytes_written_while(store, session, "y" * 1000)

    # 把历史堆到 100 条，再加一条**一模一样大**的消息。
    for _ in range(100):
        session.messages.append({"role": "assistant", "content": "z" * 1000})
    store.save(session)
    with_long_history = _bytes_written_while(store, session, "y" * 1000)

    assert with_long_history == with_short_history, (
        "落盘增量随历史长度变了 —— 说明又变成整份重写了"
    )


def test_what_was_already_written_is_never_touched(workdir):
    """已经写下去的字节**一个都不动**（前缀必须逐字节保持）。

    比"增量很小"更强的一条：它同时排除了"重写一遍但结果恰好一样"这种实现 ——
    那种实现下前缀虽然相等，但它每一次都在重新序列化整份历史。
    """
    store = JsonSessionStore(workdir)
    session = Session("s", [{"role": "user", "content": "旧"}])
    store.save(session)
    path = store._path(session.session_id)
    before = path.read_bytes()

    session.messages.append({"role": "assistant", "content": "新"})
    store.save(session)

    assert path.read_bytes().startswith(before)


# --- 崩溃安全 --------------------------------------------------------------

def test_a_crash_mid_write_leaves_everything_earlier_intact(workdir, monkeypatch):
    """写到一半被杀：**已经落盘的内容照样读得出来**，只丢掉没写完的那一条。

    这条替换掉了整份重写时代的那条 `test_save_is_atomic`（它保证"目标文件要么是
    旧的完整版、要么是新的完整版"）。机制换了，保护的东西没换，而且更强：

      * 整份重写丢的是**整个检查点**（回到上一步的开头）；
      * 只追加丢的是**最后一条消息**。

    所以断言也换了：不再比"文件字节和旧的一模一样"，而是"读出来的历史是完整的
    那一段"—— 追加式下文件字节必然变了（多了一截半截记录），那是设计内的。
    """
    store = JsonSessionStore(workdir)
    session = Session("s", [{"role": "user", "content": "旧"}])
    store.save(session)
    session.messages.append({"role": "assistant", "content": "新"})

    real_open = Path.open

    class _DiesMidWrite:
        """一个写一半就抛的句柄 —— 模拟进程被杀在 append 中途。"""

        def __init__(self, handle):
            self._handle = handle

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self._handle.close()
            return False

        def write(self, data):
            self._handle.write(data[: len(data) // 2])
            self._handle.flush()
            raise OSError("模拟进程被杀在追加到一半")

    with monkeypatch.context() as patch:
        patch.setattr(Path, "open", lambda self, *a, **kw: _DiesMidWrite(real_open(self, *a, **kw)))
        with pytest.raises(OSError):
            store.save(session)

    assert [m["content"] for m in store.load("s").messages] == ["旧"]


def test_a_torn_last_line_is_skipped(workdir):
    """半截行是**设计内**的情形，读的时候跳过它就行 —— 和审计日志同一条规矩。"""
    store = JsonSessionStore(workdir)
    session = Session("s", [{"role": "user", "content": "你好"}])
    store.save(session)

    with store._path("s").open("a", encoding="utf-8") as f:
        f.write('{"t":"msg","m":{"role":"user","cont')      # 半截

    assert [m["content"] for m in store.load("s").messages] == ["你好"]


def test_a_write_failure_does_not_duplicate_messages_on_the_next_save(workdir, monkeypatch):
    """写失败之后**下一次落盘不许把已经写下去的消息再写一遍**。

    这是"水位缓存"最容易出事的地方：失败时如果留着旧水位，下一次 save 会从那个
    旧水位重放，把已经真正写进文件的那几条重复追加 —— 而重放对重复消息没有去重，
    于是历史里真的出现两条一样的消息（看起来像模型自己重复了，查不出来）。
    """
    store = JsonSessionStore(workdir)
    session = Session("s", [{"role": "user", "content": "一"}])
    store.save(session)

    real_open = Path.open
    with monkeypatch.context() as patch:
        def die(self, *a, **kw):
            handle = real_open(self, *a, **kw)
            handle.write("garbage\n")
            handle.close()
            raise OSError("模拟磁盘满")

        patch.setattr(Path, "open", die)
        session.messages.append({"role": "assistant", "content": "二"})
        with pytest.raises(OSError):
            store.save(session)

    store.save(session)
    assert [m["content"] for m in store.load("s").messages] == ["一", "二"]


def test_a_session_file_deleted_mid_session_is_rebuilt_whole(workdir):
    """会话文件在会话中途被删掉（这个目录是用户可见的）→ 下一次落盘**重建整份**。

    不处理的话，`open("a")` 会把一个**没有 head 的文件**凭空建出来，
    而那种文件之后谁都读不了（`_replay` 会说"不是一份会话文件"）。
    """
    store = JsonSessionStore(workdir)
    session = Session("s", [{"role": "user", "content": "一"}])
    store.save(session)
    assert store._watermark["s"] == 1                     # 水位已经进了缓存

    store._path("s").unlink()
    store.save(session)                                   # 缓存说"写过 1 条"，但文件没了

    assert [m["content"] for m in store.load("s").messages] == ["一"]


def test_shrinking_messages_is_rejected(workdir):
    """消息列表被缩短 → **报错，不静默**。

    `messages` 在这个项目里只追加（`agents/agent.py` 里那四处全是 `.append()`）。
    按空增量处理的话，会话文件会停在一个"看起来正常、其实少了几条"的状态上，
    而那种错误没有任何症状。
    """
    store = JsonSessionStore(workdir)
    session = Session("s", [{"role": "user", "content": "一"},
                            {"role": "assistant", "content": "二"}])
    store.save(session)
    session.messages.pop()

    with pytest.raises(ValueError) as exc:
        store.save(session)
    assert "只支持追加" in str(exc.value)


# --- 记录格式与重放 --------------------------------------------------------

def test_metadata_follows_the_last_meta_record(workdir):
    """metadata 会被原地改写（换模型、任务列表、技能），所以**最后一条说了算**。

    这条挡的是一种很自然的实现：把 metadata 只写进 head、之后不再管它。
    那样"改完模型 /model、退出、再进来"就会回到旧值 —— 而 store 这一层
    看起来完全正常。
    """
    store = JsonSessionStore(workdir)
    session = Session("s", [{"role": "user", "content": "一"}])
    session.metadata["model"] = "flash"
    store.save(session)

    session.metadata["model"] = "pro"        # 原地改顶层键，不新增消息
    store.save(session)

    assert store.load("s").metadata["model"] == "pro"
    assert [r["t"] for r in store.read("s")] == [HEAD, META, MSG, META]


def test_a_file_without_a_head_is_unreadable(workdir):
    """没有 head 的文件不是会话文件 —— 报错，而不是读出一份"没有身份"的历史。

    这一版不再兼容老格式（`sessions/<id>.json`，整份快照），所以原来那条
    "本字段落地之前的文件当作 version=1"被这条取代了：**格式换了，老文件就不该
    被当成新文件读进来**，否则症状是"接上了一次空对话"。
    """
    store = JsonSessionStore(workdir)
    store._path("s").write_text(
        json.dumps({"t": MSG, "m": {"role": "user", "content": "孤儿"}},
                   ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError) as exc:
        store.load("s")
    assert "不是一份会话文件" in str(exc.value)


def test_load_tolerates_unknown_fields_and_unknown_record_types(workdir):
    """会话文件躺在硬盘上，比代码活得久 —— 多出来的东西不能让它打不开。

    两种"多出来的"都要容忍，而且它们是**两件不同的事**：
      * head 里多一个字段 —— 字段过滤（只挑认识的 Session 字段）能兜住；
      * 文件里多一种记录 —— 重放时跳过，这样将来加记录种类不用 bump 版本。
    """
    store = JsonSessionStore(workdir)
    store._path("s").write_text(
        json.dumps({"t": HEAD, "version": STATE_VERSION, "session_id": "s",
                    "a_field_from_the_future": {"nested": [1, 2]}}, ensure_ascii=False) + "\n"
        + json.dumps({"t": "future-record", "whatever": 1}, ensure_ascii=False) + "\n"
        + json.dumps({"t": MSG, "m": {"role": "user", "content": "你好"}}, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    back = store.load("s")
    assert back.session_id == "s"
    assert [m["content"] for m in back.messages] == ["你好"]


def test_a_fresh_store_does_not_duplicate_messages(workdir):
    """换一个 store 实例（≈ 换一个进程）之后接着写，**不许把历史重写一遍或写重**。

    水位缓存在新实例里是空的，所以它必须去文件里把它数出来 —— 数错的两个方向
    都会坏事：数少了 = 消息重复，数多了 = 消息丢失。
    """
    store = JsonSessionStore(workdir)
    session = Session("s", [{"role": "user", "content": "一"},
                            {"role": "assistant", "content": "二"}])
    store.save(session)

    fresh = JsonSessionStore(workdir)
    path = fresh._path("s")
    before = path.stat().st_size
    fresh.save(session)                       # 没有任何新消息

    # 只多了一条 meta 记录：几百字节，而不是整份历史。
    assert path.stat().st_size - before < 2000
    assert [m["content"] for m in fresh.load("s").messages] == ["一", "二"]

    session.messages.append({"role": "user", "content": "三"})
    fresh.save(session)
    assert [m["content"] for m in fresh.load("s").messages] == ["一", "二", "三"]


# --- 版本：它必须真的被读 ------------------------------------------------
#
# 字段过滤（上面那条）容忍的是"多了几个键"；它容忍不了"同一个键的含义变了"。
# 后者要靠 STATE_VERSION：写的时候记下版本，读的时候据它决定要不要信这份文件。
# 这一段盯的就是"版本不是只写不读"。

def test_save_stamps_the_current_version(workdir):
    """落盘时带上版本号 —— 否则读取端根本没有可据以判断的东西。"""
    store = JsonSessionStore(workdir)
    store.save(Session("s"))

    first_line = store._path("s").read_text(encoding="utf-8").splitlines()[0]
    assert json.loads(first_line)["version"] == STATE_VERSION


def test_load_rejects_a_newer_version(workdir):
    """更新版本写的文件不能硬读。

    真正的危险不是"读出错"，而是**静默地读成另一个意思**：将来的格式改了，
    旧程序照样把能对上的字段塞进 Session，于是拿着一份自己没读懂的历史继续跑。
    所以这里要主动停下，并说清该怎么处置。
    """
    store = JsonSessionStore(workdir)
    store.save(Session("s"))
    path = store._path("s")
    lines = path.read_text(encoding="utf-8").splitlines()
    head = json.loads(lines[0])
    head["version"] = STATE_VERSION + 1
    path.write_text(json.dumps(head, ensure_ascii=False) + "\n"
                    + "\n".join(lines[1:]) + "\n", encoding="utf-8")

    with pytest.raises(ValueError) as exc:
        store.load("s")

    assert "更新版本" in str(exc.value)


def test_load_accepts_the_current_version(workdir):
    """本版本写的照常读 —— 上面那条拦的是"更新"，不是把自己也拦了。"""
    store = JsonSessionStore(workdir)
    store.save(Session("s", [{"role": "user", "content": "你好"}]))

    assert store.load("s").messages[0]["content"] == "你好"


# --- 文件命名与清单 --------------------------------------------------------

def test_sessions_are_jsonl_so_they_do_not_pretend_to_be_one_json_document(workdir):
    """会话文件的扩展名必须是 `.jsonl`。

    内容是一行一条记录，而 `.json` 会让任何一个编辑器/工具以为"这是一份 JSON
    文档"然后读失败。`list_ids()` 也按这个后缀 glob —— 两者的判据是同一个常量，
    不是两处各写一次字面量。
    """
    store = JsonSessionStore(workdir)
    store.save(Session("s"))

    assert store._path("s").name == f"s{SUFFIX}"
    assert store.list_ids() == ["s"]


def test_old_snapshot_files_are_not_listed(workdir):
    """老的 `.json` 会话文件不再被认作会话 —— 有意为之，见 store 的模块 docstring。

    这条把那个取舍**钉成可执行的**：哪天有人顺手把 list_ids 的 glob 放宽成
    "两种后缀都收"，这条会红。
    """
    (workdir / "old-one.json").write_text('{"session_id": "old-one"}', encoding="utf-8")
    store = JsonSessionStore(workdir)

    assert store.list_ids() == []
    assert not store.exists("old-one")


def test_sink_appends_and_skips_torn_line(workdir):
    """审计日志只追加。进程被杀会留下半截行，读的时候跳过它就行。"""
    sink = JsonlSink(workdir)
    sink({"kind": "a", "session_id": "s"})
    sink({"kind": "b", "session_id": "s"})
    with (workdir / "s.jsonl").open("a", encoding="utf-8") as f:
        f.write('{"kind": "torn", "ts": "2026')       # 半截
    assert [e["kind"] for e in sink.read("s")] == ["a", "b"]


def test_new_session_ids_are_unique_and_sortable(workdir):
    store = JsonSessionStore(workdir)
    first = store.new_session_id()
    store.save(Session(first))
    assert store.new_session_id() != first            # 撞名不许覆盖
    assert first[:9].isdigit() or "-" in first        # 时间戳形态，保证 --list 按时间排


def test_step_count_is_derived(workdir):
    """步数不落盘：存成字段就有了两份，而且语义会从"这一轮"变成"整个会话"。"""
    assert set(Session.__dataclass_fields__) == {"session_id", "messages", "metadata"}
    session = Session("s", [{"role": "assistant", "content": "a"},
                            {"role": "user", "content": "u"},
                            {"role": "assistant", "content": "b"}])
    assert session.step_count() == 2
