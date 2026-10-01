"""Статистика, которую админ настраивает кнопками: какие блоки показывать, за какие периоды, сколько строк.
Сюда же относится отчёт, который приходит после рассылки."""
from __future__ import annotations

import html
import time
from dataclasses import asdict, dataclass, field, fields

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from .broadcast import Broadcaster, Result
from .db import Database

SETTINGS_KEY = "stats"
OVERRIDES_KEY = "stats_overrides"
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
# цифры, которые админ может поправить вручную: ключ -> название
METRICS = {
    "users": "👤 Пользователей",
    "users_active": "👤 Доступны для рассылки",
    "groups": "👥 Групп и каналов",
    "groups_active": "👥 Активных групп",
    "active": "📬 Получат рассылку",
    **{f"new_{p}": f"🆕 Новых за {name}" for p, name in PERIODS.items()},
    **{f"seen_{p}": f"🔥 Активны за {name}" for p, name in PERIODS.items()},
}
# цифры отчёта после рассылки: реальное значение своё у каждой рассылки, правка применяется к каждой
REPORT_METRICS = {
    "bc_sent": "✅ Доставлено в рассылке",
    "bc_failed": "❌ Не доставлено в рассылке",
}
ALL_METRICS = {**METRICS, **REPORT_METRICS}
# строки отчёта после рассылки: ключ -> название кнопки
REPORT_LINES = {
    "sent": "✅ Доставлено",
    "failed": "❌ Не доставлено",
    "total": "👥 Всего обработано",
    "gone": "🚫 Заблокировали бота",
    "stats": "📊 Статистика бота",
}
DEFAULT_REPORT = ["sent", "failed", "total", "gone"]
NUMBER_MAX = 10 ** 9
STATUS_NAMES = {"running": "идёт", "done": "готово", "stopped": "остановлена", "error": "ошибка"}


@dataclass
class StatsSettings:
    sections: list[str] = field(default_factory=lambda: list(DEFAULT_SECTIONS))
    periods: list[int] = field(default_factory=lambda: [1, 7])
    sources_limit: int = 5
    history_limit: int = 3
    title: str = DEFAULT_TITLE
    report: list[str] = field(default_factory=lambda: list(DEFAULT_REPORT))

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
        if not isinstance(s.report, list):
            s.report = list(DEFAULT_REPORT)
        s.report = [k for k in REPORT_LINES if k in s.report]
        return s

    def toggle_section(self, key: str) -> None:
        on = set(self.sections) ^ {key}
        self.sections = [k for k in SECTIONS if k in on]

    def toggle_period(self, days: int) -> None:
        on = set(self.periods) ^ {days}
        self.periods = [p for p in PERIODS if p in on]

    def toggle_report(self, key: str) -> None:
        on = set(self.report) ^ {key}
        self.report = [k for k in REPORT_LINES if k in on]


def next_value(options: tuple[int, ...], current: int) -> int:
    return options[(options.index(current) + 1) % len(options)] if current in options else options[0]


async def load_settings(db: Database) -> StatsSettings:
    return StatsSettings.from_dict(await db.get_setting(SETTINGS_KEY))


async def save_settings(db: Database, s: StatsSettings) -> None:
    await db.set_setting(SETTINGS_KEY, asdict(s))


# ---------- ручные правки цифр ----------
# правило: "=1500" — показывать ровно 1500, "+500" / "-20" — прибавить к реальному значению

def parse_rule(text: str) -> str | None:
    """Ввод админа -> правило или None, если это не число."""
    t = text.strip().replace(" ", "")
    sign = t[0] if t[:1] in ("+", "-", "=") else "="
    digits = t[1:] if t[:1] in ("+", "-", "=") else t
    if not digits.isdigit() or int(digits) > NUMBER_MAX:
        return None
    return f"{sign}{int(digits)}"


def apply_rule(real: int, rule: str | None) -> int:
    if not rule:
        return real
    n = int(rule[1:])
    value = n if rule[0] == "=" else real + n if rule[0] == "+" else real - n
    return max(value, 0)


def describe_rule(rule: str) -> str:
    return f"всегда {rule[1:]}" if rule[0] == "=" else f"{rule[0]}{rule[1:]} к реальному"


async def load_overrides(db: Database) -> dict[str, str]:
    raw = await db.get_setting(OVERRIDES_KEY, {})
    if not isinstance(raw, dict):
        return {}
    return {k: v for k, v in raw.items() if k in ALL_METRICS and isinstance(v, str) and parse_rule(v) == v}


async def save_overrides(db: Database, overrides: dict[str, str]) -> None:
    if overrides:
        await db.set_setting(OVERRIDES_KEY, overrides)
    else:
        await db.delete_setting(OVERRIDES_KEY)


async def real_numbers(db: Database) -> dict[str, int]:
    """Реальные значения всех METRICS."""
    st = await db.stats()
    nums = {k: st[k] for k in ("users", "users_active", "groups", "groups_active", "active")}
    for p in PERIODS:
        c = await db.counts_since(p)
        nums[f"new_{p}"], nums[f"seen_{p}"] = c["new"], c["seen"]
    return nums


async def shown_numbers(db: Database) -> dict[str, int]:
    """Значения с учётом правок админа."""
    overrides = await load_overrides(db)
    return {k: apply_rule(v, overrides.get(k)) for k, v in (await real_numbers(db)).items()}


def shown_delivery(sent: int, failed: int, overrides: dict[str, str]) -> tuple[int, int]:
    """Доставлено / не доставлено с учётом правок админа."""
    return apply_rule(sent, overrides.get("bc_sent")), apply_rule(failed, overrides.get("bc_failed"))


def _by_periods(st: dict[str, int], key: str, periods: list[int]) -> str:
    return ", ".join(f"за {PERIODS[p]}: {st[f'{key}_{p}']}" for p in periods)


async def stats_text(db: Database, broadcaster: Broadcaster, s: StatsSettings | None = None) -> str:
    s = s or await load_settings(db)
    on = set(s.sections)
    st = await shown_numbers(db)
    overrides = await load_overrides(db)
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
    if s.periods and "growth" in on:
        lines.append(f"🆕 Новых {_by_periods(st, 'new', s.periods)}")
    if s.periods and "activity" in on:
        lines.append(f"🔥 Активны {_by_periods(st, 'seen', s.periods)}")
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
                sent, failed = shown_delivery(b["sent"], b["failed"], overrides)
                lines.append(f"{when} — доставлено {sent}, не доставлено {failed} "
                             f"({STATUS_NAMES.get(b['status'], b['status'])})")
    if "running" in on and broadcaster.running and broadcaster.result:
        r = broadcaster.result
        lines.append(f"\n⏳ Сейчас идёт рассылка: {r.total} из {r.queued}")
    if len(lines) == 1:
        lines.append("Все блоки выключены. Включите нужные в «⚙️ Настроить».")
    return "\n".join(lines)


async def report_text(db: Database, broadcaster: Broadcaster, res: Result, stopped: bool = False,
                      s: StatsSettings | None = None) -> str:
    """Отчёт после рассылки по настройкам админа (строки и правки цифр те же, что в /stats)."""
    s = s or await load_settings(db)
    on = set(s.report)
    sent, failed = shown_delivery(res.sent, res.failed, await load_overrides(db))
    lines = ["⛔️ <b>Рассылка остановлена</b>" if stopped else "✅ <b>Рассылка завершена!</b>"]
    body = []
    if "sent" in on:
        body.append(f"✅ Успешно доставлено: {sent} пользователям")
    if "failed" in on:
        body.append(f"❌ Не доставлено: {failed} пользователям")
    if "total" in on:
        body.append(f"👥 Всего обработано: {sent + failed} пользователей")
    if body:
        lines += ["", "📊 <b>Статистика:</b>", *body]
    if "gone" in on and res.gone:
        lines += ["", f"🚫 Заблокировали бота или удалены: {res.gone} (больше не получат рассылку)"]
    if "stats" in on:
        # рассылка уже закончилась, блок «текущая рассылка» тут лишний
        bot_stats = StatsSettings(**{**asdict(s), "sections": [k for k in s.sections if k != "running"]})
        lines += ["", await stats_text(db, broadcaster, bot_stats)]
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
        InlineKeyboardButton(text="🔢 Изменить цифры", callback_data="st:nums"),
    ])
    rows.append([InlineKeyboardButton(text="📨 Отчёт после рассылки", callback_data="st:rep")])
    rows.append([InlineKeyboardButton(text="♻️ По умолчанию", callback_data="st:reset")])
    rows.append([InlineKeyboardButton(text="📊 Показать статистику", callback_data="st:show")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def report_settings_text() -> str:
    return (
        "📨 <b>Отчёт после рассылки</b>\n\n"
        "Отметьте строки, которые придут после рассылки. «📊 Статистика бота» добавит к отчёту "
        "статистику с блоками из настроек /stats.\n"
        "Цифры доставки правятся в «🔢 Изменить цифры»."
    )


def report_settings_keyboard(s: StatsSettings) -> InlineKeyboardMarkup:
    keys = list(REPORT_LINES)
    rows = [
        [InlineKeyboardButton(text=f"{'✅' if k in s.report else '▫️'} {REPORT_LINES[k]}",
                              callback_data=f"st:rep:{k}") for k in keys[i:i + 2]]
        for i in range(0, len(keys), 2)
    ]
    rows.append([InlineKeyboardButton(text="« Назад к настройкам", callback_data="st:cfg")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def numbers_text(db: Database) -> str:
    real, overrides = await real_numbers(db), await load_overrides(db)
    lines = ["🔢 <b>Изменение цифр статистики</b>\n",
             "Выберите показатель и пришлите число:",
             "<code>1500</code> — показывать ровно 1500",
             "<code>+500</code> / <code>-20</code> — прибавить или вычесть из реального значения\n"]
    for k, name in METRICS.items():
        rule = overrides.get(k)
        line = f"{name}: <b>{apply_rule(real[k], rule)}</b>"
        if rule:
            line += f" (реально {real[k]}, {describe_rule(rule)})"
        lines.append(line)
    lines.append("\n📨 <b>После рассылки</b> (поправка к каждому отчёту и к «Последним рассылкам»):")
    for k, name in REPORT_METRICS.items():
        rule = overrides.get(k)
        lines.append(f"{name}: {describe_rule(rule) if rule else 'реальное значение'}")
    return "\n".join(lines)


async def numbers_keyboard(db: Database) -> InlineKeyboardMarkup:
    overrides = await load_overrides(db)
    keys = list(ALL_METRICS)
    rows = [
        [InlineKeyboardButton(text=("✏️ " if k in overrides else "") + ALL_METRICS[k], callback_data=f"st:num:{k}")
         for k in keys[i:i + 2]]
        for i in range(0, len(keys), 2)
    ]
    if overrides:
        rows.append([InlineKeyboardButton(text="♻️ Вернуть реальные цифры", callback_data="st:numclr")])
    rows.append([InlineKeyboardButton(text="« Назад к настройкам", callback_data="st:cfg")])
    return InlineKeyboardMarkup(inline_keyboard=rows)
