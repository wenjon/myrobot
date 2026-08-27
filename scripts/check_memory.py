"""记忆模块自检（docs 第 16 章）。

不请求 LLM、不需要 API Key：用一个假的 summarizer 把反思链路跑通，
所以能放进 CI 静态检查。测七组行为：

    1. UserProfile 合并的三种分支（新字段 / 值相同 / 值变了）
    2. 冲突的接受与拒绝，以及超时保旧
    3. 反思输出的 JSON 解析（含代码块、废话前缀、坏 JSON 降级）
    4. AllMemoryRetriever 的顺序与字符预算取舍
    5. build_messages 真的把画像与摘要贴进了 system
    6. FileStore 的存读、原子写、坏文件降级、路径穿越防护
    7. 滑动窗口裁剪与持久化往返（保真）

用法：
    python scripts/check_memory.py
退出码 0 = 全部通过。1 = 有失败。
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import tempfile
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "backend"))

# 关键：先把记忆目录指到临时目录，避免自检跑一次就把真实记忆覆盖掉。
_TMP = tempfile.mkdtemp(prefix="mem_selfcheck_")
os.environ["MEMORY_DATA_DIR"] = _TMP
os.environ["MEMORY_PERSIST"] = "1"

from config import PROFILE_FIELDS, SYSTEM_PROMPT   # noqa: E402
from memory import (                                # noqa: E402
    STORE, AllMemoryRetriever, MemoryQuery, MemoryKind,
    ProfileConflict, UserProfile,
)
from pipeline.conversation import (                 # noqa: E402
    ConversationManager, Session, _parse_reflection, clean_for_memory,
)

_failures = []
_checks = 0


def ck(name, cond, extra=""):
    global _checks
    _checks += 1
    if cond:
        print(f"  [ok]   {name}")
    else:
        print(f"  [FAIL] {name} {extra}")
        _failures.append(name)


def section(title):
    print("")
    print(f"-- {title}")


# =====================================================================
def test_profile_merge():
    section("1. UserProfile.merge 三种分支")
    p = UserProfile()

    # 新字段：直接写入，不产生冲突
    c = p.merge({"name": "小王", "occupation": "CTO"})
    ck("新字段直接合并不产生冲突", c == [] and p.get("name") == "小王")

    # 值相同：什么都不做
    c = p.merge({"name": "小王"})
    ck("值相同时无冲突", c == [])

    # 值变了：产生冲突，且**不能先写进去**
    c = p.merge({"name": "老王"}, source_quote="以后叫我老王")
    ck("值变更产生 1 条冲突", len(c) == 1 and c[0].field_name == "name")
    ck("冲突未落地前保持旧值", p.get("name") == "小王", f"实际={p.get('name')!r}")
    ck("冲突带上了原句", c[0].source_quote == "以后叫我老王")

    # 白名单之外的字段直接丢掉（防 LLM 自由发挥）
    p.merge({"key_facts": "不应该存这个", "hobby": "滑雪"})
    ck("非白名单字段被丢弃", set(p.fields.keys()) <= set(PROFILE_FIELDS))

    # 过长值被截断（防撑爆 system）
    p2 = UserProfile()
    p2.merge({"preferences": "喯" * 500})
    ck("超长字段被截断到 200", len(p2.get("preferences")) == 200)


def test_conflict_resolve():
    section("2. 冲突接受 / 拒绝 / 超时保旧")
    p = UserProfile()
    p.merge({"occupation": "CTO"})

    # 接受 → 写新值
    c = p.merge({"occupation": "设计师"})[0]
    p.apply_conflict(c, accept=True)
    ck("accept 写入新值", p.get("occupation") == "设计师")

    # 拒绝 → 保旧 + 记审计
    c2 = p.merge({"occupation": "学生"})[0]
    p.apply_conflict(c2, accept=False)
    ck("reject 保留旧值", p.get("occupation") == "设计师")
    ck("reject 记了一笔审计", len(p.rejected_changes) == 1
       and p.rejected_changes[0]["new"] == "学生")

    # 超时判定
    fresh = ProfileConflict(field_name="name", old_value="a", new_value="b")
    ck("新建冲突未过期", not fresh.is_expired())
    old = ProfileConflict(field_name="name", old_value="a", new_value="b")
    old.created_at = time.time() - 99999
    ck("陈旧冲突已过期", old.is_expired())

    # Session 层面：超时清理后不再下发，且画像保旧
    s = Session("sess_timeout")
    s.profile.merge({"name": "旧名"})
    c3 = s.profile.merge({"name": "新名"})[0]
    c3.created_at = time.time() - 99999
    s.pending_conflicts[c3.conflict_id] = c3
    ck("超时冲突被清理不下发", s.take_conflicts_to_notify() == []
       and not s.pending_conflicts)
    ck("超时后画像保旧", s.profile.get("name") == "旧名")

    # 未知 conflict_id 不能炸
    ck("resolve 未知 id 返回 None", s.resolve_conflict("nope", True) is None)

    # 一轮最多下发 N 条
    from config import PROFILE_MAX_CONFLICTS_PER_TURN as MAXC
    s2 = Session("sess_many")
    for i in range(MAXC + 2):
        c = ProfileConflict(field_name="name", old_value=f"o{i}", new_value=f"n{i}")
        s2.pending_conflicts[c.conflict_id] = c
    ck(f"一轮最多下发 {MAXC} 条", len(s2.take_conflicts_to_notify()) == MAXC)


def test_parse_reflection():
    section("3. 反思输出解析")
    # 标准 JSON
    raw = json.dumps({"summary": "聊了天气", "profile": {"name": "小王"}, "quote": "叫我小王"},
                     ensure_ascii=False)
    sm, pf, q = _parse_reflection(raw)
    ck("标准 JSON 解析", sm == "聊了天气" and pf == {"name": "小王"} and q == "叫我小王")

    # 包着 markdown 代码块
    sm, pf, _ = _parse_reflection("```json\n" + raw + "\n```")
    ck("剥除 markdown 代码块", sm == "聊了天气" and pf == {"name": "小王"})

    # 前后带废话
    sm, pf, _ = _parse_reflection("好的，结果如下：" + raw + " 希望有帮助")
    ck("忽略 JSON 前后废话", sm == "聊了天气")

    # 壍 JSON → 降级当纯文本摘要（不能丢整次提炼）
    sm, pf, _ = _parse_reflection("这是一段普通摘要，没有 JSON")
    ck("坏 JSON 降级为纯文本摘要", sm == "这是一段普通摘要，没有 JSON" and pf == {})

    # 空输入
    ck("空输入不炸", _parse_reflection("") == ("", {}, ""))

    # profile 里的非字符串值被忽略
    weird = json.dumps({"summary": "s", "profile": {"name": 123, "occupation": None,
                                                     "preferences": "简洁"}}, ensure_ascii=False)
    _, pf, _ = _parse_reflection(weird)
    ck("非字符串画像值被忽略", pf == {"preferences": "简洁"})


def test_retriever():
    section("4. AllMemoryRetriever")
    s = Session("sess_r")
    s.profile.merge({"name": "小王", "preferences": "喜欢简洁"})
    s.summary = "上次聊了机票"
    r = AllMemoryRetriever()

    hits = r.retrieve(s, MemoryQuery())
    ck("取到 2 条（画像+摘要）", len(hits) == 2)
    ck("画像在前、摘要在后", hits[0].source == "profile" and hits[1].source == "summary")
    ck("画像文本含中文标签", "称呼：小王" in hits[0].text)

    # 空记忆：不应该贴空标题
    ck("空会话不产生命中", r.retrieve(Session("empty"), MemoryQuery()) == [])

    # 字符预算：不够时先丢摘要、保住画像
    small = r.retrieve(s, MemoryQuery(max_chars=len(hits[0].text)))
    ck("预算不足时优先保画像", len(small) == 1 and small[0].source == "profile")

    # kinds 过滤
    ck("kinds 过滤生效", r.retrieve(s, MemoryQuery(kinds=[MemoryKind.EPISODIC])) == [])
    # top_k
    ck("top_k 生效", len(r.retrieve(s, MemoryQuery(top_k=1))) == 1)


def test_build_messages():
    section("5. build_messages 拼接")
    s = Session("sess_b")
    s.profile.merge({"name": "小王"})
    s.summary = "上次聊了机票"
    s.history = [{"role": "user", "content": "你好"},
                 {"role": "assistant", "content": "嗨"}]

    msgs = s.build_messages("今天天气如何")
    ck("首条是 system", msgs[0]["role"] == "system")
    ck("system 含人设", SYSTEM_PROMPT[:20] in msgs[0]["content"])
    ck("system 含用户画像", "【用户画像】" in msgs[0]["content"])
    ck("system 含对话摘要", "【对话记忆摘要】" in msgs[0]["content"])
    ck("中间是窗口历史", msgs[1]["content"] == "你好" and msgs[2]["content"] == "嗨")
    ck("末条是本轮 user", msgs[-1]["role"] == "user" and msgs[-1]["content"] == "今天天气如何")

    # 空记忆时 system 应与原人设一致（不多贴空标题）
    ck("无记忆时 system 不多余", Session("e").build_messages("x")[0]["content"] == SYSTEM_PROMPT)

    # 入库清洗：标记与 markdown 被剔除
    s2 = Session("sess_clean")
    s2.begin_turn("你好")
    s2.commit_turn("[表情:开心]**你好**呀")
    ck("assistant 入库已清洗", s2.history[1]["content"] == "你好呀",
       f"实际={s2.history[1]['content']!r}")

    # 回滚：不留孤儿
    s3 = Session("sess_rb")
    s3.begin_turn("孤儿句")
    s3.rollback_turn()
    ck("rollback 不留孤儿", s3.history == [] and s3.pending_user is None)


def test_store():
    section("6. FileStore 持久化")
    ck("自检目录已隔离到临时目录", str(STORE.root) == _TMP,
       f"root={STORE.root}")

    # 存读往返
    STORE.save_session("abc", {"history": [{"role": "user", "content": "hi"}], "summary": "s"})
    got = STORE.load_session("abc")
    ck("会话存读往返", got and got["summary"] == "s")

    # 不存在的会话返回 None
    ck("不存在的会话返回 None", STORE.load_session("nope_xyz") is None)

    # 坏文件降级：不抛异常，且备份改名
    bad = STORE.session_path("broken")
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_text("{这不是 json", encoding="utf-8")
    ck("坏 JSON 降级为 None 不抛异常", STORE.load_session("broken") is None)
    ck("坏文件已备份改名", not bad.exists()
       and any(".broken." in f.name for f in bad.parent.iterdir()))

    # 长期记忆往返
    STORE.save_long_term({"profile": {"fields": {"name": "小王"}}})
    lt = STORE.load_long_term()
    ck("长期记忆往返", lt and lt["profile"]["fields"]["name"] == "小王")

    # 路径穿越防护：session_id 里的 ../ 不能逃出 sessions 目录
    evil = STORE.session_path("../../../../etc/passwd")
    ck("路径穿越被阻止", evil.parent == STORE.sessions_dir, f"实际={evil}")

    # 删除
    STORE.delete_session("abc")
    ck("删除会话生效", STORE.load_session("abc") is None)

    # 原子写：不遗留 .tmp 文件
    ck("不遗留 .tmp 中间文件",
       not any(f.name.endswith(".tmp") for f in STORE.sessions_dir.iterdir()))

    # 内存模式（MEMORY_PERSIST=0）应完全不碰磁盘
    from memory.store import FileStore
    off = FileStore(root=os.path.join(_TMP, "off"))
    off.enabled = False
    off.save_session("x", {"a": 1})
    ck("关持久化时不建目录", not os.path.exists(os.path.join(_TMP, "off")))


def test_roundtrip_and_reflect():
    section("7. 滑动窗口 / 持久化往返 / 反思链路")
    from config import MAX_TURNS

    mgr = ConversationManager()
    s = mgr.get("sess_rt")

    # 塑造超过窗口的历史，验证被裁部分进了 dropped_buffer
    for i in range(MAX_TURNS + 5):
        s.begin_turn(f"问题{i}")
        s.commit_turn(f"回答{i}")
    s.build_messages("触发裁剪")
    ck("超出窗口的历史进了待摘要缓冲", len(s.dropped_buffer) > 0)
    ck("窗口不超 MAX_TURNS*2", len(s.history) <= MAX_TURNS * 2)

    # 落盘 + 新管理器重新读回
    s.profile.merge({"name": "小王"})
    s.summary = "这是摘要"
    s.save()
    mgr2 = ConversationManager()
    s2 = mgr2.get("sess_rt")
    ck("重启后历史恢复", len(s2.history) == len(s.history))
    ck("重启后摘要恢复", s2.summary == "这是摘要")
    ck("重启后画像恢复", s2.profile.get("name") == "小王")

    # 反思链路（假 summarizer，不请求 LLM）
    async def fake_summarizer(prompt):
        # 验证提示词真的要求了双字段输出
        assert "profile" in prompt[0]["content"], "反思提示词应要求 profile"
        return json.dumps({"summary": "用户叫老王，喜欢简洁",
                           "profile": {"name": "老王", "preferences": "喜欢简洁"},
                           "quote": "叫我老王"}, ensure_ascii=False)

    s3 = mgr2.get("sess_reflect")
    s3.profile.merge({"name": "小王"})          # 先有旧值
    # 填满 dropped_buffer 以触发反思
    from config import SUMMARY_TRIGGER_CHARS
    s3.dropped_buffer = [{"role": "user", "content": "喯" * (SUMMARY_TRIGGER_CHARS + 10)}]
    ck("达阈值后 needs_summary 为真", s3.needs_summary())

    conflicts = asyncio.run(mgr2.maybe_summarize(s3, fake_summarizer))
    ck("反思后摘要已写入", "老王" in s3.summary)
    ck("新字段 preferences 直接合并", s3.profile.get("preferences") == "喜欢简洁")
    ck("name 变更产生待确认冲突", len(conflicts) == 1 and conflicts[0].field_name == "name")
    ck("冲突未确认前 name 保旧", s3.profile.get("name") == "小王")
    ck("冲突已登记到 pending", conflicts[0].conflict_id in s3.pending_conflicts)
    ck("反思后待摘要缓冲已清空", s3.dropped_buffer == [])

    # 确认接受 → 落地
    got = s3.resolve_conflict(conflicts[0].conflict_id, True)
    ck("resolve 返回冲突对象", got is not None)
    ck("接受后 name 已更新", s3.profile.get("name") == "老王")

    # 反思失败要回滚缓冲，不能丢记忆
    async def boom(prompt):
        raise RuntimeError("模拟 LLM 挂了")
    s4 = mgr2.get("sess_fail")
    s4.dropped_buffer = [{"role": "user", "content": "喯" * (SUMMARY_TRIGGER_CHARS + 10)}]
    n_before = len(s4.dropped_buffer)
    out = asyncio.run(mgr2.maybe_summarize(s4, boom))
    ck("反思失败不抛异常", out == [])
    ck("反思失败后缓冲已回滚", len(s4.dropped_buffer) == n_before)

    # 未达阈值不该调 LLM
    async def should_not_call(prompt):
        raise AssertionError("未达阈值却调了 LLM")
    s5 = mgr2.get("sess_nothresh")
    s5.dropped_buffer = [{"role": "user", "content": "太短"}]
    ck("未达阈值不调 LLM", asyncio.run(mgr2.maybe_summarize(s5, should_not_call)) == [])

    # clear 同步删盘
    s2.clear()
    ck("clear 后内存已空", s2.history == [] and s2.summary == "" and s2.profile.is_empty())
    ck("clear 后磁盘文件已删", STORE.load_session("sess_rt") is None)


def main():
    print("=" * 62)
    print("记忆模块自检（不请求 LLM）")
    print("=" * 62)
    try:
        test_profile_merge()
        test_conflict_resolve()
        test_parse_reflection()
        test_retriever()
        test_build_messages()
        test_store()
        test_roundtrip_and_reflect()
    finally:
        shutil.rmtree(_TMP, ignore_errors=True)

    print("")
    if _failures:
        print(f"失败：{len(_failures)}/{_checks} 项不通过：")
        for f in _failures:
            print(f"  - {f}")
        return 1
    print(f"OK：{_checks} 项记忆模块检查全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
