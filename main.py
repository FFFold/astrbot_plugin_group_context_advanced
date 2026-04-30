import base64
import datetime
import os
import traceback
import uuid
from collections import defaultdict, deque

import astrbot.api.message_components as Comp
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import At, Forward, Image, Plain
from astrbot.api.platform import MessageType
from astrbot.api.provider import LLMResponse, Provider, ProviderRequest
from astrbot.api.star import Context, Star, register
from astrbot.core.utils.io import download_image_by_url

try:
    from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
        AiocqhttpMessageEvent,
    )
    IS_AIOCQHTTP = True
except ImportError:
    IS_AIOCQHTTP = False


"""
群聊上下文感知插件
提供增强的群聊上下文管理，包括群聊记录追踪、图片描述、合并转发分析、指令过滤等功能
"""

SYSTEM_MARKER = "[GCPLUGIN]"
GC_CHAT_MARKER = "<!--group_context_plugin_chat-->"
CONSUMED_CHAT_COUNT_EXTRA = "group_context_consumed_chat_count"

@register("group_context_advanced", "Fold", "更优雅的群聊上下文管理，全面替代内置的“群聊上下文感知”功能，支持更灵活的上下文控制、图片识别、合并转发分析。", "0.2.2")
class GroupContextPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.session_chat_maxlen = int(self.get_cfg("session_chat_maxlen", 500))
        self.session_chats = defaultdict(lambda: deque() if self.session_chat_maxlen == -1 else deque(maxlen=self.session_chat_maxlen))
        """记录群成员的群聊消息，每个元素是包含多模态内容的列表"""

        self.group_whitelist = {str(item) for item in self.get_cfg("group_whitelist", [])}
        self.group_blacklist = {str(item) for item in self.get_cfg("group_blacklist", [])}

        self.max_context_rounds = int(self.get_cfg("max_context_rounds", -1))
        self.dequeue_context_rounds = int(self.get_cfg("dequeue_context_rounds", 2))
        self.system_prompt = self.get_cfg("system_prompt",
            "You are now in a chatroom. The chat history is as above. Now, new messages are coming.")

        self.enable_forward_analysis = bool(self.get_cfg("enable_forward_analysis", True))
        self.forward_max_messages = int(self.get_cfg("forward_max_messages", 50))
        self.forward_prefix = "【合并转发内容】"

        self.enable_image_recognition = bool(self.get_cfg("enable_image_recognition", True))
        self.image_caption = bool(self.get_cfg("image_caption", False))
        self.image_caption_provider_id = self.get_cfg("image_caption_provider_id", "")
        self.image_carry_rounds = int(self.get_cfg("image_carry_rounds", 1))

        self.enable_command_filter = bool(self.get_cfg("enable_command_filter", True))
        self.command_prefixes = self.get_cfg("command_prefixes", ["/"])

        logger.info("群聊上下文感知插件已初始化")
        logger.info(f"对话轮数控制: max={self.max_context_rounds}, dequeue={self.dequeue_context_rounds}")
        logger.info(f"消息缓存上限: {'不限制' if self.session_chat_maxlen == -1 else self.session_chat_maxlen}")
        logger.info(f"群聊白名单: {'不限制' if not self.group_whitelist else len(self.group_whitelist)}")
        logger.info(f"群聊黑名单: {'无' if not self.group_blacklist else len(self.group_blacklist)}")
        logger.info(f"合并转发分析: {'已启用' if self.enable_forward_analysis else '已禁用'}")
        logger.info(f"图片识别: {'已启用' if self.enable_image_recognition else '已禁用'}")
        if self.enable_image_recognition:
            logger.info(f"图片处理模式: {'转述描述' if self.image_caption else 'URL注入'}")
            logger.info(f"图片携带轮数: {self.image_carry_rounds}")

    def get_cfg(self, key: str, default=None):
        return self.config.get(key, default)

    def is_command(self, message: str) -> bool:
        if not self.enable_command_filter or not message:
            return False
        message = message.strip()
        for prefix in self.command_prefixes:
            if message.startswith(prefix):
                return True
        return False

    def _is_group_allowed(self, event: AstrMessageEvent) -> bool:
        group_id = event.get_group_id()
        candidates = {event.unified_msg_origin}
        if group_id:
            candidates.add(str(group_id))

        if self.group_blacklist and candidates & self.group_blacklist:
            return False

        return not self.group_whitelist or bool(candidates & self.group_whitelist)

    def _get_message_time_str(self, event: AstrMessageEvent) -> str:
        timestamp = getattr(event.message_obj, "timestamp", None)
        if timestamp is not None:
            try:
                timestamp = float(timestamp)
                if timestamp > 1_000_000_000_000:
                    timestamp /= 1000
                return datetime.datetime.fromtimestamp(timestamp).strftime("%H:%M:%S")
            except (OSError, OverflowError, TypeError, ValueError):
                logger.warning(f"消息时间戳格式异常，回退当前时间: {timestamp}")
        return datetime.datetime.now().strftime("%H:%M:%S")

    def _extract_image_url(self, image_data: str | dict | Image) -> str | None:
        if not image_data:
            return None

        if isinstance(image_data, str):
            return image_data

        if isinstance(image_data, dict):
            if "image_url" in image_data:
                image_url_obj = image_data["image_url"]
                if isinstance(image_url_obj, dict) and "url" in image_url_obj:
                    return image_url_obj["url"]
            if "url" in image_data:
                return image_data["url"]

        if isinstance(image_data, Image):
            if hasattr(image_data, "url") and image_data.url:
                return image_data.url
            if hasattr(image_data, "file") and image_data.file:
                return image_data.file

        return None

    def _get_chat_item_content(self, chat_item):
        if isinstance(chat_item, dict) and "content" in chat_item:
            return chat_item["content"]
        return chat_item

    def _get_chat_item_message_id(self, chat_item):
        if isinstance(chat_item, dict):
            return chat_item.get("message_id")
        return None

    async def _detect_forward_message(self, event) -> str | None:
        logger.debug(f"_detect_forward_message | IS_AIOCQHTTP={IS_AIOCQHTTP}, isinstance(event, AiocqhttpMessageEvent)={isinstance(event, AiocqhttpMessageEvent) if IS_AIOCQHTTP else 'N/A'}")

        if not IS_AIOCQHTTP or not isinstance(event, AiocqhttpMessageEvent):
            logger.debug("不符合合并转发检测条件，返回None")
            return None

        for seg in event.message_obj.message:
            if isinstance(seg, Comp.Forward):
                return seg.id

        reply_seg = None
        for seg in event.message_obj.message:
            if isinstance(seg, Comp.Reply):
                reply_seg = seg
                break

        if reply_seg:
            try:
                client = event.bot
                original_msg = await client.api.call_action("get_msg", message_id=reply_seg.id)
                if original_msg and "message" in original_msg:
                    original_message_chain = original_msg["message"]
                    if isinstance(original_message_chain, list):
                        for segment in original_message_chain:
                            if isinstance(segment, dict) and segment.get("type") == "forward":
                                return segment.get("data", {}).get("id")
            except Exception as e:
                logger.error(f"获取回复消息失败: {e}")

        return None

    @filter.platform_adapter_type(filter.PlatformAdapterType.ALL)
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE, priority=10000)
    async def on_message(self, event: AstrMessageEvent):
        """处理群聊消息，记录到上下文缓存"""
        if event.get_message_type() != MessageType.GROUP_MESSAGE:
            return

        if not self._is_group_allowed(event):
            logger.debug(f"群聊上下文 | {event.unified_msg_origin} | 不在允许范围内，已跳过")
            return

        message_text = ""
        for comp in event.message_obj.message:
            if isinstance(comp, Plain):
                message_text += comp.text

        if self.is_command(message_text):
            logger.debug(f"群聊上下文 | {event.unified_msg_origin} | 检测到指令消息，已过滤")
            return

        has_valid_content = False
        for comp in event.message_obj.message:
            if isinstance(comp, (Plain, Image)):
                has_valid_content = True
                break
            if IS_AIOCQHTTP and isinstance(comp, Forward):
                has_valid_content = True
                break

        if not has_valid_content:
            return

        try:
            await self.handle_message(event)
        except Exception as e:
            logger.error(f"记录群聊消息失败: {e}")

    async def handle_message(self, event: AstrMessageEvent):
        """记录群聊消息到上下文中

        图片处理逻辑：
        1. enable_image_recognition = False: 完全忽略所有图片
        2. enable_image_recognition = True, image_caption = False: 所有图片以URL形式注入，保留原始位置
        3. enable_image_recognition = True, image_caption = True: 所有图片使用转述描述，保留原始位置
        """

        datetime_str = self._get_message_time_str(event)

        current_message_content = []

        full_text = f"[{getattr(event.message_obj.sender, 'nickname', 'Unknown')}/{datetime_str}]: "

        if self.enable_forward_analysis and IS_AIOCQHTTP:

            forward_id = await self._detect_forward_message(event)

            if forward_id:
                if IS_AIOCQHTTP and isinstance(event, AiocqhttpMessageEvent):
                    try:
                        client = event.bot
                        forward_data = await client.api.call_action("get_forward_msg", id=forward_id)
                        messages = forward_data.get("messages", [])

                        full_text += f"\n{self.forward_prefix}\n\t<begin>\n"

                        for i, message_node in enumerate(messages):
                            if i >= self.forward_max_messages:
                                full_text += f"\n\t[转发消息过多，已截断，共 {self.forward_max_messages}/{len(messages)} 条]\n"
                                break

                            sender_name = message_node.get("sender", {}).get("nickname", "未知用户")
                            raw_content = message_node.get("message") or message_node.get("content", [])

                            full_text += f"{sender_name}: "

                            for seg in raw_content:
                                if isinstance(seg, dict):
                                    seg_type = seg.get("type")
                                    seg_data = seg.get("data", {})

                                    if seg_type == "text":
                                        full_text += seg_data.get("text", "")
                                    elif seg_type == "at":
                                        full_text += f"[At: {seg_data.get('qq', '')}]"
                                    elif seg_type == "image":
                                        img_url = self._extract_image_url(seg_data)
                                        if img_url:
                                            full_text, current_message_content = await self._resolve_image(img_url, full_text, current_message_content)

                            full_text += "\n"

                        full_text += "\t<end>\n"
                        logger.info("检测到合并转发消息，已保留原始结构")
                    except Exception as e:
                        logger.error(f"处理合并转发消息失败: {e}")
                        logger.error(traceback.format_exc())
                else:
                    logger.debug("未检测到合并转发消息")
        else:
            logger.debug("合并转发分析未启用或不支持当前平台")

        for comp in event.message_obj.message:
            if isinstance(comp, Plain):
                full_text += comp.text
            elif isinstance(comp, At):
                full_text += f" [At: {comp.name if hasattr(comp, 'name') else comp.qq}]"
            elif isinstance(comp, Image):
                url = self._extract_image_url(comp)
                if url:
                    full_text, current_message_content = await self._resolve_image(url, full_text, current_message_content)
            elif isinstance(comp, Forward):
                pass

        if full_text:
            current_message_content.append({"type": "text", "text": full_text})

        if current_message_content:
            self.session_chats[event.unified_msg_origin].append({
                "message_id": getattr(event.message_obj, "message_id", None),
                "content": current_message_content,
            })
            logger.debug(f"群聊上下文 | {event.unified_msg_origin} | 添加了一条包含 {len(current_message_content)} 个组件的消息")

    async def _encode_image_bs64(self, image_url: str) -> str:
        try:
            if image_url.startswith("base64://"):
                return image_url.replace("base64://", "data:image/jpeg;base64,")
            elif image_url.startswith("http"):
                image_path = await download_image_by_url(image_url)
                try:
                    with open(image_path, "rb") as f:
                        image_bs64 = base64.b64encode(f.read()).decode("utf-8")
                    return "data:image/jpeg;base64," + image_bs64
                finally:
                    try:
                        os.remove(image_path)
                    except OSError:
                        pass
            else:
                logger.warning(f"不支持的图片 URL 协议: {image_url[:50]}")
                return ""
        except Exception as e:
            logger.error(f"将图片转换为base64失败: {image_url}, 错误: {e}")
            return ""

    async def get_image_caption(self, image_url: str, image_caption_provider_id: str) -> str:
        if not image_caption_provider_id:
            provider = self.context.get_using_provider()
        else:
            provider = self.context.get_provider_by_id(image_caption_provider_id)
            if not provider:
                raise Exception(f"没有找到 ID 为 {image_caption_provider_id} 的提供商")

        if not isinstance(provider, Provider):
            raise Exception(f"提供商类型错误({type(provider)}),无法获取图片描述")

        image_caption_prompt = self.get_cfg("image_caption_prompt", "请描述这张图片的内容")

        response = await provider.text_chat(
            prompt=image_caption_prompt,
            session_id=uuid.uuid4().hex,
            image_urls=[image_url],
            persist=False,
        )
        return response.completion_text

    async def _resolve_image(self, img_url: str, full_text: str, current_message_content: list) -> tuple[str, list]:
        """根据配置处理图片，返回 (更新后的 full_text, 更新后的 current_message_content)"""
        if not self.enable_image_recognition:
            return full_text + " [图片]", current_message_content

        if self.image_caption:
            try:
                caption = await self.get_image_caption(img_url, self.image_caption_provider_id)
                full_text += f" [图片描述: {caption}]"
            except Exception as e:
                logger.error(f"获取图片描述失败: {e}")
                full_text += " [图片]"
            return full_text, current_message_content

        if full_text:
            current_message_content.append({"type": "text", "text": full_text})
            full_text = ""
        image_data = await self._encode_image_bs64(img_url)
        if image_data:
            current_message_content.append({"type": "image_url", "image_url": {"url": image_data}})
        else:
            full_text = " [图片]"
        return full_text, current_message_content

    def _find_round_ends(self, contexts: list) -> list[int]:
        """找出上下文列表中每个对话轮次的结束位置（assistant 消息的索引）"""
        round_ends = []

        for i in range(len(contexts) - 1):
            current_role = contexts[i].get("role")
            next_role = contexts[i + 1].get("role")
            if current_role == "assistant" and next_role in ["user", "system"]:
                round_ends.append(i)

        if contexts and contexts[-1].get("role") == "assistant":
            round_ends.append(len(contexts) - 1)

        return round_ends

    def _control_conversation_rounds(self, req: ProviderRequest, max_rounds: int, dequeue_rounds: int):
        """控制对话轮数，模仿框架的 truncate_by_turns 算法实现集中丢弃

        当轮数超过 max_rounds 时，保留最近 (max_rounds - dequeue_rounds + 1) 轮，
        一次性丢弃最早的 dequeue_rounds 轮，最大化缓存命中率。
        """
        if max_rounds == -1 or not req.contexts or dequeue_rounds <= 0:
            return

        round_ends = self._find_round_ends(req.contexts)

        if len(round_ends) <= max_rounds:
            return

        num_to_keep = max_rounds - dequeue_rounds + 1
        if num_to_keep <= 0:
            req.contexts = []
            return

        keep_start_index = round_ends[-num_to_keep]
        req.contexts = req.contexts[keep_start_index:]

    def _control_image_carry_rounds(self, req: ProviderRequest, image_carry_rounds: int):
        """控制图片携带轮数，只保留最后 N 轮中的图片"""
        if not req.contexts or image_carry_rounds <= 0:
            return

        round_ends = self._find_round_ends(req.contexts)

        if len(round_ends) > image_carry_rounds:
            keep_start_index = round_ends[-image_carry_rounds]

            for i, ctx in enumerate(req.contexts):
                if i < keep_start_index and ctx.get("role") == "user":
                    if isinstance(ctx.get("content"), list):
                        new_content = []
                        current_text = None

                        for item in ctx["content"]:
                            if item["type"] == "text":
                                text = item["text"]

                                if text.startswith("["):
                                    if current_text:
                                        new_content.append({"type": "text", "text": current_text})
                                    current_text = text
                                else:
                                    if current_text:
                                        current_text += text
                                    else:
                                        current_text = text
                            elif item["type"] == "image_url":
                                if current_text:
                                    current_text += " [图片]"
                                else:
                                    current_text = " [图片]"

                        if current_text:
                            new_content.append({"type": "text", "text": current_text})

                        ctx["content"] = new_content

    @filter.on_llm_request()
    async def on_req_llm(self, event: AstrMessageEvent, req: ProviderRequest):
        """当触发 LLM 请求前，修改上下文（群聊场景）"""
        if event.unified_msg_origin not in self.session_chats:
            return

        req.contexts = [
            ctx for ctx in req.contexts
            if not (ctx.get("role") == "system" and ctx.get("content", "").startswith(SYSTEM_MARKER))
        ]

        self._control_conversation_rounds(req, self.max_context_rounds, self.dequeue_context_rounds)

        self._control_image_carry_rounds(req, self.image_carry_rounds)

        combined_content = []
        text_prompt_parts = []

        session_chat = self.session_chats[event.unified_msg_origin]
        session_chat_items = list(session_chat)
        if not session_chat_items:
            return

        current_message_id = getattr(event.message_obj, "message_id", None)
        current_message_index = None
        if current_message_id is not None:
            current_message_id = str(current_message_id)
            for index, chat_item in enumerate(session_chat_items):
                item_message_id = self._get_chat_item_message_id(chat_item)
                if item_message_id is not None and str(item_message_id) == current_message_id:
                    current_message_index = index
                    break

        if current_message_index is None:
            inject_items = session_chat_items
            consumed_count = len(session_chat_items)
        else:
            inject_items = session_chat_items[:current_message_index]
            consumed_count = current_message_index + 1

        event.set_extra(CONSUMED_CHAT_COUNT_EXTRA, consumed_count)

        for chat_item in inject_items:
            message = self._get_chat_item_content(chat_item)
            combined_content.extend(message)

            text_part = ""
            for comp in message:
                if comp["type"] == "text":
                    text_part += comp["text"]
                elif comp["type"] == "image_url":
                    text_part += " [图片]"

            if text_part.strip():
                text_prompt_parts.append(text_part.strip())

        chat_text = ""
        if text_prompt_parts:
            chat_text = "\n---\n".join(text_prompt_parts)

        if req.prompt and chat_text:
            req.prompt = req.prompt + "\n" + GC_CHAT_MARKER + "\n" + chat_text
        elif chat_text:
            req.prompt = chat_text

        logger.debug(f"构建的prompt: \n{req.prompt}")

        if combined_content:
            user_message = {"role": "user", "content": combined_content}

            req.contexts.append(user_message)

        req.contexts.append({"role": "system", "content": f"{SYSTEM_MARKER}\n{self.system_prompt}"})

    @filter.on_llm_request(priority=-10000)
    async def on_req_llm_clear_prompt(self, event: AstrMessageEvent, req: ProviderRequest):
        """在所有插件处理完后，剔除群聊记录文本，保留其他插件对 prompt 的修改"""
        if event.get_message_type() != MessageType.GROUP_MESSAGE:
            return

        if req.prompt and GC_CHAT_MARKER in req.prompt:
            req.prompt = req.prompt.split(GC_CHAT_MARKER)[0].rstrip("\n")

        if req.contexts:
            req.contexts = [
                ctx for ctx in req.contexts
                if not (ctx.get("role") == "user" and
                       (ctx.get("content") == "" or
                        (isinstance(ctx.get("content"), list) and not ctx.get("content"))))
            ]

    @filter.on_llm_response(priority=-10000)
    async def save_memories(self, event: AstrMessageEvent, resp: LLMResponse):
        if event.get_message_type() != MessageType.GROUP_MESSAGE:
            return

        req = event.get_extra("provider_request")
        if req is not None:
            req.prompt = ""

        umo = event.unified_msg_origin
        if umo in self.session_chats:
            consumed_count = event.get_extra(CONSUMED_CHAT_COUNT_EXTRA, 0)
            for _ in range(min(consumed_count, len(self.session_chats[umo]))):
                self.session_chats[umo].popleft()
            if not self.session_chats[umo]:
                del self.session_chats[umo]

    async def terminate(self):
        logger.info("群聊上下文感知插件已卸载")
