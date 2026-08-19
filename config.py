"""
Общие пути и низкоуровневые константы. Никакой бизнес-логики — только то,
что нужно и indexing.py, и topics.py, и test_cascade.py одновременно.
Вынесено отдельно, чтобы indexing.py <-> topics.py не импортировали друг друга по кругу.
"""
import os
from pathlib import Path
import requests

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"

# data/documents/<topic_slug>/<файл>   — оригиналы, разложенные по темам (папкам)
# data/converted/<topic_slug>/<файл>.md — то же самое в виде markdown (таблицы читаемы для ИИ)
# data/processed/<hash>.json            — кэш разобранного docling-документа (техническое, для ускорения повторной обработки)
DOCS_DIR = DATA_DIR / "documents"
CONVERTED_DIR = DATA_DIR / "converted"
CACHE_DIR = DATA_DIR / "processed"
REGISTRY_PATH = DATA_DIR / "registry.json"

for d in (DOCS_DIR, CONVERTED_DIR, CACHE_DIR):
    d.mkdir(parents=True, exist_ok=True)

SUPPORTED_EXT = {".pdf", ".docx", ".doc", ".pptx", ".html", ".htm", ".md", ".txt"}
MAX_UPLOAD_BYTES = 50 * 1024 * 1024  # 50 МБ на файл

QDRANT_HOST = "localhost"
QDRANT_PORT = 6333

OLLAMA_URL = "http://localhost:8080"
EMBED_MODEL = "bge-m3"
EMBED_DIM = 1024
# Первый эмбеддинг грузит модель bge-m3 в память (на CPU — долго), а при индексации
# документа их считаются десятки подряд. 30 с не хватало -> документ падал в error
# по read timeout. Переопределяется переменной NEIROMASTER_EMBED_TIMEOUT.
EMBED_TIMEOUT = int(os.environ.get("NEIROMASTER_EMBED_TIMEOUT", "120"))


def get_embedding(text: str):
    r = requests.post(
        f"{OLLAMA_URL}/api/embed",
        json={"model": EMBED_MODEL, "input": text},
        timeout=EMBED_TIMEOUT,
    )
    r.raise_for_status()
    return r.json()["embeddings"][0]
