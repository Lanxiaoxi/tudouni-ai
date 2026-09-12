"""会话清单（`--list` 和 TUI 选会话面板的数据）的排序与内容。

## 为什么这一条值得单独一个文件

"按创建时间排"这件事以前是**靠 id 恰好长得像时间戳**成立的：自动分配的 id 就是
时间戳，所以 `sorted(ids, reverse=True)` 和真正的按时间排在那批会话上一模一样 ——
bug 因此一直看不出来。`--session demo` 这种自己起的名字不是时间戳，它会被排到
`20250101-…` 后面，而 `demo` 可能是昨天才建的。

所以这里的每一条都在钉**判据是数据，不是文件名的写法**：

  * 时间来自 `metadata["created_at"]`（新建会话时写进去，跟着会话文件落盘）；
  * 老会话文件没有那个键 → 退到文件 mtime；
  * 连文件都读不出来 → 排最后。

它同时是 `session_summaries` 的"输出契约"测试（`--list` 那条路读的就是它）。
"""

from agent_runtime.runtime.composition import session_summaries
from agent_runtime.state import JsonSessionStore, Session


def _save(store: JsonSessionStore, session_id: str, *, created_at=None,
          says: str | None = None, todos: list[dict] | None = None) -> Session:
    """写一个会话文件。`created_at=None` 表示**不写那个键**（模拟老文件）。"""
    session = Session.new(session_id)
    if created_at is None:
        session.metadata.pop("created_at", None)
    else:
        session.metadata["created_at"] = created_at
    if says is not None:
        session.messages.append({"role": "user", "content": says})
        session.messages.append({"role": "assistant", "content": "好"})
    if todos is not None:
        session.metadata["todos"] = todos
    store.save(session)
    return session


def _ids(items: list[dict]) -> list[str]:
    return [item["session_id"] for item in items]


def test_the_list_is_ordered_by_creation_time_not_by_id(workdir):
    """**核心那条**：`demo` 是昨天建的，它必须排在今天建的 `a-session` 前面。

    两个 id 的字典序和创建时间**故意相反** —— 这正是"按 id 排"会翻车而"按创建时间
    排"不会的那个情形。时间取自 `metadata["created_at"]`，谁大谁在前。
    """
    store = JsonSessionStore(workdir)
    _save(store, "demo", created_at=1_700_000_000.0)      # 早
    _save(store, "a-session", created_at=1_700_000_900.0)  # 晚，可 id 更"小"

    assert _ids(session_summaries(store)) == ["a-session", "demo"]


def test_creation_time_outranks_message_count(workdir):
    """"聊得多的那个"不许跑到前面来 —— 那是"最近用过的"排序，不是创建时间。

    这条挡的是一种很容易顺手写出来的实现：按 `messages` 或按文件 mtime 排。
    """
    store = JsonSessionStore(workdir)
    _save(store, "chatty", created_at=1_700_000_000.0, says="很长的一次对话")
    _save(store, "quiet", created_at=1_700_000_900.0)

    items = session_summaries(store)
    assert _ids(items) == ["quiet", "chatty"]
    assert items[1]["messages"] > items[0]["messages"], "条数确实是反的，这才说明排序没看它"


def test_old_files_fall_back_to_mtime(workdir):
    """老会话文件（没有 `created_at`）**退到文件 mtime**，而不是一起沉到底。

    这条是"升级之后老会话的列表还讲不讲道理"那件事。mtime 说的其实是"最后一次聊"，
    所以这个排序对老文件是近似的 —— 但**同一台机器上的先后**是真的，比全给 0 好。
    """
    import os

    store = JsonSessionStore(workdir)
    _save(store, "newer", created_at=1_700_000_900.0)
    _save(store, "older-named-z", created_at=None)
    _save(store, "older-named-a", created_at=None)
    # 两个老文件：z 比 a 新。
    os.utime(store._path("older-named-a"), (1_600_000_000.0, 1_600_000_000.0))
    os.utime(store._path("older-named-z"), (1_600_000_500.0, 1_600_000_500.0))

    assert _ids(session_summaries(store)) == [
        "newer", "older-named-z", "older-named-a",
    ]


def test_equal_times_are_broken_by_id(workdir):
    """时间一样时**由 id 决定次序**，不许随排序实现飘。

    同一秒里建了两个会话、或者老文件的 mtime 精度只到秒，都会撞上这一条。没有这个
    兜底分量的话，列表顺序会在"换个 Python 版本"之后变样 —— 而那种问题没人会往
    排序上想。
    """
    store = JsonSessionStore(workdir)
    _save(store, "b-second", created_at=1_700_000_000.0)
    _save(store, "a-first", created_at=1_700_000_000.0)

    assert _ids(session_summaries(store)) == ["b-second", "a-first"]


def test_the_limit_keeps_the_newest_not_the_biggest_ids(workdir):
    """`limit` 卡的是**最新的 N 个**，不是"id 最大的 N 个"。

    截断必须发生在排序**之后**：`store.list_ids()` 是按 id 升序给的，先切尾巴再排序
    会挑出一批和"最近"无关的会话（实测踩过：这条一开始就是"先切后排"的写法）。
    """
    store = JsonSessionStore(workdir)
    # id 的字典序和创建时间相反：zzz 最老，aaa 最新。
    _save(store, "zzz-oldest", created_at=1_700_000_000.0)
    _save(store, "mmm-middle", created_at=1_700_000_500.0)
    _save(store, "aaa-newest", created_at=1_700_000_900.0)

    assert _ids(session_summaries(store, limit=2)) == ["aaa-newest", "mmm-middle"]


def test_a_broken_file_does_not_take_the_whole_list_down(workdir):
    """一个坏文件只在列表里变成一行"读不出来"，别的一个都不少。

    会话文件可能被截断、也可能是更新版本写的（`store.load` 会为此抛 ValueError）。
    让整个面板打不开的后果比这坏得多：**用户根本不知道有一个坏文件**。
    """
    store = JsonSessionStore(workdir)
    _save(store, "good", created_at=1_700_000_900.0)
    store._path("broken").write_text("{ 这不是 JSON", encoding="utf-8")

    items = session_summaries(store)
    by_id = {item["session_id"]: item for item in items}
    assert set(by_id) == {"good", "broken"}
    assert "读不出来" in by_id["broken"]["preview"]
    assert by_id["broken"]["messages"] == 0


def test_the_summary_carries_what_a_picker_needs(workdir):
    """每一条都带面板要的那几样，而且**预览是第一句用户说的话**。

    预览取第一句而不是最后一句：选会话时人要认的是"这是哪一次对话"，而开头那句话
    就是它的标题（最后一句通常是"继续"、"嗯"这种认不出来的东西）。
    """
    store = JsonSessionStore(workdir)
    _save(store, "s", created_at=1_700_000_000.0, says="帮我把 TUI 的换会话做掉",
          todos=[{"content": "改协议", "status": "completed"}])

    item, = session_summaries(store)
    assert set(item) == {"session_id", "messages", "steps", "preview", "todos",
                         "modified_at"}
    assert item["preview"].startswith("帮我把 TUI 的换会话做掉")
    assert item["messages"] == 3, "system + 用户那句 + 助手那句"
    assert item["steps"] == 1, "一步 = 一条 assistant"
    assert item["todos"], "任务进度跟在这一行最后（哪个会话还剩着活）"


def test_modified_at_is_the_file_time_not_the_creation_time(workdir):
    """`modified_at` 说的是**最后一次聊**，它和排序用的 `created_at` 是两件事。

    这条挡的是一种很自然的偷懒实现：拿 `created_at` 当 `modified_at` 发出去。那样
    TUI 欢迎屏右上那栏（"最近动过哪几个会话"）会把一个昨天建、今天还在聊的会话排到
    昨天去 —— 而列表看起来完全正常。
    """
    import os

    store = JsonSessionStore(workdir)
    _save(store, "s", created_at=1_700_000_000.0, says="昨天建的")
    os.utime(store._path("s"), (1_700_900_000.0, 1_700_900_000.0))

    item, = session_summaries(store)
    assert item["modified_at"] == 1_700_900_000.0
    assert item["modified_at"] != 1_700_000_000.0, "不许拿 created_at 顶替"


def test_a_broken_file_still_reports_its_file_time(workdir):
    """坏文件的那一条**也有 `modified_at`**（它的 mtime 是好的）。

    它读不出来，但"这个文件什么时候被动过"仍然是文件系统的事实 —— 而"最近活动"
    那一栏正需要它（`store.load` 失败和"文件不存在"是两件事）。
    """
    store = JsonSessionStore(workdir)
    store._path("broken").write_text("{ 这不是 JSON", encoding="utf-8")

    item, = session_summaries(store)
    assert "读不出来" in item["preview"]
    assert isinstance(item["modified_at"], float)


def test_a_new_session_records_when_it_was_created(workdir):
    """新建会话就写下 `created_at` —— 排序的那个判据**得有人写**。

    没有它，`session_summaries` 对每个新会话都会退到 mtime，而 mtime 是"最后一次
    聊"：切回一个老会话说一句话，它就会跳到列表最上面。
    """
    import time

    before = time.time()
    session = Session.new("s")
    after = time.time()

    assert before <= session.metadata["created_at"] <= after
    # **不进 messages**：那是发给模型的东西，而这一条只是本地的书签。
    assert all("created_at" not in str(message) for message in session.messages)

    # 落盘 → 读回来，它还在（不然排序判据活不过一次重启）。
    store = JsonSessionStore(workdir)
    session.messages.append({"role": "user", "content": "你好"})
    store.save(session)
    assert store.load("s").metadata["created_at"] == session.metadata["created_at"]
