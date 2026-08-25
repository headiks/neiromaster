"""
staffing.py — импорт штатного расписания в профили сотрудников.

Штатка первична: из неё берутся ЛЮДИ и их ДОЛЖНОСТИ, на которых строится всё
остальное. Разбор произвольной xlsx-выгрузки делает переиспользуемый tablemap
(ИИ определяет, какой столбец какому полю соответствует). Здесь — только знание
о нужных полях и создание аккаунтов.

Аккаунту проставляем ФИО, должность и отдел. Табельный номер и дату приёма показываем
в превью, но в профиль НЕ пишем: «дата приёма» из штатки — это не «дата выхода на
работу», по которой считается расписание адаптации (её админ задаёт отдельно).

Логины — транслит ФИО (ivanov_i_i), при коллизии с числом. Пароль — временный,
случайный; сотрудник меняет его при первом входе (must_change_credentials).
"""

import re
import secrets
import string

import tablemap
import users

# Поля, которые вытаскиваем из штатки. description — подсказка модели для разметки.
STAFFING_FIELDS = [
    {"name": "full_name",  "description": "ФИО сотрудника: фамилия, имя, отчество"},
    {"name": "position",   "description": "должность"},
    {"name": "department", "description": "отдел, подразделение или цех"},
    {"name": "tab_number", "description": "табельный номер (только для справки)"},
    {"name": "start_date", "description": "дата приёма на работу (только для справки)"},
]

_TRANSLIT = str.maketrans({
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e", "ж": "zh",
    "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o",
    "п": "p", "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f", "х": "h", "ц": "c",
    "ч": "ch", "ш": "sh", "щ": "sch", "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
})


def _translit(word: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (word or "").lower().translate(_TRANSLIT))


def username_base(full_name: str) -> str:
    """«Иванов Иван Иванович» -> «ivanov_i_i». Фамилия + инициалы. Гарантируем длину >= 3."""
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
    # Без похожих символов (0/O, 1/l/I) — временный пароль иногда вводят вручную.
    alphabet = "abcdefghijkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "".join(secrets.choice(alphabet) for _ in range(length))


def parse(grid: list) -> dict:
    """Разметка штатки моделью + извлечение записей. {"mapping": {...}, "records": [...]}"""
    return tablemap.normalize_table(grid, STAFFING_FIELDS, required=["full_name"])


def parse_xlsx(source) -> dict:
    return parse(tablemap.read_xlsx_grid(source))


def import_records(records: list) -> dict:
    """Массово создаёт профили сотрудников из разобранных строк штатки.
    Возвращает {"created": [{full_name, username, password, position}], "skipped": [...]}.
    Пароли в ответе показываются один раз — для выгрузки администратору."""
    existing = users.list_users()
    taken = {u["username"] for u in existing if u.get("username")}
    seen_names = {(u.get("full_name") or "").strip().lower() for u in existing if u.get("full_name")}

    created, skipped = [], []
    for rec in records:
        full_name = (rec.get("full_name") or "").strip()
        if not full_name:
            continue
        key = full_name.lower()
        if key in seen_names:
            skipped.append({"full_name": full_name, "reason": "уже есть в системе"})
            continue

        username = _unique_username(username_base(full_name), taken)
        password = _temp_password()
        try:
            users.create_user(
                {"username": username, "password": password, "full_name": full_name,
                 "position": (rec.get("position") or "").strip(),
                 "department": (rec.get("department") or "").strip()},
                role=users.ROLE_EMPLOYEE, must_change_credentials=True,
            )
        except ValueError as e:
            skipped.append({"full_name": full_name, "reason": str(e)})
            continue
        taken.add(username)
        seen_names.add(key)
        created.append({"full_name": full_name, "username": username,
                        "password": password, "position": (rec.get("position") or "").strip()})
    return {"created": created, "skipped": skipped}
