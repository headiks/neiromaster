import requests
import json
import re
import time

import topics

# ---------- Конфигурация ----------
OLLAMA = "http://localhost:8080"
QDRANT = "http://localhost:6333"
COLLECTION = "reglaments"
SMALL_MODEL = "qwen2.5:3b"      # для скорости, но можно поставить 7b для точности
BIG_MODEL = "qwen3:14b"
CONFIDENCE_THRESHOLD = 0.55
MAX_CONTEXT_FRAGMENTS = 3
HISTORY_WINDOW = 3   # сколько последних вопросов пользователя учитывать при разрешении контекста
DEBUG = True

EMBED_TIMEOUT = 30
SMALL_LLM_TIMEOUT = 60
BIG_LLM_TIMEOUT = 90
QDRANT_TIMEOUT = 15

# ---------- Быстрый префильтр для общих фраз и ключевых слов ----------
GREETING_PHRASES = [
    "привет", "здравствуй", "здравствуйте", "добрый день",
    "доброе утро", "добрый вечер", "как дела", "как жизнь",
    "спасибо", "благодарю", "ок", "хорошо", "понял", "да", "нет"
]

# Ключевые слова, которые однозначно указывают на RAG-запрос
RAG_KEYWORDS = [
    "отпуск", "высота", "инструктаж", "техника безопасности", "охрана труда",
    "смена", "регламент", "правила", "норма", "требование", "обязан",
    "положено", "разрешается", "запрещается", "инструкция", "порядок"
]

def is_greeting_or_general(text):
    text_lower = text.lower().strip()
    if '?' in text_lower:
        return False
    question_starters = ["что", "как", "где", "когда", "почему", "зачем", "сколько", "кто", "какой"]
    if any(text_lower.startswith(w) for w in question_starters):
        return False
    words = text_lower.split()
    if len(words) <= 4:
        for phrase in GREETING_PHRASES:
            if phrase in text_lower:
                return True
    return False

def has_rag_keywords(text):
    text_lower = text.lower()
    for kw in RAG_KEYWORDS:
        if kw in text_lower:
            return True
    return False

# ---------- Вспомогательные функции ----------
def log(step, msg, data=None):
    print(f"[{step}] {msg}")
    if data is not None:
        print(f"    {data}")

def embed(text):
    log("EMBED", f"Запрос эмбеддинга для текста: {text[:50]}...")
    start = time.time()
    r = requests.post(f"{OLLAMA}/api/embed", json={"model": "bge-m3", "input": text}, timeout=EMBED_TIMEOUT)
    r.raise_for_status()
    result = r.json()["embeddings"][0]
    log("EMBED", f"Эмбеддинг получен за {time.time()-start:.3f} сек, размерность {len(result)}")
    return result

def small_llm(system, user, step_name="SMALL_LLM"):
    log(step_name, f"Запрос к малой модели:\n  system={system[:80]}...\n  user={user[:80]}...")
    start = time.time()
    r = requests.post(f"{OLLAMA}/api/chat", json={
        "model": SMALL_MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "stream": False,
    }, timeout=SMALL_LLM_TIMEOUT)
    r.raise_for_status()
    response = r.json()["message"]["content"]
    elapsed = time.time() - start
    log(step_name, f"Ответ получен за {elapsed:.3f} сек, длина {len(response)} символов")
    log(step_name, f"Сырой ответ: {response}")
    return response

def big_llm(system, user):
    log("BIG_LLM", f"Запрос к большой модели:\n  system={system[:80]}...\n  user={user[:80]}...")
    start = time.time()
    r = requests.post(f"{OLLAMA}/api/chat", json={
        "model": BIG_MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "stream": False,
        "think": False,
        "options": {"num_keep": 0}
    }, timeout=BIG_LLM_TIMEOUT)
    r.raise_for_status()
    response = r.json()["message"]["content"]
    elapsed = time.time() - start
    log("BIG_LLM", f"Ответ получен за {elapsed:.3f} сек, длина {len(response)} символов")
    log("BIG_LLM", f"Сырой ответ: {response}")
    return response

def parse_json_response(text):
    log("PARSE", f"Попытка извлечь JSON из: {text[:100]}...")
    text = re.sub(r'```json\s*', '', text)
    text = re.sub(r'```', '', text)
    start = text.find('{')
    end = text.rfind('}')
    if start == -1 or end == -1:
        raise ValueError("В ответе нет JSON-объекта")
    json_str = text[start:end+1]
    try:
        parsed = json.loads(json_str)
        log("PARSE", f"JSON успешно распарсен: {parsed}")
        return parsed
    except json.JSONDecodeError as e:
        log("PARSE", f"Ошибка парсинга JSON: {e}")
        raise

# ---------- Анализ истории диалога (разрешение контекстных вопросов) ----------
# Если пользователь до этого спрашивал про "работу на высоте" и "какая экипировка нужна",
# а затем задаёт короткий обрывочный вопрос "Где взять" — сам по себе он ни классификатору,
# ни поиску, ни генератору ответа не даст ничего осмысленного. Этот блок смотрит на последние
# HISTORY_WINDOW вопросов пользователя и, если текущий вопрос зависит от контекста,
# переписывает его в полный самостоятельный вопрос ДО того, как он попадёт в classify/search/rerank.
CONTEXTUALIZE_SYSTEM = """
Ты — модуль анализа истории диалога. Твоя задача — понять, ссылается ли текущий вопрос
пользователя на тему предыдущих вопросов, и если да — переписать его в полный самостоятельный вопрос.

Тебе дана история последних вопросов пользователя (от старых к новым) и текущий вопрос.

Правила:
1. Если текущий вопрос уже полный и понятен сам по себе, без истории — верни его БЕЗ ИЗМЕНЕНИЙ
   и "depends_on_context": false.
2. Если текущий вопрос короткий, обрывочный, содержит местоимения ("это", "туда", "он", "их")
   или явно продолжает тему предыдущих вопросов (например "где взять", "а сколько", "почему",
   "а если нет") — перепиши его в полный вопрос, подставив недостающую тему из истории,
   и укажи "depends_on_context": true.
3. Не придумывай фактов, которых не было в истории — только соединяй текущий вопрос с темой
   из истории, не добавляя ничего лишнего.
4. Если история не связана с текущим вопросом по смыслу — верни вопрос без изменений
   и "depends_on_context": false.

Верни ТОЛЬКО JSON: {"standalone_question": "...", "depends_on_context": true/false}. Без пояснений.

Примеры:

История:
- Что нужно для работы на высоте?
- Какие требования к страховочной привязи?
- Какая экипировка нужна для работы на высоте?
Текущий вопрос: "Где взять"
{"standalone_question": "Где взять экипировку для работы на высоте?", "depends_on_context": true}

История:
- Сколько дней отпуска положено?
Текущий вопрос: "А дополнительный?"
{"standalone_question": "Сколько дней дополнительного отпуска положено?", "depends_on_context": true}

История: (пусто или не по теме текущего вопроса)
Текущий вопрос: "Сколько огнетушителей должно быть на складе?"
{"standalone_question": "Сколько огнетушителей должно быть на складе?", "depends_on_context": false}
"""

def resolve_question(question, history):
    """
    history — список последних реплик пользователя вида [{"question": "...", "answer": "..."}, ...]
    от старых к новым (обычно уже обрезан вызывающей стороной до HISTORY_WINDOW).
    """
    if not history:
        log("CONTEXTUALIZE", "История пуста, используем вопрос как есть")
        return {"standalone_question": question, "context_used": False}

    # Явные приветствия/благодарности не нуждаются в переформулировке — не тратим вызов LLM
    if is_greeting_or_general(question):
        log("CONTEXTUALIZE", "Вопрос — приветствие/общая фраза, пропускаем анализ истории")
        return {"standalone_question": question, "context_used": False}

    # Достаточно длинный вопрос с предметными ключевыми словами уже самодостаточен
    if has_rag_keywords(question) and len(question.split()) >= 5:
        log("CONTEXTUALIZE", "Вопрос уже самодостаточен (есть ключевые слова и длина), пропускаем анализ истории")
        return {"standalone_question": question, "context_used": False}

    recent = history[-HISTORY_WINDOW:]
    history_lines = "\n".join(f"- {h['question']}" for h in recent if h.get("question"))
    prompt = f'История последних вопросов пользователя:\n{history_lines}\n\nТекущий вопрос: "{question}"'
    log("CONTEXTUALIZE", f"Анализ вопроса с учётом {len(recent)} предыдущих вопросов")

    raw = small_llm(CONTEXTUALIZE_SYSTEM, prompt, step_name="CONTEXTUALIZE")
    try:
        data = parse_json_response(raw)
        standalone = (data.get("standalone_question") or question).strip() or question
        depends = bool(data.get("depends_on_context", False))
        if depends:
            log("CONTEXTUALIZE", f"Вопрос переформулирован с учётом контекста: '{question}' -> '{standalone}'")
        else:
            log("CONTEXTUALIZE", "Вопрос признан самостоятельным, контекст не использован")
        return {"standalone_question": standalone, "context_used": depends}
    except Exception as e:
        log("CONTEXTUALIZE", f"Ошибка разбора ответа, используем исходный вопрос. Ошибка: {e}")
        return {"standalone_question": question, "context_used": False}

# ---------- Классификация (усиленная) ----------
CLASSIFY_SYSTEM = """
Ты — модуль маршрутизации. Определи, к какому маршруту отнести вопрос пользователя.

Маршруты:
- "rag" — любые вопросы о правилах, регламентах, инструкциях, нормах, процедурах, условиях работы, льготах, отпусках, технике безопасности, охране труда.
  Вопросы часто начинаются с: что, как, где, когда, почему, зачем, сколько, какой, какие, нужно ли, обязан ли, можно ли, разрешено ли.
- "general" — только короткие приветствия, прощания, благодарности без вопросительного смысла. Пример: "привет", "спасибо", "ок".
- "escalate" — если речь о травме, угрозе, насилии, конфликте, плохом самочувствии (требуется вмешательство человека).

Верни ТОЛЬКО JSON с полями: "route" (одно из значений), "risk_flag" (bool, true только для escalate), "risk_type" (строка или null).

Примеры:
{"route": "rag", "risk_flag": false, "risk_type": null}          # для "Сколько дней отпуска?"
{"route": "rag", "risk_flag": false, "risk_type": null}          # для "Что делать в начале смены?"
{"route": "general", "risk_flag": false, "risk_type": null}      # для "Привет"
{"route": "escalate", "risk_flag": true, "risk_type": "конфликт"} # для "Меня ударили"

Не добавляй пояснений, только JSON.
"""

def classify(question):
    log("CLASSIFY", f"Классификация вопроса: {question}")
    # Быстрый префильтр для приветствий
    if is_greeting_or_general(question):
        log("CLASSIFY", "Быстрый префильтр: general")
        return {"route": "general", "risk_flag": False, "risk_type": None}
    # Если есть ключевые слова RAG — сразу rag, минуя LLM (экономит время)
    if has_rag_keywords(question):
        log("CLASSIFY", "Быстрый префильтр: найдены ключевые слова RAG, маршрут rag")
        return {"route": "rag", "risk_flag": False, "risk_type": None}

    raw = small_llm(CLASSIFY_SYSTEM, question, step_name="CLASSIFY")
    try:
        data = parse_json_response(raw)
        result = {
            "route": data.get("route", "rag"),
            "risk_flag": data.get("risk_flag", False),
            "risk_type": data.get("risk_type"),
        }
        log("CLASSIFY", f"Результат: {result}")
        return result
    except Exception as e:
        log("CLASSIFY", f"Ошибка, возвращаем rag. Ошибка: {e}")
        return {"route": "rag", "risk_flag": False, "risk_type": None}

# ---------- Поиск в Qdrant ----------
def search(question, limit=3, topic_slugs=None):
    log("SEARCH", f"Поиск в Qdrant для вопроса: {question}, лимит {limit}, темы: {topic_slugs or '(все)'}")
    start = time.time()
    vector = embed(question)
    payload = {"query": vector, "limit": limit, "with_payload": True}
    # Если тема(ы) определены — ищем ТОЛЬКО среди чанков этих папок, а не по всей базе.
    # Это и есть суть маршрутизации: не отдаём модели весь корпус документов,
    # а только те, что физически лежат в нужной теме.
    if topic_slugs:
        payload["filter"] = {"must": [{"key": "topic", "match": {"any": topic_slugs}}]}
    log("SEARCH", f"Отправка запроса в Qdrant: {payload}")
    r = requests.post(f"{QDRANT}/collections/{COLLECTION}/points/query", json=payload, timeout=QDRANT_TIMEOUT)
    r.raise_for_status()
    response = r.json()
    points = response["result"]["points"]
    elapsed = time.time() - start
    log("SEARCH", f"Найдено {len(points)} кандидатов за {elapsed:.3f} сек")
    for idx, p in enumerate(points):
        log("SEARCH", f"Кандидат {idx+1}: score={p['score']:.3f}, тема={p['payload'].get('topic')}, текст: {p['payload']['text'][:80]}...")
    return points

# ---------- Реранжирование (максимально усиленный промпт) ----------
RERANK_SYSTEM = """
Ты — эксперт по оценке релевантности текстовых фрагментов.

Твоя задача: оценить, насколько данный фрагмент документа соответствует вопросу пользователя.
Оценка должна быть числом от 0.0 до 1.0, где:
- 1.0 — фрагмент полностью и точно отвечает на вопрос, содержит прямую информацию.
- 0.8–0.9 — фрагмент очень релевантен, но не даёт полного ответа.
- 0.5–0.7 — фрагмент частично релевантен, содержит смежную информацию.
- 0.1–0.4 — слабая связь, упоминаются похожие термины, но не по делу.
- 0.0 — совершенно не релевантно, нет никакой связи.

Примеры:
Вопрос: "Сколько дней отпуска положено?"
Фрагмент: "Сотруднику положен отпуск 28 календарных дней в год."
Оценка: 1.0

Вопрос: "Что надеть для работы на высоте?"
Фрагмент: "Работа на высоте разрешена только при наличии страховочного пояса и каски."
Оценка: 1.0

Вопрос: "Что надеть для работы на высоте?"
Фрагмент: "Перед началом смены необходимо пройти инструктаж по технике безопасности."
Оценка: 0.1

Вопрос: "Сколько дней отпуска?"
Фрагмент: "Работа на высоте требует страховки."
Оценка: 0.0

Теперь твоя очередь. Верни ТОЛЬКО JSON с одним полем "relevance", например: {"relevance": 0.95}.
Никаких пояснений, только JSON.
"""

def rerank(question, candidates):
    log("RERANK", f"Реранжирование {len(candidates)} кандидатов для вопроса: {question}")
    scored = []
    for idx, c in enumerate(candidates):
        fragment = c["payload"]["text"]
        prompt = f'Вопрос: "{question}"\nФрагмент: "{fragment}"'
        raw = small_llm(RERANK_SYSTEM, prompt, step_name=f"RERANK_{idx+1}")
        relevance = None
        # Пытаемся извлечь JSON
        try:
            data = parse_json_response(raw)
            if isinstance(data, dict) and "relevance" in data:
                relevance = float(data["relevance"])
        except Exception:
            pass
        # Если JSON не удался, ищем число через regex
        if relevance is None:
            numbers = re.findall(r'(\d+\.?\d*)', raw)
            if numbers:
                try:
                    val = float(numbers[0])
                    if 0.0 <= val <= 1.0:
                        relevance = val
                    elif val > 1 and val <= 100:
                        relevance = val / 100.0
                    else:
                        relevance = None
                except:
                    pass
        # Если всё равно None, используем векторный скор как fallback (но только если он > 0.5)
        if relevance is None:
            vector_score = c["score"]
            if vector_score >= 0.5:
                relevance = vector_score * 0.9  # чуть занижаем, чтобы не переоценить
                log("RERANK", f"Fallback: использован векторный скор {vector_score} -> {relevance:.3f}")
            else:
                relevance = 0.0
        # Ограничиваем
        relevance = max(0.0, min(1.0, relevance))
        scored.append({
            "text": fragment,
            "relevance": relevance,
            "vector_score": c["score"],
        })
        log("RERANK", f"Кандидат {idx+1}: релевантность={relevance:.3f}, векторный скор={c['score']:.3f}")
    sorted_scored = sorted(scored, key=lambda x: x["relevance"], reverse=True)
    log("RERANK", f"Результат реранжирования (отсортировано): {[(s['relevance'], s['text'][:40]) for s in sorted_scored]}")
    return sorted_scored

# ---------- Генерация ----------
GENERATE_SYSTEM = """
Ты — ассистент по внутренним регламентам завода.
Отвечай на вопрос сотрудника, используя только предоставленный контекст.
Если в контексте нет информации — скажи, что не знаешь, и предложи обратиться к специалисту.
Отвечай кратко, по делу, на русском языке.
Не рассуждай, не выводи свои мысли — дай только готовый ответ.
"""

def generate_answer(question, context_fragments):
    log("GENERATE", f"Генерация ответа с использованием {len(context_fragments)} фрагментов")
    context_text = "\n\n".join([f"--- Фрагмент {i+1} ---\n{frag}" for i, frag in enumerate(context_fragments)])
    user_prompt = f"Вопрос: {question}\n\nКонтекст:\n{context_text}"
    log("GENERATE", f"Сформирован промпт для большой модели:\n{user_prompt}")
    answer = big_llm(GENERATE_SYSTEM, user_prompt)
    log("GENERATE", f"Сгенерированный ответ: {answer}")
    return answer

# ---------- Основная функция ----------
def handle_question(question, history=None):
    """
    history — список предыдущих реплик текущего диалога вида
    [{"question": "...", "answer": "..."}, ...] от старых к новым.
    Обычно передаётся вызывающей стороной (app.py) уже обрезанным до последних
    HISTORY_WINDOW вопросов, но на всякий случай обрезаем и здесь.
    """
    log("START", f"Обработка вопроса: {question}")
    total_start = time.time()
    history = (history or [])[-HISTORY_WINDOW:]

    # Разрешаем зависимость от контекста ДО классификации/поиска — короткие вопросы
    # вроде "Где взять" сами по себе не несут смысла для векторного поиска.
    resolved = resolve_question(question, history)
    effective_question = resolved["standalone_question"]
    context_used = resolved["context_used"]

    route_info = classify(effective_question)
    route = route_info["route"]
    log("HANDLE", f"Маршрут: {route}")

    # Маршрутизация по темам — только для route == "rag" имеет смысл (экономим
    # вызов, если вопрос всё равно не пойдёт в поиск по документам).
    matched_topics = []
    if route == "rag":
        matched_topics = topics.route_question(effective_question)

    base_result = {
        "question": question,
        "resolved_question": effective_question if context_used else None,
        "context_used": context_used,
        "classification": route_info,
        "route": route,
        "topics_used": matched_topics,
    }

    if route == "general":
        return {
            **base_result,
            "candidates": [],
            "top_fragments": [],
            "answer": "Здравствуйте! Чем я могу вам помочь по вопросам регламентов и охраны труда?",
            "elapsed_time": time.time() - total_start,
            "error": None
        }

    if route != "rag":
        return {
            **base_result,
            "candidates": [],
            "top_fragments": [],
            "answer": None,
            "elapsed_time": time.time() - total_start,
            "error": None
        }

    candidates = search(effective_question, topic_slugs=matched_topics)
    if not candidates and matched_topics:
        # Тема была выбрана неуверенно/ошибочно — не отдаём пользователю пустой
        # ответ только из-за неверной папки, ищем по всей базе как fallback.
        log("HANDLE", "В выбранных темах пусто, повторяем поиск без фильтра по темам")
        candidates = search(effective_question, topic_slugs=None)
    if not candidates:
        return {
            **base_result,
            "candidates": [],
            "top_fragments": [],
            "answer": None,
            "elapsed_time": time.time() - total_start,
            "error": "Нет кандидатов в Qdrant"
        }

    ranked = rerank(effective_question, candidates)
    top = [r for r in ranked if r["relevance"] >= CONFIDENCE_THRESHOLD]
    top_fragments = [r["text"] for r in top[:MAX_CONTEXT_FRAGMENTS]]

    answer = None
    if top_fragments:
        answer = generate_answer(effective_question, top_fragments)
    else:
        log("HANDLE", "Confidence gate не пройден")

    return {
        **base_result,
        "candidates": ranked,
        "top_fragments": top_fragments,
        "answer": answer,
        "elapsed_time": time.time() - total_start,
        "error": None
    }

# ---------- Тест ----------
if __name__ == "__main__":
    # Имитация диалога: три вопроса по теме "работа на высоте", затем обрывочный
    # четвёртый вопрос, который должен быть разрешён через историю.
    dialogue = [
        "Что нужно для работы на высоте?",
        "Какие требования к страховочной привязи?",
        "Какая экипировка нужна для работы на высоте?",
        "Где взять",
    ]
    history = []
    for q in dialogue:
        result = handle_question(q, history=history)
        print(f"\n=== Вопрос: {q!r} ===")
        if result.get("context_used"):
            print(f"    Разрешено как: {result['resolved_question']!r}")
        print(f"    Маршрут: {result['route']}")
        print(f"    Ответ: {result.get('answer')}")
        history.append({"question": q, "answer": result.get("answer")})