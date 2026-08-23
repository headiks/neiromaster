"""
Самопроверка службы рассылки (этап 8), sender.py — без Postgres.

Две части:
  - чистый отбор «что пора доставить» (due_messages) — без заглушек;
  - логика доставки deliver_due() — с in-memory заглушками db/users/employees:
    итерация по сотрудникам, пропуск не-сотрудников и без плана, идемпотентность
    (ON CONFLICT DO NOTHING), догон пропущенного, входящий ящик.

Запуск: python3 test_sender.py
"""

import sys
import types


def _msg(mid, send_at, text="t"):
    return {"message_id": mid, "schedule": {"send_at": send_at},
            "substage": {"title": mid, "kind": "message"}, "content": {"text": text}}


def test_due_messages_pure():
    import sender
    sched = {"messages": [_msg("a", "2026-09-01T10:00"),
                          _msg("b", "2026-09-02T09:00"),
                          _msg("c", None)]}                     # без времени — никогда не «пора»
    assert [m["message_id"] for m in sender.due_messages(sched, set(), "2026-09-01T12:00")] == ["a"]
    assert sender.due_messages(sched, {"a"}, "2026-09-01T12:00") == []          # уже доставлено
    assert [m["message_id"] for m in sender.due_messages(sched, set(), "2026-09-03T00:00")] == ["a", "b"]
    assert sender.due_messages(sched, set(), "2026-08-01T00:00") == []          # ещё не время


def test_deliver_due_with_stubs():
    store = {}   # (user_id, message_id) -> row

    db = types.ModuleType("db")
    def _execute(sql, params=()):
        if sql.strip().startswith("INSERT"):
            key = (params[0], params[1])
            store.setdefault(key, {"user_id": params[0], "message_id": params[1],
                                   "send_at": params[5]})   # ON CONFLICT DO NOTHING
    db.execute = _execute
    db.query = lambda sql, params=(), fetch="all": [{"message_id": mid}
                                                    for (u, mid) in store if u == params[0]]
    sys.modules["db"] = db

    users = types.ModuleType("users"); users.ROLE_EMPLOYEE = "employee"
    users.list_users = lambda: [
        {"id": "e1", "role": "employee", "plan_id": "p1", "start_date": "2026-09-01"},
        {"id": "a1", "role": "admin",    "plan_id": "p1", "start_date": "2026-09-01"},  # не сотрудник
        {"id": "e2", "role": "employee"},                                              # без плана
    ]
    sys.modules["users"] = users

    emp = types.ModuleType("employees")
    emp.build_employee_schedule = lambda u: {"messages": [
        _msg("m1", "2026-09-01T10:00", "Здравствуйте"),
        _msg("m2", "2026-09-05T09:00"),
    ]}
    sys.modules["employees"] = emp

    # sender импортирует employees на уровне модуля — подменяем ДО импорта
    sys.modules.pop("sender", None)
    import sender

    # Наступил только m1 у e1; a1 не сотрудник; e2 без плана
    assert sender.deliver_due(now="2026-09-02T00:00") == 1
    assert set(store) == {("e1", "m1")}
    # Повтор в тот же момент — идемпотентно
    assert sender.deliver_due(now="2026-09-02T00:00") == 0
    # Позже наступает m2 — доставляется, m1 не повторяется (догон)
    assert sender.deliver_due(now="2026-09-06T00:00") == 1
    assert set(store) == {("e1", "m1"), ("e1", "m2")}
    assert {r["message_id"] for r in sender.inbox("e1")} == {"m1", "m2"}


if __name__ == "__main__":
    test_due_messages_pure()
    print("OK  due_messages (чистый отбор)")
    test_deliver_due_with_stubs()
    print("OK  deliver_due (итерация, идемпотентность, догон, inbox)")
    print("test_sender: служба рассылки (этап 8) — все проверки пройдены")
