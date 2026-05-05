from __future__ import annotations

import asyncio
import logging
import os
import re
import tempfile
import threading
import warnings
from datetime import date, time as dt_time, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from zoneinfo import ZoneInfo

warnings.filterwarnings("ignore", message="If 'per_message=False'")


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")

    def log_message(self, *args):
        pass


def run_health_server():
    HTTPServer(("0.0.0.0", 8080), HealthHandler).serve_forever()

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

import config
from sheets import (
    EXPENSE_CATEGORIES,
    INCOME_CATEGORIES,
    STUDENTS,
    TRANSFER_CATEGORY,
    TUTORING_CATEGORY,
    SheetsManager,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

(
    SELECT_TYPE,
    SELECT_CATEGORY,
    SELECT_STUDENT,
    SELECT_WALLET,
    ENTER_AMOUNT,
    ENTER_DESCRIPTION,
    TRANSFER_FROM,
    TRANSFER_TO,
    TRANSFER_AMOUNT,
    TRANSFER_CAT,
) = range(10)

sheets_mgr: SheetsManager

TYUMEN_TZ = ZoneInfo("Asia/Yekaterinburg")  # UTC+5
PAGE_SIZE = 10


async def send_reminder(context: ContextTypes.DEFAULT_TYPE) -> None:
    await context.bot.send_message(
        chat_id=context.job.data,
        text="💸 Внеси расходы!!!!",
    )

# ── Постоянная клавиатура ─────────────────────────────────────────────────────

MAIN_KB = ReplyKeyboardMarkup(
    [
        [KeyboardButton("➕ Доход"),    KeyboardButton("➖ Расход"),   KeyboardButton("🔄 Перевод")],
        [KeyboardButton("💳 Кошельки"), KeyboardButton("💼 Баланс")],
        [KeyboardButton("📊 Отчёт"),    KeyboardButton("📋 История")],
    ],
    resize_keyboard=True,
    is_persistent=True,
)

def _fmt(amount: float, decimals: int = 2) -> str:
    """Форматирует число в российском стиле: 1 500,00"""
    s = f"{amount:,.{decimals}f}"
    return s.replace(",", " ").replace(".", ",")


# ── Inline-клавиатуры ─────────────────────────────────────────────────────────


def _type_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("💰 Доход",  callback_data="type_income"),
            InlineKeyboardButton("💸 Расход", callback_data="type_expense"),
        ],
        [InlineKeyboardButton("❌ Отмена", callback_data="cancel")],
    ])


def _category_kb(categories: list[str]) -> InlineKeyboardMarkup:
    rows = []
    for i in range(0, len(categories), 2):
        rows.append([
            InlineKeyboardButton(c, callback_data=f"cat_{c}")
            for c in categories[i : i + 2]
        ])
    rows.append([InlineKeyboardButton("❌ Отмена", callback_data="cancel")])
    return InlineKeyboardMarkup(rows)


def _student_kb() -> InlineKeyboardMarkup:
    rows = []
    for i in range(0, len(STUDENTS), 2):
        rows.append([
            InlineKeyboardButton(s, callback_data=f"student_{s}")
            for s in STUDENTS[i : i + 2]
        ])
    rows.append([InlineKeyboardButton("❌ Отмена", callback_data="cancel")])
    return InlineKeyboardMarkup(rows)


def _wallet_kb(wallets: list[str]) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(f"💳 {w}", callback_data=f"wallet_{w}")] for w in wallets]
    rows.append([InlineKeyboardButton("➖ Без привязки", callback_data="wallet_")])
    rows.append([InlineKeyboardButton("❌ Отмена", callback_data="cancel")])
    return InlineKeyboardMarkup(rows)


def _report_period_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📅 Сегодня",    callback_data="report_today"),
            InlineKeyboardButton("📅 Эта неделя", callback_data="report_week"),
        ],
        [
            InlineKeyboardButton("📅 Этот месяц", callback_data="report_month"),
            InlineKeyboardButton("📅 Всё время",  callback_data="report_all"),
        ],
    ])


def _build_report(
    income_cats: dict[str, float],
    expense_cats: dict[str, float],
    tutoring_by_student: dict[str, float],
    label: str,
) -> str:
    total_in = sum(income_cats.values())
    total_ex = sum(expense_cats.values())
    net = total_in - total_ex
    sign = "+" if net >= 0 else ""
    lines = [f"📊 *Отчёт: {label}*\n"]

    if income_cats:
        lines.append("💰 *Доходы:*")
        for cat, amt in sorted(income_cats.items(), key=lambda x: -x[1]):
            lines.append(f"  {cat}: `+{_fmt(amt)} ₽`")
        lines.append(f"  *Итого: `+{_fmt(total_in)} ₽`*\n")

    if tutoring_by_student:
        total_tut = sum(tutoring_by_student.values())
        lines.append(f"👩‍🏫 *Репетиторство — {_fmt(total_tut, 0)} ₽:*")
        for student, amt in sorted(tutoring_by_student.items(), key=lambda x: -x[1]):
            lines.append(f"  ✅ {student}: `+{_fmt(amt)} ₽`")
        lines.append("")

    if expense_cats:
        lines.append("💸 *Расходы:*")
        for cat, amt in sorted(expense_cats.items(), key=lambda x: -x[1]):
            lines.append(f"  {cat}: `-{_fmt(amt)} ₽`")
        lines.append(f"  *Итого: `-{_fmt(total_ex)} ₽`*\n")

    if not income_cats and not expense_cats:
        lines.append("За этот период записей нет.")
    else:
        lines.append(f"📈 *Чистый баланс: `{sign}{_fmt(net)} ₽`*")
    return "\n".join(lines)


def _desc_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⏭ Пропустить", callback_data="skip_desc")],
        [InlineKeyboardButton("❌ Отмена",     callback_data="cancel")],
    ])


def _format_history_page(
    records: list[dict], page: int, total: int
) -> tuple[str, InlineKeyboardMarkup | None]:
    lines = ["📋 *Последние записи:*\n"]
    for r in records:
        sign   = "+" if r["Тип"] == "Доход" else "-"
        emoji  = "💰" if r["Тип"] == "Доход" else "💸"
        amt    = float(r["Сумма"]) if r["Сумма"] else 0
        wallet = f" · {r['Кошелёк']}" if r.get("Кошелёк") else ""
        cat    = r["Категория"]
        desc   = str(r.get("Описание") or "")
        if cat == TRANSFER_CATEGORY:
            lines.append(f"🔄 `{r['Дата']}` {desc}: `{_fmt(amt)} ₽`")
            continue
        elif cat == TUTORING_CATEGORY and desc:
            label = f"{cat} ({desc}){wallet}"
        else:
            label = f"{cat}{wallet}" + (f" — {desc}" if desc else "")
        lines.append(f"{emoji} `{r['Дата']}` {label}: `{sign}{_fmt(amt)} ₽`")

    if total > PAGE_SIZE:
        pages = (total + PAGE_SIZE - 1) // PAGE_SIZE
        lines.append(f"\n_Стр. {page + 1} / {pages}_")

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀ Назад", callback_data=f"history_{page - 1}"))
    if (page + 1) * PAGE_SIZE < total:
        nav.append(InlineKeyboardButton("Вперёд ▶", callback_data=f"history_{page + 1}"))
    return "\n".join(lines), InlineKeyboardMarkup([nav]) if nav else None


# ── Голос ─────────────────────────────────────────────────────────────────────


async def _transcribe(file_path: str) -> str:
    def _do() -> str:
        from openai import OpenAI
        client = OpenAI(api_key=config.OPENAI_API_KEY)
        with open(file_path, "rb") as f:
            return client.audio.transcriptions.create(
                model="whisper-1", file=f, language="ru"
            ).text
    return await asyncio.to_thread(_do)


def _parse_voice(text: str) -> tuple[float | None, str | None]:
    m = re.search(r"\b(\d[\d\s]*(?:[.,]\d{1,2})?)\b", text)
    if not m:
        return None, None
    try:
        amount = float(m.group(1).replace(" ", "").replace(",", "."))
    except ValueError:
        return None, None
    t = text.lower()
    income_kw = ["получила", "получил", "пришло", "заработала", "заработал", "доход", "плюс"]
    expense_kw = ["потратила", "потратил", "заплатила", "заплатил", "купила", "купил", "расход", "минус"]
    if any(kw in t for kw in income_kw):
        return amount, "Доход"
    if any(kw in t for kw in expense_kw):
        return amount, "Расход"
    return amount, None


# ── Общий хелпер сохранения ───────────────────────────────────────────────────


async def _save_and_reply(
    update: Update, context: ContextTypes.DEFAULT_TYPE, description: str = ""
) -> None:
    ud = context.user_data
    type_    = ud["type"]
    category = ud["category"]
    amount   = ud["amount"]
    wallet   = ud.get("wallet", "")

    row_num = sheets_mgr.add_transaction(type_, category, amount, description, wallet)

    sign  = "+" if type_ == "Доход" else "-"
    emoji = "💰" if type_ == "Доход" else "💸"

    if category == TUTORING_CATEGORY and description:
        header = f"{emoji} {category} → {description}"
    else:
        header = f"{emoji} {type_} | {category}"

    text = f"✅ Записано!\n{header}\n{sign}{_fmt(amount)} ₽"
    if wallet:
        text += f"\n💳 {wallet}"
    if description and category != TUTORING_CATEGORY:
        text += f"\n📝 {description}"

    undo_kb = (
        InlineKeyboardMarkup([[InlineKeyboardButton("↩️ Отменить", callback_data=f"undo_{row_num}")]])
        if row_num > 0 else None
    )

    context.user_data.clear()
    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=undo_kb)
    else:
        await update.message.reply_text(text, reply_markup=undo_kb or MAIN_KB)


# ── Команды ───────────────────────────────────────────────────────────────────


async def cmd_start(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "👋 *Бот учёта финансов*\n\n"
        "Используйте кнопки внизу или быстрый ввод:\n"
        "`+1500` / `+1500 Ира` / `-250`\n\n"
        "Или отправьте 🎤 голосовое сообщение.",
        parse_mode="Markdown",
        reply_markup=MAIN_KB,
    )


async def cmd_wallets(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        wallets = sheets_mgr.get_wallets()
        total   = sheets_mgr.get_wallets_total()
    except Exception as exc:
        logger.error("get_wallets: %s", exc)
        await update.message.reply_text("⚠️ Ошибка при получении данных.")
        return
    from collections import defaultdict
    grouped: dict[str, list] = defaultdict(list)
    for name, bal, rate, bank in wallets:
        grouped[bank].append((name, bal, rate))

    lines = ["💳 *Баланс по счетам:*\n"]
    for bank, accounts in grouped.items():
        lines.append(f"🏦 *{bank}*")
        for name, bal, rate in accounts:
            sign = "+" if bal >= 0 else ""
            if rate > 0:
                lines.append(f"  {name} ({rate:.0f}%): `{sign}{_fmt(bal)} ₽`")
                lines.append(f"  _└ ~{_fmt(bal * rate / 100 / 12)} ₽/мес_")
            else:
                lines.append(f"  {name}: `{sign}{_fmt(bal)} ₽`")
        lines.append("")
    lines += [f"{'─' * 22}", f"*Итого: `{_fmt(total)} ₽`*"]
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def cmd_balance(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        income, expense, net = sheets_mgr.get_balance()
    except Exception as exc:
        logger.error("get_balance: %s", exc)
        await update.message.reply_text("⚠️ Ошибка при получении данных.")
        return
    sign = "+" if net >= 0 else ""
    await update.message.reply_text(
        f"💼 *Баланс*\n\n"
        f"💰 Доходы:  `+{_fmt(income):>14} ₽`\n"
        f"💸 Расходы: `-{_fmt(expense):>14} ₽`\n"
        f"{'─' * 26}\n"
        f"📊 Итого:    `{sign}{_fmt(net):>14} ₽`",
        parse_mode="Markdown",
    )


async def cmd_history(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        records, total = sheets_mgr.get_recent(PAGE_SIZE, offset=0)
    except Exception as exc:
        logger.error("get_recent: %s", exc)
        await update.message.reply_text("⚠️ Ошибка при получении данных.")
        return
    if not records:
        await update.message.reply_text("📋 Записей пока нет.")
        return
    text, kb = _format_history_page(records, 0, total)
    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=kb)


async def cmd_report(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "📊 *Отчёт — выберите период:*",
        parse_mode="Markdown",
        reply_markup=_report_period_kb(),
    )


async def on_report_period(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    today = date.today()

    if q.data == "report_today":
        date_from = date_to = today
        label = f"Сегодня, {today.strftime('%d.%m.%Y')}"
    elif q.data == "report_week":
        date_from = today - timedelta(days=today.weekday())
        date_to = today
        label = f"Эта неделя ({date_from.strftime('%d.%m')}–{date_to.strftime('%d.%m.%Y')})"
    elif q.data == "report_month":
        date_from = today.replace(day=1)
        date_to = today
        label = today.strftime("%B %Y")
    else:
        date_from = date_to = None
        label = "Всё время"

    try:
        income_cats, expense_cats = sheets_mgr.get_report(date_from, date_to)
        tutoring_by_student = sheets_mgr.get_tutoring_report(date_from, date_to)
    except Exception as exc:
        logger.error("get_report: %s", exc)
        await q.edit_message_text("⚠️ Ошибка при получении данных.")
        return

    await q.edit_message_text(
        _build_report(income_cats, expense_cats, tutoring_by_student, label),
        parse_mode="Markdown",
    )


async def on_history_page(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    page = int(q.data.removeprefix("history_"))
    try:
        records, total = sheets_mgr.get_recent(PAGE_SIZE, offset=page * PAGE_SIZE)
    except Exception as exc:
        logger.error("get_recent: %s", exc)
        await q.edit_message_text("⚠️ Ошибка при получении данных.")
        return
    if not records:
        await q.edit_message_text("📋 Записей пока нет.")
        return
    text, kb = _format_history_page(records, page, total)
    await q.edit_message_text(text, parse_mode="Markdown", reply_markup=kb)


async def on_undo(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    row_str = q.data.removeprefix("undo_")
    try:
        rows = [int(r) for r in row_str.split(",")]
        sheets_mgr.delete_rows(rows)
        await q.edit_message_text("↩️ Запись удалена.")
    except Exception as exc:
        logger.error("undo: %s", exc)
        await q.edit_message_text("⚠️ Не удалось удалить запись.")


# ── Conversation: вход ────────────────────────────────────────────────────────


async def add_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Быстрый ввод: +1500, +1500 Ира, -250, -250 продукты."""
    text = (update.message.text or "").strip()
    m = re.match(r"^([+-])(\d+(?:[.,]\d{1,2})?)\s*(.*)?$", text)
    if m:
        sign, amount_str, desc = m.groups()
        desc   = desc.strip()
        amount = float(amount_str.replace(",", "."))

        if sign == "+" and desc in STUDENTS:
            context.user_data.update({
                "type":       "Доход",
                "category":   TUTORING_CATEGORY,
                "amount":     amount,
                "quick_desc": desc,
            })
            wallets = sheets_mgr.get_wallet_names()
            await update.message.reply_text(
                f"💰 {TUTORING_CATEGORY} → {desc}: {_fmt(amount)} ₽\nС какой карты?",
                reply_markup=_wallet_kb(wallets),
            )
            return SELECT_WALLET

        context.user_data.update({
            "type":       "Доход" if sign == "+" else "Расход",
            "amount":     amount,
            "quick_desc": desc,
        })
        cats  = INCOME_CATEGORIES if sign == "+" else EXPENSE_CATEGORIES
        emoji = "💰" if sign == "+" else "💸"
        await update.message.reply_text(
            f"{emoji} {context.user_data['type']}: {_fmt(amount)} ₽\nКатегория:",
            reply_markup=_category_kb(cats),
        )
        return SELECT_CATEGORY

    await update.message.reply_text("Что добавить?", reply_markup=_type_kb())
    return SELECT_TYPE


async def add_income_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["type"] = "Доход"
    await update.message.reply_text(
        "💰 Доход — выберите категорию:",
        reply_markup=_category_kb(INCOME_CATEGORIES),
    )
    return SELECT_CATEGORY


async def add_expense_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["type"] = "Расход"
    await update.message.reply_text(
        "💸 Расход — выберите категорию:",
        reply_markup=_category_kb(EXPENSE_CATEGORIES),
    )
    return SELECT_CATEGORY


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not config.OPENAI_API_KEY:
        await update.message.reply_text(
            "⚠️ Голосовой ввод не настроен.\nДобавьте `OPENAI_API_KEY` в .env",
            parse_mode="Markdown",
        )
        return ConversationHandler.END

    await update.message.reply_text("🎤 Распознаю…")
    tg_file = await context.bot.get_file(update.message.voice.file_id)
    with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
        tmp_path = tmp.name
    try:
        await tg_file.download_to_drive(tmp_path)
        text = await _transcribe(tmp_path)
    except Exception as exc:
        logger.error("transcription: %s", exc)
        await update.message.reply_text("⚠️ Не удалось распознать. Попробуйте ещё раз.")
        return ConversationHandler.END
    finally:
        os.unlink(tmp_path)

    amount, detected_type = _parse_voice(text)
    if amount is None:
        await update.message.reply_text(
            f"🎤 «{text}»\n\nНе удалось определить сумму — введите вручную.",
            reply_markup=MAIN_KB,
        )
        return ConversationHandler.END

    context.user_data.update({"amount": amount, "quick_desc": text})
    if detected_type:
        context.user_data["type"] = detected_type
        cats  = INCOME_CATEGORIES if detected_type == "Доход" else EXPENSE_CATEGORIES
        emoji = "💰" if detected_type == "Доход" else "💸"
        await update.message.reply_text(
            f"🎤 «{text}»\n{emoji} {detected_type}: {_fmt(amount)} ₽\nКатегория:",
            reply_markup=_category_kb(cats),
        )
        return SELECT_CATEGORY
    else:
        await update.message.reply_text(
            f"🎤 «{text}»\nСумма: {_fmt(amount)} ₽\nДоход или расход?",
            reply_markup=_type_kb(),
        )
        return SELECT_TYPE


# ── Conversation: шаги ───────────────────────────────────────────────────────


async def on_select_type(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()
    if q.data == "cancel":
        context.user_data.clear()
        await q.edit_message_text("❌ Отменено.")
        return ConversationHandler.END
    context.user_data["type"] = "Доход" if q.data == "type_income" else "Расход"
    cats  = INCOME_CATEGORIES if context.user_data["type"] == "Доход" else EXPENSE_CATEGORIES
    label = "дохода" if context.user_data["type"] == "Доход" else "расхода"
    await q.edit_message_text(f"Категория {label}:", reply_markup=_category_kb(cats))
    return SELECT_CATEGORY


async def on_select_category(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()
    if q.data == "cancel":
        context.user_data.clear()
        await q.edit_message_text("❌ Отменено.")
        return ConversationHandler.END

    category = q.data.removeprefix("cat_")
    context.user_data["category"] = category

    if category == TUTORING_CATEGORY:
        await q.edit_message_text("👩‍🏫 От кого?", reply_markup=_student_kb())
        return SELECT_STUDENT

    wallets = sheets_mgr.get_wallet_names()
    await q.edit_message_text("С какой карты?", reply_markup=_wallet_kb(wallets))
    return SELECT_WALLET


async def on_select_student(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()
    if q.data == "cancel":
        context.user_data.clear()
        await q.edit_message_text("❌ Отменено.")
        return ConversationHandler.END

    student = q.data.removeprefix("student_")

    if "quick_desc" in context.user_data:
        context.user_data["quick_desc"] = student
    else:
        context.user_data["student"] = student

    wallets = sheets_mgr.get_wallet_names()
    await q.edit_message_text(
        f"👤 {student} — с какой карты?",
        reply_markup=_wallet_kb(wallets),
    )
    return SELECT_WALLET


async def on_select_wallet(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()
    if q.data == "cancel":
        context.user_data.clear()
        await q.edit_message_text("❌ Отменено.")
        return ConversationHandler.END

    context.user_data["wallet"] = q.data.removeprefix("wallet_")

    if "quick_desc" in context.user_data:
        try:
            await _save_and_reply(update, context, context.user_data.pop("quick_desc", ""))
        except Exception as exc:
            logger.error("add_transaction: %s", exc)
            await q.edit_message_text("⚠️ Не удалось сохранить. Попробуйте ещё раз.")
            context.user_data.clear()
        return ConversationHandler.END

    await q.edit_message_text("Введите сумму (например: 1500 или 99.90):")
    return ENTER_AMOUNT


async def on_enter_amount(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip().replace(",", ".")
    try:
        amount = float(text)
        if amount <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text(
            "⚠️ Введите положительное число, например `1500` или `99.90`",
            parse_mode="Markdown",
        )
        return ENTER_AMOUNT

    context.user_data["amount"] = amount

    if "student" in context.user_data:
        try:
            await _save_and_reply(update, context, context.user_data.pop("student"))
        except Exception as exc:
            logger.error("add_transaction: %s", exc)
            await update.message.reply_text("⚠️ Не удалось сохранить. Попробуйте ещё раз.")
            context.user_data.clear()
        return ConversationHandler.END

    await update.message.reply_text(
        "Добавьте описание или пропустите:",
        reply_markup=_desc_kb(),
    )
    return ENTER_DESCRIPTION


async def on_enter_description(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        await _save_and_reply(update, context, update.message.text.strip())
    except Exception as exc:
        logger.error("add_transaction: %s", exc)
        await update.message.reply_text("⚠️ Не удалось сохранить. Попробуйте ещё раз.")
        context.user_data.clear()
    return ConversationHandler.END


async def on_desc_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()
    if q.data == "cancel":
        context.user_data.clear()
        await q.edit_message_text("❌ Отменено.")
        return ConversationHandler.END
    try:
        await _save_and_reply(update, context, "")
    except Exception as exc:
        logger.error("add_transaction: %s", exc)
        await q.edit_message_text("⚠️ Не удалось сохранить. Попробуйте ещё раз.")
        context.user_data.clear()
    return ConversationHandler.END


async def transfer_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    wallets = sheets_mgr.get_wallet_names()
    await update.message.reply_text(
        "🔄 *Перевод между картами*\nС какой карты списать?",
        reply_markup=_wallet_kb(wallets),
        parse_mode="Markdown",
    )
    return TRANSFER_FROM


async def on_transfer_from(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()
    if q.data == "cancel":
        context.user_data.clear()
        await q.edit_message_text("❌ Отменено.")
        return ConversationHandler.END

    from_wallet = q.data.removeprefix("wallet_")
    context.user_data["transfer_from"] = from_wallet

    all_wallets = sheets_mgr.get_wallet_names()
    to_wallets  = [w for w in all_wallets if w != from_wallet]
    rows = [[InlineKeyboardButton(f"💳 {w}", callback_data=f"wallet_{w}")] for w in to_wallets]
    rows.append([InlineKeyboardButton("❌ Отмена", callback_data="cancel")])

    await q.edit_message_text(
        f"🔄 С: *{from_wallet}*\nНа какую карту?",
        reply_markup=InlineKeyboardMarkup(rows),
        parse_mode="Markdown",
    )
    return TRANSFER_TO


async def on_transfer_to(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()
    if q.data == "cancel":
        context.user_data.clear()
        await q.edit_message_text("❌ Отменено.")
        return ConversationHandler.END

    context.user_data["transfer_to"] = q.data.removeprefix("wallet_")
    from_w = context.user_data["transfer_from"]
    to_w   = context.user_data["transfer_to"]

    all_cats = EXPENSE_CATEGORIES + INCOME_CATEGORIES
    rows = []
    for i in range(0, len(all_cats), 2):
        rows.append([
            InlineKeyboardButton(c, callback_data=f"tcat_{c}")
            for c in all_cats[i : i + 2]
        ])
    rows.append([InlineKeyboardButton("⏭ Без категории", callback_data="tcat_")])
    rows.append([InlineKeyboardButton("❌ Отмена", callback_data="cancel")])

    await q.edit_message_text(
        f"🔄 *{from_w}* → *{to_w}*\nКатегория перевода:",
        reply_markup=InlineKeyboardMarkup(rows),
        parse_mode="Markdown",
    )
    return TRANSFER_CAT


async def on_transfer_cat(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()
    if q.data == "cancel":
        context.user_data.clear()
        await q.edit_message_text("❌ Отменено.")
        return ConversationHandler.END

    context.user_data["transfer_cat"] = q.data.removeprefix("tcat_")
    from_w = context.user_data["transfer_from"]
    to_w   = context.user_data["transfer_to"]
    cat    = context.user_data["transfer_cat"]
    label  = f" [{cat}]" if cat else ""

    await q.edit_message_text(
        f"🔄 *{from_w}* → *{to_w}*{label}\nВведите сумму:",
        parse_mode="Markdown",
    )
    return TRANSFER_AMOUNT


async def on_transfer_amount(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip().replace(",", ".")
    try:
        amount = float(text)
        if amount <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("⚠️ Введите положительное число, например `5000`",
                                        parse_mode="Markdown")
        return TRANSFER_AMOUNT

    from_w = context.user_data["transfer_from"]
    to_w   = context.user_data["transfer_to"]
    cat    = context.user_data.get("transfer_cat", "")

    try:
        row1, row2 = sheets_mgr.add_transfer(amount, from_w, to_w, cat)
    except Exception as exc:
        logger.error("add_transfer: %s", exc)
        await update.message.reply_text("⚠️ Не удалось сохранить. Попробуйте ещё раз.")
        context.user_data.clear()
        return ConversationHandler.END

    cat_line = f"\n🏷 {cat}" if cat else ""
    undo_kb = (
        InlineKeyboardMarkup([[InlineKeyboardButton("↩️ Отменить", callback_data=f"undo_{row1},{row2}")]])
        if row1 > 0 and row2 > 0 else None
    )
    await update.message.reply_text(
        f"✅ Перевод записан!\n🔄 {from_w} → {to_w}\n{_fmt(amount)} ₽{cat_line}",
        reply_markup=undo_kb or MAIN_KB,
    )
    context.user_data.clear()
    return ConversationHandler.END


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    await update.message.reply_text("❌ Отменено.", reply_markup=MAIN_KB)
    return ConversationHandler.END


# ── Entry point ───────────────────────────────────────────────────────────────


def main() -> None:
    global sheets_mgr
    sheets_mgr = SheetsManager()
    logger.info("Connected to Google Sheets ✓")

    app = Application.builder().token(config.TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start",   cmd_start))
    app.add_handler(CommandHandler("help",    cmd_start))
    app.add_handler(MessageHandler(filters.Text(["💳 Кошельки"]), cmd_wallets))
    app.add_handler(CommandHandler("wallets", cmd_wallets))
    app.add_handler(MessageHandler(filters.Text(["💼 Баланс"]),   cmd_balance))
    app.add_handler(CommandHandler("balance", cmd_balance))
    app.add_handler(MessageHandler(filters.Text(["📋 История"]),  cmd_history))
    app.add_handler(CommandHandler("history", cmd_history))
    app.add_handler(MessageHandler(filters.Text(["📊 Отчёт"]),    cmd_report))
    app.add_handler(CommandHandler("report",  cmd_report))
    app.add_handler(CallbackQueryHandler(on_report_period, pattern="^report_"))
    app.add_handler(CallbackQueryHandler(on_history_page,  pattern="^history_"))
    app.add_handler(CallbackQueryHandler(on_undo,          pattern="^undo_"))

    conv = ConversationHandler(
        per_message=False,
        entry_points=[
            CommandHandler("add", add_start),
            MessageHandler(filters.Text(["➕ Доход"]),  add_income_start),
            MessageHandler(filters.Text(["➖ Расход"]), add_expense_start),
            MessageHandler(filters.Regex(r"^[+-]\d") & ~filters.COMMAND, add_start),
            MessageHandler(filters.VOICE, handle_voice),
            MessageHandler(filters.Text(["🔄 Перевод"]), transfer_start),
        ],
        states={
            SELECT_TYPE:     [CallbackQueryHandler(on_select_type)],
            SELECT_CATEGORY: [CallbackQueryHandler(on_select_category)],
            SELECT_STUDENT:  [CallbackQueryHandler(on_select_student)],
            SELECT_WALLET:   [CallbackQueryHandler(on_select_wallet)],
            ENTER_AMOUNT:    [MessageHandler(filters.TEXT & ~filters.COMMAND, on_enter_amount)],
            ENTER_DESCRIPTION: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, on_enter_description),
                CallbackQueryHandler(on_desc_callback),
            ],
            TRANSFER_FROM:   [CallbackQueryHandler(on_transfer_from)],
            TRANSFER_TO:     [CallbackQueryHandler(on_transfer_to)],
            TRANSFER_CAT:    [CallbackQueryHandler(on_transfer_cat)],
            TRANSFER_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, on_transfer_amount)],
        },
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
        allow_reentry=True,
    )
    app.add_handler(conv)

    if config.TELEGRAM_CHAT_ID and app.job_queue:
        app.job_queue.run_daily(
            send_reminder,
            time=dt_time(21, 0, tzinfo=TYUMEN_TZ),
            name="reminder",
            data=int(config.TELEGRAM_CHAT_ID),
        )
        logger.info("Reminder scheduled at 21:00 Tyumen time")

    threading.Thread(target=run_health_server, daemon=True).start()
    logger.info("Bot is running…")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
