import asyncio
import base64
import json
import os
import re
import time
import uuid
from pathlib import Path

import httpx

from astrbot.api.event import filter, AstrMessageEvent, MessageChain
from astrbot.api import logger
from astrbot.api.star import Context, Star
import astrbot.api.message_components as Comp
from astrbot.api.message_components import Image, File, Plain, Node, Nodes
from astrbot.core.utils.astrbot_path import get_astrbot_data_path


class DshBridgePlugin(Star):
    """DSH ↔ QQ 双向桥接插件。

    功能：
    - QQ → DSH：白名单群/用户的消息自动转发到绑定的 DSH 会话（session.prompt）
    - DSH → QQ：轮询绑定的 DSH 会话，新回复转发回 QQ
    - 会话管理：/dsh 会话、/dsh 选择、/dsh 创建、/dsh 切换、/dsh 历史、/dsh 状态
    - 图片/文件：QQ 图片转 base64 发给 DSH，文件保存到服务器并给 DSH 路径
    - 权限：白名单群/用户控制
    """

    def __init__(self, context: Context, config: dict = None):
        super().__init__(context)
        self.config = config or {}
        self.base_url = str(self.config.get("dsh_base_url", "http://127.0.0.1:3080")).rstrip("/")
        # QQ 会话(umo) -> DSH sessionId 的绑定关系
        self._bindings: dict[str, str] = {}
        # umo -> 已读的最大 seq（增量转发）
        self._last_seq: dict[str, int] = {}
        # umo -> 会话显示名（便于识别）
        self._session_names: dict[str, str] = {}
        self._poll_task: asyncio.Task | None = None
        self._data_dir = Path(get_astrbot_data_path()) / "plugin_data" / "dsh_bridge"
        self._bindings_file = self._data_dir / "bindings.json"
        # 已附加纯净指令的会话集合（避免重复附加）
        self._pure_inited: set[str] = set()

    # ------------------------------------------------------------------ #
    # 生命周期                                                           #
    # ------------------------------------------------------------------ #

    async def initialize(self):
        """初始化插件，加载绑定关系，启动轮询。"""
        self._load_bindings()
        logger.info(
            f"DSH 桥接插件已加载，DSH 地址: {self.base_url}，绑定会话数: {len(self._bindings)}"
        )
        self._poll_task = asyncio.create_task(self._poll_loop())

    async def terminate(self):
        """插件卸载时停止轮询。"""
        if self._poll_task:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass

    # ------------------------------------------------------------------ #
    # 数据持久化                                                         #
    # ------------------------------------------------------------------ #

    def _load_bindings(self):
        """加载绑定关系。"""
        try:
            self._data_dir.mkdir(parents=True, exist_ok=True)
            if self._bindings_file.exists():
                data = json.loads(self._bindings_file.read_text(encoding="utf-8"))
                self._bindings = data.get("bindings", {})
                self._last_seq = {k: int(v) for k, v in data.get("last_seq", {}).items()}
                self._session_names = data.get("session_names", {})
        except Exception as e:
            logger.error(f"加载绑定数据失败: {e}")

    def _save_bindings(self):
        """保存绑定关系。"""
        try:
            self._data_dir.mkdir(parents=True, exist_ok=True)
            data = {
                "bindings": self._bindings,
                "last_seq": self._last_seq,
                "session_names": self._session_names,
            }
            self._bindings_file.write_text(
                json.dumps(data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as e:
            logger.error(f"保存绑定数据失败: {e}")

    # ------------------------------------------------------------------ #
    # DSH API 封装                                                       #
    # ------------------------------------------------------------------ #

    async def _dsh_call(self, method: str, payload: dict) -> dict | None:
        """调用 DSH RPC API。"""
        rpc_id = str(uuid.uuid4())
        body = {
            "type": "client-request",
            "rpcId": rpc_id,
            "method": method,
            "payload": payload,
        }
        try:
            async with httpx.AsyncClient(timeout=120) as client:
                resp = await client.post(
                    f"{self.base_url}/api/{method}",
                    json=body,
                    headers={"Content-Type": "application/json"},
                )
                if resp.status_code != 200:
                    logger.error(f"DSH API {method} HTTP {resp.status_code}: {resp.text[:200]}")
                    return None
                data = resp.json()
                if data.get("type") != "server-response":
                    logger.error(f"DSH API {method} 异常响应: {str(data)[:200]}")
                    return None
                result = data.get("result", {})
                if not result.get("ok"):
                    logger.error(f"DSH API {method} 失败: {json.dumps(result, ensure_ascii=False)[:300]}")
                    return None
                return result.get("value")
        except Exception as e:
            logger.error(f"DSH API {method} 异常: {e}")
            return None

    async def _create_session(self, cwd: str | None = None) -> str | None:
        """创建 DSH 会话（支持指定 agent preset）。"""
        payload = {}
        if cwd:
            payload["cwd"] = cwd
        preset = self.config.get("agent_preset", "")
        if preset:
            payload["agentPreset"] = preset
        value = await self._dsh_call("session.create", payload)
        if value:
            return value.get("sessionId")
        return None

    async def _prompt(self, session_id: str, content: list, extra: str = "", first: bool = False) -> bool:
        """向 DSH 会话发送消息（支持附加纯净指令）。

        Args:
            first: 是否为该会话的第一条桥接消息（此时附加纯净指令）
        """
        parts = list(content)
        # 纯净模式：仅首条消息附加指令，避免每次重复浪费 token
        if first:
            pure_instruction = self.config.get("pure_mode_instruction", "")
            if pure_instruction:
                parts.append({"type": "text", "text": pure_instruction})
        if extra:
            parts.append({"type": "text", "text": extra})

        value = await self._dsh_call(
            "session.prompt",
            {"sessionId": session_id, "mode": "queue", "content": parts},
        )
        return bool(value and value.get("accepted"))

    async def _list_sessions(self) -> list[dict]:
        """列出 DSH 会话。"""
        value = await self._dsh_call("session.list", {})
        if value:
            return value.get("items", [])
        return []

    async def _history(self, session_id: str) -> list[dict] | None:
        """获取 DSH 会话历史事件。

        Returns:
            事件列表；调用失败返回 None（调用方应区分失败与空）
        """
        value = await self._dsh_call("session.history", {"sessionId": session_id})
        if value is None:
            return None
        return value.get("events", [])

    # ------------------------------------------------------------------ #
    # 消息内容构建                                                       #
    # ------------------------------------------------------------------ #

    async def _event_to_prompt_content(self, event: AstrMessageEvent) -> tuple[list, str | None]:
        """将 QQ 消息事件转换为 DSH prompt content 列表。

        Returns:
            (content, file_hint): DSH 内容列表，和可选的文件路径提示文本
        """
        content = []
        file_hints = []
        texts = []

        for comp in event.get_messages():
            if isinstance(comp, Plain):
                texts.append(comp.text)
            elif isinstance(comp, Image):
                image_data = await self._image_to_base64(comp)
                if image_data:
                    content.append(
                        {
                            "type": "image",
                            "mediaType": "image/png",
                            "data": image_data,
                            "name": f"qq_image_{int(time.time())}.png",
                        }
                    )
                else:
                    texts.append("[图片]")
            elif isinstance(comp, File):
                path = await self._save_file(comp)
                if path:
                    file_hints.append(f"[文件已保存到: {path}]")
                else:
                    texts.append(f"[文件: {comp.name}]")

        text = " ".join(t for t in texts if t).strip()
        if text:
            content.insert(0, {"type": "text", "text": text})
        elif not content and not file_hints:
            content.append({"type": "text", "text": "(空消息)"})

        hint = "\n".join(file_hints) if file_hints else None
        return content, hint

    async def _image_to_base64(self, comp: Image) -> str | None:
        """将 QQ 图片转为 base64（支持本地路径和 URL，异步下载）。"""
        try:
            # 优先用 Image 组件自带的异步转换（处理 file/url/base64://）
            try:
                bs64 = await comp.convert_to_base64()
                if bs64:
                    return bs64
            except Exception:
                pass
            file = comp.file or comp.url or ""
            if not file:
                return None
            if file.startswith("http://") or file.startswith("https://"):
                async with httpx.AsyncClient(timeout=30) as client:
                    resp = await client.get(file)
                    if resp.status_code == 200:
                        return base64.b64encode(resp.content).decode()
                    return None
            else:
                path = comp.path or file
                if os.path.exists(path):
                    with open(path, "rb") as f:
                        return base64.b64encode(f.read()).decode()
                return None
        except Exception as e:
            logger.error(f"图片转 base64 失败: {e}", exc_info=True)
            return None

    async def _save_file(self, comp: File) -> str | None:
        """保存 QQ 文件到服务器路径，返回路径（异步下载）。"""
        try:
            save_dir = Path(get_astrbot_data_path()) / "plugin_data" / "dsh_bridge" / "files"
            save_dir.mkdir(parents=True, exist_ok=True)
            name = comp.name or f"file_{int(time.time())}"
            # 清理文件名中的不安全字符
            name = re.sub(r'[\\/:*?"<>|]', "_", name)
            target = save_dir / name
            if target.exists():
                # 防同名覆盖
                target = save_dir / f"{int(time.time())}_{name}"

            # 优先用 File 组件自带的异步获取
            try:
                path = await comp.get_file()
                if path and os.path.exists(path):
                    import shutil

                    shutil.copy2(path, target)
                    return str(target)
            except Exception:
                pass

            file_path = comp.file  # 本地路径
            if file_path and os.path.exists(file_path):
                import shutil

                shutil.copy2(file_path, target)
                return str(target)

            url = comp.url
            if url:
                async with httpx.AsyncClient(timeout=60) as client:
                    resp = await client.get(url)
                    if resp.status_code == 200:
                        target.write_bytes(resp.content)
                        return str(target)
            return None
        except Exception as e:
            logger.error(f"保存文件失败: {e}", exc_info=True)
            return None

    # ------------------------------------------------------------------ #
    # 权限检查                                                           #
    # ------------------------------------------------------------------ #

    def _is_allowed(self, event: AstrMessageEvent) -> bool:
        """检查消息来源是否在白名单内。"""
        if not self.config.get("whitelist_enable", True):
            return True

        group_id = str(event.get_group_id() or "")
        sender_id = str(event.get_sender_id() or "")

        # 归一化为字符串，兼容数字/字符串混合配置
        whitelist_groups = [str(x) for x in (self.config.get("whitelist_groups") or [])]
        whitelist_users = [str(x) for x in (self.config.get("whitelist_users") or [])]

        if group_id and group_id in whitelist_groups:
            return True
        if sender_id and sender_id in whitelist_users:
            return True
        # 管理员始终允许
        if event.is_admin():
            return True
        return False

    # ------------------------------------------------------------------ #
    # 轮询 DSH 回复                                                      #
    # ------------------------------------------------------------------ #

    async def _poll_loop(self):
        """轮询所有绑定的 DSH 会话，转发新回复到 QQ。"""
        interval = max(1, int(self.config.get("poll_interval", 3) or 3))
        while True:
            try:
                await self._poll_once()
            except Exception as e:
                logger.error(f"DSH 轮询异常: {e}")
            await asyncio.sleep(interval)

    async def _poll_once(self):
        """执行一次轮询。"""
        for umo, session_id in list(self._bindings.items()):
            try:
                await self._check_session_replies(umo, session_id)
            except Exception as e:
                logger.error(f"检查会话 {session_id} 回复失败: {e}")

    async def _check_session_replies(self, umo: str, session_id: str):
        """检查一个绑定会话的新进展并转发/通知。

        处理两类事件：
        1. assistant/message —— 最终回复，转发到 QQ
        2. 进度事件（turn/end、tool/result、step/end）—— 按 notify_mode 通知进展
        """
        events = await self._history(session_id)
        if events is None:
            # API 调用失败，不更新 seq，下次重试
            return
        if not events:
            return

        last_seq = self._last_seq.get(umo, 0)
        notify_mode = self.config.get("notify_mode", "final")

        # 收集新事件
        new_messages = []      # (seq, assistant/message 事件)
        new_progress = []      # (seq, 进度通知文本)
        max_seq = last_seq
        for entry in events:
            ev = entry.get("event", {})
            seq = ev.get("seq", 0)
            if seq <= last_seq:
                continue
            etype = ev.get("type", "")
            if etype == "assistant/message":
                # off 模式完全不转发（仅手动查询）；其他模式转发最终回复
                if notify_mode != "off":
                    new_messages.append((seq, ev))
            elif etype == "turn/end":
                # 一轮完成（notify_mode=turn 或 step 时通知；final/off 不通知）
                if notify_mode in ("turn", "step"):
                    turn = ev.get("data", {}).get("turn", "?")
                    reason = ev.get("data", {}).get("reason", {}).get("kind", "completed")
                    if reason == "completed":
                        new_progress.append((seq, f"✅ DSH 第 {turn} 轮已完成"))
                    else:
                        new_progress.append((seq, f"⏸ DSH 第 {turn} 轮结束（{reason}）"))
            elif etype == "step/end" and notify_mode == "step":
                turn = ev.get("data", {}).get("turn", "?")
                step = ev.get("data", {}).get("step", "?")
                new_progress.append((seq, f"⚙️ DSH 第 {turn} 轮 · 第 {step} 步完成"))
            elif etype == "tool/result" and self.config.get("notify_tool_result", True):
                # 工具执行完成通知（final/off 模式不通知）
                if notify_mode in ("turn", "step"):
                    msg = ev.get("data", {}).get("message", {})
                    text = self._extract_tool_result(msg)
                    if text:
                        max_len = int(self.config.get("notify_tool_result_max_len", 200) or 200)
                        t = text if len(text) <= max_len else text[:max_len] + "..."
                        new_progress.append((seq, f"🔧 DSH 工具执行: {t}"))
            if seq > max_seq:
                max_seq = seq

        if not new_messages and not new_progress:
            # off 模式或其他无内容情况：推进已读 seq，避免重复扫描
            if max_seq > last_seq:
                self._last_seq[umo] = max_seq
                self._save_bindings()
            return

        # 会话上下文（多会话绑定时在通知里带上会话名，方便区分）
        session_label = ""
        if len(self._bindings) > 1:
            name = self._session_names.get(umo, "") or session_id[:8]
            session_label = f"[{name}] "

        # 尝试发送所有通知/回复，全部成功后再更新已读 seq
        all_sent = True
        # 进度通知优先发（先让用户看到进展），再发最终回复
        for seq, text in sorted(new_progress):
            if not text:
                continue
            ok = await self._send_to_qq(umo, session_label + text)
            if not ok:
                all_sent = False
            await asyncio.sleep(0.3)  # 间隔，避免刷屏

        for seq, ev in sorted(new_messages):
            message = ev.get("data", {}).get("message", {})
            text = self._extract_text(message)
            if not text:
                continue
            ok = await self._send_to_qq(umo, session_label + text)
            if not ok:
                all_sent = False

        if all_sent:
            # 全部转发成功，更新已读 seq
            self._last_seq[umo] = max_seq
            self._save_bindings()
        else:
            logger.warning(
                f"[DSH桥接] 部分进展发送失败，暂不更新已读 seq（下次重试）"
            )

    def _extract_tool_result(self, message: dict) -> str:
        """从 tool/result 消息中提取工具执行结果文本。"""
        content = message.get("content", [])
        parts = []
        for part in content:
            if isinstance(part, dict):
                if part.get("type") == "tool-result":
                    inner = part.get("content", [])
                    for i in inner:
                        if isinstance(i, dict) and i.get("type") == "text":
                            parts.append(i.get("text", ""))
                elif part.get("type") == "text":
                    parts.append(part.get("text", ""))
        return "\n".join(p for p in parts if p).strip()

    def _extract_text(self, message: dict) -> str:
        """从 DSH 消息中提取纯文本（跳过 tool-call 等）。"""
        content = message.get("content", [])
        parts = []
        for part in content:
            if isinstance(part, dict):
                if part.get("type") == "text":
                    parts.append(part.get("text", ""))
            elif isinstance(part, str):
                parts.append(part)
        return "\n".join(p for p in parts if p).strip()

    async def _send_to_qq(self, umo: str, text: str) -> bool:
        """将 DSH 回复发送到 QQ 会话（通过 context.send_message 主动发送）。"""
        if not text:
            return True

        reply_forward = self.config.get("reply_forward", "plain")
        threshold = int(self.config.get("forward_threshold", 500) or 500)

        if reply_forward == "forward" and len(text) > threshold:
            return await self._send_forward(umo, text)
        else:
            # 纯文本，超长分段
            if len(text) > 2000:
                ok = True
                segments = self._split_text(text, 1500)
                for seg in segments:
                    if not await self._send_plain(umo, seg):
                        ok = False
                    await asyncio.sleep(0.5)
                return ok
            else:
                return await self._send_plain(umo, text)

    async def _send_plain(self, umo: str, text: str) -> bool:
        """发送纯文本到 QQ 会话。"""
        try:
            chain = MessageChain()
            chain.chain = [Plain(text)]
            await self.context.send_message(umo, chain)
            return True
        except Exception as e:
            logger.error(f"发送消息到 {umo} 失败: {e}")
            return False

    async def _send_forward(self, umo: str, text: str) -> bool:
        """发送合并转发消息。"""
        bot_name = self.config.get("bot_name", "DSH助手")
        segments = self._split_text(text, 200)
        nodes = []
        for seg in segments:
            if seg.strip():
                nodes.append(
                    Node(
                        name=bot_name,
                        uin="0",
                        content=[Plain(seg)],
                    )
                )
        if not nodes:
            return True
        try:
            chain = MessageChain()
            chain.chain = [Nodes(nodes=nodes)]
            await self.context.send_message(umo, chain)
            return True
        except Exception as e:
            logger.error(f"合并转发失败，降级为纯文本: {e}")
            return await self._send_plain(umo, text)

    def _split_text(self, text: str, max_len: int = 200) -> list[str]:
        """按标点分段。"""
        segments = []
        current = ""
        for ch in text:
            current += ch
            if len(current) >= max_len or ch in "。！？!?\n":
                if current.strip():
                    segments.append(current.strip())
                    current = ""
        if current.strip():
            segments.append(current.strip())
        return [s for s in segments if s.strip()] or [text]

    # ------------------------------------------------------------------ #
    # 指令                                                               #
    # ------------------------------------------------------------------ #

    @filter.command("dsh")
    async def cmd_dsh(self, event: AstrMessageEvent):
        """DSH 桥接控制命令（支持 /dsh xxx 或 /dshxxx 无空格写法）"""
        # 阻止主 LLM，避免命令消息被机器人人设再次回复
        event.should_call_llm(False)
        msg = self._normalize_cmd(event.get_message_str())
        parts = msg.split()
        if len(parts) < 2:
            yield event.plain_result(self._help_text())
            return

        action = parts[1]
        umo = event.unified_msg_origin

        if action == "帮助":
            yield event.plain_result(self._help_text())
        elif action == "会话":
            yield await self._cmd_list_sessions(event)
        elif action in ("选择", "切换"):
            if len(parts) < 3:
                yield event.plain_result("用法: /dsh 选择 <会话ID>")
                return
            yield await self._cmd_select_session(event, parts[2])
        elif action == "创建":
            cwd = parts[2] if len(parts) > 2 else None
            yield await self._cmd_create_session(event, cwd)
        elif action == "历史":
            yield await self._cmd_show_history(event)
        elif action == "状态":
            yield await self._cmd_status(event)
        elif action == "解绑":
            self._bindings.pop(umo, None)
            self._last_seq.pop(umo, None)
            self._session_names.pop(umo, None)
            self._save_bindings()
            yield event.plain_result("✅ 已解绑当前会话")
        elif action == "重命名":
            if len(parts) < 3:
                yield event.plain_result("用法: /dsh 重命名 <名称>")
                return
            name = " ".join(parts[2:])
            self._session_names[umo] = name
            self._save_bindings()
            yield event.plain_result(f"✅ 当前会话已命名为: {name}")
        elif action == "发送":
            if len(parts) < 3:
                yield event.plain_result("用法: /dsh 发送 <内容>")
                return
            content = " ".join(parts[2:])
            yield await self._cmd_send(event, content)
        else:
            yield event.plain_result(self._help_text())

    def _normalize_cmd(self, msg: str) -> str:
        """将 /dshxxx 规范化为 /dsh xxx，兼容无空格写法。

        仅当 /dsh 后跟中文子命令时规范化（如 /dsh创建 -> /dsh 创建）；
        英文单词（如 /dshark）不处理。
        """
        msg = re.sub(r'\[MSG_ID:\d+\]', '', msg).strip()
        m = re.match(r'^/?dsh([\u4e00-\u9fff].*)$', msg)
        if m:
            return "/dsh " + m.group(1)
        return msg

    def _is_dsh_command(self, msg: str) -> bool:
        """判断消息是否为 /dsh 命令（兼容带斜杠、被剥离斜杠、无空格写法）。

        匹配规则：
        - /dsh、dsh（单独）
        - /dsh xxx、dsh xxx（带子命令）
        - /dsh创建、dsh创建（无空格中文子命令）
        不匹配：/dshark、dshark（英文单词）
        """
        msg = re.sub(r'\[MSG_ID:\d+\]', '', msg).strip()
        # 带斜杠形式：/dsh 后跟空白/行尾/中文
        if re.match(r'^/dsh(?:\s|$|[\u4e00-\u9fff])', msg):
            return True
        # 剥离斜杠形式（AstrBot 唤醒前缀已剥离 /）
        if re.match(r'^dsh(?:\s|$|[\u4e00-\u9fff])', msg):
            return True
        return False

    def _help_text(self) -> str:
        trigger_mode = self.config.get("trigger_mode", "at")
        mode_desc = {
            "at": "@机器人才转发",
            "all": "全部消息转发",
            "prefix": f"前缀 {self.config.get('trigger_prefix', 'dsh:')} 触发",
        }.get(trigger_mode, "at")
        notify_mode = self.config.get("notify_mode", "final")
        notify_desc = {
            "final": "只报最终结果",
            "turn": "每轮+工具通知",
            "step": "每步通知",
            "off": "不通知",
        }.get(notify_mode, "final")

        return (
            "📡 DSH 桥接插件\n\n"
            "控制命令:\n"
            "  /dsh 会话      列出 DSH 会话（带编号）\n"
            "  /dsh 选择 <编号|ID> 绑定当前QQ会话到指定DSH会话\n"
            "  /dsh 创建 [路径] 创建新DSH会话并绑定\n"
            "  /dsh 历史      查看当前会话的最近历史\n"
            "  /dsh 状态      查看桥接状态\n"
            "  /dsh 发送 <内容> 手动发送消息到DSH\n"
            "  /dsh 重命名 <名称> 给当前会话命名\n"
            "  /dsh 解绑      解除当前绑定\n"
            "  /dsh 帮助      显示本帮助\n\n"
            f"当前模式:\n"
            f"  转发: {mode_desc}  |  通知: {notify_desc}\n\n"
            "使用说明:\n"
            "  绑定后按当前触发模式转发消息到DSH，DSH干活时按通知模式汇报进度。\n"
            "  图片自动转给DSH，文件保存到服务器并告知DSH路径。\n"
            "  支持无空格写法：/dsh创建 = /dsh 创建。"
        )

    def _session_title(self, s: dict) -> str:
        """从 DSH 会话数据中提取标题（projections.values.title 或 fallback）。"""
        title = (
            s.get("projections", {})
            .get("values", {})
            .get("title")
        )
        if title:
            return title
        # fallback：cwd + 会话ID前缀
        cwd = s.get("cwd", "")
        return f"{cwd or '默认'} · {s.get('sessionId', '')[:8]}"

    async def _cmd_list_sessions(self, event: AstrMessageEvent):
        """列出 DSH 会话（带编号，显示会话标题）。"""
        sessions = await self._list_sessions()
        if not sessions:
            return event.plain_result("没有 DSH 会话，可用 /dsh 创建 创建新会话")

        umo = event.unified_msg_origin
        current = self._bindings.get(umo)

        lines = ["📋 DSH 会话列表:", ""]
        for idx, s in enumerate(sessions[:20], 1):
            sid = s.get("sessionId", "")
            marker = " 👈当前" if sid == current else ""
            running = "🟢" if s.get("running") else "⚪"
            title = self._session_title(s)
            lines.append(f"  {idx}. {running} {title}{marker}")
            lines.append(f"      {sid}")
        lines.append("")
        lines.append("使用 /dsh 选择 <编号或ID> 绑定，/dsh 创建 新建")
        return event.plain_result("\n".join(lines))

    async def _resolve_session_id(self, sessions: list[dict], target: str) -> str | None:
        """根据编号或完整 ID 解析会话 ID。"""
        # 完整 ID 匹配
        for s in sessions:
            if s.get("sessionId") == target:
                return target
        # 编号匹配（1-based）
        if target.isdigit():
            idx = int(target)
            if 1 <= idx <= len(sessions):
                return sessions[idx - 1].get("sessionId")
        # 前缀匹配
        for s in sessions:
            if s.get("sessionId", "").startswith(target):
                return s.get("sessionId")
        return None

    async def _cmd_select_session(self, event: AstrMessageEvent, target: str):
        """绑定当前 QQ 会话到指定 DSH 会话（支持编号或完整 ID）。"""
        sessions = await self._list_sessions()
        if not sessions:
            return event.plain_result("没有 DSH 会话，用 /dsh 创建 创建新会话")

        session_id = await self._resolve_session_id(sessions, target)
        if not session_id:
            return event.plain_result(
                f"❌ 找不到会话 '{target}'，用 /dsh 会话 查看编号列表"
            )

        valid = [s for s in sessions if s.get("sessionId") == session_id]
        umo = event.unified_msg_origin
        self._bindings[umo] = session_id
        self._session_names[umo] = self._session_title(valid[0])

        # 已读 seq 处理：默认不转发历史，从当前最大 seq 开始
        events = await self._history(session_id)
        max_seq = 0
        if events:
            max_seq = max((e.get("event", {}).get("seq", 0) for e in events), default=0)
        self._last_seq[umo] = max_seq

        self._save_bindings()
        return event.plain_result(
            f"✅ 已绑定当前QQ会话到 DSH 会话: {session_id}\n"
            f"📌 会话标题: {self._session_names[umo]}\n"
            f"（只转发新消息；如需看历史用 /dsh 历史）"
        )

    async def _cmd_create_session(self, event: AstrMessageEvent, cwd: str | None):
        """创建新 DSH 会话并绑定。"""
        session_id = await self._create_session(cwd)
        if not session_id:
            return event.plain_result("❌ 创建 DSH 会话失败")

        umo = event.unified_msg_origin
        self._bindings[umo] = session_id
        self._last_seq[umo] = 0
        self._session_names[umo] = f"新建 {session_id[:8]}"
        self._save_bindings()

        return event.plain_result(
            f"✅ 已创建并绑定 DSH 会话: {session_id}\n"
            f"工作目录: {cwd or '(默认)'}"
        )

    async def _cmd_show_history(self, event: AstrMessageEvent):
        """查看当前绑定会话的最近历史。"""
        umo = event.unified_msg_origin
        session_id = self._bindings.get(umo)
        if not session_id:
            return event.plain_result("当前QQ会话未绑定 DSH 会话，用 /dsh 选择 或 /dsh 创建")

        events = await self._history(session_id)
        if not events:
            return event.plain_result("该会话暂无消息或无法获取历史")
        # 提取最近的 user/assistant 消息（各取最近 10 条）
        messages = []
        for entry in events:
            ev = entry.get("event", {})
            if ev.get("type") == "user/message":
                msg = ev.get("data", {}).get("message", ev.get("data", {}))
                text = self._extract_text(msg)
                if text:
                    messages.append(("🧑 用户", text))
            elif ev.get("type") == "assistant/message":
                msg = ev.get("data", {}).get("message", {})
                text = self._extract_text(msg)
                if text:
                    messages.append(("🤖 DSH", text))

        if not messages:
            return event.plain_result("该会话暂无消息")

        lines = [f"📜 会话历史 ({session_id[:12]}...):", ""]
        for role, text in messages[-20:]:
            t = text if len(text) <= 100 else text[:100] + "..."
            lines.append(f"{role}: {t}")
            lines.append("")
        return event.plain_result("\n".join(lines))

    async def _cmd_status(self, event: AstrMessageEvent):
        """查看桥接状态。"""
        umo = event.unified_msg_origin
        session_id = self._bindings.get(umo)
        allowed = "✅ 允许" if self._is_allowed(event) else "❌ 未在白名单"

        lines = [
            "📡 DSH 桥接状态:",
            f"  DSH 地址: {self.base_url}",
            f"  当前QQ会话: {umo}",
            f"  绑定DSH会话: {session_id or '(未绑定)'}",
            f"  会话名称: {self._session_names.get(umo, '-')}",
            f"  权限: {allowed}",
            f"  触发方式: {self.config.get('trigger_mode', 'at')}",
            f"  通知模式: {self.config.get('notify_mode', 'final')}",
            f"  轮询间隔: {self.config.get('poll_interval', 3)}s",
        ]
        return event.plain_result("\n".join(lines))

    async def _cmd_send(self, event: AstrMessageEvent, content: str):
        """手动发送消息到 DSH。"""
        umo = event.unified_msg_origin
        session_id = self._bindings.get(umo)
        if not session_id:
            return event.plain_result("当前QQ会话未绑定 DSH 会话，用 /dsh 选择 或 /dsh 创建")

        ok = await self._prompt(session_id, [{"type": "text", "text": content}])
        if ok:
            return event.plain_result(f"✅ 已发送到 DSH:\n{content}")
        return event.plain_result("❌ 发送到 DSH 失败")

    # ------------------------------------------------------------------ #
    # 消息监听（QQ → DSH）                                               #
    # ------------------------------------------------------------------ #

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent):
        """监听所有 QQ 消息，转发到绑定的 DSH 会话。

        处理优先级：
        1. /dsh 命令（含无空格写法）→ 交给命令处理器，不转发、阻止主LLM
        2. 已绑定会话 + 白名单 → 转发到 DSH，并阻止 AstrBot 主 LLM（避免人设回复干扰）
        """
        if not self.config.get("enable", True):
            return

        umo = event.unified_msg_origin
        msg_text = event.get_message_str()

        # 1. /dsh 命令（含无空格写法、以及被唤醒前缀剥离斜杠后的 dsh xxx）→ 不转发；
        #    阻止主 LLM，避免命令消息被机器人人设再次回复（不 stop_event，避免影响命令处理器）
        if self._is_dsh_command(msg_text):
            event.should_call_llm(False)
            # 无空格形式（如 /dsh创建）不会被 AstrBot 命令过滤器触发，
            # 这里手动解析并执行对应逻辑，避免消息被静默吞掉
            stripped = msg_text.strip().lstrip("/")
            if re.match(r'^dsh[\u4e00-\u9fff]', stripped) and not stripped.startswith("dsh "):
                try:
                    async for _ in self.cmd_dsh(event):
                        pass
                except Exception as e:
                    logger.error(f"[DSH桥接] 无空格命令处理失败: {e}", exc_info=True)
            return

        # 2. 需要自动转发 + 已绑定 + 白名单 + 触发条件
        if not self.config.get("auto_forward", True):
            return

        session_id = self._bindings.get(umo)
        if not session_id:
            return

        if not self._is_allowed(event):
            return

        if not self._should_forward(event):
            return

        # 构建 DSH 内容
        content, file_hint = await self._event_to_prompt_content(event)
        if not content:
            return

        # 附加文件提示
        if file_hint:
            content.append({"type": "text", "text": file_hint})

        # 首条消息附加纯净指令
        is_first = session_id not in self._pure_inited
        ok = await self._prompt(session_id, content, first=is_first)
        if ok:
            self._pure_inited.add(session_id)

        # 转发成功后阻止 AstrBot 主 LLM，避免用人设回复（纯净模式核心）
        # 仅 should_call_llm + 清空结果，不 stop_event（避免终止事件管线影响其他插件）
        if ok:
            event.should_call_llm(False)
            try:
                # 清空可能的结果，避免主 LLM 链继续
                if event.get_result() is not None:
                    event.get_result().chain = []
            except Exception:
                pass
            logger.info(f"[DSH桥接] 消息已转发到 DSH 会话 {session_id[:12]}，已阻止主LLM回复")
            if file_hint:
                await event.send(event.plain_result(file_hint))

    def _should_forward(self, event: AstrMessageEvent) -> bool:
        """判断消息是否满足转发触发条件。

        trigger_mode:
        - at:    仅 @机器人 / 唤醒词 的消息转发（私聊始终转发）
        - all:   所有白名单消息转发
        - prefix: 以配置前缀开头的消息转发
        """
        mode = self.config.get("trigger_mode", "at")
        msg_text = event.get_message_str()

        if mode == "all":
            return True

        if mode == "prefix":
            prefix = self.config.get("trigger_prefix", "dsh:")
            return msg_text.strip().startswith(prefix)

        # at 模式（默认）
        if event.is_private_chat():
            return True  # 私聊始终转发
        # 群聊：需要 @机器人 或唤醒（AstrBot 的 is_at_or_wake_command 已判断）
        return bool(getattr(event, "is_at_or_wake_command", False))
