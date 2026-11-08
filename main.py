"""记日子插件：主人私聊记下重要日子，当天（考试类还有前一晚）主动发消息；
另外告诉模型现在几点、距主人上一条消息过了多久，避免把一小时前的“去吃饭”当成正在吃。"""
from __future__ import annotations

import asyncio
import time
from contextlib import suppress
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star, StarTools

from .dates_core import (CST, Clock, Store, build_prompt, describe, due, fallback_text, gap_hint,
                         morning_due, morning_prompt, now_line, occurrence, parse_add)

PLUGIN = "astrbot_plugin_dafeiyu_dates"
GAP_KEY = "_owner_gap"
SEEN_KEY = "_owner_seen"


class DatesPlugin(Star):
    def __init__(self, context: Context, config: dict | None = None) -> None:
        super().__init__(context)
        self.config = config or {}
        data_dir = Path(StarTools.get_data_dir(PLUGIN))
        self.store = Store(data_dir / "dates.json")
        self.clock = Clock(data_dir / "last_seen.json")
        self._task: asyncio.Task | None = None

    @property
    def owner(self) -> str:
        return str(self.config.get("owner_qq", "") or "").strip()

    def owner_umo(self) -> str:
        # adapter_id 留空时用主人最近一次私聊的会话标识兜底（保存于 last_seen.json）。
        adapter = str(self.config.get("adapter_id", "") or "").strip()
        if not adapter:
            return self.clock.get_umo()
        return f"{adapter}:FriendMessage:{self.owner}"

    def is_owner(self, event: AstrMessageEvent) -> bool:
        if not self.owner:
            return False
        adapter = str(self.config.get("adapter_id", "") or "").strip()
        if not adapter:
            adapter = event.unified_msg_origin.split(":", 1)[0]
        expected = f"{adapter}:FriendMessage:{self.owner}"
        return event.unified_msg_origin == expected and str(event.get_sender_id()) == self.owner

    async def initialize(self) -> None:
        self._task = asyncio.create_task(self._loop())

    async def terminate(self) -> None:
        if self._task:
            self._task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await self._task

    async def _loop(self) -> None:
        from datetime import datetime
        while True:
            try:
                now = datetime.now(CST)
                self.store.prune(now.date())
                data = self.store.load()
                sent = set(data["sent"])
                items = due(data["entries"], now, str(self.config.get("morning_time", "09:30")),
                            str(self.config.get("eve_time", "21:30")), sent)
                for key, entry, kind in items:
                    self.store.mark_sent(key)  # mark first: never send twice
                    await self._send(entry, kind, now.date())
                if self.config.get("morning_greeting", True):
                    mkey = morning_due(now, str(self.config.get("morning_window", "09:20-10:20")),
                                       self.clock.get(), sent)
                    if mkey:
                        self.store.mark_sent(mkey)
                        # 今天本来就有记着的日子要来找他，就不再单独道早安
                        if not any(occurrence(e, now.date()) == now.date() for e in data["entries"]):
                            await self._greet(now)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(f"[记日子] 检查失败: {type(exc).__name__}")
            await asyncio.sleep(60)

    async def _persona_prompt(self) -> str:
        pid = str(self.config.get("persona_id", "") or "").strip()
        if pid:
            with suppress(Exception):
                persona = await self.context.persona_manager.get_persona(pid)
                text = getattr(persona, "system_prompt", "") or ""
                if text:
                    return text
        return "你是一个和主人很亲近的人，说话口语化、真诚，在乎他的感受。"

    async def _ask(self, prompt: str) -> str:
        system = await self._persona_prompt()
        for pid in self.config.get("provider_ids") or []:
            provider = self.context.get_provider_by_id(pid)
            if provider is None:
                continue
            try:
                resp = await provider.text_chat(prompt=prompt, system_prompt=system, contexts=[])
            except Exception as exc:
                logger.warning(f"[记日子] 模型 {pid} 失败: {type(exc).__name__}")
                continue
            text = (getattr(resp, "completion_text", "") or "").strip()
            if text:
                return text
        return ""

    async def _compose(self, entry: dict, kind: str, today) -> str:
        return await self._ask(build_prompt(entry, kind, today)) or fallback_text(entry, kind)

    async def _deliver(self, text: str, note: str) -> None:
        """发给主人，并记进当前对话，这样他回话时机器人知道自己刚说过什么。"""
        umo = self.owner_umo()
        if not umo:
            logger.warning("[记日子] 还没有主人的会话记录，跳过主动发送；请填写 adapter_id，或等主人先发一条私聊")
            return
        await self.context.send_message(umo, MessageChain().message(text))
        try:
            mgr = self.context.conversation_manager
            cid = await mgr.get_curr_conversation_id(umo)
            if cid:
                await mgr.add_message_pair(
                    cid=cid,
                    user_message={"role": "user", "content": [{"type": "text", "text": note}]},
                    assistant_message={"role": "assistant", "content": [{"type": "text", "text": text}]},
                )
        except Exception as exc:
            logger.warning(f"[记日子] 写入对话记录失败: {type(exc).__name__}")

    async def _greet(self, now) -> None:
        if not self.owner:
            return
        text = await self._ask(morning_prompt(now))
        if not text:
            return  # 模型都失败就不硬发固定句
        await self._deliver(text, f"（{now.strftime('%H:%M')} 他还没来找你，你主动发了早安）")
        logger.info("[记日子] 已发送早安")

    async def _send(self, entry: dict, kind: str, today) -> None:
        if not self.owner:
            return
        text = await self._compose(entry, kind, today)
        await self._deliver(text, f"（今天记着的日子：{entry['title']}，你主动来找他）" if kind == "day"
                            else f"（明天是他的{entry['title']}，你前一晚主动来给他打气）")
        logger.info(f"[记日子] 已发送 #{entry['id']} {kind}")

    # ---------- 时间感 ----------
    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE, priority=20)
    async def track_owner(self, event: AstrMessageEvent):
        """只记真实收到的主人私聊（主动消息不经过这里）。"""
        if not self.is_owner(event):
            return
        now = time.time()
        last = self.clock.get()
        if self.config.get("time_gap_hint", True):
            event.set_extra(SEEN_KEY, now)
            if last:
                event.set_extra(GAP_KEY, (last, now))
        try:
            self.clock.set(now, event.unified_msg_origin)
        except OSError:
            pass

    @filter.on_llm_request()
    async def add_gap_hint(self, event: AstrMessageEvent, req: ProviderRequest):
        seen = event.get_extra(SEEN_KEY)
        if not seen or not self.is_owner(event):
            return
        from datetime import datetime
        parts = [now_line(datetime.fromtimestamp(seen, CST))]
        gap = event.get_extra(GAP_KEY)
        if gap:
            hint = gap_hint(gap[0], gap[1], int(self.config.get("time_gap_minutes", 20)) * 60)
            if hint:
                parts.append(hint)
        req.system_prompt = (req.system_prompt or "") + "\n\n" + "\n".join(parts)

    @staticmethod
    def _rest(event: AstrMessageEvent) -> str:
        parts = (event.message_str or "").strip().split(None, 1)
        return parts[1] if len(parts) > 1 else ""

    @filter.command("记日子")
    async def add_date(self, event: AstrMessageEvent):
        if not self.is_owner(event):
            return
        event.stop_event()
        from datetime import datetime
        now = datetime.now(CST)
        today = now.date()
        item = parse_add(self._rest(event), today)
        if item is None:
            yield event.plain_result("这么记：/记日子 11-15 TOPIK考试\n每年都过的加「每年」：/记日子 每年 03-08 我生日")
            return
        item = self.store.add(item)
        # 今天已过发送时间的提醒不再补发，避免刚记完马上又来一条
        for key, _entry, _kind in due([item], now, str(self.config.get("morning_time", "09:30")),
                                      str(self.config.get("eve_time", "21:30")), set()):
            self.store.mark_sent(key)
        extra = "，前一天晚上我也会来给你打气" if item["eve"] else ""
        yield event.plain_result(f"记下了：{describe(item, today)}。到那天我来找你{extra}。")

    @filter.command("日子")
    async def list_dates(self, event: AstrMessageEvent):
        if not self.is_owner(event):
            return
        event.stop_event()
        from datetime import datetime
        today = datetime.now(CST).date()
        entries = self.store.load()["entries"]
        if not entries:
            yield event.plain_result("还没记任何日子。用 /记日子 11-15 TOPIK考试 来记。")
            return
        yield event.plain_result("记着的日子：\n" + "\n".join(describe(e, today) for e in entries) + "\n删掉用 /删日子 编号")

    @filter.command("删日子")
    async def del_date(self, event: AstrMessageEvent):
        if not self.is_owner(event):
            return
        event.stop_event()
        raw = self._rest(event).lstrip("#").strip()
        ok = raw.isdigit() and self.store.remove(int(raw))
        yield event.plain_result("删掉了。" if ok else "没找到这个编号，先发 /日子 看看。")
