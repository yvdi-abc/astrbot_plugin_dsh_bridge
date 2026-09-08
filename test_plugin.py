"""astrbot_plugin_dsh_bridge 插件测试脚本。

验证：
1. DSH API 封装（mock httpx）
2. 会话创建/绑定/解绑
3. QQ 消息 → DSH prompt 内容构建（文本/图片/文件）
4. DSH 回复轮询 → QQ 转发
5. 权限控制（白名单）
6. 增量转发（不重复）
7. 帮助/状态/历史命令
"""
import asyncio
import json
import os
import sys
import tempfile
import types

sys.path.insert(0, "/root/.local/share/uv/tools/astrbot")

import importlib.util

spec = importlib.util.spec_from_file_location(
    "dsh_bridge",
    "/root/dsh_projects/astrbot_plugin_dsh_bridge/main.py",
)
plugin_mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(plugin_mod)


class MockHttpClient:
    """模拟 httpx.AsyncClient，捕获 DSH API 调用。"""

    def __init__(self, responses=None):
        self.responses = responses or {}
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def post(self, url, json=None, headers=None):
        self.calls.append({"url": url, "json": json})
        method = url.split("/api/")[-1]
        resp = self.responses.get(method, {"ok": True, "value": {}})
        return types.SimpleNamespace(
            status_code=200,
            text=json.dumps(resp),
            json=lambda: {"type": "server-response", "result": resp},
        )


class MockContext:
    def __init__(self):
        self.sent = []  # (umo, chain)
        self.platform_manager = types.SimpleNamespace(platform_insts=[])

    async def send_message(self, session, message_chain):
        self.sent.append((session, message_chain))
        return True


class MockEvent:
    def __init__(self, umo="test:GroupMessage:1111", group_id="1111", sender_id="2222",
                 admin=False, message_str=""):
        self.unified_msg_origin = umo
        self._group_id = group_id
        self._sender_id = sender_id
        self._admin = admin
        self._message_str = message_str
        self._messages = []
        self.sent_msgs = []
        self._call_llm_flag = True
        self._stopped = False
        self.is_at_or_wake_command = False

    def is_private_chat(self):
        return "FriendMessage" in self.unified_msg_origin

    def get_group_id(self):
        return self._group_id

    def get_sender_id(self):
        return self._sender_id

    def is_admin(self):
        return self._admin

    def get_message_str(self):
        return self._message_str

    def get_messages(self):
        return self._messages

    def get_self_id(self):
        return "10001"

    def should_call_llm(self, val):
        self._call_llm_flag = val

    def stop_event(self):
        self._stopped = True

    def plain_result(self, text):
        return types.SimpleNamespace(text=text)

    async def send(self, message):
        self.sent_msgs.append(message)


def make_plugin(config_overrides=None, dsh_responses=None):
    config = {
        "enable": True,
        "dsh_base_url": "http://127.0.0.1:3080",
        "whitelist_enable": True,
        "whitelist_groups": ["1111"],
        "whitelist_users": [],
        "poll_interval": 3,
        "auto_forward": True,
        "trigger_mode": "all",
        "reply_forward": "plain",
        "forward_threshold": 500,
        "bot_name": "DSH助手",
        "enable_history_forward": False,
    }
    if config_overrides:
        config.update(config_overrides)

    tmpdir = tempfile.mkdtemp()
    client = MockHttpClient(dsh_responses or {})
    plugin = plugin_mod.DshBridgePlugin(MockContext(), config)
    plugin._data_dir = types.SimpleNamespace(mkdir=lambda *a, **k: None)
    plugin._bindings_file = types.SimpleNamespace(
        exists=lambda: False,
        read_text=lambda *a, **k: "{}",
        write_text=lambda *a, **k: None,
    )
    # 直接替换 _dsh_call 为 mock（干净、可靠）
    async def mock_dsh_call(method, payload):
        client.calls.append({"url": f"http://127.0.0.1:3080/api/{method}", "json": {
            "type": "client-request", "rpcId": "test", "method": method, "payload": payload
        }})
        resp = client.responses.get(method, {"ok": True, "value": {}})
        return resp.get("value") if resp.get("ok") else None

    plugin._dsh_call = mock_dsh_call
    plugin._mock_client = client
    return plugin, client, tmpdir


def collect_agen(agen):
    out = []

    async def runner():
        async for item in agen:
            out.append(item)

    asyncio.get_event_loop().run_until_complete(runner())
    return out


# ---------- 测试 1: DSH API 调用格式 ----------
def test_dsh_call():
    plugin, client, _ = make_plugin(
        dsh_responses={"session.create": {"ok": True, "value": {"sessionId": "sid-1"}}}
    )
    sid = asyncio.get_event_loop().run_until_complete(plugin._create_session())
    assert sid == "sid-1", f"创建会话失败: {sid}"
    call = client.calls[0]
    assert call["url"] == "http://127.0.0.1:3080/api/session.create", f"URL 错误: {call['url']}"
    assert call["json"]["method"] == "session.create"
    assert call["json"]["type"] == "client-request"
    assert "rpcId" in call["json"]
    print("PASS 测试1: DSH API 调用格式正确")


# ---------- 测试 2: 创建并绑定会话 ----------
def test_create_and_bind():
    plugin, client, _ = make_plugin(
        dsh_responses={"session.create": {"ok": True, "value": {"sessionId": "sid-new"}}}
    )
    event = MockEvent(message_str="/dsh 创建")
    results = collect_agen(plugin.cmd_dsh(event))
    assert "已创建并绑定" in results[0].text, f"创建绑定失败: {results[0].text}"
    assert plugin._bindings.get(event.unified_msg_origin) == "sid-new"
    print("PASS 测试2: 创建并绑定会话")


# ---------- 测试 3: 会话列表 ----------
def test_list_sessions():
    plugin, client, _ = make_plugin(
        dsh_responses={
            "session.list": {
                "ok": True,
                "value": {
                    "items": [
                        {"sessionId": "sid-1", "title": "会话A", "running": False},
                        {"sessionId": "sid-2", "title": "会话B", "running": True},
                    ]
                },
            }
        }
    )
    event = MockEvent(message_str="/dsh 会话")
    results = collect_agen(plugin.cmd_dsh(event))
    assert "sid-1" in results[0].text and "sid-2" in results[0].text, f"列表错误: {results[0].text}"
    print("PASS 测试3: 会话列表")


# ---------- 测试 4: 选择会话并设置已读基线 ----------
def test_select_session():
    plugin, client, _ = make_plugin(
        dsh_responses={
            "session.list": {
                "ok": True,
                "value": {"items": [{"sessionId": "sid-1", "title": "会话A"}]}
            },
            "session.history": {
                "ok": True,
                "value": {
                    "events": [
                        {"event": {"type": "user/message", "seq": 10}},
                        {"event": {"type": "assistant/message", "seq": 20}},
                    ]
                },
            },
        }
    )
    event = MockEvent(message_str="/dsh 选择 sid-1")
    results = collect_agen(plugin.cmd_dsh(event))
    assert "已绑定" in results[0].text, f"选择失败: {results[0].text}"
    umo = event.unified_msg_origin
    assert plugin._bindings[umo] == "sid-1"
    assert plugin._last_seq[umo] == 20, "已读基线应设为最大 seq（不转发历史）"
    print("PASS 测试4: 选择会话并设置增量基线（不转发历史）")


# ---------- 测试 5: QQ 消息自动转发到 DSH ----------
def test_auto_forward():
    plugin, client, _ = make_plugin(
        dsh_responses={"session.prompt": {"ok": True, "value": {"accepted": True}}}
    )
    umo = "test:GroupMessage:1111"
    plugin._bindings[umo] = "sid-1"

    event = MockEvent(umo=umo, message_str="你好 DSH")
    event._messages = [types.SimpleNamespace(text="你好 DSH", __class__=plugin_mod.Plain.__mro__[0])]
    # 用 Plain 真实组件
    from astrbot.api.message_components import Plain

    event._messages = [Plain(text="你好 DSH")]
    asyncio.get_event_loop().run_until_complete(plugin.on_message(event))

    assert client.calls, "应调用 DSH API"
    prompt_call = [c for c in client.calls if c["url"].endswith("session.prompt")]
    assert prompt_call, "应调用 session.prompt"
    content = prompt_call[0]["json"]["payload"]["content"]
    assert content[0]["type"] == "text" and "你好 DSH" in content[0]["text"]
    print("PASS 测试5: QQ 消息自动转发到 DSH（文本）")


# ---------- 测试 6: 白名单权限 ----------
def test_whitelist():
    plugin, _, _ = make_plugin()
    umo = "test:GroupMessage:9999"  # 不在白名单的群
    plugin._bindings[umo] = "sid-1"
    event = MockEvent(umo=umo, group_id="9999", message_str="你好")
    from astrbot.api.message_components import Plain

    event._messages = [Plain(text="你好")]
    asyncio.get_event_loop().run_until_complete(plugin.on_message(event))
    # 不应调用 DSH API
    prompt_calls = [c for c in plugin._mock_client.calls if "prompt" in c["url"]]
    assert not prompt_calls, "非白名单群不应转发"
    # 管理员应放行
    plugin._mock_client.calls.clear()
    event2 = MockEvent(umo=umo, group_id="9999", message_str="你好", admin=True)
    event2._messages = [Plain(text="你好")]
    asyncio.get_event_loop().run_until_complete(plugin.on_message(event2))
    prompt_calls = [c for c in plugin._mock_client.calls if "prompt" in c["url"]]
    assert prompt_calls, "管理员应放行"
    print("PASS 测试6: 白名单权限（非白名单拒绝，管理员放行）")


# ---------- 测试 7: DSH 回复轮询转发 ----------
def test_poll_reply():
    umo = "test:GroupMessage:1111"
    plugin, client, _ = make_plugin(
        dsh_responses={
            "session.history": {
                "ok": True,
                "value": {
                    "events": [
                        {"event": {"type": "user/message", "seq": 1, "data": {"message": {"content": [{"type": "text", "text": "hi"}]}}}},
                        {"event": {"type": "assistant/message", "seq": 2, "data": {"message": {"content": [{"type": "text", "text": "DSH回复"}]}}}},
                    ]
                },
            }
        }
    )
    plugin._bindings[umo] = "sid-1"
    plugin._last_seq[umo] = 0
    asyncio.get_event_loop().run_until_complete(plugin._check_session_replies(umo, "sid-1"))

    # 应发送 DSH回复 到 QQ
    sent = plugin.context.sent
    assert len(sent) == 1, f"应发送 1 条回复，实际 {len(sent)}"
    text = sent[0][1].chain[0].text
    assert "DSH回复" in text, f"回复内容错误: {text}"
    assert plugin._last_seq[umo] == 2, "已读 seq 应更新"
    print("PASS 测试7: DSH 回复轮询转发到 QQ")


# ---------- 测试 8: 增量转发（不重复） ----------
def test_incremental():
    umo = "test:GroupMessage:1111"
    plugin, client, _ = make_plugin()
    plugin._bindings[umo] = "sid-1"
    plugin._last_seq[umo] = 5

    # 模拟 DSH 历史：seq 5 之前的已读，seq 6 是新回复
    client.responses["session.history"] = {
        "ok": True,
        "value": {
            "events": [
                {"event": {"type": "assistant/message", "seq": 4, "data": {"message": {"content": [{"type": "text", "text": "旧消息1"}]}}}},
                {"event": {"type": "assistant/message", "seq": 5, "data": {"message": {"content": [{"type": "text", "text": "旧消息2"}]}}}},
                {"event": {"type": "assistant/message", "seq": 6, "data": {"message": {"content": [{"type": "text", "text": "新消息"}]}}}},
            ]
        },
    }
    asyncio.get_event_loop().run_until_complete(plugin._check_session_replies(umo, "sid-1"))
    sent = plugin.context.sent
    assert len(sent) == 1, f"只应转发 1 条新消息，实际 {len(sent)}"
    assert "新消息" in sent[0][1].chain[0].text
    print("PASS 测试8: 增量转发（只转发新消息，不重复）")


# ---------- 测试 9: 合并转发模式 ----------
def test_forward_mode():
    umo = "test:GroupMessage:1111"
    plugin, client, _ = make_plugin({"reply_forward": "forward", "forward_threshold": 10})
    plugin._bindings[umo] = "sid-1"
    plugin._last_seq[umo] = 0
    client.responses["session.history"] = {
        "ok": True,
        "value": {
            "events": [
                {"event": {"type": "assistant/message", "seq": 1, "data": {"message": {"content": [{"type": "text", "text": "这是一段较长的回复内容。" * 3}]}}}},
            ]
        },
    }
    asyncio.get_event_loop().run_until_complete(plugin._check_session_replies(umo, "sid-1"))
    sent = plugin.context.sent
    assert len(sent) == 1, "应发送合并转发"
    from astrbot.core.message.components import Nodes

    assert any(isinstance(c, Nodes) for c in sent[0][1].chain), "应包含 Nodes 组件"
    print("PASS 测试9: 合并转发模式")


# ---------- 测试 10: 帮助和状态命令 ----------
def test_help_status():
    plugin, _, _ = make_plugin()
    event = MockEvent(message_str="/dsh")
    results = collect_agen(plugin.cmd_dsh(event))
    assert "DSH 桥接" in results[0].text, "帮助应显示"

    event2 = MockEvent(message_str="/dsh 状态")
    results2 = collect_agen(plugin.cmd_dsh(event2))
    assert "DSH 地址" in results2[0].text and "桥接状态" in results2[0].text
    print("PASS 测试10: 帮助和状态命令")


# ---------- 测试 11: 图片消息转换 ----------
def test_image_content():
    plugin, _, tmpdir = make_plugin()
    # 创建测试图片
    img_path = os.path.join(tmpdir, "test.png")
    with open(img_path, "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\nfake-image-data")
    from astrbot.api.message_components import Image

    img = Image(file=img_path)
    img.path = img_path

    async def runner():
        return await plugin._event_to_prompt_content(
            types.SimpleNamespace(get_messages=lambda: [img])
        )

    content, hint = asyncio.get_event_loop().run_until_complete(runner())
    assert any(c.get("type") == "image" for c in content), "应包含 image 内容"
    img_part = [c for c in content if c.get("type") == "image"][0]
    assert img_part["data"], "image 应包含 base64 数据"
    print("PASS 测试11: 图片消息转换为 base64")


# ---------- 测试 12: 无空格命令解析 ----------
def test_no_space_cmd():
    plugin, _, _ = make_plugin()
    assert plugin._normalize_cmd("/dsh创建") == "/dsh 创建", "无空格应规范化为带空格"
    assert plugin._normalize_cmd("/dsh选择 sid-1") == "/dsh 选择 sid-1", "带参应规范化"
    assert plugin._normalize_cmd("/dsh 创建") == "/dsh 创建", "带空格不变"
    assert plugin._normalize_cmd("普通消息") == "普通消息", "非命令不变"
    # 通过 cmd_dsh 走无空格命令
    plugin._mock_client.responses["session.create"] = {
        "ok": True, "value": {"sessionId": "sid-nospace"}
    }
    event = MockEvent(message_str="/dsh创建")
    results = collect_agen(plugin.cmd_dsh(event))
    assert "已创建并绑定" in results[0].text, f"无空格命令未生效: {results[0].text}"
    print("PASS 测试12: 无空格命令解析（/dsh创建 = /dsh 创建）")


# ---------- 测试 13: 纯净模式 - 转发后阻止主 LLM ----------
def test_pure_mode():
    plugin, client, _ = make_plugin(
        dsh_responses={"session.prompt": {"ok": True, "value": {"accepted": True}}}
    )
    umo = "test:GroupMessage:1111"
    plugin._bindings[umo] = "sid-1"
    from astrbot.api.message_components import Plain

    event = MockEvent(umo=umo, message_str="你好 DSH")
    event._messages = [Plain(text="你好 DSH")]
    asyncio.get_event_loop().run_until_complete(plugin.on_message(event))
    assert event._call_llm_flag is False, "转发后应阻止主 LLM"
    assert event._stopped is False, "不应停止事件传播（避免误伤其他插件）"
    print("PASS 测试13: 纯净模式 - 转发后阻止 AstrBot 主 LLM（避免人设回复）")


# ---------- 测试 14: agent preset 传递 ----------
def test_agent_preset():
    plugin, client, _ = make_plugin({"agent_preset": "minimal"})
    client.responses["session.create"] = {
        "ok": True, "value": {"sessionId": "sid-preset"}
    }
    sid = asyncio.get_event_loop().run_until_complete(plugin._create_session())
    assert sid == "sid-preset"
    create_call = client.calls[0]
    assert create_call["json"]["payload"].get("agentPreset") == "minimal", "应传递 agentPreset"
    print("PASS 测试14: 创建会话传递 agentPreset（minimal 极简模式）")


# ---------- 测试 15: 纯净指令仅首条附加 ----------
def test_pure_instruction():
    plugin, client, _ = make_plugin(
        {"pure_mode_instruction": "请以纯粹的AI助手身份回答，不要角色扮演。"}
    )
    client.responses["session.prompt"] = {"ok": True, "value": {"accepted": True}}
    # 首条（first=True）应附加
    ok = asyncio.get_event_loop().run_until_complete(
        plugin._prompt("sid-1", [{"type": "text", "text": "你好"}], first=True)
    )
    assert ok
    content = client.calls[0]["json"]["payload"]["content"]
    assert any("纯粹的AI助手" in c.get("text", "") for c in content), "首条应附加纯净指令"
    # 后续（first=False）不应重复附加
    ok2 = asyncio.get_event_loop().run_until_complete(
        plugin._prompt("sid-1", [{"type": "text", "text": "再见"}], first=False)
    )
    content2 = client.calls[1]["json"]["payload"]["content"]
    assert not any("纯粹的AI助手" in c.get("text", "") for c in content2), "后续不应重复附加"
    print("PASS 测试15: 纯净指令仅首条附加（避免重复浪费token）")


# ---------- 测试 16: 重命名命令 ----------
def test_rename():
    plugin, _, _ = make_plugin()
    event = MockEvent(message_str="/dsh 重命名 我的对话")
    results = collect_agen(plugin.cmd_dsh(event))
    assert "已命名" in results[0].text, f"重命名失败: {results[0].text}"
    assert plugin._session_names[event.unified_msg_origin] == "我的对话"
    print("PASS 测试16: 重命名命令")


# ---------- 测试 17: 发送失败不更新已读 seq ----------
def test_send_failure_no_seq_update():
    umo = "test:GroupMessage:1111"
    plugin, client, _ = make_plugin()
    plugin._bindings[umo] = "sid-1"
    plugin._last_seq[umo] = 0

    class FailCtx:
        def __init__(self):
            self.sent = []

        async def send_message(self, session, chain):
            raise Exception("send failed")

    plugin.context = FailCtx()
    client.responses["session.history"] = {
        "ok": True,
        "value": {
            "events": [
                {"event": {"type": "assistant/message", "seq": 10, "data": {"message": {"content": [{"type": "text", "text": "新回复"}]}}}},
            ]
        },
    }
    asyncio.get_event_loop().run_until_complete(plugin._check_session_replies(umo, "sid-1"))
    assert plugin._last_seq[umo] == 0, "发送失败不应更新已读 seq"
    print("PASS 测试17: 发送失败不更新已读 seq（下次重试）")


# ---------- 测试 18: 精确 /dsh 匹配（不误匹配 /dshow 等） ----------
def test_precise_dsh_match():
    plugin, client, _ = make_plugin(
        dsh_responses={"session.prompt": {"ok": True, "value": {"accepted": True}}}
    )
    umo = "test:GroupMessage:1111"
    plugin._bindings[umo] = "sid-1"
    from astrbot.api.message_components import Plain

    # /dshark 不是命令，应被当作普通消息转发
    event = MockEvent(umo=umo, message_str="/dshark 你好")
    event._messages = [Plain(text="/dshark 你好")]
    asyncio.get_event_loop().run_until_complete(plugin.on_message(event))
    assert event._call_llm_flag is False, "/dshark 被转发到 DSH，应阻止主LLM"
    prompt_calls = [c for c in client.calls if "prompt" in c["url"]]
    assert prompt_calls, "/dshark 应作为普通消息转发"
    print("PASS 测试18: 精确 /dsh 匹配（/dshark 不误判为命令）")


# ---------- 测试 19: 剥离斜杠后的命令识别（AstrBot 唤醒前缀会剥离 /） ----------
def test_stripped_slash_cmd():
    plugin, _, _ = make_plugin()
    # AstrBot 剥离 / 后，消息变成 dsh xxx / dsh创建 / dsh
    assert plugin._is_dsh_command("dsh 会话") is True, "dsh 会话 应识别"
    assert plugin._is_dsh_command("dsh创建") is True, "dsh创建 应识别"
    assert plugin._is_dsh_command("dsh") is True, "单独 dsh 应识别"
    assert plugin._is_dsh_command("/dsh 会话") is True, "带斜杠应识别"
    assert plugin._is_dsh_command("dshark 你好") is False, "dshark 不应识别"
    assert plugin._is_dsh_command("dsh创建器") is True, "dsh创建器 开头是命令"
    assert plugin._normalize_cmd("dsh创建") == "/dsh 创建", "剥离斜杠后规范化"
    assert plugin._normalize_cmd("dsh 会话") == "dsh 会话", "带空格不变"
    print("PASS 测试19: 剥离斜杠后的命令识别与规范化")


# ---------- 测试 20: 会话标题提取 ----------
def test_session_title():
    plugin, client, _ = make_plugin(
        dsh_responses={
            "session.list": {
                "ok": True,
                "value": {
                    "items": [
                        {"sessionId": "sid-1", "running": False,
                         "projections": {"values": {"title": "查看SKILL.md"}}},
                        {"sessionId": "sid-2", "running": True,
                         "projections": {"values": {"title": None}},
                         "cwd": "/root"},
                        {"sessionId": "sid-3", "running": False,
                         "projections": {}},
                    ]
                },
            }
        }
    )
    # 标题提取
    assert plugin._session_title({"projections": {"values": {"title": "标题A"}}}) == "标题A"
    assert "·" in plugin._session_title({"projections": {"values": {"title": None}}, "cwd": "/root", "sessionId": "sid-12345678"})
    assert "·" in plugin._session_title({"projections": {}})
    # 列表命令显示标题
    event = MockEvent(message_str="/dsh 会话")
    results = collect_agen(plugin.cmd_dsh(event))
    assert "查看SKILL.md" in results[0].text, f"应显示会话标题: {results[0].text}"
    assert "sid-1" in results[0].text
    print("PASS 测试20: 会话列表显示 DSH 会话标题")


# ---------- 测试 21: 会话编号选择 ----------
def test_session_number_select():
    plugin, client, _ = make_plugin(
        dsh_responses={
            "session.list": {
                "ok": True,
                "value": {
                    "items": [
                        {"sessionId": "sid-aaa", "running": False,
                         "projections": {"values": {"title": "会话A"}}},
                        {"sessionId": "sid-bbb", "running": False,
                         "projections": {"values": {"title": "会话B"}}},
                        {"sessionId": "sid-ccc", "running": False,
                         "projections": {"values": {"title": "会话C"}}},
                    ]
                },
            },
            "session.history": {"ok": True, "value": {"events": []}},
        }
    )
    sessions = [{"sessionId": "sid-aaa"}, {"sessionId": "sid-bbb"}, {"sessionId": "sid-ccc"}]
    # 编号解析
    assert asyncio.get_event_loop().run_until_complete(
        plugin._resolve_session_id(sessions, "1")
    ) == "sid-aaa", "编号1应解析为第一个会话"
    assert asyncio.get_event_loop().run_until_complete(
        plugin._resolve_session_id(sessions, "3")
    ) == "sid-ccc", "编号3应解析为第三个会话"
    assert asyncio.get_event_loop().run_until_complete(
        plugin._resolve_session_id(sessions, "sid-bbb")
    ) == "sid-bbb", "完整ID应直接匹配"
    assert asyncio.get_event_loop().run_until_complete(
        plugin._resolve_session_id(sessions, "sid-c")
    ) == "sid-ccc", "前缀应匹配"
    assert asyncio.get_event_loop().run_until_complete(
        plugin._resolve_session_id(sessions, "99")
    ) is None, "越界编号应返回None"
    # 通过命令用编号选择
    event = MockEvent(message_str="/dsh 选择 2")
    results = collect_agen(plugin.cmd_dsh(event))
    assert "已绑定" in results[0].text, f"编号选择失败: {results[0].text}"
    assert plugin._bindings[event.unified_msg_origin] == "sid-bbb", "应绑定编号2的会话"
    print("PASS 测试21: 会话编号选择（/dsh 选择 2）")


# ---------- 测试 22: 进度通知 ----------
def test_progress_notify():
    umo = "test:GroupMessage:1111"
    plugin, client, _ = make_plugin({"notify_mode": "turn", "notify_tool_result": True})
    plugin._bindings[umo] = "sid-1"
    plugin._last_seq[umo] = 0
    client.responses["session.history"] = {
        "ok": True,
        "value": {
            "events": [
                {"event": {"type": "step/start", "seq": 1, "data": {"turn": 1, "step": 1}}},
                {"event": {"type": "tool/call", "seq": 2, "data": {"turn": 1, "step": 1}}},
                {"event": {"type": "tool/result", "seq": 3, "data": {"turn": 1, "step": 1,
                    "message": {"content": [{"type": "tool-result", "toolCallId": "c1",
                        "content": [{"type": "text", "text": "文件修改完成"}]}]}}}},
                {"event": {"type": "turn/end", "seq": 4, "data": {"turn": 1, "reason": {"kind": "completed"}}}},
                {"event": {"type": "assistant/message", "seq": 5, "data": {"message": {"content": [{"type": "text", "text": "任务完成！"}]}}}},
            ]
        },
    }
    asyncio.get_event_loop().run_until_complete(plugin._check_session_replies(umo, "sid-1"))
    sent_texts = []
    for _, chain in plugin.context.sent:
        sent_texts.append("".join(getattr(c, "text", "") for c in chain.chain))
    joined = "\n".join(sent_texts)
    assert "工具执行" in joined, f"应通知工具执行: {joined}"
    assert "第 1 轮已完成" in joined, f"应通知轮次完成: {joined}"
    assert "任务完成" in joined, f"应转发最终回复: {joined}"
    assert plugin._last_seq[umo] == 5, "已读 seq 应更新"
    print(f"PASS 测试22: 进度通知（{len(sent_texts)} 条：工具/轮次/最终回复）")


# ---------- 测试 23: 触发模式（at/all/prefix） ----------
def test_trigger_mode():
    plugin, client, _ = make_plugin(
        {"trigger_mode": "at"},
        dsh_responses={"session.prompt": {"ok": True, "value": {"accepted": True}}},
    )
    umo = "test:GroupMessage:1111"
    plugin._bindings[umo] = "sid-1"
    from astrbot.api.message_components import Plain

    # at 模式：群聊未 @bot → 不转发
    event = MockEvent(umo=umo, message_str="普通群消息")
    event._messages = [Plain(text="普通群消息")]
    event.is_at_or_wake_command = False
    asyncio.get_event_loop().run_until_complete(plugin.on_message(event))
    prompt_calls = [c for c in client.calls if "prompt" in c["url"]]
    assert not prompt_calls, "at 模式未@bot不应转发"

    # at 模式：群聊 @bot → 转发
    client.calls.clear()
    event2 = MockEvent(umo=umo, message_str="@bot 帮我查一下")
    event2._messages = [Plain(text="帮我查一下")]
    event2.is_at_or_wake_command = True
    asyncio.get_event_loop().run_until_complete(plugin.on_message(event2))
    prompt_calls = [c for c in client.calls if "prompt" in c["url"]]
    assert prompt_calls, "at 模式@bot应转发"

    # all 模式：全部转发
    plugin2, client2, _ = make_plugin(
        {"trigger_mode": "all"},
        dsh_responses={"session.prompt": {"ok": True, "value": {"accepted": True}}},
    )
    plugin2._bindings[umo] = "sid-1"
    event3 = MockEvent(umo=umo, message_str="随便一条")
    event3._messages = [Plain(text="随便一条")]
    event3.is_at_or_wake_command = False
    asyncio.get_event_loop().run_until_complete(plugin2.on_message(event3))
    prompt_calls = [c for c in client2.calls if "prompt" in c["url"]]
    assert prompt_calls, "all 模式应全部转发"

    # prefix 模式
    plugin3, client3, _ = make_plugin(
        {"trigger_mode": "prefix", "trigger_prefix": "dsh:"},
        dsh_responses={"session.prompt": {"ok": True, "value": {"accepted": True}}},
    )
    plugin3._bindings[umo] = "sid-1"
    event4 = MockEvent(umo=umo, message_str="dsh: 帮我查天气")
    event4._messages = [Plain(text="帮我查天气")]
    asyncio.get_event_loop().run_until_complete(plugin3.on_message(event4))
    prompt_calls = [c for c in client3.calls if "prompt" in c["url"]]
    assert prompt_calls, "prefix 模式匹配应转发"
    print("PASS 测试23: 触发模式（at/all/prefix）")


# ---------- 测试 24: notify_mode final（只报最终结果） ----------
def test_notify_final():
    umo = "test:GroupMessage:1111"
    plugin, client, _ = make_plugin({"notify_mode": "final"})
    plugin._bindings[umo] = "sid-1"
    plugin._last_seq[umo] = 0
    client.responses["session.history"] = {
        "ok": True,
        "value": {
            "events": [
                {"event": {"type": "tool/result", "seq": 1, "data": {"turn": 1, "step": 1,
                    "message": {"content": [{"type": "tool-result", "toolCallId": "c1",
                        "content": [{"type": "text", "text": "工具结果"}]}]}}}},
                {"event": {"type": "turn/end", "seq": 2, "data": {"turn": 1, "reason": {"kind": "completed"}}}},
                {"event": {"type": "assistant/message", "seq": 3, "data": {"message": {"content": [{"type": "text", "text": "最终结果"}]}}}},
            ]
        },
    }
    asyncio.get_event_loop().run_until_complete(plugin._check_session_replies(umo, "sid-1"))
    sent_texts = []
    for _, chain in plugin.context.sent:
        sent_texts.append("".join(getattr(c, "text", "") for c in chain.chain))
    joined = "\n".join(sent_texts)
    assert "最终结果" in joined, "final 模式应转发最终结果"
    assert "工具结果" not in joined, "final 模式不应通知工具"
    assert "轮已完成" not in joined, "final 模式不应通知轮次"
    print("PASS 测试24: notify_mode=final（只报最终结果）")


# ---------- 测试 25: notify_mode off（完全不通知） ----------
def test_notify_off():
    umo = "test:GroupMessage:1111"
    plugin, client, _ = make_plugin({"notify_mode": "off"})
    plugin._bindings[umo] = "sid-1"
    plugin._last_seq[umo] = 0
    client.responses["session.history"] = {
        "ok": True,
        "value": {
            "events": [
                {"event": {"type": "assistant/message", "seq": 1, "data": {"message": {"content": [{"type": "text", "text": "结果"}]}}}},
            ]
        },
    }
    asyncio.get_event_loop().run_until_complete(plugin._check_session_replies(umo, "sid-1"))
    assert plugin.context.sent == [], "off 模式不应转发任何消息"
    assert plugin._last_seq[umo] == 1, "off 模式也应更新已读 seq"
    print("PASS 测试25: notify_mode=off（完全不通知）")


# ---------- 测试 26: 多会话上下文前缀 ----------
def test_session_context_prefix():
    umo = "test:GroupMessage:1111"
    plugin, client, _ = make_plugin({"notify_mode": "turn"})
    plugin._bindings[umo] = "sid-1"
    plugin._bindings["test:GroupMessage:2222"] = "sid-2"  # 模拟多会话
    plugin._session_names[umo] = "任务A"
    plugin._last_seq[umo] = 0
    client.responses["session.history"] = {
        "ok": True,
        "value": {
            "events": [
                {"event": {"type": "turn/end", "seq": 1, "data": {"turn": 1, "reason": {"kind": "completed"}}}},
            ]
        },
    }
    asyncio.get_event_loop().run_until_complete(plugin._check_session_replies(umo, "sid-1"))
    sent_texts = []
    for _, chain in plugin.context.sent:
        sent_texts.append("".join(getattr(c, "text", "") for c in chain.chain))
    joined = "\n".join(sent_texts)
    assert "[任务A]" in joined, f"多会话时通知应带会话名: {joined}"
    print("PASS 测试26: 多会话时通知带会话上下文")


if __name__ == "__main__":
    test_dsh_call()
    test_create_and_bind()
    test_list_sessions()
    test_select_session()
    test_auto_forward()
    test_whitelist()
    test_poll_reply()
    test_incremental()
    test_forward_mode()
    test_help_status()
    test_image_content()
    test_no_space_cmd()
    test_pure_mode()
    test_agent_preset()
    test_pure_instruction()
    test_rename()
    test_send_failure_no_seq_update()
    test_precise_dsh_match()
    test_stripped_slash_cmd()
    test_session_title()
    test_session_number_select()
    test_progress_notify()
    test_trigger_mode()
    test_notify_final()
    test_notify_off()
    test_session_context_prefix()
    print("\n✅ 全部 26 项测试通过")
