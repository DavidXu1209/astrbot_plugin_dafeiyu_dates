"""Pure date logic for the reminder plugin (no AstrBot imports)."""
from __future__ import annotations

import hashlib
import json
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

CST = timezone(timedelta(hours=8))
EVE_WORDS = re.compile(r"考|面试|比赛|答辩|手术|体检|出发|航班|报到|演出|上台")
_ADD = re.compile(r"^\s*(每年)?\s*(?:(\d{4})[-./年])?(\d{1,2})[-./月](\d{1,2})日?\s+(.+?)\s*$")


def parse_add(arg: str, today: date) -> dict | None:
    m = _ADD.match(arg or "")
    if not m:
        return None
    yearly = bool(m.group(1))
    month, day = int(m.group(3)), int(m.group(4))
    title = m.group(5).strip()[:40]
    if not title:
        return None
    try:
        date(2000, month, day)  # leap-safe validity check
    except ValueError:
        return None
    year = None
    if not yearly:
        year = int(m.group(2)) if m.group(2) else today.year
        try:
            target = date(year, month, day)
        except ValueError:
            return None
        if not m.group(2) and target < today:
            year += 1
            try:
                date(year, month, day)
            except ValueError:
                return None
        if date(year, month, day) < today:
            return None
    return {"month": month, "day": day, "year": year, "title": title, "eve": bool(EVE_WORDS.search(title))}


def occurrence(entry: dict, today: date) -> date | None:
    """Next date (>= today) this entry happens, or None if a one-off has passed."""
    m, d = entry["month"], entry["day"]
    if entry.get("year"):
        try:
            when = date(entry["year"], m, d)
        except ValueError:
            return None
        return when if when >= today else None
    for y in (today.year, today.year + 1, today.year + 2, today.year + 3, today.year + 4):
        try:
            when = date(y, m, d)
        except ValueError:  # Feb 29 in a non-leap year
            continue
        if when >= today:
            return when
    return None


def due(entries: list, now: datetime, morning: str, eve: str, sent: set) -> list:
    """Return (key, entry, kind) items that should fire now and were not sent."""
    now = now.astimezone(CST)
    today = now.date()
    hhmm = now.strftime("%H:%M")
    out = []
    for e in entries:
        when = occurrence(e, today)
        if when is None:
            continue
        if when == today and hhmm >= morning:
            key = f"{e['id']}:{when.isoformat()}:day"
            if key not in sent:
                out.append((key, e, "day"))
        if e.get("eve") and when == today + timedelta(days=1) and hhmm >= eve:
            key = f"{e['id']}:{when.isoformat()}:eve"
            if key not in sent:
                out.append((key, e, "eve"))
    return out


def describe(e: dict, today: date) -> str:
    when = occurrence(e, today)
    label = f"{e['month']:02d}-{e['day']:02d}"
    kind = "每年" if not e.get("year") else str(e["year"])
    if when is None:
        return f"#{e['id']} {kind} {label} {e['title']}（已过）"
    left = (when - today).days
    tail = "就是今天" if left == 0 else f"还有 {left} 天"
    return f"#{e['id']} {kind} {label} {e['title']}（{tail}）"


def build_prompt(e: dict, kind: str, today: date) -> str:
    when = occurrence(e, today) or today
    if kind == "eve":
        return (f"明天（{when.month}月{when.day}日）是他的「{e['title']}」。现在是前一天晚上，"
                "你主动给他发一条私聊消息：给他打气、让他别太紧张、早点休息。1 到 3 句，口语化，像平时一样说话，"
                "不要说你是被提醒的，不要编造他没说过的细节。只输出消息正文。")
    return (f"今天（{when.month}月{when.day}日）是「{e['title']}」，是他之前特意让你记住的日子。"
            "你主动给他发一条私聊消息：如果是生日或纪念日就好好祝福、可以撒个娇；如果是考试、面试这类事就给他加油，"
            "说等他回来告诉你结果。1 到 3 句，口语化，像平时一样说话，不要说你是被提醒的，不要编造他没说过的细节。只输出消息正文。")


def fallback_text(e: dict, kind: str) -> str:
    if kind == "eve":
        return f"明天就是{e['title']}啦，别熬夜，早点睡，你肯定行的。"
    return f"今天是{e['title']}！我可一直记着呢。"


class Store:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def load(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            data = {}
        data.setdefault("next_id", 1)
        data.setdefault("entries", [])
        data.setdefault("sent", [])
        return data

    def save(self, data: dict) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(self.path)

    def add(self, item: dict) -> dict:
        data = self.load()
        item = dict(item, id=data["next_id"])
        data["next_id"] += 1
        data["entries"].append(item)
        self.save(data)
        return item

    def remove(self, entry_id: int) -> bool:
        data = self.load()
        before = len(data["entries"])
        data["entries"] = [e for e in data["entries"] if e["id"] != entry_id]
        self.save(data)
        return len(data["entries"]) < before

    def mark_sent(self, key: str) -> None:
        data = self.load()
        data["sent"] = (data["sent"] + [key])[-500:]
        self.save(data)

    def prune(self, today: date) -> None:
        data = self.load()
        keep = [e for e in data["entries"] if e.get("year") is None or occurrence(e, today - timedelta(days=1)) is not None]
        if len(keep) != len(data["entries"]):
            data["entries"] = keep
            self.save(data)


# ---------- 时间感：距他上一条消息过了多久 ----------
GAP_MIN_SECONDS = 20 * 60


def _span(seconds: float) -> str:
    minutes = int(seconds // 60)
    if minutes < 60:
        return f"{minutes} 分钟"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours} 小时" + (f" {minutes} 分钟" if minutes >= 5 else "")
    days, hours = divmod(hours, 24)
    return f"{days} 天" + (f" {hours} 小时" if hours else "")


def gap_hint(prev_ts: float, now_ts: float, min_seconds: int = GAP_MIN_SECONDS) -> str | None:
    """System-prompt note telling the model how long he was away; None for short gaps."""
    gap = now_ts - prev_ts
    if prev_ts <= 0 or gap < min_seconds:
        return None
    prev = datetime.fromtimestamp(prev_ts, CST)
    now = datetime.fromtimestamp(now_ts, CST)
    days = (now.date() - prev.date()).days
    when = prev.strftime("%H:%M") if days == 0 else ("昨天 " if days == 1 else prev.strftime("%m月%d日 ")) + prev.strftime("%H:%M")
    text = (f"【时间】他上一条消息是{when}发的，现在是 {now.strftime('%H:%M')}，中间隔了 {_span(gap)}。"
            "聊天记录里他说正在做或准备去做的事（吃饭、洗澡、上课、打工、出门、睡觉之类），按常理这会儿多半已经做完了；"
            "按现在的时间接话，可以自然问一句弄完没、怎么样，别再当成他还在做那件事。")
    if days >= 1:
        text += "已经不是同一天了，不要接着昨天的情绪或安排往下说。"
    return text


class Clock:
    """Remembers when the owner last messaged (survives restarts)."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def _load(self) -> dict:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def get(self) -> float:
        try:
            return float(self._load().get("last", 0))
        except Exception:
            return 0.0

    def get_umo(self) -> str:
        """主人最近一次私聊的 unified_msg_origin，作为 adapter_id 留空时的兜底。"""
        try:
            return str(self._load().get("umo", "") or "")
        except Exception:
            return ""

    def set(self, ts: float, umo: str = "") -> None:
        data = self._load()
        data["last"] = ts
        if umo:
            data["umo"] = umo
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        tmp.replace(self.path)


# ---------- 准确的当前时间 / 早安 ----------
WEEKDAYS = "一二三四五六日"


def now_line(now: datetime) -> str:
    now = now.astimezone(CST)
    return (f"【现在】{now.month}月{now.day}日 星期{WEEKDAYS[now.weekday()]} {now.strftime('%H:%M')}。"
            "说到时间就照这个说（比如 18:43 就说“六点四十多了”），别说成聊天记录里更早的时间。")


def _hm(text: str, default: tuple) -> tuple:
    try:
        h, m = text.strip().split(":")
        return int(h), int(m)
    except Exception:
        return default


def morning_due(now: datetime, window: str, last_seen: float, sent: set) -> str | None:
    """Key for today's good-morning message if it should go out now, else None.

    Fires once a day at a stable random minute inside the window, only if he
    has not messaged since 05:00 today."""
    now = now.astimezone(CST)
    start_s, _, end_s = (window or "09:20-10:20").partition("-")
    sh, sm = _hm(start_s, (9, 20))
    eh, em = _hm(end_s, (10, 20))
    start = now.replace(hour=sh, minute=sm, second=0, microsecond=0)
    end = now.replace(hour=eh, minute=em, second=0, microsecond=0)
    if end <= start:
        return None
    key = f"morning:{now.date().isoformat()}"
    if key in sent:
        return None
    span = int((end - start).total_seconds() // 60)
    offset = int(hashlib.sha256(key.encode()).hexdigest(), 16) % max(span, 1)
    target = start + timedelta(minutes=offset)
    if not (target <= now < end):
        return None
    if last_seen >= now.replace(hour=5, minute=0, second=0, microsecond=0).timestamp():
        return None
    return key


def morning_prompt(now: datetime) -> str:
    now = now.astimezone(CST)
    return (f"现在是{now.month}月{now.day}日 星期{WEEKDAYS[now.weekday()]} 早上 {now.strftime('%H:%M')}，他今天还没来找你。"
            "你主动给他发一条早安消息：1 到 2 句，口语，可以撒个娇或者关心他一句今天的安排。"
            "不要编造他没说过的具体安排，不要说你是被提醒的。只输出消息正文。")
