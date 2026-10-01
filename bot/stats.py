"""Статистика, которую админ настраивает кнопками: какие блоки показывать, за какие периоды, сколько строк."""
from __future__ import annotations

import html
import time
from dataclasses import asdict, dataclass, field, fields

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from .broadcast import Broadcaster
from .db import Database

SETTINGS_KEY = "stats"
DEFAULT_TITLE = "📊 Статистика бота"
TITLE_MAX = 100

# блоки отчёта в порядке вывода: ключ -> название кнопки
SECTIONS = {
    "users": "👤 Пользователи",
    "groups": "👥 Группы и каналы",
    "reach": "📬 Охват рассылки",
    "types": "🧩 По типам чатов",
    "growth": "🆕 Новые",
    "activity": "🔥 Активные",
    "sources": "🔗 Источники",
    "history": "📨 Последние рассылки",
    "running": "⏳ Текущая рассылка",
}
DEFAULT_SECTIONS = ["users", "groups", "reach", "growth", "activity", "sources", "history", "running"]

PERIODS = {1: "сутки", 7: "неделю", 30: "месяц"}
SOURCES_LIMITS = (3, 5, 10, 20)
HISTORY_LIMITS = (1, 3, 5, 10)
TYPE_NAMES = {"private": "личные", "group": "группы", "supergroup": "супергруппы", "channel": "каналы"}
STATUS_NAMES = {"running": "идёт", "done": "готово", "stopped": "остановлена"}


@dataclass
class StatsSettings:
    sections: list[str] = field(default_factory=lambda: list(DEFAULT_SECTIONS))
    periods: list[int] = field(default_factory=lambda: [1, 7])
    sources_limit: int = 5
    history_limit: int = 3
    title: str = DEFAULT_TITLE

    @classmethod
    def from_dict(cls, raw: dict | None) -> "StatsSettings":
        """Неизвестные ключи и мусорные значения из базы отбрасываются."""
        s = cls()
        raw = raw if isinstance(raw, dict) else {}
        names = {f.name for f in fields(cls)}
        for key, value in raw.items():
            if key in names:
                setattr(s, key, value)
        if not isinstance(s.sections, list):
            s.sections = list(DEFAULT_SECTIONS)
        s.sections = [k for k in SECTIONS if k in s.sections]
        if not isinstance(s.periods, list):
            s.periods = [1, 7]
        s.periods = [p for p in PERIODS if p in s.periods]
        if s.sources_limit not in SOURCES_LIMITS:
            s.sources_limit = 5
        if s.history_limit not in HISTORY_LIMITS:
            s.history_limit = 3
        if not isinstance(s.title, str) or not s.title.strip():
            s.title = DEFAULT_TITLE
        return s

    def toggle_section(self, key: str) -> None:
        on = set(self.sections) ^ {key}
        self.sections = [k for k in SECTIONS if k in on]

    def toggle_period(self, days: int) -> None:
        on = set(self.periods) ^ {days}
        self.periods = [p for p in PERIODS if p in on]


def next_value(options: tuple[int, ...], current: int) -> int:
    return options[(options.index(current) + 1) % len(options)] if current in options else options[0]


async def load_settings(db: Database) -> StatsSettings:
    return StatsSettings.from_dict(await db.get_setting(SETTINGS_KEY))


async def save_settings(db: Database, s: StatsSettings) -> None:
    await db.set_setting(SETTINGS_KEY, asdict(s))


def _by_periods(counts: dict[int, dict], key: str, periods: list[int]) -> str:
    return ", ".join(f"за {PERIODS[p]}: {counts[p][key]}" for p in periods)


async def stats_text(db: Database, broadcaster: Broadcaster, s: StatsSettings | None = None) -> str:
    s = s or await load_settings(db)
    on = set(s.sections)
    st = await db.stats()
    lines = [f"<b>{html.escape(s.title)}</b>\n"]
    if "users" in on:
        lines.append(f"👤 Пользователей: {st['users']} (доступны для рассылки: {st['users_active']})")
    if "groups" in on:
        lines.append(f"👥 Групп и каналов: {st['groups']} (активны: {st['groups_active']})")
    if "reach" in on:
        lines.append(f"📬 Всего получат рассылку: {st['active']}")
    if "types" in on:
        for r in await db.by_type():
            lines.append(f"   • {TYPE_NAMES.get(r['type'], r['type'])}: {r['n']} (активны: {r['active']})")
    if s.periods and on & {"growth", "activity"}:
        counts = {p: await db.counts_since(p) for p in s.periods}
        if "growth" in on:
            lines.append(f"🆕 Новых {_by_periods(counts, 'new', s.periods)}")
        if "activity" in on:
            lines.append(f"🔥 Активны {_by_periods(counts, 'seen', s.periods)}")
    if "sources" in on:
        sources = await db.top_sources(s.sources_limit)
        if sources:
            lines.append("\n🔗 <b>Источники</b> (параметр /start):")
            lines += [f"<code>{html.escape(r['start_arg'])}</code> — {r['n']}" for r in sources]
    if "history" in on:
        history = await db.last_broadcasts(s.history_limit)
        if history:
            lines.append("\n📨 <b>Последние рассылки:</b>")
            for b in history:
                when = time.strftime("%d.%m %H:%M", time.localtime(b["started_at"]))
                lines.append(f"{when} — доставлено {b['sent']}, не доставлено {b['failed']} "
                             f"({STATUS_NAMES[b['status']]})")
    if "running" in on and broadcaster.running and broadcaster.result:
        r = broadcaster.result
        lines.append(f"\n⏳ Сейчас идёт рассылка: {r.total} из {r.queued}")
    if len(lines) == 1:
        lines.append("Все блоки выключены. Включите нужные в «⚙️ Настроить».")
    return "\n".join(lines)


def stats_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🔄 Обновить", callback_data="st:show"),
        InlineKeyboardButton(text="⚙️ Настроить", callback_data="st:cfg"),
    ]])


def settings_text(s: StatsSettings) -> str:
    return (
        "⚙️ <b>Настройка статистики</b>\n\n"
        "Отметьте блоки, которые показывать в /stats. Настройки общие для всех админов.\n"
        f"Заголовок: <b>{html.escape(s.title)}</b>"
    )


def settings_keyboard(s: StatsSettings) -> InlineKeyboardMarkup:
    def mark(on: bool) -> str:
        return "✅" if on else "▫️"

    keys = list(SECTIONS)
    rows = [
        [InlineKeyboardButton(text=f"{mark(k in s.sections)} {SECTIONS[k]}", callback_data=f"st:sec:{k}")
         for k in keys[i:i + 2]]
        for i in range(0, len(keys), 2)
    ]
    rows.append([InlineKeyboardButton(text=f"{mark(p in s.periods)} за {name}", callback_data=f"st:per:{p}")
                 for p, name in PERIODS.items()])
    rows.append([
        InlineKeyboardButton(text=f"🔗 Источников: {s.sources_limit}", callback_data="st:src"),
        InlineKeyboardButton(text=f"📨 Рассылок: {s.history_limit}", callback_data="st:hist"),
    ])
    rows.append([
        InlineKeyboardButton(text="✏️ Заголовок", callback_data="st:title"),
        InlineKeyboardButton(text="♻️ По умолчанию", callback_data="st:reset"),
    ])
    rows.append([InlineKeyboardButton(text="📊 Показать статистику", callback_data="st:show")])
    return InlineKeyboardMarkup(inline_keyboard=rows)
