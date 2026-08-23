"""
Этап 7 — персональные расписания рассылки.

Материализует по каждому сотруднику «следующую дату и время рассылки»: сервер
хранит готовую строку (кому, когда и какое сообщение следующее), чтобы рассыльщик
или выгрузка расписания брали её из БД, а не пересчитывали план каждого сотрудника
на лету.

«Следующее» определяется ЧИСТО ПО ДАТАМ плана: ближайшее сообщение, чьё время
отправки ещё не прошло. Факт реальной отправки здесь не отслеживается —
refresh_*() пересчитывает состояние из плана и даты выхода в любой момент.

Модуль самодостаточный (этап 7): НЕ меняет код других этапов, только читает их
(users — список сотрудников; employees/planner — расчёт персонального расписания)
и держит свою таблицу employee_mailing. Подключён в app.py: init() в lifespan,
авто-синхронизация строки при изменении сотрудника/плана (_sync_mailing / refresh_all)
и роуты GET /mailing, GET /mailing/due, POST /mailing/refresh, GET /users/{id}/mailing.
Реальные отправки — это этап 8 (служба рассылки), он берёт очередь из mailing.due().

Расчёт (compute) вынесен без обращения к БД — его можно тестировать без Postgres.
Функции хранилища импортируют db/users локально: драйвер БД нужен только при
реальной работе, но не для расчётной логики и её тестов.
"""

import time
from datetime import datetime
from typing import Optional

import employees as adaptation   # переиспользуем расчёт персонального расписания (этап адаптации)

TABLE = "employee_mailing"

# Статусы строки рассылки:
#   active       — план и дата есть, впереди ещё есть сообщения (next_send_at заполнен)
#   completed    — план и дата есть, но все сообщения уже в прошлом
#   no_plan      — сотруднику не назначен план
#   no_start_date— план есть, но не указана дата выхода
#   plan_missing — назначенный план не найден (удалён)
STATUS_ACTIVE = "active"

CREATE_TABLE = f"""
CREATE TABLE IF NOT EXISTS {TABLE} (
    user_id         TEXT PRIMARY KEY,
    full_name       TEXT,
    plan_id         TEXT,
    plan_title      TEXT,
    start_date      TEXT,
    next_send_at    TEXT,
    next_message_id TEXT,
    next_title      TEXT,
    total_messages  INTEGER NOT NULL DEFAULT 0,
    remaining       INTEGER NOT NULL DEFAULT 0,
    status          TEXT    NOT NULL DEFAULT 'no_plan',
    updated_at      TEXT
)
"""
CREATE_INDEX = f"CREATE INDEX IF NOT EXISTS idx_{TABLE}_due ON {TABLE}(status, next_send_at)"


def init():
    """Создаёт таблицу и индекс, если их ещё нет. Идемпотентно — звать при старте."""
    import db
    db.execute(CREATE_TABLE)
    db.execute(CREATE_INDEX)


def _now(now: Optional[str]) -> str:
    return now or datetime.now().strftime("%Y-%m-%dT%H:%M")


# ---------- Чистый расчёт «следующего» (без БД — тестируется отдельно) ----------
def compute(employee: dict, now: Optional[str] = None) -> dict:
    """
    Считает состояние рассылки сотрудника по назначенному плану и дате выхода.
    Ничего не пишет — возвращает поля строки employee_mailing (без user_id/updated_at).
    now — момент отсчёта в формате 'YYYY-MM-DDTHH:MM' (по умолчанию — сейчас).
    """
    now = _now(now)
    state = {
        "full_name": employee.get("full_name") or "",
        "plan_id": employee.get("plan_id"),
        "plan_title": None,
        "start_date": employee.get("start_date"),
        "next_send_at": None,
        "next_message_id": None,
        "next_title": None,
        "total_messages": 0,
        "remaining": 0,
        "status": "no_plan",
    }
    if not employee.get("plan_id"):
        return state
    if not employee.get("start_date"):
        state["status"] = "no_start_date"
        return state
    try:
        schedule = adaptation.build_employee_schedule(employee)
    except ValueError:
        state["status"] = "plan_missing"
        return state

    messages = schedule.get("messages") or []
    state["plan_title"] = schedule.get("plan_title")
    state["total_messages"] = len(messages)

    # Формат send_at ('YYYY-MM-DDTHH:MM') сравним лексикографически при равной длине.
    future = [m for m in messages if (m["schedule"].get("send_at") or "") >= now]
    state["remaining"] = len(future)
    if future:
        nxt = min(future, key=lambda m: m["schedule"]["send_at"])
        state["next_send_at"] = nxt["schedule"]["send_at"]
        state["next_message_id"] = nxt["message_id"]
        state["next_title"] = (nxt.get("substage") or {}).get("title")
        state["status"] = STATUS_ACTIVE
    else:
        state["status"] = "completed"
    return state


# ---------- Хранилище (БД) ----------
# db/users импортируются внутри функций: расчётной логике выше драйвер БД не нужен,
# и модуль остаётся импортируемым/тестируемым в окружении без Postgres.
def _params(user_id: str, s: dict) -> tuple:
    return (user_id, s["full_name"], s["plan_id"], s["plan_title"], s["start_date"],
            s["next_send_at"], s["next_message_id"], s["next_title"],
            s["total_messages"], s["remaining"], s["status"],
            time.strftime("%Y-%m-%dT%H:%M:%S"))


_UPSERT = f"""
INSERT INTO {TABLE}
    (user_id, full_name, plan_id, plan_title, start_date, next_send_at,
     next_message_id, next_title, total_messages, remaining, status, updated_at)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (user_id) DO UPDATE SET
    full_name       = EXCLUDED.full_name,
    plan_id         = EXCLUDED.plan_id,
    plan_title      = EXCLUDED.plan_title,
    start_date      = EXCLUDED.start_date,
    next_send_at    = EXCLUDED.next_send_at,
    next_message_id = EXCLUDED.next_message_id,
    next_title      = EXCLUDED.next_title,
    total_messages  = EXCLUDED.total_messages,
    remaining       = EXCLUDED.remaining,
    status          = EXCLUDED.status,
    updated_at      = EXCLUDED.updated_at
"""


def refresh_employee(user_id: str, now: Optional[str] = None) -> Optional[dict]:
    """Пересчитывает и сохраняет строку рассылки одного сотрудника. None — если нет такого."""
    import db, users
    employee = users.get_user(user_id)
    if employee is None:
        return None
    db.execute(_UPSERT, _params(user_id, compute(employee, now)))
    return get(user_id)


def refresh_all(now: Optional[str] = None) -> int:
    """
    Пересчитывает рассылку по всем сотрудникам (role=employee) и возвращает их число.
    Звать после изменения планов/дат выхода или периодически (см. инструкцию).
    """
    import db, users
    now = _now(now)
    count = 0
    for u in users.list_users():
        if u.get("role") != users.ROLE_EMPLOYEE:
            continue
        db.execute(_UPSERT, _params(u["id"], compute(u, now)))
        count += 1
    return count


def due(before: Optional[str] = None) -> list:
    """
    Кому пора слать: строки со статусом active и next_send_at <= before
    (по умолчанию — сейчас). Это очередь для рассыльщика/выгрузки, по времени.
    """
    import db
    return db.query(
        f"""SELECT * FROM {TABLE}
            WHERE status = %s AND next_send_at IS NOT NULL AND next_send_at <= %s
            ORDER BY next_send_at""",
        (STATUS_ACTIVE, _now(before)), "all")


def list_all() -> list:
    """Все сотрудники с их следующей рассылкой — для админского обзора (без рассылки — в конце)."""
    import db
    return db.query(f"SELECT * FROM {TABLE} ORDER BY next_send_at IS NULL, next_send_at", (), "all")


def get(user_id: str) -> Optional[dict]:
    import db
    return db.query(f"SELECT * FROM {TABLE} WHERE user_id = %s", (user_id,), "one")


def remove(user_id: str) -> None:
    """Убрать запись рассылки (когда сотрудника удалили)."""
    import db
    db.execute(f"DELETE FROM {TABLE} WHERE user_id = %s", (user_id,))
