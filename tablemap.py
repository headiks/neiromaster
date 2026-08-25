"""
tablemap.py — переиспользуемый инструмент: привести ПРОИЗВОЛЬНУЮ таблицу к заданному
набору выходных полей.

Вход — сырой 2D-грид (строки × ячейки, как выгружается из xlsx со всеми шапками,
пустыми строками и объединёнными ячейками) плюс список желаемых выходных полей.
Большая модель на сервере определяет:
  - какой столбец какому выходному полю соответствует (по смыслу заголовков и данных);
  - с какой строки начинаются собственно данные (шапки/мусор сверху игнорируются).
Извлечение записей по найденной разметке — чистая функция без ИИ (тестируется отдельно).

Инструмент общий: сегодня им разбираем штатное расписание (ФИО, должность, отдел…),
завтра — любую таблицу под любой набор колонок. Знаний о штатке здесь нет — только
механика «вход-таблица + желаемые поля → нормализованные записи».
"""

import os
import re
import json
from typing import Optional

import requests

from config import OLLAMA_URL

LLM_MODEL = os.environ.get("NEIROMASTER_TABLEMAP_MODEL", "qwen3:14b")
LLM_TIMEOUT = 180
SAMPLE_ROWS = 15   # сколько первых строк показываем модели для определения разметки


def _cell(v) -> str:
    return "" if v is None else str(v).strip()


def _llm(system: str, user: str) -> str:
    r = requests.post(f"{OLLAMA_URL}/api/chat", json={
        "model": LLM_MODEL,
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
        "stream": False, "think": False,
    }, timeout=LLM_TIMEOUT)
    r.raise_for_status()
    return r.json()["message"]["content"].strip()


def _parse_json(text: str) -> dict:
    text = re.sub(r"```json\s*|```", "", text)
    a, b = text.find("{"), text.rfind("}")
    if a == -1 or b == -1:
        raise ValueError("нет JSON в ответе модели")
    return json.loads(text[a:b + 1])


MAP_SYSTEM = """Ты — парсер табличных выгрузок. Тебе дают первые строки таблицы (с индексами
строк и столбцов, начиная с 0) и список ЖЕЛАЕМЫХ ПОЛЕЙ на выходе. Определи:
1) с какого индекса строки начинаются собственно ДАННЫЕ (шапки, заголовки, пустые строки
   сверху — не данные);
2) какой индекс СТОЛБЦА соответствует каждому желаемому полю (по смыслу заголовков и
   образца значений). Если подходящего столбца нет — верни null.
Ответ — СТРОГО JSON без пояснений:
{"data_start_row": <int>, "columns": {"<поле>": <индекс столбца или null>, ...}}"""


def _grid_preview(grid: list, rows: int = SAMPLE_ROWS) -> str:
    lines = []
    for r, row in enumerate(grid[:rows]):
        cells = [f"[{c}]{_cell(val)}" for c, val in enumerate(row) if _cell(val)]
        lines.append(f"строка {r}: " + " | ".join(cells))
    return "\n".join(lines)


def map_columns(grid: list, target_fields: list) -> dict:
    """Определить разметку таблицы большой моделью.
    target_fields — [{"name": "full_name", "description": "ФИО"}, ...].
    Возвращает {"data_start_row": int, "columns": {name: col_index|None}}."""
    names = [f["name"] for f in target_fields]
    fields_txt = "\n".join(f"- {f['name']}: {f.get('description', '')}" for f in target_fields)
    user = f"Желаемые поля:\n{fields_txt}\n\nПервые строки таблицы:\n{_grid_preview(grid)}"
    data = _parse_json(_llm(MAP_SYSTEM, user))
    cols = data.get("columns") or {}
    # оставляем только запрошенные поля; приводим индексы к int или None
    clean = {}
    for name in names:
        v = cols.get(name)
        clean[name] = int(v) if isinstance(v, (int, float)) or (isinstance(v, str) and v.isdigit()) else None
    return {"data_start_row": int(data.get("data_start_row") or 0), "columns": clean}


def extract_records(grid: list, mapping: dict, required: Optional[list] = None) -> list:
    """Извлечь записи по найденной разметке — чистая функция без ИИ.
    mapping — результат map_columns. required — поля, без которых строка пропускается
    (по умолчанию первое поле с не-null столбцом). Возвращает список dict по полям."""
    columns = mapping.get("columns") or {}
    start = mapping.get("data_start_row") or 0
    if required is None:
        required = [name for name, col in columns.items() if col is not None][:1]
    records = []
    for row in grid[start:]:
        rec = {}
        for name, col in columns.items():
            rec[name] = _cell(row[col]) if (col is not None and col < len(row)) else ""
        if all(rec.get(k) for k in required):
            records.append(rec)
    return records


def normalize_table(grid: list, target_fields: list, required: Optional[list] = None) -> dict:
    """Полный проход: разметка моделью + извлечение записей.
    Возвращает {"mapping": {...}, "records": [...]}."""
    mapping = map_columns(grid, target_fields)
    return {"mapping": mapping, "records": extract_records(grid, mapping, required)}


def read_xlsx_grid(source) -> list:
    """Читает xlsx (путь или bytes) в 2D-грид строк активного листа. openpyxl импортируется
    лениво — чистой логике маппинга/извлечения драйвер xlsx не нужен (и её тесты тоже)."""
    import io
    import openpyxl
    wb = openpyxl.load_workbook(io.BytesIO(source) if isinstance(source, (bytes, bytearray)) else source,
                                data_only=True, read_only=True)
    ws = wb.active
    return [[_cell(v) for v in row] for row in ws.iter_rows(values_only=True)]
