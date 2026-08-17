"""
Общая логика индексации регламентов: docling-конвертация -> автосортировка по
темам (папкам) -> смысловой чанкинг -> эмбеддинги -> Qdrant. Используется и
CLI-скриптом (index_documents.py), и веб-приложением (app.py, ручка загрузки).

Путь документа от загрузки до готовности к нейропоиску:

    оригинал (pdf/docx/...), лежит в data/documents/ (пока без темы)
        -> DocumentConverter (docling): разбор макета, таблиц, заголовков
        -> DoclingDocument (кэшируется как JSON в data/processed —
           не гоняем повторно тяжёлый layout-анализ PDF при переиндексации)
        -> topics.classify_document(): определяем тему по краткому содержанию
           -> файл переезжает в data/documents/<тема>/, markdown-версия
              сохраняется в data/converted/<тема>/ (таблицы в MD читаемы для ИИ)
        -> HybridChunker: смысловые чанки с учётом токенного бюджета
        -> contextualize(): к каждому чанку приклеивается путь заголовков
        -> эмбеддинг (bge-m3) -> вектор, в payload добавляется "topic": <slug>
        -> Qdrant: HNSW-индекс по векторам, чанки помечены темой — при вопросе
           сначала выбираются нужные темы (topics.route_question), а поиск по
           чанкам идёт только среди них, а не по всей базе

Установка зависимостей:
    pip install docling qdrant-client requests fastapi uvicorn python-multipart
"""

import re
import json
import time
import uuid
import queue
import hashlib
import threading
from pathlib import Path
from typing import Optional

from qdrant_client import QdrantClient
from qdrant_client.models import (
    VectorParams, Distance, PointStruct, Filter, FieldCondition, MatchValue, MatchAny,
    PayloadSchemaType,
)

from docling.document_converter import DocumentConverter
from docling.chunking import HybridChunker
from docling_core.types.doc.document import DoclingDocument

import topics
from config import (
    DOCS_DIR, CONVERTED_DIR, CACHE_DIR, REGISTRY_PATH,
    SUPPORTED_EXT, MAX_UPLOAD_BYTES,
    QDRANT_HOST, QDRANT_PORT, EMBED_DIM, get_embedding,
)

# ---------- Qdrant (основная коллекция чанков) ----------
COLLECTION_NAME = "reglaments"

MAX_TOKENS = 512     # бюджет токенов на чанк (под окно эмбеддинг-модели)
MERGE_PEERS = True   # склеивать соседние мелкие чанки одного уровня иерархии
UPSERT_BATCH = 64

client = QdrantClient(host=QDRANT_HOST, port=QDRANT_PORT)
converter = DocumentConverter()
chunker = HybridChunker(max_tokens=MAX_TOKENS, merge_peers=MERGE_PEERS)

_registry_lock = threading.Lock()


def _log(step, msg):
    print(f"[INDEX:{step}] {msg}")


# ---------- Реестр документов (статус, тема, кол-во чанков, ошибки) ----------
def _load_registry() -> dict:
    if not REGISTRY_PATH.exists():
        return {}
    try:
        with open(REGISTRY_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def _save_registry(reg: dict):
    tmp_path = REGISTRY_PATH.with_suffix(".tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(reg, f, ensure_ascii=False, indent=2)
    tmp_path.replace(REGISTRY_PATH)


def _update_registry(filename: str, **fields):
    with _registry_lock:
        reg = _load_registry()
        entry = reg.get(filename, {"filename": filename})
        entry.update(fields)
        reg[filename] = entry
        _save_registry(reg)
        return entry


def list_documents() -> list:
    with _registry_lock:
        reg = _load_registry()
    return sorted(reg.values(), key=lambda e: e.get("uploaded_at", ""), reverse=True)


def folder_stats() -> list:
    """Сводка смысловых папок для панели администратора.

    Источник истины о количестве файлов и чанков — реестр документов: он
    доступен и во время обработки, когда значения в Qdrant ещё не обновились.
    Метаданные папок (название и описание) берём из коллекции тем.
    """
    with _registry_lock:
        registry = _load_registry()

    stats: dict[str, dict] = {}
    try:
        for topic in topics.list_topics():
            slug = topic.get("slug")
            if not slug:
                continue
            stats[slug] = {
                "slug": slug,
                "name": topic.get("name") or slug,
                "description": topic.get("description") or "",
                "documents": 0,
                "chunks": 0,
            }
    except Exception as exc:
        # Не скрываем уже обработанные документы, если Qdrant временно недоступен.
        _log("FOLDERS", f"Не удалось получить метаданные папок: {exc}")

    for document in registry.values():
        slug = document.get("topic") or "unclassified"
        folder = stats.setdefault(slug, {
            "slug": slug,
            "name": document.get("topic_name") or "Без категории",
            "description": "",
            "documents": 0,
            "chunks": 0,
        })
        folder["documents"] += 1
        folder["chunks"] += int(document.get("chunks") or 0)

    return sorted(stats.values(), key=lambda folder: folder["name"].casefold())


# ---------- Вспомогательные функции ----------
def safe_filename(filename: str) -> str:
    """Убираем путь и опасные символы — защита от path traversal при загрузке."""
    name = Path(filename).name
    name = re.sub(r"[^\w\-. а-яА-ЯёЁ]", "_", name)
    return name or f"document_{uuid.uuid4().hex[:8]}"


def file_hash(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()[:16]


# Поля payload, по которым идёт фильтрация при поиске. Без явного индекса Qdrant
# фильтрует полным перебором точек — на 100-200 документах (тысячи чанков) это
# заметно медленнее. Индекс делает отбор по теме/источнику почти бесплатным,
# поэтому двухступенчатый поиск (сначала темы, потом чанки внутри них) масштабируется.
PAYLOAD_INDEXES = {
    "topic": PayloadSchemaType.KEYWORD,
    "source": PayloadSchemaType.KEYWORD,
    "section": PayloadSchemaType.KEYWORD,
}


def ensure_payload_indexes():
    for field, schema in PAYLOAD_INDEXES.items():
        try:
            client.create_payload_index(COLLECTION_NAME, field_name=field, field_schema=schema)
        except Exception as exc:
            # Индекс уже есть — Qdrant отвечает ошибкой, это не фатально.
            _log("INDEX", f"payload-индекс {field}: {exc}")


def create_collection(recreate: bool = False):
    collections = [c.name for c in client.get_collections().collections]
    if COLLECTION_NAME in collections:
        if recreate:
            client.delete_collection(COLLECTION_NAME)
        else:
            ensure_payload_indexes()
            topics.ensure_topics_collection()
            return
    client.create_collection(
        collection_name=COLLECTION_NAME,
        vectors_config=VectorParams(size=EMBED_DIM, distance=Distance.COSINE),
    )
    ensure_payload_indexes()
    topics.ensure_topics_collection()


def extract_page_no(chunk) -> Optional[int]:
    try:
        return chunk.meta.doc_items[0].prov[0].page_no
    except (AttributeError, IndexError, TypeError):
        return None


# ---------- Конвертация с кэшированием ----------
def convert_document(filepath: Path) -> DoclingDocument:
    """
    Гоняем файл через docling. Разбор PDF/DOCX (особенно PDF с layout-моделью)
    — самая тяжёлая часть пайплайна, поэтому результат кэшируется в JSON:
    при повторной обработке того же файла (переезд между папками, смена
    параметров чанкинга) можно переиспользовать готовую структуру документа.
    """
    cache_path = CACHE_DIR / f"{filepath.stem}__{file_hash(filepath)}.json"
    if cache_path.exists():
        return DoclingDocument.load_from_json(str(cache_path))

    result = converter.convert(str(filepath))
    doc = result.document
    doc.save_as_json(str(cache_path))
    return doc


def _doc_gist(markdown_text: str, filename: str) -> str:
    """Краткое содержание документа для классификации по темам: имя файла +
    начало markdown-версии (обычно там заголовок и первые разделы)."""
    return f"Файл: {filename}\n\n{markdown_text[:1500]}"


# Номер пункта регламента в начале строки: «5», «5.1», «5.1.2», с точкой или скобкой.
# Регламенты часто нумеруют пункты цифрами без стилей заголовков — docling такой
# документ отдаёт сплошным текстом, и без этого деления один пункт не отделить от другого.
CLAUSE_RE = re.compile(r"(?m)^[ \t]*(\d+(?:\.\d+){0,3})[.)]?[ \t]+(?=\S)")


def sectionize_by_clause(text: str) -> list:
    """
    Делит текст чанка на пункты по номерам в начале строки («5.1 ...»).
    Возвращает [(section_label, segment_text), ...]. Меньше двух номеров —
    возвращаем чанк целиком (section = единственный номер или None).
    Применяется только когда у чанка нет заголовков от docling (см. index_document).
    """
    marks = [(m.start(), m.group(1)) for m in CLAUSE_RE.finditer(text)]
    if len(marks) < 2:
        label = marks[0][1] if marks else None
        return [(label, text.strip())]
    segments = []
    for i, (pos, num) in enumerate(marks):
        end = marks[i + 1][0] if i + 1 < len(marks) else len(text)
        seg = text[pos:end].strip()
        if seg:
            segments.append((num, seg))
    return segments


# ---------- Индексация одного документа ----------
def index_document(filepath: Path) -> dict:
    filename = filepath.name
    _update_registry(filename, status="processing", error=None)

    try:
        start = time.time()
        doc = convert_document(filepath)
        markdown_text = doc.export_to_markdown()  # таблицы -> markdown-таблицы, лучший формат для LLM

        # ---- Определяем тему (папку) ----
        if filepath.parent == DOCS_DIR:
            # Файл только что загружен и лежит в корне data/documents — сортируем автоматически
            gist = _doc_gist(markdown_text, filename)
            topic = topics.classify_document(gist)
        else:
            # Файл уже лежит в подпапке (загружен раньше или разложен вручную) — уважаем это
            topic = {"slug": filepath.parent.name, "name": filepath.parent.name}
            topics.register_manual_topic(topic["slug"])
        slug = topic["slug"]

        # ---- Переносим оригинал в data/documents/<тема>/, если он был в корне ----
        target_path = DOCS_DIR / slug / filename
        if filepath != target_path:
            target_path.parent.mkdir(parents=True, exist_ok=True)
            filepath.replace(target_path)
            filepath = target_path

        # ---- Сохраняем читаемую markdown-версию (таблицы, заголовки) ----
        md_path = CONVERTED_DIR / slug / f"{filepath.stem}.md"
        md_path.parent.mkdir(parents=True, exist_ok=True)
        md_path.write_text(markdown_text, encoding="utf-8")

        # Фиксируем тему/путь сразу — если дальше (чанкинг/эмбеддинги) что-то упадёт,
        # delete_document всё равно будет знать, где физически лежит файл.
        _update_registry(
            filename,
            topic=slug,
            topic_name=topic["name"],
            path=str(filepath.relative_to(DOCS_DIR)),
            markdown_path=str(md_path.relative_to(CONVERTED_DIR)),
        )

        # ---- Чанкинг ----
        chunks = list(chunker.chunk(doc))
        if not chunks:
            _update_registry(filename, status="error", error="Не удалось выделить ни одного чанка")
            return {"filename": filename, "status": "error", "error": "Нет чанков"}

        # Сносим предыдущие чанки этого файла (переиндексация при повторной загрузке)
        delete_document_vectors(filename)

        src_hash = file_hash(filepath)
        points = []
        seg_index = 0
        for chunk in chunks:
            headings = list(getattr(chunk.meta, "headings", None) or [])
            page_no = extract_page_no(chunk)

            # Посекционный чанкинг: делим по оформлению (заголовки docling), а где их
            # нет — по номерам пунктов («5.1»). Каждый пункт становится отдельной точкой
            # со своей меткой section — точнее и цитирование, и фильтрация в поиске.
            if headings:
                section = " / ".join(h for h in headings if h)
                segments = [(section, chunk.text, chunker.contextualize(chunk=chunk))]
            else:
                segments = [
                    (label, seg, f"[{label}] {seg}" if label else seg)
                    for label, seg in sectionize_by_clause(chunk.text)
                ]

            for section, raw_text, text_for_embedding in segments:
                if not (raw_text or "").strip():
                    continue
                vec = get_embedding(text_for_embedding)
                point_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{filename}-{src_hash}-{seg_index}"))
                points.append(PointStruct(
                    id=point_id,
                    vector=vec,
                    payload={
                        "text": text_for_embedding,
                        "raw_text": raw_text,
                        "source": filename,
                        "topic": slug,
                        "section": section,
                        "headings": headings,
                        "page": page_no,
                        "chunk_index": seg_index,
                        "length": len(text_for_embedding),
                    }
                ))
                seg_index += 1

        if not points:
            _update_registry(filename, status="error", error="После сегментации не осталось текста")
            return {"filename": filename, "status": "error", "error": "Нет чанков"}

        for start_i in range(0, len(points), UPSERT_BATCH):
            client.upsert(collection_name=COLLECTION_NAME, points=points[start_i:start_i + UPSERT_BATCH])

        elapsed = time.time() - start
        _update_registry(
            filename,
            status="indexed",
            chunks=len(points),
            error=None,
            indexed_in_seconds=round(elapsed, 2),
            topic=slug,
            topic_name=topic["name"],
            path=str(filepath.relative_to(DOCS_DIR)),
            markdown_path=str(md_path.relative_to(CONVERTED_DIR)),
        )
        _log("DONE", f"{filename}: {len(points)} чанков за {elapsed:.2f} сек, тема «{topic['name']}» ({slug})")
        return {"filename": filename, "status": "indexed", "chunks": len(points),
                "elapsed": elapsed, "topic": slug, "topic_name": topic["name"]}

    except Exception as e:
        _update_registry(filename, status="error", error=str(e))
        return {"filename": filename, "status": "error", "error": str(e)}


def search_chunks(query_text: str, topic_slugs: Optional[list] = None, limit: int = 6) -> list:
    """
    Поиск чанков с опциональным сужением до конкретных тем. Тем же приёмом, что и
    в диалоге (test_cascade.search), пользуется конструктор плана: сначала выбираем
    темы, потом ищем только внутри них.
    Возвращает [{"text", "source", "topic", "page", "score"}].
    """
    vector = get_embedding(query_text)
    query_filter = None
    if topic_slugs:
        query_filter = Filter(must=[FieldCondition(key="topic", match=MatchAny(any=list(topic_slugs)))])

    hits = client.query_points(
        collection_name=COLLECTION_NAME,
        query=vector,
        limit=limit,
        with_payload=True,
        query_filter=query_filter,
    ).points

    return [{
        "text": h.payload.get("text", ""),
        "source": h.payload.get("source"),
        "topic": h.payload.get("topic"),
        "section": h.payload.get("section"),
        "page": h.payload.get("page"),
        "score": h.score,
    } for h in hits]


def delete_document_vectors(filename: str):
    """Удаляет из Qdrant все точки данного документа (по полю source)."""
    client.delete(
        collection_name=COLLECTION_NAME,
        points_selector=Filter(
            must=[FieldCondition(key="source", match=MatchValue(value=filename))]
        ),
    )


def delete_document(filename: str, remove_file: bool = True) -> bool:
    """Полное удаление документа: векторы из Qdrant + оригинал + markdown-версия + кэш + запись в реестре."""
    delete_document_vectors(filename)

    with _registry_lock:
        reg = _load_registry()
        entry = reg.get(filename)

    if entry and remove_file:
        rel_path = entry.get("path", filename)
        filepath = DOCS_DIR / rel_path
        if filepath.exists():
            filepath.unlink()
            for cache_file in CACHE_DIR.glob(f"{filepath.stem}__*.json"):
                cache_file.unlink()
        md_rel = entry.get("markdown_path")
        if md_rel:
            md_path = CONVERTED_DIR / md_rel
            if md_path.exists():
                md_path.unlink()
        if entry.get("topic"):
            topics.bump_doc_count(entry["topic"], -1)

    with _registry_lock:
        reg = _load_registry()
        existed = reg.pop(filename, None) is not None
        _save_registry(reg)
    return existed


# ---------- Приём загруженного файла (используется веб-ручкой upload) ----------
def save_uploaded_file(filename: str, content: bytes) -> Path:
    filename = safe_filename(filename)
    ext = Path(filename).suffix.lower()
    if ext not in SUPPORTED_EXT:
        raise ValueError(f"Неподдерживаемый формат файла: {ext or '(нет расширения)'}")
    if len(content) > MAX_UPLOAD_BYTES:
        raise ValueError(f"Файл превышает лимит {MAX_UPLOAD_BYTES // (1024*1024)} МБ")

    # Загруженный файл сначала попадает в корень data/documents — тему определит
    # index_document() (автосортировка) и физически переложит в подпапку темы.
    filepath = DOCS_DIR / filename
    with open(filepath, "wb") as f:
        f.write(content)

    # ИСПРАВЛЕНО: убран дублирующий именованный аргумент 'filename'
    _update_registry(
        filename,
        size_bytes=len(content),
        uploaded_at=time.strftime("%Y-%m-%dT%H:%M:%S"),
        status="uploaded",
        chunks=0,
        topic=None,
        topic_name=None,
        error=None,
    )
    return filepath


# ---------- Фоновая очередь индексации (используется веб-ручкой upload) ----------
# docling-разбор тяжёлый (layout PDF — секунды-минуты), поэтому загрузка через сайт
# не ждёт обработку синхронно, а ставит файл в очередь. Один воркер обрабатывает файлы
# по очереди (FIFO): параллельный docling на нескольких файлах съел бы всю память.
# ponytail: один глобальный воркер; если понадобится пропускная способность —
# несколько воркеров + семафор под память.
_index_queue: "queue.Queue[str]" = queue.Queue()
_index_jobs: dict = {}
_index_jobs_lock = threading.Lock()
_worker_started = False
_worker_lock = threading.Lock()


def _set_index_job(job_id: str, **fields):
    with _index_jobs_lock:
        job = _index_jobs.setdefault(job_id, {"job_id": job_id})
        job.update(fields)
        return dict(job)


def get_index_job(job_id: str) -> Optional[dict]:
    with _index_jobs_lock:
        job = _index_jobs.get(job_id)
        return dict(job) if job else None


def queue_status() -> dict:
    with _index_jobs_lock:
        jobs = list(_index_jobs.values())
    queued = sum(1 for j in jobs if j.get("status") == "queued")
    running = sum(1 for j in jobs if j.get("status") == "processing")
    return {"queued": queued, "running": running}


def _index_worker():
    while True:
        job_id = _index_queue.get()
        try:
            job = get_index_job(job_id)
            if not job:
                continue
            filepath = Path(job["filepath"])
            _set_index_job(job_id, status="processing", started_at=time.strftime("%Y-%m-%dT%H:%M:%S"))
            _update_registry(filepath.name, status="processing", error=None)
            result = index_document(filepath)
            _set_index_job(job_id, status=result["status"], result=result,
                           finished_at=time.strftime("%Y-%m-%dT%H:%M:%S"))
        except Exception as e:
            _set_index_job(job_id, status="error", result={"status": "error", "error": str(e)})
        finally:
            _index_queue.task_done()


def _ensure_worker():
    global _worker_started
    with _worker_lock:
        if _worker_started:
            return
        threading.Thread(target=_index_worker, name="index-worker", daemon=True).start()
        _worker_started = True


def enqueue_document(filepath: Path) -> dict:
    """Ставит уже сохранённый файл в очередь на индексацию. Возвращает запись задачи."""
    _ensure_worker()
    job_id = str(uuid.uuid4())
    filename = Path(filepath).name
    _set_index_job(job_id, filename=filename, filepath=str(filepath),
                   status="queued", queued_at=time.strftime("%Y-%m-%dT%H:%M:%S"),
                   position=_index_queue.qsize() + 1)
    _index_queue.put(job_id)
    return get_index_job(job_id)


# ---------- Массовая индексация папки (используется CLI-скриптом) ----------
def index_all_documents(docs_dir: Optional[Path] = None, recreate: bool = False):
    docs_dir = Path(docs_dir) if docs_dir else DOCS_DIR
    create_collection(recreate=recreate)

    # Рекурсивно: файлы и в корне (ещё не отсортированы), и уже разложенные по
    # подпапкам тем (вручную или из прошлого запуска) — index_document() сам
    # разбирается, что с чем делать.
    files = sorted(
        p for p in docs_dir.rglob("*")
        if p.is_file() and p.suffix.lower() in SUPPORTED_EXT and CACHE_DIR not in p.parents
    )
    if not files:
        print(f"В папке {docs_dir} не найдено поддерживаемых файлов ({', '.join(sorted(SUPPORTED_EXT))}).")
        return

    for filepath in files:
        if filepath.name not in _load_registry():
            _update_registry(
                filepath.name,
                filename=filepath.name,
                size_bytes=filepath.stat().st_size,
                uploaded_at=time.strftime("%Y-%m-%dT%H:%M:%S"),
                status="uploaded",
                chunks=0,
                topic=None,
                topic_name=None,
                error=None,
            )
        print(f"\n=== Обработка файла: {filepath.name} ===")
        result = index_document(filepath)
        if result["status"] == "indexed":
            print(f"  Загружено {result['chunks']} чанков за {result['elapsed']:.2f} сек -> тема «{result['topic_name']}»")
        else:
            print(f"  ОШИБКА: {result.get('error')}")

    print("\nИндексация завершена.")
    print("\nТемы в базе:")
    for t in topics.list_topics():
        print(f"  - {t['name']} ({t['slug']}): {t.get('doc_count', 0)} документ(ов)")   
