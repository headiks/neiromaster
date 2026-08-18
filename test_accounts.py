"""
Самопроверка бэкенда аккаунтов: регистрация -> модерация -> вход -> роли.
Гоняет реальные users.py и auth.py во временной папке, без FastAPI и тяжёлых
зависимостей (docling/qdrant). Запуск: python test_accounts.py
"""
import tempfile
from pathlib import Path

import users
import auth


def _isolate(tmp: Path):
    """Увести всё файловое хранилище во временную папку — не трогаем реальные data/."""
    users.DATA_DIR = tmp
    users.USERS_PATH = tmp / "users.json"
    users.INITIAL_CREDENTIALS_PATH = tmp / "owner_initial_credentials.txt"
    users.LEGACY_EMPLOYEES_PATH = tmp / "employees.json"


def run():
    with tempfile.TemporaryDirectory() as d:
        _isolate(Path(d))

        # 1. Первый запуск: создаётся единственный owner с одноразовыми данными
        creds = users.ensure_owner()
        assert creds and creds["username"].startswith("admin-")
        assert users.ensure_owner() is None, "owner должен создаваться только один раз"
        owner = users.get_owner()
        assert owner["role"] == users.ROLE_OWNER and owner["must_change_credentials"]

        # 2. Owner входит по выданным данным, затем задаёт свои логин/пароль
        token, u = auth.login(creds["username"], creds["password"])
        assert u["id"] == owner["id"]
        users.set_credentials(owner["id"], "director", "s3cret-pass")
        token, u = auth.login("director", "s3cret-pass")
        assert not users.get_user(owner["id"])["must_change_credentials"]

        # 3. Самостоятельная регистрация сотрудника -> ждёт подтверждения (active=False)
        emp = users.register_employee("ivanov", "employee-pass", "Иванов Иван", position="Водитель")
        assert emp["active"] is False, "регистрация должна требовать модерации"

        # 4. Пока не подтверждён — вход запрещён
        try:
            auth.login("ivanov", "employee-pass")
            assert False, "неподтверждённый сотрудник не должен входить"
        except ValueError as e:
            assert "подтвержд" in str(e).lower()

        # 5. Owner подтверждает -> сотрудник входит
        users.set_active(emp["id"], True)
        token, u = auth.login("ivanov", "employee-pass")
        assert u["role"] == users.ROLE_EMPLOYEE

        # 6. Неверный пароль отклоняется, публичное представление без секретов
        try:
            auth.login("ivanov", "wrong")
            assert False
        except ValueError:
            pass
        assert "hash" not in users.public_view(u) and "salt" not in users.public_view(u)

        # 7. Лок-аут перебора: после LOCKOUT_ATTEMPTS попыток — блокировка
        for _ in range(auth.LOCKOUT_ATTEMPTS):
            try:
                auth.login("ivanov", "nope", client="1.2.3.4")
            except ValueError:
                pass
        try:
            auth.login("ivanov", "employee-pass", client="1.2.3.4")
            assert False, "после серии неудач вход должен быть временно заблокирован"
        except ValueError as e:
            assert "много" in str(e).lower()

        # 8. Роли: обычный admin правит сотрудников, но не других админов; owner — всех
        admin = users.create_user({"username": "hrdept", "password": "hr-pass-123",
                                    "full_name": "HR"}, role=users.ROLE_ADMIN)
        assert users.can_manage(admin, users.get_user(emp["id"]))       # admin -> сотрудник: да
        assert not users.can_manage(admin, users.get_owner())           # admin -> owner: нет
        assert users.can_manage(owner, admin)                           # owner -> admin: да

        # 9. Смена собственного пароля рвёт старые сессии
        auth.change_own_password(users.get_user(emp["id"]), "employee-pass", "new-pass-999")
        assert auth.get_session_user(token) is None
        auth.login("ivanov", "new-pass-999")  # новый пароль работает

    print("OK: регистрация, модерация, вход, лок-аут и роли работают")


if __name__ == "__main__":
    run()
