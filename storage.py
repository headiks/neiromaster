"""
storage.py — объектное хранилище документов (S3, Timeweb Cloud).

Файлы регламентов больше не лежат на диске приложения, а хранятся в S3-бакете
(как в SmartA: endpoint s3.twcstorage.ru, path-style). В таблице documents лежит
только ключ объекта (storage_key) — сам файл в бакете.

Доступы берём из окружения (имена — как в SmartA, чтобы единообразно):
    S3_ENDPOINT   (по умолчанию https://s3.twcstorage.ru)
    S3_REGION     (по умолчанию ru-1)
    S3_BUCKET     — имя бакета
    S3_KEY        — access key
    S3_SECRET     — secret key
Секреты НИКОГДА не хранятся в коде — только в .env (он в .gitignore) или в
переменных окружения сервера.

Документы приватные: наружу отдаём не публичной ссылкой, а временной подписанной
(presigned_url) — доступ только у того, кому приложение её выдало.

boto3 импортируется внутри функций: разбор ключей/путей тестируется без сети и без
установленного boto3.
"""

import os
from pathlib import Path
from typing import Optional

BASE_DIR = Path(__file__).resolve().parent


def _load_env():
    """Подхватывает .env.production / .env в окружение (не перекрывая уже заданное).
    Тот же приём, что в seed_knowledge.py — чтобы storage работал и без внешнего загрузчика."""
    for name in (".env.production", ".env"):
        path = BASE_DIR / name
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_env()

S3_ENDPOINT = os.environ.get("S3_ENDPOINT", "https://s3.twcstorage.ru")
S3_REGION = os.environ.get("S3_REGION", "ru-1")
S3_BUCKET = os.environ.get("S3_BUCKET", "")
S3_KEY = os.environ.get("S3_KEY", "")
S3_SECRET = os.environ.get("S3_SECRET", "")

PREFIX = "documents"          # все документы складываем под этим префиксом в бакете
PRESIGN_TTL = 3600            # время жизни временной ссылки на скачивание, сек


def configured() -> bool:
    """Есть ли всё для работы с S3 (ключи и бакет). Если нет — вызовы упадут явно."""
    return bool(S3_BUCKET and S3_KEY and S3_SECRET)


def object_key(sha256: str, filename: str) -> str:
    """Ключ объекта в бакете: documents/<hash>/<имя>. Хэш в пути = дедуп на уровне
    хранилища (один и тот же файл — один и тот же ключ) + человекочитаемое имя."""
    safe = os.path.basename(filename or "file")
    return f"{PREFIX}/{sha256}/{safe}"


# ---------- Клиент (boto3 импортируется здесь, чтобы верх модуля был без сети) ----------
_client = None


def client():
    global _client
    if _client is None:
        if not configured():
            raise RuntimeError("S3 не настроен: заданы не все S3_BUCKET/S3_KEY/S3_SECRET")
        import boto3
        from botocore.config import Config
        _client = boto3.client(
            "s3",
            endpoint_url=S3_ENDPOINT,
            region_name=S3_REGION,
            aws_access_key_id=S3_KEY,
            aws_secret_access_key=S3_SECRET,
            # path-style: bucket в пути, а не в поддомене — как use_path_style_endpoint в SmartA
            config=Config(s3={"addressing_style": "path"}),
        )
    return _client


# ---------- Операции ----------
def put(key: str, content: bytes, content_type: Optional[str] = None) -> str:
    """Загрузить объект. Возвращает key. content_type — MIME (для корректной отдачи)."""
    extra = {"ContentType": content_type} if content_type else {}
    client().put_object(Bucket=S3_BUCKET, Key=key, Body=content, **extra)
    return key


def get(key: str) -> bytes:
    """Скачать объект целиком в память."""
    obj = client().get_object(Bucket=S3_BUCKET, Key=key)
    return obj["Body"].read()


def exists(key: str) -> bool:
    from botocore.exceptions import ClientError
    try:
        client().head_object(Bucket=S3_BUCKET, Key=key)
        return True
    except ClientError:
        return False


def delete(key: str) -> None:
    client().delete_object(Bucket=S3_BUCKET, Key=key)


def presigned_url(key: str, expires: int = PRESIGN_TTL) -> str:
    """Временная подписанная ссылка на скачивание приватного объекта."""
    return client().generate_presigned_url(
        "get_object", Params={"Bucket": S3_BUCKET, "Key": key}, ExpiresIn=expires)


if __name__ == "__main__":
    # Чистая логика без сети.
    assert object_key("abc123", "Инструктаж по ТБ.pdf") == "documents/abc123/Инструктаж по ТБ.pdf"
    assert object_key("h", "../../evil.pdf") == "documents/h/evil.pdf"   # basename отсекает путь
    print("storage: pure logic — OK; S3 настроен:", configured())
