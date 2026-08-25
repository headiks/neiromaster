"""
staffing.py — импорт штатного расписания в профили сотрудников.

Единый вывод для ЛЮБОГО файла — ровно четыре поля: ФИО, Должность, Отдел, Дата
приёма/выхода. Именно эта таблица показывается в превью и заполняется. Никаких других
столбцов.

Разбор произвольной таблицы: tablemap (ИИ определяет, где какие столбцы). Плюс
ПОСТРОЧНАЯ классификация малой моделью: в реальных ШР должности и названия
подразделений идут в ОДНОМ столбце вперемешку (баннер «Обособленное подразделение …»,
затем должности под ним) — по столбцам это не разделить. Малая модель на каждую
неоднозначную строку решает: это ПОДРАЗДЕЛЕНИЕ (баннер, протягивается в «Отдел») или
ДОЛЖНОСТЬ/человек (строка данных). Результат кэшируется по тексту — вызовов немного.

Импорт: если у строки есть настоящее ФИО — создаётся профиль сотрудника (логин по ФИО,
временный пароль). Если ФИО нет (штатное расписание должностей) — профиль-вакансия
«(вакансия) <должность>» без логина.
"""

import re
import secrets

import requests

import tablemap
import users
from config import OLLAMA_URL

# Единая выходная схема — 4 поля, и только они.
UNIFIED_FIELDS = [
    {"name": "full_name",  "description": "ФИО человека: фамилия имя отчество (если в файле есть люди)"},
    {"name": "position",   "description": "должность/профессия"},
    {"name": "department", "description": "подразделение/отдел; часто отдельная строка-баннер над блоком"},
    {"name": "start_date", "description": "дата приёма или выхода на работу"},
]
FIELD_KEYS = ["full_name", "position", "department", "start_date"]

SMALL_MODEL = "qwen2.5:3b"

_TRANSLIT = str.maketrans({
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e", "ж": "zh",
    "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o",
    "п": "p", "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f", "х": "h", "ц": "c",
    "ч": "ch", "ш": "sh", "щ": "sch", "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
})

_HEADER_WORDS = {"сотрудник", "фио", "ф.и.о.", "работник", "наименование", "имя",
                 "должность", "№", "n", "no", "п/п", "№ п/п", "итого", "всего"}

# Сильные признаки названия подразделения — без модели (экономим вызовы и повышаем точность).
_ORG_HINTS = ("подразделение", "департамент", "управление", "дирекция", "служба",
              "цех", "участок", "сектор", "бюро", "администрация", "бухгалтери")


# ---------- Логины/пароли ----------
def _translit(word: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (word or "").lower().translate(_TRANSLIT))


def username_base(full_name: str) -> str:
    """«Иванов Иван Иванович» -> «ivanov_i_i». Фамилия + инициалы, длина >= 3."""
    parts = [p for p in re.split(r"\s+", (full_name or "").strip()) if p]
    if not parts:
        return "user"
    surname = _translit(parts[0])
    initials = [_translit(p)[:1] for p in parts[1:] if _translit(p)]
    base = "_".join([surname] + initials) if surname else "_".join(initials)
    base = base.strip("_") or "user"
    while len(base) < 3:
        base += "0"
    return base


def _unique_username(base: str, taken: set) -> str:
    if base not in taken:
        return base
    i = 2
    while f"{base}{i}" in taken:
        i += 1
    return f"{base}{i}"


def _temp_password(length: int = 10) -> str:
    alphabet = "abcdefghijkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "".join(secrets.choice(alphabet) for _ in range(length))


_PATRONYMIC = ("вич", "вна", "ична", "оглы", "кызы", "угли", "уулу")
_STOP = {"по", "и", "на", "в", "с", "для", "отдела", "отдел", "участка", "службы", "работ"}


def _looks_like_name(s: str) -> bool:
    """Похоже на ФИО, а не на должность/подразделение. Настоящее ФИО: без цифр, не
    орг-единица, и либо есть отчество (…вич/…вна/…кызы), либо 3+ слов с заглавной без
    служебных слов. Двухсловные должности («Главный геолог») сюда НЕ проходят."""
    s = (s or "").strip()
    if not s or any(ch.isdigit() for ch in s):
        return False
    low = s.lower()
    if any(h in low for h in _ORG_HINTS):
        return False
    toks = s.split()
    if len(toks) < 2:
        return False
    if any(t.lower().endswith(p) for p in _PATRONYMIC for t in toks):
        return True
    return len(toks) >= 3 and all(t[:1].isupper() for t in toks) and not any(t.lower() in _STOP for t in toks)


# ---------- Построчная классификация «подразделение / должность» ----------
UNIT_SYSTEM = """Определи, чем является строка штатного расписания. Ответь ОДНИМ словом:
«подразделение» — если это название организационной единицы (отдел, департамент,
управление, служба, цех, участок, обособленное подразделение, администрация, бухгалтерия);
«должность» — если это наименование должности/профессии человека;
«другое» — если ни то, ни другое (шапка, итог, примечание). Только одно слово, без пояснений."""


def _classify_unit_llm(text: str) -> str:
    try:
        r = requests.post(f"{OLLAMA_URL}/api/chat", json={
            "model": SMALL_MODEL,
            "messages": [{"role": "system", "content": UNIT_SYSTEM}, {"role": "user", "content": text[:200]}],
            "stream": False, "think": False,
        }, timeout=60)
        r.raise_for_status()
        ans = r.json()["message"]["content"].strip().lower()
    except Exception:
        ans = ""
    if "подразделен" in ans or "отдел" in ans:
        return "org"
    if "должност" in ans or "професс" in ans:
        return "job"
    return "other"


def make_unit_classifier(llm=_classify_unit_llm):
    """Классификатор текста строки с кэшем и быстрым путём по ключевым словам."""
    cache = {}

    def classify(text: str) -> str:
        t = (text or "").strip()
        if not t:
            return "other"
        if t in cache:
            return cache[t]
        low = t.lower()
        role = "org" if any(h in low for h in _ORG_HINTS) else llm(t)
        cache[t] = role
        return role

    return classify


# ---------- Разбор в единую схему ----------
def _cell(row, col):
    return (row[col].strip() if (col is not None and col < len(row) and row[col] is not None) else "")


def extract_unified(grid: list, mapping: dict, classifier=None) -> list:
    """Единые записи [{full_name, position, department, start_date}]. Отдел протягивается
    из строк-баннеров-подразделений. Неоднозначные строки (должность/подразделение в одном
    столбце) разбирает classifier."""
    classifier = classifier or make_unit_classifier()
    cols = mapping.get("columns") or {}
    sections = mapping.get("sections") or {}
    start = mapping.get("data_start_row") or 0
    name_c, pos_c, date_c = cols.get("full_name"), cols.get("position"), cols.get("start_date")
    dept_c = cols.get("department")
    dept_sec = sections.get("department", dept_c if dept_c is not None else None)

    carried_dept = ""
    records = []
    for row in grid[start:]:
        name = _cell(row, name_c)
        pos = _cell(row, pos_c)
        date = _cell(row, date_c)
        dept_col_val = _cell(row, dept_c) if (dept_c is not None and dept_c != pos_c) else ""
        sec_val = _cell(row, dept_sec) if dept_sec is not None else ""

        key = (name or pos or sec_val).strip().lower()
        if key in _HEADER_WORDS:
            continue
        if _looks_like_name(name):
            # отдел — из отдельной колонки отдела либо протянутый из баннера (НЕ из sec_val
            # текущей строки: там, где баннер = столбец №, на строках данных стоит номер)
            records.append({"full_name": name, "position": pos,
                            "department": dept_col_val or carried_dept, "start_date": date})
            continue
        # ФИО нет: решаем — подразделение (баннер) или должность (вакансия) или мусор
        text = pos or sec_val or name
        if not text:
            continue
        role = classifier(text)
        if role == "org":
            carried_dept = text
            continue
        if role == "job":
            records.append({"full_name": "", "position": text,
                            "department": dept_col_val or carried_dept, "start_date": date})
        # role == other -> пропускаем
    return records


def _repair_mapping(grid: list, mapping: dict) -> None:
    """Если модель отнесла к ФИО столбец, где на деле почти нет настоящих имён (должности/
    подразделения), — снимаем ФИО и используем этот столбец как источник должности."""
    cols = mapping.get("columns") or {}
    fn = cols.get("full_name")
    if fn is None:
        return
    start = mapping.get("data_start_row") or 0
    vals = [_cell(row, fn) for row in grid[start:start + 40]]
    vals = [v for v in vals if v]
    if vals and sum(1 for v in vals if _looks_like_name(v)) < len(vals) * 0.3:
        if cols.get("position") is None:
            cols["position"] = fn
        cols["full_name"] = None


def parse_file(source, filename: str = None, classifier=None) -> dict:
    """Разбор xlsx/xls/csv -> {"mapping", "records"} в единой схеме (4 поля)."""
    grid = tablemap.read_table_grid(source, filename)
    mapping = tablemap.map_columns(grid, UNIFIED_FIELDS)
    _repair_mapping(grid, mapping)
    records = extract_unified(grid, mapping, classifier)
    return {"mapping": mapping, "records": records, "count": len(records)}


# ---------- Создание профилей/вакансий ----------
def import_records(records: list) -> dict:
    """Строки с ФИО -> профили сотрудников (логин/пароль в ответе, один раз).
    Строки без ФИО -> профили-вакансии «(вакансия) <должность>» без логина.
    Возвращает {"profiles": [...], "vacancies": [...], "skipped": [...]}."""
    existing = users.list_users()
    taken = {u["username"] for u in existing if u.get("username")}
    seen = {(u.get("full_name") or "").strip().lower() for u in existing if u.get("full_name")}

    profiles, vacancies, skipped = [], [], []
    for rec in records:
        name = (rec.get("full_name") or "").strip()
        position = (rec.get("position") or "").strip()
        department = (rec.get("department") or "").strip()
        date = (rec.get("start_date") or "").strip()

        if _looks_like_name(name):
            if name.lower() in seen:
                skipped.append({"full_name": name, "reason": "уже есть"})
                continue
            username = _unique_username(username_base(name), taken)
            password = _temp_password()
            try:
                users.create_user(
                    {"username": username, "password": password, "full_name": name,
                     "position": position, "department": department, "start_date": date or None},
                    role=users.ROLE_EMPLOYEE, must_change_credentials=True)
            except ValueError as e:
                skipped.append({"full_name": name, "reason": str(e)})
                continue
            taken.add(username)
            seen.add(name.lower())
            profiles.append({"full_name": name, "username": username,
                             "password": password, "position": position})
        elif position:
            users.create_user(
                {"full_name": f"(вакансия) {position}", "position": position,
                 "department": department, "notes": "Вакансия из штатного расписания."},
                role=users.ROLE_EMPLOYEE)
            vacancies.append({"position": position, "department": department})
    return {"profiles": profiles, "vacancies": vacancies, "skipped": skipped}
