"""
Самопроверка чистой логики хранилища (без сети и без S3-доступов):
формирование ключа объекта и защита от path-traversal в имени файла.
Запуск: python3 test_storage.py
"""
import storage


def test_object_key():
    assert storage.object_key("abc123", "Инструктаж.pdf") == "documents/abc123/Инструктаж.pdf"
    # basename отсекает путь — нельзя вылезти за пределы префикса
    assert storage.object_key("h", "../../etc/passwd") == "documents/h/passwd"
    assert storage.object_key("h", "sub/dir/file.docx") == "documents/h/file.docx"
    # пустое имя не роняет
    assert storage.object_key("h", "").startswith("documents/h/")


if __name__ == "__main__":
    test_object_key()
    print("OK: логика ключей хранилища работает")
