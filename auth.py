"""
Вход в систему и сессии — для всех пользователей: и сотрудников, и администраторов.

Учётные записи, пароли и роли лежат в users.py. Здесь только:
    - проверка логина и пароля;
    - защита от перебора;
    - выдача и проверка токена сессии.

Сессии живут в памяти процесса: перезапуск сервера = перелогин. Этого достаточно
для одного uvicorn-воркера во внутренней сети; для многопроцессного деплоя сессии
нужно вынести в Redis или аналог.
"""

import time
import secrets
import threading
from typing import Optional

import users

COOKIE_NAME = "nm_session"
SESSION_TTL = 12 * 3600          # сколько живёт сессия без активности

# Защита от перебора: после LOCKOUT_ATTEMPTS неудач вход блокируется на LOCKOUT_SECONDS
LOCKOUT_ATTEMPTS = 10
LOCKOUT_SECONDS = 300

_lock = threading.Lock()
_sessions: dict = {}
_failures: dict = {}


# ---------- Блокировка перебора ----------
def _is_locked(key: str) -> int:
    """Сколько секунд осталось до разблокировки (0 — не заблокирован)."""
    record = _failures.get(key)
    if not record or record["count"] < LOCKOUT_ATTEMPTS:
        return 0
    remaining = int(record["last"] + LOCKOUT_SECONDS - time.time())
    if remaining <= 0:
        _failures.pop(key, None)
        return 0
    return remaining


def _record_failure(key: str):
    record = _failures.setdefault(key, {"count": 0, "last": 0})
    # Серия считается прерванной, если между попытками прошло больше окна блокировки
    if time.time() - record["last"] > LOCKOUT_SECONDS:
        record["count"] = 0
    record["count"] += 1
    record["last"] = time.time()


# ---------- Вход ----------
def login(username: str, password: str, client: str = "") -> tuple:
    """
    Возвращает (токен сессии, пользователь) либо поднимает ValueError с причиной отказа.
    """
    username = (username or "").strip().lower()
    key = f"{client}|{username}"

    with _lock:
        locked_for = _is_locked(key)
    if locked_for:
        raise ValueError(f"Слишком много неудачных попыток. Повторите через {locked_for} сек.")

    user = users.get_by_username(username)

    # Хэш считаем всегда — иначе по времени ответа видно, существует ли логин
    salt = user["salt"] if user and user.get("salt") else secrets.token_bytes(16).hex()
    stored = user["hash"] if user and user.get("hash") else secrets.token_bytes(32).hex()
    password_ok = users.verify_password(password or "", salt, stored)

    if not user or not user.get("hash") or not password_ok:
        with _lock:
            _record_failure(key)
        raise ValueError("Неверный логин или пароль")

    if not user.get("active"):
        raise ValueError("Учётная запись ещё не подтверждена администратором")

    with _lock:
        _failures.pop(key, None)
        token = secrets.token_urlsafe(32)
        _sessions[token] = {"user_id": user["id"], "created": time.time(), "seen": time.time()}
    return token, user


def get_session_user(token: Optional[str]) -> Optional[dict]:
    """
    Возвращает актуальную запись пользователя по токену и продлевает сессию.
    Роль и активность читаются из хранилища при каждом запросе — снятие прав
    или блокировка действуют сразу, без ожидания перелогина.
    """
    if not token:
        return None
    with _lock:
        session = _sessions.get(token)
        if not session:
            return None
        if time.time() - session["seen"] > SESSION_TTL:
            _sessions.pop(token, None)
            return None
        session["seen"] = time.time()
        user_id = session["user_id"]

    user = users.get_user(user_id)
    if not user or not user.get("active"):
        with _lock:
            _sessions.pop(token, None)
        return None
    return user


def logout(token: Optional[str]):
    if not token:
        return
    with _lock:
        _sessions.pop(token, None)


def drop_user_sessions(user_id: str):
    """Разлогинить пользователя везде — после смены пароля или снятия прав."""
    with _lock:
        for token, session in list(_sessions.items()):
            if session["user_id"] == user_id:
                _sessions.pop(token, None)


def change_own_password(user: dict, old_password: str, new_password: str):
    if not users.verify_password(old_password or "", user.get("salt"), user.get("hash")):
        raise ValueError("Текущий пароль указан неверно")
    users.set_password(user["id"], new_password)
    drop_user_sessions(user["id"])
