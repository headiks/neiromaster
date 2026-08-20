"""
Ручной посев стартовой структуры знаний (смысловые папки + этапы) из
data/knowledge_seed.json. То же, что делает приложение при старте, но запускается
руками — удобно, если папки не появились (таблица уже была непустой при первом
старте, старт не перезасеял, файл сида отсутствовал и т.п.).

    python seed_knowledge.py          # засеять, только если таблицы пусты
    python seed_knowledge.py --force  # очистить folders/stages и засеять заново
    python seed_knowledge.py --show   # ничего не менять, показать что в БД

После посева строит векторы папок (folder_tags) — нужен работающий Ollama; при
ошибке они соберутся при первом изменении папки в админке или при рестарте.
"""

import sys
import json
from pathlib import Path

import db
import folders
import stages

SEED_PATH = Path(__file__).resolve().parent / "data" / "knowledge_seed.json"


def _state():
    return f"в БД: папок {len(folders.list_folders())}, этапов {len(stages.list_stages())}"


def main():
    force = "--force" in sys.argv
    show = "--show" in sys.argv

    db.init_schema()

    if show:
        print(_state())
        for f in folders.list_folders():
            print(f"  - {f['name']} ({f['slug']}): критериев {len(f['criteria'])}, "
                  f"этапов {len(f['stage_ids'])}, {'вкл' if f['enabled'] else 'выкл'}")
        return

    if not SEED_PATH.exists():
        print(f"ОШИБКА: нет файла сида {SEED_PATH}")
        print("Проверьте, что он подтянулся из git (см. .gitignore: исключение "
              "!/data/knowledge_seed.json).")
        sys.exit(1)

    seed = json.loads(SEED_PATH.read_text(encoding="utf-8"))
    print(f"Сид: этапов {len(seed.get('stages', []))}, папок {len(seed.get('folders', []))}")

    if force:
        db.execute("TRUNCATE folders")
        db.execute("TRUNCATE stages")
        print("Таблицы folders и stages очищены (--force)")

    s = stages.seed_if_empty(seed.get("stages", []))
    f = folders.seed_if_empty(seed.get("folders", []))
    print(f"Засеяно: этапов {s}, папок {f}")
    if s == 0 and f == 0 and not force:
        print("Ничего не засеяно — таблицы уже непусты. Для пересева: --force")

    print(_state())

    try:
        import classify
        n = classify.sync_folder_vectors()
        print(f"Векторы папок построены: {n}")
    except Exception as e:
        print(f"Векторы папок не построены ({e}); соберутся при рестарте/правке папки.")


if __name__ == "__main__":
    main()
