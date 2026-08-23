"""
Самопроверка расчёта «следующей рассылки» (этап 7), mailing.compute().
Проверяет чистую логику по датам — без Postgres и psycopg (БД-функции не трогаются).
Запуск: python3 test_mailing.py
"""
import tempfile
from pathlib import Path

import planner
import mailing


def _plan():
    return planner.save_plan(planner.normalize_plan({
        "title": "Адаптация водителя",
        "role": "Водитель",
        "stages": [{
            "title": "Первая неделя",
            "anchor": "from_start",
            "duration": {"value": 2, "unit": "days"},
            "substages": [
                {"title": "Приветствие", "schedule": {"day": 1, "time": "10:00"}},
                {"title": "Инструктаж",  "schedule": {"day": 2, "time": "09:00"}},
            ],
        }],
    }))


def run():
    with tempfile.TemporaryDirectory() as d:
        planner.PLANS_DIR = Path(d)
        plan = _plan()
        emp = {"id": "u1", "full_name": "Иванов Иван", "position": "Водитель",
               "plan_id": plan["plan_id"], "start_date": "2026-09-01"}
        # send_at: приветствие 2026-09-01T10:00, инструктаж 2026-09-02T09:00

        # Всё впереди
        s = mailing.compute(emp, now="2026-08-01T00:00")
        assert s["status"] == "active"
        assert s["total_messages"] == 2 and s["remaining"] == 2
        assert s["next_send_at"] == "2026-09-01T10:00"
        assert s["next_message_id"] and s["next_title"] == "Приветствие"
        assert s["plan_title"] == "Адаптация водителя"

        # Первое ушло по времени — следующее второе
        s = mailing.compute(emp, now="2026-09-01T12:00")
        assert s["status"] == "active" and s["remaining"] == 1
        assert s["next_send_at"] == "2026-09-02T09:00" and s["next_title"] == "Инструктаж"

        # Всё в прошлом
        s = mailing.compute(emp, now="2027-01-01T00:00")
        assert s["status"] == "completed"
        assert s["remaining"] == 0 and s["next_send_at"] is None

        # План не назначен
        s = mailing.compute({"id": "u2", "full_name": "Без плана"})
        assert s["status"] == "no_plan" and s["next_send_at"] is None

        # План есть, даты выхода нет
        s = mailing.compute({"id": "u3", "plan_id": plan["plan_id"]})
        assert s["status"] == "no_start_date"

        # Назначен несуществующий план
        s = mailing.compute({"id": "u4", "plan_id": "ghost", "start_date": "2026-09-01"})
        assert s["status"] == "plan_missing"

    print("OK: расчёт следующей рассылки (этап 7) работает")


if __name__ == "__main__":
    run()
