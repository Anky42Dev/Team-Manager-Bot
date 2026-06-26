import asyncio
import csv
import io
import logging
import json
import os
from datetime import datetime, time, timedelta
from typing import Optional
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    BotCommand
)
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    ContextTypes, MessageHandler, filters, ConversationHandler
)
from telegram.helpers import escape_markdown
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
import pytz

from config import (
    BOT_TOKEN,
    GROUP_ID, TOPIC_WORK_TIME, TOPIC_GENERAL, TOPIC_REPORTS,
    TIMEZONE, ADMIN_IDS
)
from database import db
from ai_client import call_ai

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)

scheduler = AsyncIOScheduler(timezone=pytz.timezone(TIMEZONE))


# ─── HELPERS ────────────────────────────────────────────────────────────────

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


async def send_long_message(reply_func, text: str, **kwargs):
    """Split text longer than 4000 chars and send in parts."""
    limit = 4000
    if len(text) <= limit:
        await reply_func(text, **kwargs)
        return
    parts = [text[i:i + limit] for i in range(0, len(text), limit)]
    for part in parts:
        await reply_func(part, **kwargs)


STATUS_LABELS = {"vacation": "в отпуске", "sick": "на больничном", "active": "активен"}
STATUS_ICONS  = {"vacation": "🏖", "sick": "🤒", "active": "✅"}

# ── Report structure shown in reminders ─────────────────────────────────────
REPORT_STRUCTURE = (
    "Структура отчёта:\n"
    "1. Что сделал сегодня (конкретные задачи и результаты)\n"
    "2. Над чем работаю завтра\n"
    "3. Есть ли блокеры или вопросы"
)

# ── AI prompt templates ──────────────────────────────────────────────────────
AI_PROMPT_DAILY = (
    "Отчёты команды за {date}:\n\n{reports_text}\n\n"
    "Составь краткий итог дня строго по этой структуре:\n\n"
    "ЧТО СДЕЛАНО\n"
    "2-3 предложения о реальных результатах на основе отчётов.\n\n"
    "КЛЮЧЕВЫЕ РЕЗУЛЬТАТЫ\n"
    "До 4 пунктов — только крупные задачи и завершённые вещи. "
    "Если задача мелкая или непонятная — не включай.\n\n"
    "БЛОКЕРЫ\n"
    "Перечисли если есть. Если нет — напиши: Нет.\n\n"
    "КАЧЕСТВО ОТЧЁТОВ\n"
    "Если какие-то отчёты написаны размыто, слишком кратко или без конкретики — "
    "дай общую рекомендацию команде как писать лучше (без имён участников).\n\n"
    "Пиши по-русски, кратко, только факты из отчётов."
)

AI_PROMPT_PERIOD_BRIEF = (
    "Состав команды: {member_names}.\n"
    "Период: {date_range} ({total_reports} отчётов от {active_members} участников).\n\n"
    "Отчёты:\n\n{reports_text}\n\n"
    "Составь краткий итог периода строго по этой структуре:\n\n"
    "ЧТО СДЕЛАНО ЗА ПЕРИОД\n"
    "3-4 предложения о реальных результатах.\n\n"
    "КЛЮЧЕВЫЕ РЕЗУЛЬТАТЫ\n"
    "До 5 пунктов — только крупные проекты и важные завершения.\n\n"
    "БЛОКЕРЫ И ПРОБЛЕМЫ\n"
    "Если есть — перечисли. Если нет — напиши: Нет.\n\n"
    "АКТИВНОСТЬ\n"
    "Кто писал регулярно, кто редко — только факты из данных.\n\n"
    "КАЧЕСТВО ОТЧЁТОВ\n"
    "Общая оценка: насколько отчёты конкретные и понятные. "
    "Если есть проблемы — дай рекомендации команде без упоминания имён.\n\n"
    "Пиши по-русски, только факты из отчётов."
)

AI_PROMPT_PERIOD_FULL = (
    "Состав команды: {member_names}.\n"
    "Период: {date_range} ({total_reports} отчётов от {active_members} участников).\n\n"
    "Отчёты по участникам:\n{reports_text}\n\n"
    "Составь подробный отчёт строго по этой структуре:\n\n"
    "ОБЩИЕ ИТОГИ\n"
    "3-4 предложения об общем прогрессе команды за период.\n\n"
    "ПО КАЖДОМУ УЧАСТНИКУ\n"
    "Для каждого: имя, что делал (конкретные задачи), количество отчётов. "
    "Только то что реально написано в отчётах.\n\n"
    "КЛЮЧЕВЫЕ ДОСТИЖЕНИЯ КОМАНДЫ\n"
    "До 5 самых важных результатов за период.\n\n"
    "ПРОБЛЕМЫ И БЛОКЕРЫ\n"
    "Что мешало работе согласно отчётам. Если не упоминалось — напиши: Нет.\n\n"
    "КАЧЕСТВО ОТЧЁТОВ\n"
    "Общая оценка: насколько отчёты информативны. "
    "Конкретные рекомендации команде — писать более чётко, с результатами, "
    "без воды, с указанием проекта. Без имён участников.\n\n"
    "РЕКОМЕНДАЦИИ\n"
    "2-3 конкретных совета по улучшению процессов на основе анализа.\n\n"
    "Пиши по-русски, только факты из отчётов."
)


def fmt_until(iso_date: str) -> str:
    """'2026-06-28' → '28.06'"""
    if not iso_date:
        return ""
    parts = iso_date.split("-")
    return f"{parts[2]}.{parts[1]}"


def get_members_keyboard(absent_ids: list[int] = None):
    """Build inline keyboard with team members checkboxes."""
    members = db.get_members()
    absent_ids = absent_ids or []
    keyboard = []
    for m in members:
        checked = "✅" if m["id"] not in absent_ids else "☐"
        keyboard.append([InlineKeyboardButton(
            f"{checked} {m['name']}",
            callback_data=f"toggle_{m['id']}"
        )])
    keyboard.append([
        InlineKeyboardButton("📤 Отправить предупреждения", callback_data="send_warnings"),
        InlineKeyboardButton("❌ Отмена", callback_data="cancel_attendance")
    ])
    return InlineKeyboardMarkup(keyboard)


def build_notif_keyboard() -> InlineKeyboardMarkup:
    """Build notifications menu keyboard with delete buttons for custom ones."""
    notifs = db.get_notifications()
    keyboard = []
    for n in notifs:
        status = "✅" if n["enabled"] else "❌"
        row = [InlineKeyboardButton(
            f"{status} {n['name']} ({n['time']})",
            callback_data=f"notif_toggle_{n['id']}"
        )]
        if not n["is_builtin"]:
            row.append(InlineKeyboardButton("🗑", callback_data=f"notif_delete_{n['id']}"))
        keyboard.append(row)
    keyboard.append([InlineKeyboardButton("➕ Добавить уведомление", callback_data="notif_add")])
    return InlineKeyboardMarkup(keyboard)


# ─── SCHEDULED JOBS ─────────────────────────────────────────────────────────

async def job_attendance_reminder(app: Application):
    """10:30 — open attendance check for all admins."""
    if not ADMIN_IDS:
        return
    db.reset_daily_absent()
    keyboard = get_members_keyboard(absent_ids=[])
    for admin_id in ADMIN_IDS:
        try:
            await app.bot.send_message(
                chat_id=admin_id,
                text="🕙 *10:30 — Отметьте отсутствующих сегодня*\n\n"
                     "Снимите галочку с тех, кто не присутствует.\n"
                     "Затем нажмите «Отправить предупреждения».",
                reply_markup=keyboard,
                parse_mode="Markdown"
            )
        except Exception as e:
            logger.error(f"Не удалось отправить посещаемость администратору {admin_id}: {e}")


async def job_evening_reminder(app: Application):
    """20:00 — remind to write reports and push."""
    members = db.get_members()
    mentions = " ".join(f"[{m['name']}](tg://user?id={m['id']})" for m in members)
    text = (
        "🌙 *Напоминание перед концом дня*\n\n"
        f"{mentions}\n\n"
        "📝 Не забудьте написать **отчёт** в тему Отчёты\n"
        "🚀 Не забудьте **запушить все изменения** в Git"
    )
    await app.bot.send_message(
        chat_id=GROUP_ID,
        message_thread_id=TOPIC_GENERAL,
        text=text,
        parse_mode="Markdown"
    )


async def job_morning_reminder(app: Application):
    """10:00 — remind to pull from git."""
    members = db.get_members()
    mentions = " ".join(f"[{m['name']}](tg://user?id={m['id']})" for m in members)
    text = (
        "☀️ *Доброе утро, команда!*\n\n"
        f"{mentions}\n\n"
        "⬇️ Не забудьте **стянуть все изменения с Git** перед началом работы"
    )
    await app.bot.send_message(
        chat_id=GROUP_ID,
        message_thread_id=TOPIC_GENERAL,
        text=text,
        parse_mode="Markdown"
    )


async def job_analyze_reports(app: Application):
    """End of day — read report topic and analyze with Claude."""
    messages = db.get_today_report_messages()
    members = db.get_members()

    if not messages:
        await app.bot.send_message(
            chat_id=GROUP_ID,
            message_thread_id=TOPIC_REPORTS,
            text="📊 *Анализ отчётов за сегодня*\n\n❗ Сегодня никто не написал отчёт.",
            parse_mode="Markdown"
        )
        return

    member_names = [m["name"] for m in members]
    reported_ids = [msg["user_id"] for msg in messages]
    reported_names = [m["name"] for m in members if m["id"] in reported_ids]
    missing_names = [m["name"] for m in members if m["id"] not in reported_ids]

    # Build prompt for Claude
    reports_text = "\n\n".join(
        f"[{msg['user_name']}]: {msg['text']}" for msg in messages
    )
    prompt = AI_PROMPT_DAILY.format(
        date=datetime.now().strftime("%d.%m.%Y"),
        reports_text=reports_text
    )

    try:
        summary = call_ai(prompt, max_tokens=800)
    except Exception as e:
        logger.error("AI error in analyze_reports: %s", e)
        summary = "(не удалось получить AI-анализ)"

    wrote = ", ".join(escape_markdown(n, version=1) for n in reported_names) if reported_names else "никто"
    didnt = ", ".join(escape_markdown(n, version=1) for n in missing_names) if missing_names else "все написали 🎉"

    stats_text = (
        f"📊 *Анализ отчётов за {datetime.now().strftime('%d.%m.%Y')}*\n\n"
        f"✅ *Написали отчёт:* {wrote}\n"
        f"❌ *Не написали:* {didnt}"
    )
    await app.bot.send_message(
        chat_id=GROUP_ID,
        message_thread_id=TOPIC_REPORTS,
        text=stats_text,
        parse_mode="Markdown"
    )
    await app.bot.send_message(
        chat_id=GROUP_ID,
        message_thread_id=TOPIC_REPORTS,
        text=f"🤖 AI-выжимка:\n\n{summary}"
    )


# ─── MULTI-LEVEL INTERACTIVE MENU ───────────────────────────────────────────
#
# Callback data format:  m|<section>|<action>|<arg>
# Sections: hm=home, td=today, tm=team, an=analytics, no=notifications, ex=export
#

_WAITING: dict[int, dict] = {}  # user_id → {action, data}  for text-input flows
_BC_SEL: dict[int, set] = {}   # admin_uid → set of selected member_ids for broadcast


def _kb(*rows) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(list(rows))


def _btn(label: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(label, callback_data=data)


def _back(to: str = "hm") -> list[InlineKeyboardButton]:
    return [_btn("◀ Назад", f"m|{to}")]


# ── HOME ────────────────────────────────────────────────────────────────────

def _home_kb() -> InlineKeyboardMarkup:
    return _kb(
        [_btn("📅 Сегодня",       "m|td"),   _btn("👥 Команда",        "m|tm")],
        [_btn("📊 Аналитика",     "m|an"),   _btn("🔔 Уведомления",    "m|no")],
        [_btn("📥 Экспорт",       "m|ex"),   _btn("📤 Рассылка",       "m|bc")],
    )


async def cmd_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Выберите раздел:", reply_markup=_home_kb())


# ── TODAY ────────────────────────────────────────────────────────────────────

async def _show_today(query) -> None:
    today = datetime.now().strftime("%d.%m.%Y")
    msgs = db.get_today_report_messages()
    members = db.get_members()
    reported_ids = {r["user_id"] for r in msgs}
    wrote = sum(1 for m in members if m["id"] in reported_ids)
    total = len(members)
    kb = _kb(
        [_btn("✅ Отметить посещаемость", "m|td|att")],
        [_btn("📝 Отчёты сегодня",        "m|td|rep")],
        [_btn("📊 Кто присутствует",      "m|td|pres")],
        _back(),
    )
    await query.edit_message_text(
        f"📅 *Сегодня, {today}*\n"
        f"Отчётов: {wrote}/{total}",
        parse_mode="Markdown", reply_markup=kb
    )


# ── TEAM ─────────────────────────────────────────────────────────────────────

def _team_kb() -> InlineKeyboardMarkup:
    return _kb(
        [_btn("👥 Список",          "m|tm|list"), _btn("✏️ Переименовать",  "m|tm|ren")],
        [_btn("🏷 Изменить статус", "m|tm|sts"),  _btn("➕ Добавить",       "m|tm|add")],
        [_btn("🗑 Удалить",         "m|tm|del")],
        _back(),
    )


# ── ANALYTICS ────────────────────────────────────────────────────────────────

def _analytics_kb() -> InlineKeyboardMarkup:
    return _kb(
        [_btn("📈 Всё время",         "m|an|all"),  _btn("📅 30 дней",       "m|an|30d")],
        [_btn("🤖 AI краткий",        "m|an|ai"),   _btn("🤖 AI подробный",  "m|an|aif")],
        [_btn("⚠️ Нарушения",         "m|an|vio")],
        _back(),
    )


# ── NOTIFICATIONS ─────────────────────────────────────────────────────────────

async def _show_notifs_menu(query) -> None:
    notifs = db.get_notifications()
    lines = ["🔔 *Уведомления*\n"]
    rows = []
    for n in notifs:
        icon = "🟢" if n["enabled"] else "⚫"
        time_str = n["time"]
        lines.append(f"{icon} {time_str} — {n['text'][:30]}")
        toggle_lbl = "Выкл" if n["enabled"] else "Вкл"
        rows.append([
            _btn(f"{icon} {time_str} {n['text'][:18]}", f"m|no|inf|{n['id']}"),
            _btn(toggle_lbl, f"m|no|tog|{n['id']}"),
        ])
    rows.append([_btn("➕ Новое уведомление", "m|no|new")])
    rows.append(_back())
    await query.edit_message_text(
        "\n".join(lines), parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(rows)
    )


# ── EXPORT ───────────────────────────────────────────────────────────────────

def _export_kb() -> InlineKeyboardMarkup:
    return _kb(
        [_btn("⚠️ CSV нарушений",    "m|ex|vio")],
        [_btn("📊 CSV аналитики",    "m|ex|ana")],
        _back(),
    )


def _bc_kb(admin_uid: int) -> InlineKeyboardMarkup:
    """Broadcast recipient selector — checkboxes per member."""
    members = db.get_members()
    selected = _BC_SEL.get(admin_uid, set())
    rows = []
    for m in members:
        icon = "✅" if m["id"] in selected else "☐"
        rows.append([_btn(f"{icon} {m['name']}", f"m|bc|tog|{m['id']}")])
    count = len(selected)
    rows.append([_btn(f"📨 Отправить выбранным ({count})", "m|bc|txt")])
    rows.append(_back())
    return _kb(*rows)


# ── MEMBER PICKERS ───────────────────────────────────────────────────────────

def _member_pick_kb(action_prefix: str, back_to: str = "tm") -> InlineKeyboardMarkup:
    members = db.get_members()
    rows = []
    pair = []
    for m in members:
        pair.append(_btn(m["name"], f"{action_prefix}|{m['id']}"))
        if len(pair) == 2:
            rows.append(pair); pair = []
    if pair:
        rows.append(pair)
    rows.append(_back(back_to))
    return InlineKeyboardMarkup(rows)


def _status_pick_kb(member_id: int) -> InlineKeyboardMarkup:
    return _kb(
        [_btn("✅ Активен",    f"m|tm|sts|{member_id}|active")],
        [_btn("🏖 Отпуск",    f"m|tm|sts|{member_id}|vacation")],
        [_btn("🤒 Болезнь",   f"m|tm|sts|{member_id}|sick")],
        _back("tm"),
    )


# ── MAIN HANDLER ─────────────────────────────────────────────────────────────

async def handle_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id
    admin = is_admin(uid)
    parts = query.data.split("|")
    # parts[0] = 'm', parts[1] = section, parts[2+] = sub-action/args

    sec = parts[1] if len(parts) > 1 else "hm"

    # ── home ──
    if sec == "hm":
        await query.edit_message_text("Выберите раздел:", reply_markup=_home_kb())
        return

    # ── today ──
    if sec == "td":
        sub = parts[2] if len(parts) > 2 else ""
        if not sub:
            await _show_today(query); return

        if sub == "att":
            if not admin:
                await query.answer("Только для администраторов", show_alert=True); return
            await _start_att_wizard(query, context); return

        if sub == "rep":
            msgs = db.get_today_report_messages()
            members = db.get_members()
            reported_ids = {r["user_id"] for r in msgs}
            today = datetime.now().strftime("%d.%m.%Y")
            lines = [f"📝 *Отчёты за {today}*\n"]
            for m in members:
                icon = "✅" if m["id"] in reported_ids else "❌"
                lines.append(f"{icon} {escape_markdown(m['name'], version=1)}")
            await query.edit_message_text("\n".join(lines), parse_mode="Markdown",
                                          reply_markup=_kb(_back("td")))
            return

        if sub == "pres":
            today_str = datetime.now().date().isoformat()
            members = db.get_members()
            lines = [f"📋 *Присутствие сегодня*\n"]
            for m in members:
                hist = db.get_attendance_history(m["id"], days=1)
                status = hist[0]["status"] if hist and hist[-1]["date"] == today_str else "present"
                icons = {"present": "✅", "sick": "🤒", "vacation": "🏖", "absent": "❓"}
                lines.append(f"{icons.get(status,'✅')} {escape_markdown(m['name'], version=1)}")
            await query.edit_message_text("\n".join(lines), parse_mode="Markdown",
                                          reply_markup=_kb(_back("td")))
            return

    # ── team ──
    if sec == "tm":
        if not admin:
            await query.answer("Только для администраторов", show_alert=True); return
        sub = parts[2] if len(parts) > 2 else ""

        if not sub:
            await query.edit_message_text("👥 Управление командой:", reply_markup=_team_kb())
            return

        if sub == "list":
            members = db.get_members()
            lines = ["👥 *Команда*\n"]
            for m in members:
                icon = STATUS_ICONS.get(m.get("status", "active"), "✅")
                lines.append(f"{icon} {escape_markdown(m['name'], version=1)}")
            await query.edit_message_text("\n".join(lines), parse_mode="Markdown",
                                          reply_markup=_kb(_back("tm")))
            return

        if sub == "ren":
            if len(parts) > 3:
                mid = int(parts[3])
                members = db.get_members()
                name = next((m["name"] for m in members if m["id"] == mid), str(mid))
                _WAITING[uid] = {"action": "rename", "id": mid, "old_name": name,
                                  "chat_id": query.message.chat_id, "msg_id": query.message.message_id}
                await query.edit_message_text(
                    f"✏️ Введите новое имя для *{escape_markdown(name, version=1)}*:\n"
                    f"_(или /cancel для отмены)_",
                    parse_mode="Markdown"
                )
            else:
                await query.edit_message_text("✏️ Выберите участника для переименования:",
                                               reply_markup=_member_pick_kb("m|tm|ren"))
            return

        if sub == "sts":
            if len(parts) > 4:
                mid, status = int(parts[3]), parts[4]
                db.update_member_status(mid, status)
                members = db.get_members()
                name = next((m["name"] for m in members if m["id"] == mid), str(mid))
                label = STATUS_LABELS.get(status, status)
                await query.edit_message_text(
                    f"✅ {escape_markdown(name, version=1)} → {label}",
                    parse_mode="Markdown", reply_markup=_kb(_back("tm"))
                )
            elif len(parts) > 3:
                mid = int(parts[3])
                members = db.get_members()
                name = next((m["name"] for m in members if m["id"] == mid), str(mid))
                await query.edit_message_text(
                    f"🏷 Новый статус для *{escape_markdown(name, version=1)}*:",
                    parse_mode="Markdown", reply_markup=_status_pick_kb(mid)
                )
            else:
                await query.edit_message_text("🏷 Выберите участника:",
                                               reply_markup=_member_pick_kb("m|tm|sts"))
            return

        if sub == "add":
            _WAITING[uid] = {"action": "addmember", "chat_id": query.message.chat_id,
                              "msg_id": query.message.message_id}
            await query.edit_message_text(
                "➕ Введите ID и имя участника через пробел:\n"
                "`123456789 Иван`\n_(или /cancel)_",
                parse_mode="Markdown"
            )
            return

        if sub == "del":
            if len(parts) > 3:
                mid = int(parts[3])
                members = db.get_members()
                name = next((m["name"] for m in members if m["id"] == mid), str(mid))
                db.remove_member(mid)
                await query.edit_message_text(
                    f"🗑 {escape_markdown(name, version=1)} удалён",
                    parse_mode="Markdown", reply_markup=_kb(_back("tm"))
                )
            else:
                await query.edit_message_text("🗑 Выберите участника для удаления:",
                                               reply_markup=_member_pick_kb("m|tm|del"))
            return

    # ── analytics ──
    if sec == "an":
        if not admin:
            await query.answer("Только для администраторов", show_alert=True); return
        sub = parts[2] if len(parts) > 2 else ""

        if not sub:
            await query.edit_message_text("📊 Аналитика:", reply_markup=_analytics_kb())
            return

        if sub in ("all", "30d"):
            start = None if sub == "all" else (datetime.now() - timedelta(days=30)).date().isoformat()
            label = "всё время" if sub == "all" else "30 дней"
            stats = db.get_analytics(start_date=start)
            members = db.get_members()
            lines = [f"📈 *Аналитика — {label}*\n"]
            for m in members:
                s = stats.get(m["id"], {})
                rep = s.get("reports", 0); ab = s.get("absences", 0)
                streak = db.get_streak(m["id"])
                bar = "🟩" * min(rep, 5) + "⬜" * max(0, 5 - rep)
                streak_txt = f" {_streak_label(streak)}" if streak > 0 else ""
                lines.append(f"{bar} {escape_markdown(m['name'], version=1)} — {rep} отч, {ab} отс{streak_txt}")
            await query.edit_message_text("\n".join(lines), parse_mode="Markdown",
                                          reply_markup=_kb(_back("an")))
            return

        if sub in ("ai", "aif"):
            detailed = (sub == "aif")
            await query.edit_message_text("🤖 Запрашиваю AI-анализ...")
            messages_list = db.get_reports_for_period()
            if not messages_list:
                await query.edit_message_text("Отчётов нет.", reply_markup=_kb(_back("an")))
                return
            members = db.get_members()
            member_names = [m["name"] for m in members]
            from collections import defaultdict
            by_member: dict[str, list] = defaultdict(list)
            for msg in messages_list:
                by_member[msg["user_name"]].append(f"[{msg['date']}] {msg['text']}")
            if detailed:
                rt = "".join(f"\n\n=== {n} ===\n" + "\n---\n".join(r) for n, r in sorted(by_member.items()))
                prompt = AI_PROMPT_PERIOD_FULL.format(
                    member_names=", ".join(member_names), date_range="всё время",
                    total_reports=len(messages_list), active_members=len(by_member), reports_text=rt)
                max_tok = 2000
            else:
                rt = "\n\n".join(f"[{m['date']}] {m['user_name']}: {m['text']}" for m in messages_list)[-8000:]
                prompt = AI_PROMPT_PERIOD_BRIEF.format(
                    member_names=", ".join(member_names), date_range="всё время",
                    total_reports=len(messages_list), active_members=len(by_member), reports_text=rt)
                max_tok = 1000
            try:
                summary = call_ai(prompt, max_tokens=max_tok)
            except Exception as e:
                await query.edit_message_text(f"❌ AI недоступен: {e}", reply_markup=_kb(_back("an")))
                return
            mode = "подробный" if detailed else "краткий"
            await query.edit_message_text(f"🤖 AI-анализ ({mode})\n\n" + summary[:3900],
                                           reply_markup=_kb(_back("an")))
            return

        if sub == "vio":
            await query.edit_message_text("⏳ Генерирую CSV...")
            file_data, total = generate_violations_csv()
            await context.bot.send_document(
                chat_id=query.message.chat_id,
                document=file_data,
                filename=f"violations_{datetime.now().strftime('%Y%m%d')}.csv",
                caption=f"⚠️ Нарушений: {total}"
            )
            await query.edit_message_text("✅ CSV отправлен.", reply_markup=_kb(_back("an")))
            return

    # ── notifications ──
    if sec == "no":
        sub = parts[2] if len(parts) > 2 else ""
        if not sub:
            await _show_notifs_menu(query); return

        if sub == "tog":
            nid = int(parts[3])
            db.toggle_notification(nid)
            notif = db.get_notification(nid)
            if notif and notif["enabled"] and app_ref:
                h, m = map(int, notif["time"].split(":"))
                schedule_custom_notification(
                    app_ref, nid, h, m,
                    notif.get("topic_id"), notif.get("text")
                )
            elif app_ref:
                job_id = f"custom_{nid}"
                if scheduler.get_job(job_id):
                    scheduler.remove_job(job_id)
            await _show_notifs_menu(query); return

        if sub == "inf":
            nid = int(parts[3])
            n = db.get_notification(nid)
            if not n:
                await _show_notifs_menu(query); return
            st = "включено" if n["enabled"] else "выключено"
            is_custom = not n.get("is_builtin", 1)
            rows = [_back("no")]
            if is_custom:
                rows.insert(0, [_btn("🗑 Удалить", f"m|no|del|{nid}")])
            await query.edit_message_text(
                f"🔔 *{escape_markdown(n['text'], version=1)}*\n"
                f"Время: {n['time']}\n"
                f"Статус: {st}",
                parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(rows)
            )
            return

        if sub == "del":
            nid = int(parts[3])
            db.delete_notification(nid)
            job_id = f"custom_{nid}"
            if scheduler.get_job(job_id):
                scheduler.remove_job(job_id)
            await _show_notifs_menu(query); return

        if sub == "new":
            _WAITING[uid] = {"action": "addnotif", "chat_id": query.message.chat_id,
                              "msg_id": query.message.message_id}
            await query.edit_message_text(
                "🔔 Введите уведомление в формате:\n"
                "`HH:MM Текст сообщения`\n\n"
                "Например: `09:00 Не забудьте написать план на день`\n"
                "_(или /cancel)_",
                parse_mode="Markdown"
            )
            return

    # ── export ──
    if sec == "ex":
        if not admin:
            await query.answer("Только для администраторов", show_alert=True); return
        sub = parts[2] if len(parts) > 2 else ""

        if not sub:
            await query.edit_message_text("📥 Экспорт:", reply_markup=_export_kb())
            return

        if sub == "vio":
            await query.edit_message_text("⏳ Генерирую CSV...")
            file_data, total = generate_violations_csv()
            await context.bot.send_document(
                chat_id=query.message.chat_id,
                document=file_data,
                filename=f"violations_{datetime.now().strftime('%Y%m%d')}.csv",
                caption=f"⚠️ Нарушений: {total}"
            )
            await query.edit_message_text("✅ CSV отправлен.", reply_markup=_kb(_back("ex")))
            return

        if sub == "ana":
            await query.edit_message_text("⏳ Генерирую CSV аналитики...")
            # reuse existing export function result directly
            members = db.get_members()
            stats = db.get_analytics()
            import io, csv as _csv
            out = io.StringIO()
            w = _csv.writer(out)
            w.writerow(["Участник", "Отчётов", "Отсутствий", "Предупреждений"])
            for m in members:
                s = stats.get(m["id"], {})
                w.writerow([m["name"], s.get("reports", 0), s.get("absences", 0), s.get("warnings", 0)])
            raw = out.getvalue().encode("utf-8-sig")
            await context.bot.send_document(
                chat_id=query.message.chat_id,
                document=io.BytesIO(raw),
                filename=f"analytics_{datetime.now().strftime('%Y%m%d')}.csv",
                caption="📊 Аналитика — всё время"
            )
            await query.edit_message_text("✅ CSV отправлен.", reply_markup=_kb(_back("ex")))
            return

    # ── broadcast ──
    if sec == "bc":
        if not admin:
            await query.answer("Только для администраторов", show_alert=True); return
        sub = parts[2] if len(parts) > 2 else ""

        if sub == "":
            # Init selection: all non-admin members pre-checked
            members = db.get_members()
            _BC_SEL[uid] = {m["id"] for m in members if m["id"] not in ADMIN_IDS}
            await query.edit_message_text(
                "📤 *Рассылка* — выберите получателей:\n\n"
                "✅ — получит сообщение\n☐ — не получит",
                parse_mode="Markdown",
                reply_markup=_bc_kb(uid)
            )
            return

        if sub == "tog":
            member_id = int(parts[3])
            sel = _BC_SEL.setdefault(uid, set())
            if member_id in sel:
                sel.discard(member_id)
            else:
                sel.add(member_id)
            await query.edit_message_reply_markup(reply_markup=_bc_kb(uid))
            return

        if sub == "txt":
            recipients = list(_BC_SEL.get(uid, []))
            if not recipients:
                await query.answer("Выберите хотя бы одного получателя", show_alert=True)
                return
            _WAITING[uid] = {"action": "broadcast", "recipients": recipients}
            await query.edit_message_text(
                "✍️ Введите текст рассылки:\n_(или /cancel для отмены)_"
            )
            return


async def handle_menu_text_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle text input for menu flows (rename, add member, add notif, broadcast)."""
    uid = update.effective_user.id
    state = _WAITING.get(uid)
    if not state:
        return  # not waiting for anything

    text = update.message.text.strip()
    action = state["action"]
    del _WAITING[uid]

    if text.startswith("/"):
        await update.message.reply_text("Отменено.")
        return

    if action == "rename":
        mid, old = state["id"], state["old_name"]
        db.rename_member(mid, text)
        await update.message.reply_text(
            f"✅ *{escape_markdown(old, version=1)}* → *{escape_markdown(text, version=1)}*",
            parse_mode="Markdown", reply_markup=_kb(_back("tm"))
        )

    elif action == "addmember":
        parts = text.split(None, 1)
        if len(parts) < 2 or not parts[0].isdigit():
            await update.message.reply_text(
                "❌ Формат: `ID Имя` — например: `123456789 Иван`", parse_mode="Markdown"
            )
            return
        mid, name = int(parts[0]), parts[1]
        db.add_member(mid, name)
        await update.message.reply_text(
            f"✅ Добавлен: *{escape_markdown(name, version=1)}*",
            parse_mode="Markdown", reply_markup=_kb(_back("tm"))
        )

    elif action == "addnotif":
        parts = text.split(None, 1)
        if len(parts) < 2 or ":" not in parts[0]:
            await update.message.reply_text(
                "❌ Формат: `HH:MM Текст` — например: `09:00 Доброе утро!`", parse_mode="Markdown"
            )
            return
        try:
            h, m = map(int, parts[0].split(":"))
            notif_text = parts[1]
        except ValueError:
            await update.message.reply_text("❌ Неверный формат времени. Пример: `09:30`", parse_mode="Markdown")
            return
        nid = db.add_notification(h, m, notif_text, topic_id=TOPIC_GENERAL)
        if app_ref:
            schedule_custom_notification(app_ref, nid, h, m, TOPIC_GENERAL, notif_text)
        await update.message.reply_text(
            f"✅ Уведомление добавлено: *{h:02d}:{m:02d}* — {escape_markdown(notif_text, version=1)}",
            parse_mode="Markdown", reply_markup=_kb(_back("no"))
        )

    elif action == "broadcast":
        from telegram.error import Forbidden, BadRequest
        recipients = state.get("recipients", [])
        members = db.get_members()
        name_map = {m["id"]: m["name"] for m in members}
        sent = 0
        no_chat: list[str] = []   # never started bot
        other_fail: list[str] = []
        for mid in recipients:
            try:
                await context.bot.send_message(chat_id=mid, text=text)
                sent += 1
            except Forbidden:
                no_chat.append(name_map.get(mid, str(mid)))
            except Exception as e:
                other_fail.append(f"{name_map.get(mid, str(mid))} ({e})")
        lines = [f"✅ Отправлено: {sent}/{len(recipients)}"]
        if no_chat:
            lines.append(
                f"\n⚠️ Не получили ({len(no_chat)}) — не начали диалог с ботом:\n"
                + "\n".join(f"  • {n}" for n in no_chat)
                + "\n\nПопросите их написать /start боту в личку."
            )
        if other_fail:
            lines.append("\n❌ Ошибки:\n" + "\n".join(f"  • {n}" for n in other_fail))
        await update.message.reply_text("\n".join(lines))
        _BC_SEL.pop(uid, None)


def _main_menu_keyboard(is_admin_user: bool) -> InlineKeyboardMarkup:
    return _home_kb()
    if is_admin_user:
        return InlineKeyboardMarkup([
            [
                InlineKeyboardButton("👥 Команда",         callback_data="menu|team"),
                InlineKeyboardButton("📊 Аналитика",       callback_data="menu|analytics"),
            ],
            [
                InlineKeyboardButton("✅ Посещаемость",    callback_data="menu|attendance"),
                InlineKeyboardButton("📝 Отчёты сегодня", callback_data="menu|reports"),
            ],
            [
                InlineKeyboardButton("🤖 AI-анализ",       callback_data="menu|ai_brief"),
                InlineKeyboardButton("🤖 AI подробно",     callback_data="menu|ai_full"),
            ],
            [
                InlineKeyboardButton("⚠️ Нарушения CSV",  callback_data="menu|violations"),
                InlineKeyboardButton("🔔 Уведомления",    callback_data="menu|notifs"),
            ],
            [
                InlineKeyboardButton("❓ Помощь",          callback_data="menu|help"),
            ],
        ])
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Мои показатели", callback_data="menu|mystats")],
        [InlineKeyboardButton("❓ Помощь",          callback_data="menu|help")],
    ])





def _back_to_menu(admin: bool) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("◀ Меню", callback_data=f"menu|back")
    ]])


# ─── ATTENDANCE WIZARD ───────────────────────────────────────────────────────

_ATT: dict[str, dict] = {}  # session_id → {date, absents: {member_id: status}}

_STATUS_LABELS = {"sick": "🤒 Болезнь", "vacation": "🏖 Отпуск", "absent": "❓ Другое"}


def _att_text(state: dict) -> str:
    members = db.get_members()
    date_display = datetime.fromisoformat(state["date"]).strftime("%d.%m.%Y")
    n = len(members)
    n_abs = len(state["absents"])
    return (
        f"✅ Посещаемость за {date_display}\n\n"
        f"Нажмите на участника чтобы отметить отсутствие.\n"
        f"Присутствуют: {n - n_abs} / {n}"
    )


def _att_keyboard(sid: str, state: dict) -> InlineKeyboardMarkup:
    members = db.get_members()
    rows = []
    pair = []
    for m in members:
        mid = m["id"]
        status = state["absents"].get(mid)
        if status:
            label = f"❌ {m['name']}"
        else:
            label = f"✅ {m['name']}"
        pair.append(InlineKeyboardButton(label, callback_data=f"att|tog|{sid}|{mid}"))
        if len(pair) == 2:
            rows.append(pair)
            pair = []
    if pair:
        rows.append(pair)
    need_warn = any(s == "absent" for s in state["absents"].values())
    if need_warn:
        rows.append([InlineKeyboardButton("⚠️ Предупредить отсутствующих", callback_data=f"att|wrn|{sid}")])
    rows.append([
        InlineKeyboardButton("💾 Сохранить", callback_data=f"att|sav|{sid}"),
        InlineKeyboardButton("✖ Отмена",    callback_data=f"att|can|{sid}"),
    ])
    return InlineKeyboardMarkup(rows)


async def _start_att_wizard(query, context):
    import uuid
    sid = uuid.uuid4().hex[:8]
    state = {"date": datetime.now().date().isoformat(), "absents": {}}
    _ATT[sid] = state
    await query.edit_message_text(_att_text(state), reply_markup=_att_keyboard(sid, state))


async def cmd_attendance_wizard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start interactive attendance marking wizard."""
    if not is_admin(update.effective_user.id):
        return
    import uuid
    sid = uuid.uuid4().hex[:8]
    state = {"date": datetime.now().date().isoformat(), "absents": {}}
    _ATT[sid] = state
    await update.message.reply_text(_att_text(state), reply_markup=_att_keyboard(sid, state))


async def handle_att_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle all attendance wizard callbacks (att|...)."""
    query = update.callback_query
    if not is_admin(query.from_user.id):
        await query.answer("Только для администраторов", show_alert=True)
        return
    await query.answer()

    parts = query.data.split("|")
    action, sid = parts[1], parts[2]
    state = _ATT.get(sid)
    if not state:
        await query.edit_message_text("Сессия устарела. Запустите /attendance заново.")
        return

    if action == "tog":
        mid = int(parts[3])
        if mid in state["absents"]:
            del state["absents"][mid]
            await query.edit_message_text(_att_text(state), reply_markup=_att_keyboard(sid, state))
        else:
            members = db.get_members()
            name = next((m["name"] for m in members if m["id"] == mid), str(mid))
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("🤒 Болезнь",    callback_data=f"att|rea|{sid}|{mid}|sick")],
                [InlineKeyboardButton("🏖 Отпуск",     callback_data=f"att|rea|{sid}|{mid}|vacation")],
                [InlineKeyboardButton("❓ Другое",     callback_data=f"att|rea|{sid}|{mid}|absent")],
                [InlineKeyboardButton("◀ Назад",       callback_data=f"att|bak|{sid}")],
            ])
            await query.edit_message_text(
                f"❌ {name} отсутствует\nУкажите причину:",
                reply_markup=kb
            )

    elif action == "rea":
        mid, status = int(parts[3]), parts[4]
        state["absents"][mid] = status
        await query.edit_message_text(_att_text(state), reply_markup=_att_keyboard(sid, state))

    elif action == "bak":
        await query.edit_message_text(_att_text(state), reply_markup=_att_keyboard(sid, state))

    elif action == "sav":
        members = db.get_members()
        date_str = state["date"]
        for m in members:
            mid = m["id"]
            status = state["absents"].get(mid, "present")
            db.log_attendance(mid, date_str, status)
        n_abs = len(state["absents"])
        n_total = len(members)
        absent_lines = []
        for mid, status in state["absents"].items():
            name = next((m["name"] for m in members if m["id"] == mid), str(mid))
            absent_lines.append(f"  ❌ {name} — {_STATUS_LABELS.get(status, status)}")
        del _ATT[sid]
        date_display = datetime.fromisoformat(date_str).strftime("%d.%m.%Y")
        text = (
            f"✅ Посещаемость за {date_display} сохранена\n\n"
            f"Присутствуют: {n_total - n_abs} / {n_total}"
        )
        if absent_lines:
            text += "\n\nОтсутствуют:\n" + "\n".join(absent_lines)
        await query.edit_message_text(text)

    elif action == "wrn":
        members = db.get_members()
        warn_members = [
            m for m in members
            if state["absents"].get(m["id"]) == "absent"
        ]
        if not warn_members:
            await query.answer("Нет отсутствующих без уважительной причины", show_alert=True)
            return
        mentions = " ".join(f"[{m['name']}](tg://user?id={m['id']})" for m in warn_members)
        warn_text = (
            f"⚠️ *Предупреждение*\n\n"
            f"{mentions}\n\n"
            f"Вы не отмечены как присутствующие в рабочее время. "
            f"Пожалуйста, сообщите о своём статусе."
        )
        await context.bot.send_message(
            chat_id=GROUP_ID,
            message_thread_id=TOPIC_WORK_TIME,
            text=warn_text,
            parse_mode="Markdown"
        )
        names = ", ".join(m["name"] for m in warn_members)
        await query.answer(f"Предупреждение отправлено: {names}", show_alert=True)

    elif action == "can":
        del _ATT[sid]
        await query.edit_message_text("Отменено.")


# ─── CALLBACK HANDLERS ──────────────────────────────────────────────────────

async def handle_attendance_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query

    if not is_admin(query.from_user.id):
        await query.answer("Только для администраторов", show_alert=True)
        return

    await query.answer()

    data = query.data
    if data.startswith("toggle_"):
        member_id = int(data.split("_")[1])
        absent = db.toggle_absent(member_id)
        keyboard = get_members_keyboard(absent_ids=absent)
        await query.edit_message_reply_markup(reply_markup=keyboard)

    elif data == "send_warnings":
        absent_ids = db.get_absent_ids()
        if not absent_ids:
            await query.edit_message_text("✅ Все присутствуют, предупреждения не нужны.")
            return
        members = db.get_members()
        statuses = db.get_member_statuses()
        # Exclude members on vacation or sick leave
        absent_members = [
            m for m in members
            if m["id"] in absent_ids
            and statuses.get(m["id"], {}).get("status", "active") == "active"
        ]
        if not absent_members:
            await query.edit_message_text(
                "✅ Отсутствующие — в отпуске или на больничном. Предупреждения не нужны."
            )
            return
        mentions = " ".join(
            f"[{m['name']}](tg://user?id={m['id']})" for m in absent_members
        )
        text = (
            f"⚠️ *Предупреждение*\n\n"
            f"{mentions}\n\n"
            f"Вы не отмечены как присутствующие в рабочее время. "
            f"Пожалуйста, сообщите о своём статусе."
        )
        await context.bot.send_message(
            chat_id=GROUP_ID,
            message_thread_id=TOPIC_WORK_TIME,
            text=text,
            parse_mode="Markdown"
        )
        await query.edit_message_text(
            f"✅ Предупреждение отправлено для: {', '.join(m['name'] for m in absent_members)}"
        )

    elif data == "cancel_attendance":
        await query.edit_message_text("❌ Отмена отметки посещаемости.")


# ─── COMMAND HANDLERS ────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text(
            "👋 Бот управления командой активен.\n\n"
            "• /mystatus — ваша статистика\n"
            "• /help — справка"
        )
        return
    await update.message.reply_text(
        "👋 *Team Manager Bot*\n\n"
        "Доступные команды:\n"
        "• /attendance — отметить посещаемость прямо сейчас\n"
        "• /members — список команды\n"
        "• /addmember — добавить участника\n"
        "• /removemember — удалить участника\n"
        "• /syncmembers — синхронизировать участников из группы\n"
        "• /exportmembers — выгрузить список участников в CSV\n"
        "• /setstatus — установить статус (отпуск/больничный)\n"
        "• /today — сводка за сегодня\n"
        "• /attendance\\_history — история посещаемости участника\n"
        "• /notifications — управление уведомлениями\n"
        "• /analytics — аналитика за период\n"
        "• /report\\_now — запустить анализ отчётов сейчас",
        parse_mode="Markdown"
    )


async def cmd_attendance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    db.reset_daily_absent()
    keyboard = get_members_keyboard(absent_ids=[])
    await update.message.reply_text(
        "👥 *Отметьте отсутствующих сегодня*\n\n"
        "Нажмите на участника чтобы снять/поставить галочку.\n"
        "Затем нажмите «Отправить предупреждения».",
        reply_markup=keyboard,
        parse_mode="Markdown"
    )


async def cmd_members(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    members = db.get_members()
    if not members:
        await update.message.reply_text("Команда пуста. Добавьте участников через /addmember")
        return
    text = "👥 *Команда:*\n\n" + "\n".join(
        f"{i+1}. {escape_markdown(m['name'], version=1)} (ID: `{m['id']}`)"
        for i, m in enumerate(members)
    )
    await send_long_message(update.message.reply_text, text, parse_mode="Markdown")


async def cmd_addmember(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    args = context.args
    if len(args) < 2:
        await update.message.reply_text(
            "Использование: `/addmember <telegram_id> <Имя>`\n\n"
            "Пример: `/addmember 123456789 Анна`\n\n"
            "ID можно узнать через @userinfobot",
            parse_mode="Markdown"
        )
        return
    try:
        user_id = int(args[0])
        name = " ".join(args[1:])
        db.add_member(user_id, name)
        await update.message.reply_text(f"✅ Добавлен: {name} (ID: {user_id})")
    except ValueError:
        await update.message.reply_text("❌ ID должен быть числом")


async def cmd_removemember(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    members = db.get_members()
    if not members:
        await update.message.reply_text("Команда пуста.")
        return
    keyboard = [[InlineKeyboardButton(
        f"🗑 {m['name']}", callback_data=f"remove_{m['id']}"
    )] for m in members]
    keyboard.append([InlineKeyboardButton("❌ Отмена", callback_data="remove_cancel")])
    await update.message.reply_text(
        "Выберите кого удалить:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def handle_remove_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        return
    if query.data == "remove_cancel":
        await query.edit_message_text("❌ Удаление отменено.")
        return
    member_id = int(query.data.split("_")[1])
    name = db.remove_member(member_id)
    await query.edit_message_text(f"✅ Удалён: {name}")


async def cmd_notifications(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    await update.message.reply_text(
        "🔔 *Управление уведомлениями*\n\nНажмите чтобы вкл/выкл. 🗑 — удалить кастомное:",
        reply_markup=build_notif_keyboard(),
        parse_mode="Markdown"
    )


async def handle_notif_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        return

    data = query.data
    if data.startswith("notif_toggle_"):
        notif_id = int(data.split("_")[2])
        db.toggle_notification(notif_id)
        await query.edit_message_reply_markup(reply_markup=build_notif_keyboard())

    elif data.startswith("notif_delete_"):
        notif_id = int(data.split("_")[2])
        db.delete_notification(notif_id)
        job_id = f"custom_notif_{notif_id}"
        if scheduler.get_job(job_id):
            scheduler.remove_job(job_id)
        await query.edit_message_reply_markup(reply_markup=build_notif_keyboard())

    elif data == "notif_add":
        await query.edit_message_text(
            "Для добавления нового уведомления используйте команду:\n\n"
            "`/addnotif <время HH:MM> <топик: general/reports/work> <текст>`\n\n"
            "Пример:\n"
            "`/addnotif 09:00 general Доброе утро команда!`",
            parse_mode="Markdown"
        )


async def cmd_addnotif(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    args = context.args
    if len(args) < 3:
        await update.message.reply_text(
            "Использование:\n`/addnotif <HH:MM> <топик> <текст>`\n\n"
            "Топики: `general`, `reports`, `work`\n\n"
            "Пример:\n`/addnotif 09:00 general Доброе утро!`",
            parse_mode="Markdown"
        )
        return
    time_str = args[0]
    topic_key = args[1].lower()
    text = " ".join(args[2:])
    topic_map = {
        "general": TOPIC_GENERAL,
        "reports": TOPIC_REPORTS,
        "work": TOPIC_WORK_TIME,
    }
    if topic_key not in topic_map:
        await update.message.reply_text("❌ Топик должен быть: general, reports или work")
        return
    try:
        h, m = map(int, time_str.split(":"))
    except ValueError:
        await update.message.reply_text("❌ Время в формате HH:MM, например 09:30")
        return

    notif_id = db.add_notification(time_str, topic_map[topic_key], topic_key, text)
    # Schedule it immediately
    schedule_custom_notification(app_ref, notif_id, h, m, topic_map[topic_key], text)
    await update.message.reply_text(
        f"✅ Уведомление добавлено!\n⏰ {time_str} → #{topic_key}\n📝 {text}"
    )


async def cmd_analytics(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return

    from datetime import date as ddate, timedelta as tdelta

    def parse_ddmm(s: str) -> str:
        day, month = map(int, s.split("."))
        year = datetime.now().year
        return f"{year}-{month:02d}-{day:02d}"

    args = context.args
    try:
        if not args:
            start_date = None
            end_date = None
            period_label = "всё время"
        elif len(args) == 1:
            days = int(args[0])
            start_date = (datetime.now() - timedelta(days=days)).date().isoformat()
            end_date = ddate.today().isoformat()
            period_label = f"последние {days} дней"
        elif len(args) == 2:
            start_date = parse_ddmm(args[0])
            end_date = parse_ddmm(args[1])
            period_label = f"{args[0]} – {args[1]}"
        else:
            raise ValueError
    except (ValueError, IndexError):
        await update.message.reply_text(
            "Использование:\n"
            "`/analytics` — 30 дней\n"
            "`/analytics 7` — последние 7 дней\n"
            "`/analytics 01.06 30.06` — конкретный период",
            parse_mode="Markdown"
        )
        return

    stats = db.get_analytics(start_date, end_date)
    members = db.get_members()
    if not members:
        await update.message.reply_text("Команда пуста. Добавьте участников через /addmember")
        return
    lines = [f"📈 *Аналитика за {escape_markdown(period_label, version=1)}*\n"]
    for m in members:
        s = stats.get(m["id"], {"absences": 0, "reports": 0, "warnings": 0})
        name = escape_markdown(m["name"], version=1)
        streak = db.get_streak(m["id"])
        streak_txt = f"   🔥 Серия: {_streak_label(streak)}\n" if streak > 0 else ""
        lines.append(
            f"👤 *{name}*\n"
            f"   📝 Отчётов: {s['reports']}\n"
            f"   ❌ Отсутствий: {s['absences']}\n"
            + streak_txt
        )
    await send_long_message(update.message.reply_text, "\n".join(lines), parse_mode="Markdown")


async def cmd_export_members(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    members = db.get_members()
    if not members:
        await update.message.reply_text("Команда пуста. Добавьте участников через /addmember")
        return
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Telegram ID", "Имя", "Добавлен"])
    for m in members:
        writer.writerow([m["id"], m["name"], m.get("created_at", "")])
    csv_bytes = output.getvalue().encode("utf-8-sig")  # BOM для корректного открытия в Excel
    await update.message.reply_document(
        document=io.BytesIO(csv_bytes),
        filename="team_members.csv",
        caption=f"📋 Список участников команды ({len(members)} чел.)"
    )


async def cmd_export_analytics(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    stats = db.get_analytics()
    members = db.get_members()
    if not members:
        await update.message.reply_text("Команда пуста. Добавьте участников через /addmember")
        return
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Имя", "Telegram ID", "Отсутствий (30 дн.)", "Отчётов (30 дн.)", "Предупреждений (30 дн.)"])
    for m in members:
        s = stats.get(m["id"], {"absences": 0, "reports": 0, "warnings": 0})
        writer.writerow([m["name"], m["id"], s["absences"], s["reports"], s["warnings"]])
    csv_bytes = output.getvalue().encode("utf-8-sig")
    await update.message.reply_document(
        document=io.BytesIO(csv_bytes),
        filename=f"analytics_{datetime.now().strftime('%Y%m%d')}.csv",
        caption="📊 Аналитика за последние 30 дней"
    )


async def cmd_report_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    messages = db.get_today_report_messages()
    members = db.get_members()
    if not members:
        await update.message.reply_text("Команда пуста. Добавьте участников через /addmember")
        return
    reported_ids = {msg["user_id"] for msg in messages}
    lines = [f"📝 *Статус отчётов на {datetime.now().strftime('%d.%m %H:%M')}*\n"]
    for m in members:
        icon = "✅" if m["id"] in reported_ids else "❌"
        lines.append(f"{icon} {escape_markdown(m['name'], version=1)}")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def cmd_sync_members(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Fetch group admins and offer to add them as members."""
    if not is_admin(update.effective_user.id):
        return
    try:
        admins = await context.bot.get_chat_administrators(chat_id=GROUP_ID)
    except Exception as e:
        await update.message.reply_text(f"❌ Не удалось получить участников группы: {e}")
        return

    existing_members = db.get_members()
    existing_ids = {m["id"] for m in existing_members}

    added = []
    skipped = []
    for admin in admins:
        user = admin.user
        if user.is_bot:
            continue
        name = user.full_name
        if user.id in existing_ids:
            skipped.append(name)
        else:
            db.add_member(user.id, name)
            added.append(name)

    lines = ["👥 *Синхронизация администраторов группы*\n"]
    if added:
        lines.append("✅ *Добавлены:*\n" + "\n".join(f"  • {escape_markdown(n, version=1)}" for n in added))
    if skipped:
        lines.append("⏭ *Уже в списке:*\n" + "\n".join(f"  • {escape_markdown(n, version=1)}" for n in skipped))
    if not added and not skipped:
        lines.append("Администраторы-люди не найдены.")

    await update.message.reply_text("\n\n".join(lines), parse_mode="Markdown")


async def cmd_set_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    args = context.args
    if len(args) < 2:
        await update.message.reply_text(
            "Использование:\n"
            "`/setstatus <id_или_имя> <статус> [DD.MM]`\n\n"
            "Статусы: `active`, `vacation`, `sick`\n\n"
            "Примеры:\n"
            "`/setstatus 123456 vacation 30.06`\n"
            "`/setstatus Анна sick`\n"
            "`/setstatus 123456 active`",
            parse_mode="Markdown"
        )
        return

    id_or_name = args[0]
    member = db.find_member(id_or_name)
    if not member:
        await update.message.reply_text(f"❌ Участник не найден: {id_or_name}")
        return

    new_status = args[1].lower()
    if new_status not in ("active", "vacation", "sick"):
        await update.message.reply_text("❌ Статус: `active`, `vacation` или `sick`", parse_mode="Markdown")
        return

    until_iso = None
    until_display = ""
    if len(args) >= 3:
        date_str = args[2].replace("до", "").strip()
        try:
            day, month = map(int, date_str.split("."))
            year = datetime.now().year
            from datetime import date as ddate
            until_date = ddate(year, month, day)
            if until_date < ddate.today():
                until_date = ddate(year + 1, month, day)
            until_iso = until_date.isoformat()
            until_display = f" до {until_date.strftime('%d.%m.%Y')}"
        except (ValueError, AttributeError):
            await update.message.reply_text("❌ Дата в формате DD.MM, например: `30.06`", parse_mode="Markdown")
            return

    db.set_member_status(member["id"], new_status, until_iso)
    name = escape_markdown(member["name"], version=1)
    icon = STATUS_ICONS.get(new_status, "")
    label = STATUS_LABELS.get(new_status, new_status)
    await update.message.reply_text(
        f"{icon} *{name}* → *{label}*{until_display}",
        parse_mode="Markdown"
    )


async def cmd_today(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    summary = db.get_today_summary()
    if not summary:
        await update.message.reply_text("Команда пуста. Добавьте участников через /addmember")
        return

    today_str = datetime.now().strftime("%d.%m.%Y")
    attendance_lines = []
    report_lines = []

    for m in summary:
        name = escape_markdown(m["name"], version=1)
        st = m["status"]
        until = f" до {fmt_until(m['status_until'])}" if m["status_until"] else ""

        if st == "vacation":
            attendance_lines.append(f"🏖 {name} — в отпуске{until}")
        elif st == "sick":
            attendance_lines.append(f"🤒 {name} — на больничном{until}")
        elif m["attendance"] == "present":
            attendance_lines.append(f"✅ {name} — присутствует")
        elif m["attendance"] == "absent":
            attendance_lines.append(f"❌ {name} — отсутствует")
        else:
            attendance_lines.append(f"❓ {name} — не отмечен")

        if st == "vacation":
            report_lines.append(f"⏭ {name} — пропускает (отпуск)")
        elif st == "sick":
            report_lines.append(f"⏭ {name} — пропускает (больничный)")
        elif m["reported"]:
            report_lines.append(f"✅ {name} — сдан")
        else:
            report_lines.append(f"❌ {name} — ещё нет")

    att = "\n".join(f"   {l}" for l in attendance_lines)
    rep = "\n".join(f"   {l}" for l in report_lines)
    text = (
        f"📅 *Сводка за {today_str}*\n\n"
        f"👥 *Присутствие:*\n{att}\n\n"
        f"📝 *Отчёты:*\n{rep}"
    )
    await update.message.reply_text(text, parse_mode="Markdown")


async def cmd_attendance_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    if not context.args:
        await update.message.reply_text(
            "Использование: `/attendance_history <имя_или_id>`",
            parse_mode="Markdown"
        )
        return

    id_or_name = " ".join(context.args)
    member = db.find_member(id_or_name)
    if not member:
        await update.message.reply_text(f"❌ Участник не найден: {id_or_name}")
        return

    history = db.get_attendance_history(member["id"], days=14)
    name = escape_markdown(member["name"], version=1)

    if not history:
        await update.message.reply_text(
            f"📅 *{name}*: нет данных за последние 14 дней.",
            parse_mode="Markdown"
        )
        return

    history_map = {r["date"]: r["status"] for r in history}
    icons = {"present": "✅", "absent": "❌"}

    from datetime import date as ddate, timedelta as tdelta
    today = ddate.today()
    cells = []
    for i in range(13, -1, -1):
        d = today - tdelta(days=i)
        icon = icons.get(history_map.get(d.isoformat(), ""), "—")
        cells.append(f"{d.strftime('%d.%m')} {icon}")

    rows = [cells[i:i+7] for i in range(0, 14, 7)]
    grid = "\n".join("  ".join(r) for r in rows)

    await update.message.reply_text(
        f"📅 *История посещаемости: {name}*\n_(последние 14 дней)_\n\n{grid}",
        parse_mode="Markdown"
    )


async def cmd_report_now(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    await update.message.reply_text("🤖 Анализирую отчёты...")
    await job_analyze_reports(context.application)


# ─── MESSAGE TRACKING ────────────────────────────────────────────────────────

async def track_report_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Track messages in the reports topic."""
    msg = update.message
    if not msg or not msg.text:
        return
    logger.debug(
        "MSG chat=%s thread=%s from=%s text=%.40s",
        msg.chat_id, msg.message_thread_id, msg.from_user.id, msg.text
    )
    if msg.message_thread_id == TOPIC_REPORTS:
        db.save_report_message(
            user_id=msg.from_user.id,
            user_name=msg.from_user.full_name,
            text=msg.text,
            date=datetime.now().date().isoformat()
        )
        logger.info("Report saved from user %s (%s)", msg.from_user.id, msg.from_user.full_name)


async def cmd_ai_analysis(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """AI analysis of reports for a period.

    Usage:
      /aianalysis              — all time, brief
      /aianalysis full         — all time, detailed
      /aianalysis 7            — last 7 days, brief
      /aianalysis 7 full       — last 7 days, detailed
      /aianalysis 01.06 25.06       — specific period, brief
      /aianalysis 01.06 25.06 full  — specific period, detailed
    """
    if not is_admin(update.effective_user.id):
        return

    from datetime import date as ddate

    def parse_ddmm(s: str) -> str:
        day, month = map(int, s.split("."))
        return f"{datetime.now().year}-{month:02d}-{day:02d}"

    args = list(context.args)

    # Detect "full" flag
    detailed = False
    if args and args[-1].lower() == "full":
        detailed = True
        args = args[:-1]

    # Parse period
    start_date = None
    end_date = None
    period_label = "всё время"
    try:
        if not args:
            pass  # all time
        elif len(args) == 1:
            days = int(args[0])
            start_date = (datetime.now() - timedelta(days=days)).date().isoformat()
            end_date = ddate.today().isoformat()
            period_label = f"последние {days} дней"
        elif len(args) == 2:
            start_date = parse_ddmm(args[0])
            end_date = parse_ddmm(args[1])
            period_label = f"{args[0]} – {args[1]}"
        else:
            raise ValueError
    except (ValueError, IndexError):
        await update.message.reply_text(
            "Использование:\n"
            "`/aianalysis` — всё время\n"
            "`/aianalysis 7` — последние 7 дней\n"
            "`/aianalysis 01.06 25.06` — период\n"
            "Добавьте `full` для подробного анализа:\n"
            "`/aianalysis 7 full`",
            parse_mode="Markdown"
        )
        return

    messages = db.get_reports_for_period(start_date, end_date)
    if not messages:
        await update.message.reply_text(
            f"📭 За период *{escape_markdown(period_label, version=1)}* отчётов не найдено.",
            parse_mode="Markdown"
        )
        return

    members = db.get_members()
    member_names = [m["name"] for m in members]

    # Group reports by member for summary stats
    from collections import defaultdict
    by_member: dict[str, list[str]] = defaultdict(list)
    by_date: dict[str, list[str]] = defaultdict(list)
    for msg in messages:
        by_member[msg["user_name"]].append(f"[{msg['date']}] {msg['text']}")
        by_date[msg["date"]].append(f"{msg['user_name']}: {msg['text']}")

    # Build prompt
    total_reports = len(messages)
    active_members = len(by_member)
    date_range = f"{min(by_date.keys())} — {max(by_date.keys())}"

    if detailed:
        # Full prompt: per-member breakdown
        reports_text = ""
        for name, reports in sorted(by_member.items()):
            reports_text += f"\n\n=== {name} ({len(reports)} отчётов) ===\n"
            reports_text += "\n---\n".join(reports)

        prompt = AI_PROMPT_PERIOD_FULL.format(
            member_names=", ".join(member_names),
            date_range=date_range,
            total_reports=total_reports,
            active_members=active_members,
            reports_text=reports_text,
        )
        mode_label = "подробный"
        max_tokens = 2000
    else:
        # Brief prompt: all reports flat
        reports_text = "\n\n".join(
            f"[{msg['date']}] {msg['user_name']}: {msg['text']}"
            for msg in messages
        )
        if len(reports_text) > 8000:
            reports_text = "...(часть отчётов пропущена)\n\n" + reports_text[-8000:]

        prompt = AI_PROMPT_PERIOD_BRIEF.format(
            member_names=", ".join(member_names),
            date_range=date_range,
            total_reports=total_reports,
            active_members=active_members,
            reports_text=reports_text,
        )
        mode_label = "краткий"
        max_tokens = 1000

    mode_str = "подробный" if detailed else "краткий"
    await update.message.reply_text(
        f"🤖 Запрашиваю {mode_str} AI-анализ за *{escape_markdown(period_label, version=1)}*...\n"
        f"_(отчётов: {total_reports}, участников: {active_members})_",
        parse_mode="Markdown"
    )

    try:
        summary = call_ai(prompt, max_tokens=max_tokens)
    except Exception as e:
        logger.error("AI error in aianalysis: %s", e)
        await update.message.reply_text(f"❌ Все AI-провайдеры недоступны: {e}")
        return

    header = (
        f"📊 *AI-анализ за {period_label}* ({mode_label})\n"
        f"_Отчётов: {total_reports} · Участников: {active_members} · {date_range}_"
    )
    await update.message.reply_text(header, parse_mode="Markdown")
    await send_long_message(update.message.reply_text, summary)


async def cmd_debuginfo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show diagnostic info to help check why reports are not tracked."""
    if not is_admin(update.effective_user.id):
        return
    import sqlite3 as _sq
    with _sq.connect(db.path) as c:
        c.row_factory = _sq.Row
        rep_cnt = c.execute("SELECT COUNT(*) as n FROM report_messages").fetchone()["n"]
        rep_last = c.execute(
            "SELECT date, user_name FROM report_messages ORDER BY created_at DESC LIMIT 3"
        ).fetchall()
        att_cnt = c.execute("SELECT COUNT(*) as n FROM attendance_log").fetchone()["n"]

    lines = [
        "🔧 *Диагностика бота*\n",
        f"• `TOPIC_REPORTS` = `{TOPIC_REPORTS}`",
        f"• `GROUP_ID` = `{GROUP_ID}`",
        f"• Отчётов в БД: {rep_cnt}",
        f"• Записей посещаемости: {att_cnt}",
    ]
    if rep_last:
        lines.append("\nПоследние отчёты:")
        for r in rep_last:
            lines.append(f"  {r['date']} — {escape_markdown(r['user_name'], version=1)}")
    else:
        lines.append("\n⚠️ Отчётов нет\\. Возможные причины:")
        lines.append("1\\. Бот не получает сообщения группы — выключите Privacy Mode в @BotFather")
        lines.append(f"2\\. ID темы Отчёты неверный \\(сейчас: {TOPIC_REPORTS}\\)")
        lines.append("3\\. Отчёты писали до запуска бота")

    await update.message.reply_text("\n".join(lines), parse_mode="MarkdownV2")


async def cmd_addreport(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Manually add a report record for a member: /addreport <id_or_name> [DD.MM]"""
    if not is_admin(update.effective_user.id):
        return
    args = context.args
    if not args:
        await update.message.reply_text(
            "Использование:\n"
            "`/addreport <имя\\_или\\_id>` — за сегодня\n"
            "`/addreport <имя\\_или\\_id> DD.MM` — за дату",
            parse_mode="MarkdownV2"
        )
        return

    member = db.find_member(args[0])
    if not member:
        await update.message.reply_text(f"❌ Участник не найден: {args[0]}")
        return

    if len(args) >= 2:
        try:
            day, month = map(int, args[1].split("."))
            report_date = f"{datetime.now().year}-{month:02d}-{day:02d}"
        except ValueError:
            await update.message.reply_text("❌ Дата в формате DD.MM, например: 25.06")
            return
    else:
        report_date = datetime.now().date().isoformat()

    db.save_report_message(
        user_id=member["id"],
        user_name=member["name"],
        text="[вручную добавлено администратором]",
        date=report_date,
    )
    name = escape_markdown(member["name"], version=1)
    await update.message.reply_text(
        f"✅ Отчёт добавлен: *{name}* за `{report_date}`",
        parse_mode="Markdown"
    )


async def cmd_test_notif(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    args = context.args
    if not args:
        await update.message.reply_text("Использование: /testnotif <id>")
        return
    try:
        notif_id = int(args[0])
    except ValueError:
        await update.message.reply_text("❌ ID должен быть числом")
        return
    notif = db.get_notification(notif_id)
    if not notif:
        await update.message.reply_text("❌ Уведомление не найдено")
        return
    await context.bot.send_message(
        chat_id=GROUP_ID,
        message_thread_id=notif["topic_id"],
        text=f"[ТЕСТ] {notif['text']}"
    )
    await update.message.reply_text("✅ Тестовое уведомление отправлено")


# ─── SCHEDULED JOBS (continued) ─────────────────────────────────────────────

async def job_report_reminder(app: Application):
    """21:00 — remind those who haven't submitted a report yet."""
    today = datetime.now().date().isoformat()
    reports = db.get_reports_for_period(today, today)
    reported_ids = {r["user_id"] for r in reports}
    members = db.get_members()
    missing = [m for m in members if m["id"] not in reported_ids]

    if not missing:
        text = "✅ Отлично! Все написали отчёт сегодня 🎉"
    else:
        names = ", ".join(m["name"] for m in missing)
        text = (
            f"⏰ *Напоминание об отчёте*\n\n"
            f"Ещё не написали: *{escape_markdown(names, version=1)}*\n\n"
            f"📝 Напишите отчёт в эту тему, используя структуру:\n\n"
            f"{REPORT_STRUCTURE}"
        )
    await app.bot.send_message(
        chat_id=GROUP_ID,
        message_thread_id=TOPIC_REPORTS,
        text=text,
        parse_mode="Markdown"
    )


async def job_morning_analysis(app: Application):
    """07:00 — analyze yesterday's reports and post AI summary."""
    from datetime import date as ddate, timedelta as tdelta
    yesterday = (ddate.today() - tdelta(days=1)).isoformat()
    yesterday_display = datetime.fromisoformat(yesterday).strftime("%d.%m.%Y")

    reports = db.get_reports_for_period(yesterday, yesterday)
    members = db.get_members()
    reported_ids = {r["user_id"] for r in reports}

    wrote = [m["name"] for m in members if m["id"] in reported_ids]
    missing = [m for m in members if m["id"] not in reported_ids]

    wrote_str = ", ".join(wrote) if wrote else "—"
    miss_str = ", ".join(m["name"] for m in missing) if missing else "все написали 🎉"

    stats_lines = (
        f"📊 *Итог отчётов за {escape_markdown(yesterday_display, version=1)}*\n\n"
        f"✅ Написали: {escape_markdown(wrote_str, version=1)}\n"
        f"❌ Не написали: {escape_markdown(miss_str, version=1)}"
    )
    await app.bot.send_message(
        chat_id=GROUP_ID,
        message_thread_id=TOPIC_REPORTS,
        text=stats_lines,
        parse_mode="Markdown"
    )

    # AI summary of yesterday's reports
    if reports:
        member_names = [m["name"] for m in members]
        reports_text = "\n\n".join(
            f"[{r['user_name']}]: {r['text']}" for r in reports
        )
        prompt = AI_PROMPT_DAILY.format(
            date=yesterday_display,
            reports_text=reports_text,
        )
        try:
            summary = call_ai(prompt, max_tokens=800)
            await app.bot.send_message(
                chat_id=GROUP_ID,
                message_thread_id=TOPIC_REPORTS,
                text=f"🤖 *AI-дайджест за {escape_markdown(yesterday_display, version=1)}*",
                parse_mode="Markdown"
            )
            for chunk in [summary[i:i+4000] for i in range(0, len(summary), 4000)]:
                await app.bot.send_message(
                    chat_id=GROUP_ID,
                    message_thread_id=TOPIC_REPORTS,
                    text=chunk
                )
        except Exception as e:
            logger.error("AI error in morning analysis: %s", e)


def generate_violations_csv() -> tuple:
    """Build two CSV files merged into one download. Returns (BytesIO, total_count)."""
    import io, csv

    members = db.get_members()
    all_dates = db.get_all_report_dates()

    # Count violations per member for summary
    violation_count: dict[int, int] = {m["id"]: 0 for m in members}

    output = io.StringIO()
    writer = csv.writer(output)

    # Section 1: violations list
    writer.writerow(["=== НАРУШЕНИЯ (список) ==="])
    writer.writerow(["Дата", "Участник", "Telegram ID", "Статус"])
    total = 0
    for date_str in all_dates:
        reported_ids = db.get_reporter_ids_for_date(date_str)
        date_display = datetime.fromisoformat(date_str).strftime("%d.%m.%Y")
        for m in members:
            if m["id"] not in reported_ids:
                writer.writerow([date_display, m["name"], m["id"], "Отчёт не сдан"])
                violation_count[m["id"]] += 1
                total += 1

    # Blank row separator
    writer.writerow([])
    writer.writerow(["=== ИТОГ ПО УЧАСТНИКАМ ==="])
    writer.writerow(["Участник", "Пропущено отчётов", "Дней в базе"])
    total_days = len(all_dates)
    for m in sorted(members, key=lambda x: violation_count[x["id"]], reverse=True):
        writer.writerow([m["name"], violation_count[m["id"]], total_days])

    # Encode with BOM so Excel opens Cyrillic correctly
    raw = output.getvalue().encode("utf-8-sig")
    return io.BytesIO(raw), total


async def cmd_violations(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Generate and send CSV file with all missing report violations."""
    if not is_admin(update.effective_user.id):
        return
    await update.message.reply_text("⏳ Генерирую файл нарушений...")
    try:
        file_data, total = generate_violations_csv()
        filename = f"violations_{datetime.now().strftime('%Y%m%d_%H%M')}.csv"
        await update.message.reply_document(
            document=file_data,
            filename=filename,
            caption=(
                f"📋 *Файл нарушений*\n"
                f"Пропущено отчётов: *{total}*\n"
                f"Разделы: список нарушений + итог по участникам"
            ),
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.error("Violations CSV error: %s", e)
        await update.message.reply_text(f"❌ Ошибка генерации файла: {e}")


async def job_weekly_summary(app: Application):
    """Friday 18:00 — send weekly attendance and report summary."""
    from datetime import date as ddate, timedelta as tdelta
    today = ddate.today()
    monday = today - tdelta(days=today.weekday())
    friday = monday + tdelta(days=4)
    start_date = monday.isoformat()
    end_date = friday.isoformat()

    stats = db.get_weekly_analytics(start_date, end_date)
    members = db.get_members()
    if not members:
        return

    week_str = f"{monday.strftime('%d.%m')} – {friday.strftime('%d.%m')}"
    lines = [f"📊 *Итоги недели ({week_str})*\n"]
    for m in members:
        s = stats.get(m["id"], {"absences": 0, "reports": 0})
        name = escape_markdown(m["name"], version=1)
        days_present = max(0, 5 - s["absences"])
        att_icon = "✅" if s["absences"] == 0 else ("⚠️" if s["absences"] <= 1 else "❌")
        rep_icon = "✅" if s["reports"] >= 5 else ("⚠️" if s["reports"] >= 3 else "❌")
        lines.append(
            f"👤 *{name}:*   {days_present}/5 дней {att_icon}  |  {s['reports']}/5 отчётов {rep_icon}"
        )

    await app.bot.send_message(
        chat_id=GROUP_ID,
        message_thread_id=TOPIC_GENERAL,
        text="\n".join(lines),
        parse_mode="Markdown"
    )


# ─── FEATURE 1: STREAKS ─────────────────────────────────────────────────────

def _streak_label(n: int) -> str:
    if n == 0:
        return "нет серии"
    if n < 3:
        return f"{n} д"
    if n < 7:
        return f"🔥 {n} д"
    return f"🔥🔥 {n} д"


# ─── FEATURE 2: PERSONAL 21:00 REMINDER ─────────────────────────────────────

async def job_personal_reminder(app: Application):
    """21:00 — send personal DM to members who haven't written a report today."""
    today = datetime.now().date().isoformat()
    reports = db.get_reports_for_period(today, today)
    reported_ids = {r["user_id"] for r in reports}
    members = db.get_members()
    missing = [m for m in members if m["id"] not in reported_ids]

    if not missing:
        return  # everyone reported — no DMs needed

    for m in missing:
        try:
            await app.bot.send_message(
                chat_id=m["id"],
                text=(
                    f"⏰ Привет, {m['name']}!\n\n"
                    f"Не забудь написать отчёт за сегодня в тему Отчёты.\n\n"
                    f"{REPORT_STRUCTURE}"
                )
            )
        except Exception:
            pass  # user hasn't started the bot — skip silently


# ─── FEATURE 3: ADMIN MORNING DASHBOARD ─────────────────────────────────────

async def job_morning_dashboard(app: Application):
    """09:00 — send private morning dashboard to each admin."""
    from datetime import date as ddate, timedelta as tdelta
    yesterday = (ddate.today() - tdelta(days=1)).isoformat()
    yesterday_disp = datetime.fromisoformat(yesterday).strftime("%d.%m")
    today_disp = datetime.now().strftime("%d.%m.%Y")

    members = db.get_members()
    yesterday_reports = db.get_reports_for_period(yesterday, yesterday)
    reported_yesterday = {r["user_id"] for r in yesterday_reports}

    wrote = [m["name"] for m in members if m["id"] in reported_yesterday]
    missed = [m["name"] for m in members if m["id"] not in reported_yesterday]

    # Streaks
    streak_lines = []
    for m in sorted(members, key=lambda x: db.get_streak(x["id"]), reverse=True):
        s = db.get_streak(m["id"])
        if s > 0:
            streak_lines.append(f"  {_streak_label(s)} — {m['name']}")

    text = (
        f"🌅 *Доброе утро! Дашборд на {today_disp}*\n\n"
        f"📝 *Отчёты за {yesterday_disp}:*\n"
        f"  ✅ Написали ({len(wrote)}): {', '.join(wrote) if wrote else '—'}\n"
        f"  ❌ Пропустили ({len(missed)}): {', '.join(missed) if missed else 'никто 🎉'}\n\n"
        + (f"🔥 *Серии:*\n" + "\n".join(streak_lines) + "\n\n" if streak_lines else "")
        + f"👥 Команда: {len(members)} чел."
    )

    for admin_id in ADMIN_IDS:
        try:
            await app.bot.send_message(chat_id=admin_id, text=text, parse_mode="Markdown")
        except Exception as e:
            logger.warning("Morning dashboard to admin %s failed: %s", admin_id, e)


# ─── FEATURE 4: REPORT TEMPLATE (/report in private) ────────────────────────

_REP_DONE, _REP_TOMORROW, _REP_BLOCKERS = range(3)


async def cmd_report_template(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start the interactive report form in private chat."""
    if update.effective_chat.type != "private":
        await update.message.reply_text("Используй эту команду в личных сообщениях с ботом.")
        return
    await update.message.reply_text(
        "📝 *Отчёт за сегодня*\n\n"
        "Шаг 1/3 — Что ты сделал сегодня?\n"
        "_(опиши конкретные задачи и результаты)_",
        parse_mode="Markdown"
    )
    return _REP_DONE


async def _rep_got_done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["rep_done"] = update.message.text
    await update.message.reply_text(
        "Шаг 2/3 — Над чем будешь работать завтра?"
    )
    return _REP_TOMORROW


async def _rep_got_tomorrow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["rep_tomorrow"] = update.message.text
    await update.message.reply_text(
        "Шаг 3/3 — Есть ли блокеры или вопросы?\n"
        "_(если нет — напиши «нет»)_"
    )
    return _REP_BLOCKERS


async def _rep_got_blockers(update: Update, context: ContextTypes.DEFAULT_TYPE):
    blockers = update.message.text
    done = context.user_data.get("rep_done", "")
    tomorrow = context.user_data.get("rep_tomorrow", "")
    user = update.effective_user
    today = datetime.now().strftime("%d.%m.%Y")

    report_text = (
        f"Отчёт за {today}\n\n"
        f"Что сделал:\n{done}\n\n"
        f"Завтра:\n{tomorrow}\n\n"
        f"Блокеры:\n{blockers}"
    )

    # Send to group reports topic
    try:
        await context.bot.send_message(
            chat_id=GROUP_ID,
            message_thread_id=TOPIC_REPORTS,
            text=f"📝 {user.full_name}:\n\n{report_text}"
        )
        # Save to DB
        members = db.get_members()
        member = next((m for m in members if m["id"] == user.id), None)
        if member:
            db.save_report_message(
                user_id=user.id,
                user_name=member["name"],
                text=report_text,
                date=datetime.now().date().isoformat()
            )
        await update.message.reply_text(
            "✅ Отчёт отправлен в группу!\n\nСпасибо 🙌"
        )
    except Exception as e:
        await update.message.reply_text(f"❌ Не удалось отправить: {e}")

    context.user_data.pop("rep_done", None)
    context.user_data.pop("rep_tomorrow", None)
    return ConversationHandler.END


async def _rep_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("rep_done", None)
    context.user_data.pop("rep_tomorrow", None)
    await update.message.reply_text("Отменено.")
    return ConversationHandler.END


report_conv = ConversationHandler(
    entry_points=[CommandHandler("report", cmd_report_template)],
    states={
        _REP_DONE:     [MessageHandler(filters.TEXT & ~filters.COMMAND, _rep_got_done)],
        _REP_TOMORROW: [MessageHandler(filters.TEXT & ~filters.COMMAND, _rep_got_tomorrow)],
        _REP_BLOCKERS: [MessageHandler(filters.TEXT & ~filters.COMMAND, _rep_got_blockers)],
    },
    fallbacks=[CommandHandler("cancel", _rep_cancel)],
    per_chat=True,
)


# ─── FEATURE 5: WEEKLY RATING ────────────────────────────────────────────────

async def job_weekly_rating(app: Application):
    """Friday 18:30 — post weekly team performance rating."""
    from datetime import date as ddate, timedelta as tdelta
    today = ddate.today()
    monday = today - tdelta(days=today.weekday())
    week_str = f"{monday.strftime('%d.%m')} – {today.strftime('%d.%m')}"

    stats = db.get_week_stats()
    members = db.get_members()

    # Score: each report = 2pts, each present day = 1pt, absence = -1pt
    scored = []
    for m in members:
        s = stats.get(m["id"], {"reports": 0, "absences": 0})
        score = s["reports"] * 2 - s["absences"]
        streak = db.get_streak(m["id"])
        scored.append((m["name"], s["reports"], s["absences"], streak, score))

    scored.sort(key=lambda x: x[4], reverse=True)

    medals = ["🥇", "🥈", "🥉"]
    lines = [f"🏆 *Рейтинг недели ({week_str})*\n"]

    for i, (name, reports, absences, streak, score) in enumerate(scored):
        medal = medals[i] if i < 3 else f"{i+1}."
        streak_txt = f" {_streak_label(streak)}" if streak > 0 else ""
        lines.append(
            f"{medal} {escape_markdown(name, version=1)}"
            f" — {reports} отч{streak_txt}"
        )

    # Highlight best and worst
    if scored:
        best = scored[0][0]
        worst = scored[-1][0]
        lines.append(f"\n⭐ Лучший: *{escape_markdown(best, version=1)}*")
        if best != worst:
            lines.append(f"💪 Подтянись: *{escape_markdown(worst, version=1)}*")

    await app.bot.send_message(
        chat_id=GROUP_ID,
        message_thread_id=TOPIC_GENERAL,
        text="\n".join(lines),
        parse_mode="Markdown"
    )


# ─── SCHEDULER SETUP ────────────────────────────────────────────────────────

app_ref = None  # will be set in main


def schedule_custom_notification(app, notif_id, hour, minute, topic_id=None, text=None):
    async def job():
        notif = db.get_notification(notif_id)
        if notif and notif["enabled"]:
            await app.bot.send_message(
                chat_id=GROUP_ID,
                message_thread_id=notif["topic_id"],
                text=notif["text"]
            )
    job_id = f"custom_notif_{notif_id}"
    if scheduler.get_job(job_id):
        scheduler.remove_job(job_id)
    scheduler.add_job(job, CronTrigger(hour=hour, minute=minute), id=job_id)


def setup_scheduler(app: Application):
    global app_ref
    app_ref = app

    tz = TIMEZONE

    # Built-in jobs
    scheduler.add_job(
        lambda: asyncio.create_task(job_attendance_reminder(app)),
        CronTrigger(hour=10, minute=30, timezone=tz),
        id="attendance"
    )
    scheduler.add_job(
        lambda: asyncio.create_task(job_morning_reminder(app)),
        CronTrigger(hour=10, minute=0, timezone=tz),
        id="morning_reminder"
    )
    scheduler.add_job(
        lambda: asyncio.create_task(job_evening_reminder(app)),
        CronTrigger(hour=20, minute=0, timezone=tz),
        id="evening_reminder"
    )
    scheduler.add_job(
        lambda: asyncio.create_task(job_personal_reminder(app)),
        CronTrigger(hour=21, minute=0, timezone=tz),
        id="personal_reminder"
    )
    scheduler.add_job(
        lambda: asyncio.create_task(job_morning_analysis(app)),
        CronTrigger(hour=7, minute=0, timezone=tz),
        id="morning_analysis"
    )
    scheduler.add_job(
        lambda: asyncio.create_task(job_morning_dashboard(app)),
        CronTrigger(hour=9, minute=0, timezone=tz),
        id="morning_dashboard"
    )
    scheduler.add_job(
        lambda: asyncio.create_task(job_weekly_summary(app)),
        CronTrigger(day_of_week="fri", hour=18, minute=0, timezone=tz),
        id="weekly_summary"
    )
    scheduler.add_job(
        lambda: asyncio.create_task(job_weekly_rating(app)),
        CronTrigger(day_of_week="fri", hour=18, minute=30, timezone=tz),
        id="weekly_rating"
    )

    # Custom notifications from DB
    for n in db.get_notifications():
        if n["enabled"] and n["id"] >= 100:  # custom ones start at 100
            h, m = map(int, n["time"].split(":"))
            schedule_custom_notification(app, n["id"], h, m, n["topic_id"], n["text"])

    scheduler.start()
    logger.info("Scheduler started")


# ─── HELP SECTIONS ──────────────────────────────────────────────────────────

HELP_SECTIONS: dict[str, str] = {
    "help_team": (
        "👥 *Команда:*\n"
        "• /members — список участников\n"
        "• /addmember `<id> <имя>` — добавить\n"
        "• /removemember — удалить \\(с выбором\\)\n"
        "• /syncmembers — синхронизировать из группы\n"
        "• /exportmembers — скачать CSV\n"
        "• /setstatus `<id> vacation/sick/active [DD.MM]` — статус"
    ),
    "help_attendance": (
        "📋 *Посещаемость:*\n"
        "• /attendance — отметить сейчас\n"
        "• /today — сводка за сегодня\n"
        "• /attendance\\_history `<имя\\_или\\_id>` — история 14 дней"
    ),
    "help_notifications": (
        "🔔 *Уведомления:*\n"
        "• /notifications — управление \\(вкл/выкл/удалить\\)\n"
        "• /addnotif `<HH:MM> <топик> <текст>` — добавить\n"
        "• /testnotif `<id>` — тест уведомления"
    ),
    "help_analytics": (
        "📊 *Аналитика:*\n"
        "• /analytics `[дней]` или `[ДД\\.ММ ДД\\.ММ]` — статистика\n"
        "• /exportanalytics — выгрузить CSV\n"
        "• /reportstatus — кто сдал отчёт сегодня\n"
        "• /report\\_now — запустить анализ вручную\n"
        "• /broadcast `<текст>` — рассылка участникам"
    ),
}

_HELP_MAIN_KB = InlineKeyboardMarkup([
    [InlineKeyboardButton("👥 Команда", callback_data="help_team"),
     InlineKeyboardButton("📋 Посещаемость", callback_data="help_attendance")],
    [InlineKeyboardButton("🔔 Уведомления", callback_data="help_notifications"),
     InlineKeyboardButton("📊 Аналитика", callback_data="help_analytics")],
])

_HELP_BACK_KB = InlineKeyboardMarkup([
    [InlineKeyboardButton("« Назад", callback_data="help_back")]
])


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text(
            "🤖 *Team Manager Bot*\n\n"
            "Доступные команды:\n"
            "• /mystatus — ваша статистика за 30 дней",
            parse_mode="Markdown"
        )
        return
    await update.message.reply_text(
        "🤖 *Team Manager Bot — справка*\n\nВыберите раздел:",
        reply_markup=_HELP_MAIN_KB,
        parse_mode="Markdown"
    )


async def handle_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "help_back":
        await query.edit_message_text(
            "🤖 *Team Manager Bot — справка*\n\nВыберите раздел:",
            reply_markup=_HELP_MAIN_KB,
            parse_mode="Markdown"
        )
    else:
        text = HELP_SECTIONS.get(query.data, "Раздел не найден.")
        await query.edit_message_text(text, reply_markup=_HELP_BACK_KB, parse_mode="MarkdownV2")


# ─── NON-ADMIN COMMANDS ──────────────────────────────────────────────────────

async def cmd_my_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    members = db.get_members()
    member = next((m for m in members if m["id"] == user.id), None)
    if not member:
        await update.message.reply_text(
            "❌ Вы не добавлены в команду. Обратитесь к администратору."
        )
        return

    stats = db.get_analytics()
    s = stats.get(user.id, {"absences": 0, "reports": 0, "warnings": 0})

    today_summary = db.get_today_summary()
    today_data = next((m for m in today_summary if m["id"] == user.id), None)

    if today_data:
        att_today = "✅ присутствуете" if today_data["attendance"] == "present" else "❓ не отмечен"
        rep_today = "✅ сдан" if today_data["reported"] else "❌ не сдан"
    else:
        att_today = "❓ не отмечен"
        rep_today = "❌ не сдан"

    name = escape_markdown(member["name"], version=1)
    await update.message.reply_text(
        f"📊 *{name} — статистика за 30 дней:*\n\n"
        f"❌ Отсутствий: {s['absences']}\n"
        f"📝 Отчётов сдано: {s['reports']}\n"
        f"⚠️ Предупреждений: {s['warnings']}\n\n"
        f"📅 *Сегодня:* {att_today} | 📝 отчёт {rep_today}",
        parse_mode="Markdown"
    )


# ─── BROADCAST ───────────────────────────────────────────────────────────────

async def cmd_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    if not context.args:
        await update.message.reply_text("Использование: /broadcast <текст>")
        return
    text = " ".join(context.args)
    members = db.get_members()
    sent, failed = 0, 0
    for m in members:
        try:
            await context.bot.send_message(
                chat_id=m["id"],
                text=f"📢 Сообщение от руководителя:\n\n{text}"
            )
            sent += 1
        except Exception:
            failed += 1
    await update.message.reply_text(
        f"✅ Отправлено: {sent}\n❌ Не доставлено: {failed}\n"
        f"_\\(участники должны написать боту хотя бы раз\\)_",
        parse_mode="MarkdownV2"
    )


# ─── NEW USER TRACKING ───────────────────────────────────────────────────────

async def track_new_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Notify admins when an unknown user messages the bot in private."""
    msg = update.message
    if not msg or msg.chat.type != "private":
        return
    user = update.effective_user
    member_ids = {m["id"] for m in db.get_members()}
    if user.id in member_ids or is_admin(user.id):
        return
    name_preview = user.full_name[:20]
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton(
            "➕ Добавить в команду",
            callback_data=f"adduser_{user.id}_{name_preview}"
        ),
        InlineKeyboardButton("❌ Игнорировать", callback_data="adduser_ignore"),
    ]])
    for admin_id in ADMIN_IDS:
        try:
            await context.bot.send_message(
                chat_id=admin_id,
                text=f"👤 Новый пользователь написал боту:\n"
                     f"Имя: {user.full_name}\n"
                     f"ID: {user.id}\n\n"
                     f"Добавить в команду?",
                reply_markup=keyboard
            )
        except Exception:
            pass


async def handle_adduser(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        return
    if query.data == "adduser_ignore":
        await query.edit_message_text("❌ Пользователь проигнорирован.")
        return
    parts = query.data.split("_", 2)
    user_id = int(parts[1])
    name = parts[2] if len(parts) > 2 else str(user_id)
    db.add_member(user_id, name)
    await query.edit_message_text(f"✅ Добавлен в команду: {name} (ID: {user_id})")


# ─── BOT COMMANDS REGISTRATION ───────────────────────────────────────────────

async def post_init(app: Application):
    commands = [
        BotCommand("menu",       "Главное меню"),
        BotCommand("attendance", "Отметить посещаемость"),
        BotCommand("report",     "Написать отчёт по шаблону"),
        BotCommand("analytics",  "Аналитика"),
        BotCommand("aianalysis", "AI-анализ отчётов"),
        BotCommand("violations", "CSV нарушений"),
        BotCommand("mystatus",   "Моя статистика"),
        BotCommand("help",       "Справка"),
    ]
    await app.bot.set_my_commands(commands)


# ─── MAIN ────────────────────────────────────────────────────────────────────

def main():
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()

    # Commands — admin
    # Report template conversation (must be before plain message handler)
    app.add_handler(report_conv)

    # Main entry points
    app.add_handler(CommandHandler("start",      cmd_start))
    app.add_handler(CommandHandler("menu",       cmd_menu))
    app.add_handler(CommandHandler("help",       cmd_help))

    # Shortcuts that still work as commands
    app.add_handler(CommandHandler("attendance", cmd_attendance_wizard))
    app.add_handler(CommandHandler("analytics",  cmd_analytics))
    app.add_handler(CommandHandler("aianalysis", cmd_ai_analysis))
    app.add_handler(CommandHandler("violations", cmd_violations))
    app.add_handler(CommandHandler("mystatus",   cmd_my_status))

    # Power-user / advanced commands
    app.add_handler(CommandHandler("members",          cmd_members))
    app.add_handler(CommandHandler("addmember",        cmd_addmember))
    app.add_handler(CommandHandler("removemember",     cmd_removemember))
    app.add_handler(CommandHandler("syncmembers",      cmd_sync_members))
    app.add_handler(CommandHandler("notifications",    cmd_notifications))
    app.add_handler(CommandHandler("addnotif",         cmd_addnotif))
    app.add_handler(CommandHandler("exportmembers",    cmd_export_members))
    app.add_handler(CommandHandler("exportanalytics",  cmd_export_analytics))
    app.add_handler(CommandHandler("today",            cmd_today))
    app.add_handler(CommandHandler("attendance_history", cmd_attendance_history))
    app.add_handler(CommandHandler("report_now",       cmd_report_now))
    app.add_handler(CommandHandler("reportstatus",     cmd_report_status))
    app.add_handler(CommandHandler("testnotif",        cmd_test_notif))
    app.add_handler(CommandHandler("broadcast",        cmd_broadcast))
    app.add_handler(CommandHandler("debuginfo",        cmd_debuginfo))
    app.add_handler(CommandHandler("addreport",        cmd_addreport))

    # Callbacks — new multi-level menu first, then legacy handlers
    app.add_handler(CallbackQueryHandler(handle_menu,              pattern="^m\\|"))
    app.add_handler(CallbackQueryHandler(handle_att_callback,      pattern="^att\\|"))
    app.add_handler(CallbackQueryHandler(handle_attendance_toggle, pattern="^(toggle_|send_warnings|cancel_attendance)"))
    app.add_handler(CallbackQueryHandler(handle_remove_member,     pattern="^remove_"))
    app.add_handler(CallbackQueryHandler(handle_notif_toggle,      pattern="^notif_"))
    app.add_handler(CallbackQueryHandler(handle_help,              pattern="^help_"))
    app.add_handler(CallbackQueryHandler(handle_adduser,           pattern="^adduser_"))

    # Text input handler for menu flows (rename, add member, etc.) — must be first in group 0
    app.add_handler(
        # group=0: text input for menu flows — no chat-type filter so works in group AND private
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_menu_text_input),
        group=0
    )
    # group=1: report tracking — runs independently of group 0 result
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, track_report_messages), group=1)
    # group=2: new user detection in private
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, track_new_users),
        group=2
    )

    setup_scheduler(app)

    logger.info("Bot started!")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
