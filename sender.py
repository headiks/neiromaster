"""
Этап 8 — служба рассылки: сообщения плана адаптации уходят сотруднику сами по
расписанию.

Модель «клиент-сервер» из ТЗ: сервер по времени складывает готовые сообщения в
ИСХОДЯЩИЙ ЯЩИК (outbox, таблица deliveries), а клиент сотрудника (этап 9) забирает их
из своего входящего — GET /my/inbox. Конкретного внешнего мессенджера в ТЗ нет,
поэтому доставка по умолчанию = запись в outbox (клиент опрашивает сервер). Если
понадобится внешний канал (Telegram/email/push) — достаточно зарегистрировать функцию
в TRANSPORTS, менять цикл рассылки не нужно.

Свойства доставки:
  - Идемпотентность: PRIMARY KEY (user_id, message_id) + ON CONFLICT DO NOTHING — одно
    сообщение уходит РОВНО ОДИН РАЗ, даже если цикл сработал дважды или воркеров несколько.
  - Догон: планировщик кладёт в outbox ВСЕ сообщения, чьё время уже наступило и которых
    там ещё нет. Если сервер лежал — после старта разошлёт всё пропущенное.

Факт «что уже отправлено» живёт ЗДЕСЬ (deliveries), в отличие от mailing.py, который
считает только «следующее по датам» для обзора. Модули независимы и дополняют друг друга:
mailing — витрина «когда следующее», sender — фактическая доставка и история.

Ответы на вопросы сотрудника (вторая половина ТЗ этапа 8 — «получение ответа на вопрос
от конкретного пользователя») уже реализованы маршрутом /ask (rag.py); здесь — только
исходящая авто-рассылка.

Чистый отбор «что пора доставить» (due_messages) вынесен без обращения к БД — тестируется
без Postgres. Функции хранилища импортируют db/users локально.
"""

import os
import time
import threading
from datetime import datetime
from typing import Optional

import employees as adaptation   # расчёт персонального расписания сотрудника (этап 6)

TABLE = "deliveries"
STATUS_SENT = "sent"

CREATE_TABLE = f"""
CREATE TABLE IF NOT EXISTS {TABLE} (
    user_id      TEXT NOT NULL,
    message_id   TEXT NOT NULL,
    title        TEXT,
    kind         TEXT,
    body         TEXT,
    send_at      TEXT,
    status       TEXT NOT NULL DEFAULT 'sent',
    created_at   TEXT,
    PRIMARY KEY (user_id, message_id)
)
"""
CREATE_INDEX = f"CREATE INDEX IF NOT EXISTS idx_{TABLE}_user ON {TABLE}(user_id, send_at)"

# Внешние каналы доставки: {имя: функция(user_id, msg) -> None}. По умолчанию пусто —
# доставка = запись в outbox, клиент забирает через /my/inbox. Зарегистрируй сюда
# функцию, чтобы дублировать сообщение в Telegram/email/push.
TRANSPORTS: dict = {}

# Интервал фонового цикла рассылки, сек. 0 — планировщик выключен (полезно в деве и в
# тестах). Переопределяется переменной NEIROMASTER_SENDER_INTERVAL.
DEFAULT_INTERVAL = int(os.environ.get("NEIROMASTER_SENDER_INTERVAL", "60"))

_scheduler_started = False
_scheduler_lock = threading.Lock()


def _now(now: Optional[str]) -> str:
    return now or datetime.now().strftime("%Y-%m-%dT%H:%M")


def init():
    """Создаёт таблицу outbox и индекс. Идемпотентно — звать при старте."""
    import db
    db.execute(CREATE_TABLE)
    db.execute(CREATE_INDEX)


# ---------- Чистый отбор «что пора доставить» (без БД — тестируется отдельно) ----------
def due_messages(schedule: dict, delivered_ids: set, now: Optional[str] = None) -> list:
    """
    Сообщения расписания, чьё время уже наступило (send_at <= now) и которых ещё нет в
    delivered_ids, по возрастанию времени. Чистая функция без БД.
    Формат send_at — 'YYYY-MM-DDTHH:MM', сравним лексикографически при равной длине.
    """
    now = _now(now)
    delivered_ids = delivered_ids or set()
    due = []
    for m in schedule.get("messages") or []:
        send_at = (m.get("schedule") or {}).get("send_at")
        if not send_at or send_at > now:
            continue
        if m.get("message_id") in delivered_ids:
            continue
        due.append(m)
    due.sort(key=lambda m: m["schedule"]["send_at"])
    return due


# ---------- Доставка ----------
def _deliver(user_id: str, msg: dict) -> None:
    """Кладёт сообщение в outbox (idempotent) и прогоняет через внешние транспорты, если есть."""
    import db
    content = (msg.get("content") or {}).get("text") or ""
    sub = msg.get("substage") or {}
    db.execute(
        f"""INSERT INTO {TABLE} (user_id, message_id, title, kind, body, send_at, status, created_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (user_id, message_id) DO NOTHING""",
        (user_id, msg["message_id"], sub.get("title"), sub.get("kind"), content,
         (msg.get("schedule") or {}).get("send_at"), STATUS_SENT,
         time.strftime("%Y-%m-%dT%H:%M:%S")),
    )
    for name, transport in TRANSPORTS.items():
        try:
            transport(user_id, msg)
        except Exception as e:
            print(f"[SENDER] транспорт {name} не смог отправить {user_id}/{msg.get('message_id')}: {e}")


def deliver_due(now: Optional[str] = None) -> int:
    """
    Проходит по всем сотрудникам и доставляет все наступившие, ещё не отправленные
    сообщения. Возвращает число доставленных. Идемпотентно и с догоном пропущенного.
    """
    import db, users
    now = _now(now)
    sent = 0
    for u in users.list_users():
        if u.get("role") != users.ROLE_EMPLOYEE:
            continue
        if not (u.get("plan_id") and u.get("start_date")):
            continue
        try:
            schedule = adaptation.build_employee_schedule(u)
        except ValueError:
            continue   # план удалён/не назначен — пропускаем
        rows = db.query(f"SELECT message_id FROM {TABLE} WHERE user_id = %s", (u["id"],), "all")
        delivered = {r["message_id"] for r in rows}
        for msg in due_messages(schedule, delivered, now):
            _deliver(u["id"], msg)
            sent += 1
    return sent


def inbox(user_id: str) -> list:
    """Входящие сообщения сотрудника (для клиента, этап 9), свежие сверху."""
    import db
    return db.query(f"SELECT * FROM {TABLE} WHERE user_id = %s ORDER BY send_at DESC", (user_id,), "all")


# ---------- Планировщик (фоновый поток) ----------
def start_scheduler(interval: int = DEFAULT_INTERVAL):
    """
    Запускает фоновый цикл рассылки (только один на процесс). Отключается
    NEIROMASTER_SENDER_INTERVAL=0.

    Multi-worker: при `uvicorn --workers N` цикл поднимется в каждом воркере. Дубликатов
    не будет (доставка идемпотентна по PRIMARY KEY), но работа лишняя — для прод-нагрузки
    планировщик стоит вынести в отдельный процесс (cron/systemd timer, дёргающий
    POST /mailing/run) или закрыть Postgres advisory-lock'ом.
    """
    global _scheduler_started
    if interval <= 0:
        print("[SENDER] планировщик выключен (NEIROMASTER_SENDER_INTERVAL=0)")
        return
    with _scheduler_lock:
        if _scheduler_started:
            return
        _scheduler_started = True

    def loop():
        while True:
            try:
                n = deliver_due()
                if n:
                    print(f"[SENDER] доставлено сообщений: {n}")
            except Exception as e:
                print(f"[SENDER] сбой цикла рассылки: {e}")
            time.sleep(max(5, interval))

    threading.Thread(target=loop, name="sender", daemon=True).start()
    print(f"[SENDER] планировщик запущен, интервал {interval} c")


if __name__ == "__main__":
    # Самопроверка чистого отбора «что пора доставить» — без БД.
    def _msg(mid, send_at, text="t"):
        return {"message_id": mid, "schedule": {"send_at": send_at},
                "substage": {"title": mid, "kind": "message"}, "content": {"text": text}}

    sched = {"messages": [
        _msg("a", "2026-09-01T10:00"),
        _msg("b", "2026-09-02T09:00"),
        _msg("c", None),                    # без времени — никогда не «пора»
    ]}
    now = "2026-09-01T12:00"
    # Наступило только a; b в будущем; c без времени
    assert [m["message_id"] for m in due_messages(sched, set(), now)] == ["a"]
    # a уже доставлено — не повторяем
    assert due_messages(sched, {"a"}, now) == []
    # Позже наступают оба, порядок по времени
    assert [m["message_id"] for m in due_messages(sched, set(), "2026-09-03T00:00")] == ["a", "b"]
    # Ничего не пора
    assert due_messages(sched, set(), "2026-08-01T00:00") == []
    print("sender: отбор «что пора доставить» — OK")
