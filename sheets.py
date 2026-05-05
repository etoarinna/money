from __future__ import annotations

import logging
import time
from datetime import date, datetime, timedelta

import gspread
from google.oauth2.service_account import Credentials

import config

logger = logging.getLogger(__name__)


class _TTLCache:
    def __init__(self, ttl: int = 60) -> None:
        self._data: dict[str, tuple] = {}
        self._ttl = ttl

    def get(self, key: str):
        entry = self._data.get(key)
        if entry and time.time() - entry[1] < self._ttl:
            return entry[0], True
        return None, False

    def set(self, key: str, value) -> None:
        self._data[key] = (value, time.time())

    def invalidate(self) -> None:
        self._data.clear()


_SHEETS_EPOCH = date(1899, 12, 30)


def _to_date(val) -> date | None:
    """Конвертирует серийный номер Sheets или ISO-строку в date."""
    if isinstance(val, (int, float)):
        return _SHEETS_EPOCH + timedelta(days=int(val))
    try:
        return date.fromisoformat(str(val))
    except ValueError:
        return None

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

TRANSACTIONS_SHEET = "Транзакции"
SUMMARY_SHEET = "Сводка"
WALLETS_SHEET = "Кошельки"

TUTORING_CATEGORY  = "Репетиторство"
TRANSFER_CATEGORY  = "Перевод между картами"

INCOME_CATEGORIES = [TUTORING_CATEGORY, "Ресницы", "Маркетплейс"]
EXPENSE_CATEGORIES = [
    "Ипотека",
    "Продукты",
    "Транспорт",
    "Кафе и рестораны",
    "Развлечения",
    "Красота и здоровье",
    "Спорт",
    "Одежда",
    "Подписки",
    "Госуслуги",
    "Штрафы",
    "Переводы",
    "Учеба",
    "Прочее",
]

STUDENTS: list[str] = [
    "Ира", "Катя", "Маша", "Тина", "Света", "Надя", "Соня",
]

INITIAL_WALLETS: list[tuple[str, float, float, str]] = [
    ("Сбербанк",           7331.47,  0.0, "Сбербанк"),
    ("Фонд накопительный", 4682.79,  0.0, "Сбербанк"),
    ("Тиньков",            56.10,    0.0, "Тинькофф"),
    ("Накопилка",          21483.23, 9.0, "Тинькофф"),
]

TRANSACTIONS_HEADERS = ["Дата", "Время", "Тип", "Категория", "Сумма", "Описание", "Кошелёк"]


class SheetsManager:
    def __init__(self) -> None:
        creds = Credentials.from_service_account_file(
            config.GOOGLE_CREDENTIALS_FILE, scopes=SCOPES
        )
        self.client = gspread.authorize(creds)
        self.spreadsheet = self.client.open_by_key(config.GOOGLE_SHEET_ID)
        self._cache = _TTLCache(60)
        self._ensure_sheets()

    # ── Sheet initialisation ──────────────────────────────────────────────────

    def _ensure_sheets(self) -> None:
        existing = {ws.title for ws in self.spreadsheet.worksheets()}

        if TRANSACTIONS_SHEET not in existing:
            ws = self.spreadsheet.add_worksheet(
                title=TRANSACTIONS_SHEET, rows=10000, cols=7
            )
            ws.append_row(TRANSACTIONS_HEADERS, value_input_option="USER_ENTERED")
            ws.format("A1:G1", {"textFormat": {"bold": True},
                                "backgroundColor": {"red": 0.18, "green": 0.56, "blue": 0.18}})
            logger.info("Created sheet: %s", TRANSACTIONS_SHEET)
        else:
            ws = self.spreadsheet.worksheet(TRANSACTIONS_SHEET)
            headers = ws.row_values(1)
            if "Кошелёк" not in headers:
                col = len(headers) + 1
                if ws.col_count < col:
                    ws.resize(rows=ws.row_count, cols=col)
                ws.update_cell(1, col, "Кошелёк")
                logger.info("Added 'Кошелёк' column to %s", TRANSACTIONS_SHEET)

        if WALLETS_SHEET not in existing:
            ws = self.spreadsheet.add_worksheet(title=WALLETS_SHEET, rows=20, cols=6)
            logger.info("Created sheet: %s", WALLETS_SHEET)
        else:
            ws = self.spreadsheet.worksheet(WALLETS_SHEET)
            ws.clear()
            if ws.col_count < 6:
                ws.resize(rows=ws.row_count, cols=6)
        self._populate_wallets(ws)

        if SUMMARY_SHEET not in existing:
            ws = self.spreadsheet.add_worksheet(title=SUMMARY_SHEET, rows=60, cols=4)
            logger.info("Created sheet: %s", SUMMARY_SHEET)
        else:
            ws = self.spreadsheet.worksheet(SUMMARY_SHEET)
            ws.clear()
        self._populate_summary(ws)

    def _populate_wallets(self, ws: gspread.Worksheet) -> None:
        t = TRANSACTIONS_SHEET
        rows: list[list] = [
            ["Карта/Счёт", "Банк", "Нач. баланс (₽)", "Текущий баланс (₽)", "Ставка (%)", "Доход/мес (₽)"]
        ]
        for i, (name, initial, rate, bank) in enumerate(INITIAL_WALLETS, start=2):
            balance_formula = (
                f'=C{i}+SUMPRODUCT(({t}!G$2:G$10000="{name}")'
                f'*(({t}!C$2:C$10000="Доход")-({t}!C$2:C$10000="Расход"))'
                f'*({t}!E$2:E$10000))'
            )
            interest_formula = f"=ROUND(D{i}*E{i}/100/12;2)" if rate > 0 else ""
            rows.append([name, bank, initial, balance_formula, rate if rate > 0 else "", interest_formula])

        n = len(INITIAL_WALLETS)
        total_row = n + 2
        rows.append(["ИТОГО", "", f"=SUM(C2:C{n+1})", f"=SUM(D2:D{n+1})", "", f"=SUM(F2:F{n+1})"])

        ws.update("A1", rows, value_input_option="USER_ENTERED")
        ws.format("A1:F1", {"textFormat": {"bold": True},
                            "backgroundColor": {"red": 0.18, "green": 0.56, "blue": 0.18}})
        ws.format(f"A{total_row}:F{total_row}", {"textFormat": {"bold": True}})

    def _populate_summary(self, ws: gspread.Worksheet) -> None:
        t = TRANSACTIONS_SHEET
        rows: list[list] = [
            ["ФИНАНСОВАЯ СВОДКА", "", "", ""],
            ["Обновляется автоматически по формулам", "", "", ""],
            ["", "", "", ""],
            ["Показатель", "Доходы (₽)", "Расходы (₽)", "Баланс (₽)"],
            [
                "За всё время",
                f'=SUMIF({t}!C:C;"Доход";{t}!E:E)',
                f'=SUMIF({t}!C:C;"Расход";{t}!E:E)',
                "=B5-C5",
            ],
            [
                "Текущий месяц",
                (f'=SUMPRODUCT((YEAR({t}!A$2:A$10000)=YEAR(TODAY()))'
                 f'*(MONTH({t}!A$2:A$10000)=MONTH(TODAY()))'
                 f'*({t}!C$2:C$10000="Доход")*({t}!E$2:E$10000))'),
                (f'=SUMPRODUCT((YEAR({t}!A$2:A$10000)=YEAR(TODAY()))'
                 f'*(MONTH({t}!A$2:A$10000)=MONTH(TODAY()))'
                 f'*({t}!C$2:C$10000="Расход")*({t}!E$2:E$10000))'),
                "=B6-C6",
            ],
            ["", "", "", ""],
            ["ДОХОДЫ ПО КАТЕГОРИЯМ", "Сумма (₽)", "", ""],
        ]
        for cat in INCOME_CATEGORIES:
            rows.append([cat, f'=SUMIF({t}!D:D;"{cat}";{t}!E:E)', "", ""])
        rows.append(["", "", "", ""])
        rows.append(["РАСХОДЫ ПО КАТЕГОРИЯМ", "Сумма (₽)", "", ""])
        for cat in EXPENSE_CATEGORIES:
            rows.append([cat, f'=SUMIF({t}!D:D;"{cat}";{t}!E:E)', "", ""])

        ws.update("A1", rows, value_input_option="USER_ENTERED")
        ws.format("A1:D1", {"textFormat": {"bold": True}})
        ws.format("A4:D4", {"textFormat": {"bold": True}})

    # ── Private helpers ───────────────────────────────────────────────────────

    @staticmethod
    def _row_from_result(result: dict) -> int:
        try:
            updated = result["updates"]["updatedRange"]
            cell = updated.split("!")[1].split(":")[0]
            return int("".join(c for c in cell if c.isdigit()))
        except (KeyError, IndexError, ValueError):
            return -1

    def _get_all_records(self) -> list[dict]:
        cached, hit = self._cache.get("records")
        if hit:
            return cached
        ws = self.spreadsheet.worksheet(TRANSACTIONS_SHEET)
        records = ws.get_all_records(value_render_option="UNFORMATTED_VALUE")
        for r in records:
            if isinstance(r.get("Дата"), (int, float)):
                r["Дата"] = (_SHEETS_EPOCH + timedelta(days=int(r["Дата"]))).isoformat()
        self._cache.set("records", records)
        return records

    # ── Public API ────────────────────────────────────────────────────────────

    def add_transaction(
        self, type_: str, category: str, amount: float, description: str, wallet: str = ""
    ) -> int:
        ws = self.spreadsheet.worksheet(TRANSACTIONS_SHEET)
        now = datetime.now()
        result = ws.append_row(
            [
                now.strftime("%Y-%m-%d"),
                now.strftime("%H:%M:%S"),
                type_,
                category,
                amount,
                description,
                wallet,
            ],
            value_input_option="USER_ENTERED",
        )
        self._cache.invalidate()
        return self._row_from_result(result)

    def add_transfer(self, amount: float, from_wallet: str, to_wallet: str, category: str = "") -> tuple[int, int]:
        ws = self.spreadsheet.worksheet(TRANSACTIONS_SHEET)
        now = datetime.now()
        dt  = now.strftime("%Y-%m-%d")
        tm  = now.strftime("%H:%M:%S")
        cat_suffix = f" [{category}]" if category else ""
        r1 = ws.append_row(
            [dt, tm, "Расход", TRANSFER_CATEGORY, amount, f"→ {to_wallet}{cat_suffix}", from_wallet],
            value_input_option="USER_ENTERED",
        )
        r2 = ws.append_row(
            [dt, tm, "Доход", TRANSFER_CATEGORY, amount, f"← {from_wallet}{cat_suffix}", to_wallet],
            value_input_option="USER_ENTERED",
        )
        self._cache.invalidate()
        return self._row_from_result(r1), self._row_from_result(r2)

    def get_balance(self) -> tuple[float, float, float]:
        records = self._get_all_records()
        income = sum(
            float(r["Сумма"]) for r in records
            if r["Тип"] == "Доход" and r["Сумма"] and r.get("Категория") != TRANSFER_CATEGORY
        )
        expense = sum(
            float(r["Сумма"]) for r in records
            if r["Тип"] == "Расход" and r["Сумма"] and r.get("Категория") != TRANSFER_CATEGORY
        )
        return income, expense, income - expense

    def get_recent(self, n: int = 10, offset: int = 0) -> tuple[list[dict], int]:
        records = self._get_all_records()
        newest_first = list(reversed(records))
        return newest_first[offset : offset + n], len(records)

    def get_report(
        self,
        date_from: date | None = None,
        date_to:   date | None = None,
    ) -> tuple[dict[str, float], dict[str, float]]:
        """Доходы и расходы по категориям за период. None = без ограничения."""
        records = self._get_all_records()
        income_by_cat:  dict[str, float] = {}
        expense_by_cat: dict[str, float] = {}
        for r in records:
            if not r["Сумма"] or r.get("Категория") == TRANSFER_CATEGORY:
                continue
            rec_date = _to_date(r["Дата"])
            if rec_date is None:
                continue
            if date_from and rec_date < date_from:
                continue
            if date_to and rec_date > date_to:
                continue
            amt = float(r["Сумма"])
            if r["Тип"] == "Доход":
                income_by_cat[r["Категория"]] = income_by_cat.get(r["Категория"], 0) + amt
            elif r["Тип"] == "Расход":
                expense_by_cat[r["Категория"]] = expense_by_cat.get(r["Категория"], 0) + amt
        return income_by_cat, expense_by_cat

    def get_wallet_names(self) -> list[str]:
        return [name for name, _, _, _ in self.get_wallets()]

    def get_wallets(self) -> list[tuple[str, float, float, str]]:
        """Возвращает [(название, баланс, годовая_ставка_%, банк)]."""
        cached, hit = self._cache.get("wallets")
        if hit:
            return cached

        def _num(val) -> float:
            try:
                return float(str(val).replace(",", ".").replace("\xa0", "").replace(" ", "") or "0")
            except ValueError:
                return 0.0

        ws = self.spreadsheet.worksheet(WALLETS_SHEET)
        rows = ws.get_all_values(value_render_option="UNFORMATTED_VALUE")[1:]
        result = []
        for row in rows:
            name = row[0] if row else ""
            if not name or name == "ИТОГО":
                continue
            bank    = row[1] if len(row) > 1 else ""
            balance = _num(row[3]) if len(row) > 3 else 0.0
            rate    = _num(row[4]) if len(row) > 4 else 0.0
            result.append((name, balance, rate, bank))
        self._cache.set("wallets", result)
        return result

    def get_wallets_total(self) -> float:
        return sum(b for _, b, _, _ in self.get_wallets())

    def get_tutoring_report(
        self,
        date_from: date | None = None,
        date_to:   date | None = None,
    ) -> dict[str, float]:
        """Возвращает {имя_ученицы: сумма} за период. None = без ограничения."""
        records = self._get_all_records()
        by_student: dict[str, float] = {}
        for r in records:
            if not (r["Тип"] == "Доход" and r["Категория"] == TUTORING_CATEGORY and r["Сумма"]):
                continue
            rec_date = _to_date(r["Дата"])
            if rec_date is None:
                continue
            if date_from and rec_date < date_from:
                continue
            if date_to and rec_date > date_to:
                continue
            student = str(r.get("Описание") or "Без имени")
            by_student[student] = by_student.get(student, 0) + float(r["Сумма"])
        return by_student

    def delete_rows(self, rows: list[int]) -> None:
        ws = self.spreadsheet.worksheet(TRANSACTIONS_SHEET)
        for row in sorted(rows, reverse=True):
            if row > 1:
                ws.delete_rows(row)
        self._cache.invalidate()
