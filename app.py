import time
import json
import uuid
import threading
from collections import defaultdict, deque
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, UploadFile, File, Depends, Request, Response
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse, RedirectResponse
from pydantic import BaseModel
import uvicorn

from test_cascade import handle_question, HISTORY_WINDOW
import indexing
import planner
import auth
import users
import questions
import employees as adaptation
from indexing import DOCS_DIR

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"

app = FastAPI(title="RAG Assistant API")
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


class QuestionRequest(BaseModel):
    question: str
    session_id: str | None = None   # если не передан, сервер создаст новый


class QuestionResponse(BaseModel):
    question: str
    resolved_question: str | None = None   # вопрос, переформулированный с учётом истории (если применимо)
    context_used: bool = False             # был ли использован контекст предыдущих вопросов
    session_id: str
    classification: dict
    route: str
    candidates: list
    top_fragments: list
    answer: str | None = None
    escalated: bool = False                 # вопрос без ответа передан администратору
    elapsed_time: float
    error: str | None = None


# ---------- Память диалога по сессиям (in-memory) ----------
# Хранит последние вопросы/ответы для каждого session_id — используется, чтобы
# handle_question мог разрешать контекстные вопросы вроде "Где взять".
# Живёт только в памяти текущего процесса: подходит для одного uvicorn-воркера;
# для многопроцессного/многосерверного деплоя нужно вынести в Redis или аналог.
HISTORY_MAX_STORE = 20  # сколько реплик хранить на сессию (окно анализа контекста меньше — HISTORY_WINDOW)

_history_lock = threading.Lock()
_conversation_history: dict[str, deque] = defaultdict(lambda: deque(maxlen=HISTORY_MAX_STORE))


def get_recent_history(session_id: str, n: int = HISTORY_WINDOW) -> list:
    with _history_lock:
        hist = list(_conversation_history.get(session_id, []))
    return hist[-n:]


def append_history(session_id: str, question: str, answer: str | None):
    with _history_lock:
        _conversation_history[session_id].append({"question": question, "answer": answer})


@app.on_event("startup")
async def on_startup():
    # Коллекция должна существовать до первого /ask или первой загрузки файла
    indexing.create_collection(recreate=False)
    # Стартовый набор смысловых папок — «словарь тем», от которого отталкивается
    # LLM-классификатор, чтобы не плодить синонимичные папки

    moved = users.migrate_legacy_employees()
    if moved:
        print(f"Перенесено записей сотрудников из employees.json в users.json: {moved}")

    initial = users.ensure_owner()
    if initial:
        print("=" * 70)
        print("Создана учётная запись главного администратора.")
        print(f"  Логин:  {initial['username']}")
        print(f"  Пароль: {initial['password']}")
        print(f"  Дубль записан в {users.INITIAL_CREDENTIALS_PATH}")
        print("  При первом входе система попросит задать свои логин и пароль.")
        print("=" * 70)


# ---------- Доступ ----------
def current_user(request: Request) -> dict:
    """Любой вошедший пользователь. Без валидной сессии — 401."""
    user = auth.get_session_user(request.cookies.get(auth.COOKIE_NAME))
    if user is None:
        raise HTTPException(status_code=401, detail="Требуется вход")
    return user


def require_setup_done(user: dict = Depends(current_user)) -> dict:
    """
    Пока главный администратор не задал свои логин и пароль, дальше первичной
    настройки его не пускаем — иначе сгенерированные данные так и останутся жить.
    """
    if user.get("must_change_credentials"):
        raise HTTPException(status_code=403, detail="Сначала задайте свои логин и пароль")
    return user


def require_admin(user: dict = Depends(require_setup_done)) -> dict:
    if not users.is_admin(user):
        raise HTTPException(status_code=403, detail="Доступ только для администраторов")
    return user


def require_owner(user: dict = Depends(require_setup_done)) -> dict:
    if not users.is_owner(user):
        raise HTTPException(status_code=403,
                            detail="Это может сделать только главный администратор")
    return user


logged_in = [Depends(require_setup_done)]
admin_only = [Depends(require_admin)]
owner_only = [Depends(require_owner)]


class LoginRequest(BaseModel):
    username: str
    password: str


class RegisterRequest(BaseModel):
    username: str
    password: str
    full_name: str
    position: str | None = None
    contact: str | None = None


class CredentialsRequest(BaseModel):
    username: str
    password: str


class PasswordChangeRequest(BaseModel):
    old_password: str
    new_password: str


def _read_static(name: str) -> str:
    with open(STATIC_DIR / name, "r", encoding="utf-8") as f:
        return f.read()


def _set_session_cookie(response: Response, token: str):
    response.set_cookie(
        auth.COOKIE_NAME, token,
        httponly=True,          # кука недоступна из JS — защита от кражи через XSS
        samesite="lax",         # браузер не пришлёт её при кросс-сайтовых POST — базовая защита от CSRF
        max_age=auth.SESSION_TTL,
        path="/",
    )


@app.get("/", response_class=HTMLResponse)
async def root(request: Request):
    """Личный кабинет сотрудника: чат с ассистентом и свой план адаптации."""
    user = auth.get_session_user(request.cookies.get(auth.COOKIE_NAME))
    if user is None:
        return RedirectResponse(url="/login", status_code=303)
    if user.get("must_change_credentials"):
        return RedirectResponse(url="/setup", status_code=303)
    return HTMLResponse(_read_static("index.html"))


@app.get("/login", response_class=HTMLResponse)
async def login_page():
    return _read_static("login.html")


@app.get("/register", response_class=HTMLResponse)
async def register_page():
    return _read_static("register.html")


@app.get("/setup", response_class=HTMLResponse)
async def setup_page(request: Request):
    """Первичная настройка: замена выданных логина и пароля своими."""
    user = auth.get_session_user(request.cookies.get(auth.COOKIE_NAME))
    if user is None:
        return RedirectResponse(url="/login", status_code=303)
    if not user.get("must_change_credentials"):
        return RedirectResponse(url="/admin" if users.is_admin(user) else "/", status_code=303)
    return HTMLResponse(_read_static("setup.html"))


@app.get("/admin", response_class=HTMLResponse)
async def admin_page(request: Request):
    """Админка: база знаний, конструктор плана, пользователи."""
    user = auth.get_session_user(request.cookies.get(auth.COOKIE_NAME))
    if user is None:
        return RedirectResponse(url="/login", status_code=303)
    if user.get("must_change_credentials"):
        return RedirectResponse(url="/setup", status_code=303)
    if not users.is_admin(user):
        return RedirectResponse(url="/", status_code=303)
    return HTMLResponse(_read_static("admin.html"))


# ---------- Вход, регистрация, свой профиль ----------
@app.post("/api/login")
async def api_login(req: LoginRequest, request: Request, response: Response):
    client = request.client.host if request.client else ""
    try:
        token, user = auth.login(req.username, req.password, client=client)
    except ValueError as e:
        raise HTTPException(status_code=401, detail=str(e))
    _set_session_cookie(response, token)
    return {
        "username": user["username"],
        "role": user["role"],
        "must_change_credentials": bool(user.get("must_change_credentials")),
    }


@app.post("/api/register")
async def api_register(req: RegisterRequest):
    """Самостоятельная регистрация сотрудника."""
    try:
        user = users.register_employee(req.username, req.password, req.full_name,
                                       position=req.position or "", contact=req.contact or "")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {
        "id": user["id"],
        "username": user["username"],
        "active": user["active"],
        "needs_approval": not user["active"],
    }


@app.post("/api/logout")
async def api_logout(request: Request, response: Response):
    auth.logout(request.cookies.get(auth.COOKIE_NAME))
    response.delete_cookie(auth.COOKIE_NAME, path="/")
    return {"logged_out": True}


@app.get("/api/me")
async def api_me(user: dict = Depends(current_user)):
    return users.public_view(user)


@app.post("/api/setup-credentials")
async def api_setup_credentials(req: CredentialsRequest, response: Response,
                                user: dict = Depends(current_user)):
    """
    Первичная настройка: пользователь заменяет выданные логин и пароль своими.
    Доступна только тем, у кого стоит флаг must_change_credentials.
    """
    if not user.get("must_change_credentials"):
        raise HTTPException(status_code=400, detail="Учётные данные уже настроены")
    try:
        users.set_credentials(user["id"], req.username, req.password)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    # Логин сменился — старые сессии больше не действуют
    auth.drop_user_sessions(user["id"])
    response.delete_cookie(auth.COOKIE_NAME, path="/")
    return {"changed": True}


@app.post("/api/password")
async def api_change_password(req: PasswordChangeRequest, response: Response,
                              user: dict = Depends(current_user)):
    try:
        auth.change_own_password(user, req.old_password, req.new_password)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    # Смена пароля разлогинивает все сессии, включая текущую
    response.delete_cookie(auth.COOKIE_NAME, path="/")
    return {"changed": True}


@app.get("/api/my/schedule", dependencies=logged_in)
async def api_my_schedule(user: dict = Depends(require_setup_done)):
    """Свой план адаптации — то, что сотрудник видит в личном кабинете."""
    try:
        return adaptation.build_employee_schedule(user)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@app.get("/api/my/questions", dependencies=logged_in)
async def api_my_questions(user: dict = Depends(require_setup_done)):
    """Свои эскалированные вопросы и ответы на них от администратора."""
    return {"questions": questions.list_for_user(user["id"])}


# ---------- RAG-вопросы ----------
ESCALATE_REPLY = ("⚠️ Вопрос требует внимания специалиста — передал его ответственному. "
                  "Ответ придёт в личный кабинет.")
NO_ANSWER_REPLY = ("В регламентах точного ответа не нашлось — передал вопрос ответственному. "
                   "Ответ придёт в личный кабинет.")


def _route_to_human(result: dict, user: dict) -> dict:
    """
    Вопрос без ответа не теряем: ставим в очередь администратору и показываем
    сотруднику понятное сообщение вместо пустого ответа/технической ошибки.
    Срабатывает для escalate (ЧС) и для rag без найденного ответа.
    """
    if result.get("answer") or result.get("route") not in ("rag", "escalate"):
        return result
    cls = result.get("classification") or {}
    reason = questions.REASON_ESCALATE if (result["route"] == "escalate" or cls.get("risk_flag")) \
        else questions.REASON_NO_ANSWER
    questions.record(user, result["question"], result.get("resolved_question"),
                     reason, cls.get("risk_type"))
    result["escalated"] = True
    result["error"] = None  # «нет кандидатов» — не ошибка для пользователя, это эскалация
    result["answer"] = ESCALATE_REPLY if reason == questions.REASON_ESCALATE else NO_ANSWER_REPLY
    return result


@app.post("/ask", response_model=QuestionResponse, dependencies=logged_in)
async def ask(req: QuestionRequest, user: dict = Depends(require_setup_done)):
    start = time.time()
    question = req.question.strip()
    if not question:
        raise HTTPException(status_code=400, detail="Вопрос не может быть пустым")

    # session_id связывает подряд идущие вопросы в один диалог. Фронтенд генерирует
    # его один раз на вкладку и присылает с каждым запросом; если его нет — заводим новый.
    session_id = req.session_id or str(uuid.uuid4())
    history = get_recent_history(session_id)

    try:
        result = handle_question(question, history=history)
        result = _route_to_human(result, user)
        result["elapsed_time"] = time.time() - start
        result["session_id"] = session_id
        append_history(session_id, question, result.get("answer"))
        return QuestionResponse(**result)
    except Exception as e:
        return QuestionResponse(
            question=question,
            session_id=session_id,
            classification={},
            route="error",
            candidates=[],
            top_fragments=[],
            answer=None,
            elapsed_time=time.time() - start,
            error=str(e)
        )


@app.delete("/session/{session_id}", dependencies=logged_in)
async def reset_session(session_id: str):
    """Очищает историю диалога для сессии (например, при нажатии «Новый диалог» на сайте)."""
    with _history_lock:
        existed = session_id in _conversation_history
        _conversation_history.pop(session_id, None)
    return {"session_id": session_id, "cleared": existed}


# ---------- Управление документами ----------
@app.get("/documents", dependencies=admin_only)
async def get_documents():
    """Список документов в базе с их статусом индексации (загружен / обрабатывается / готов / ошибка)."""
    return {"documents": indexing.list_documents()}


@app.post("/documents/upload", dependencies=admin_only)
async def upload_document(file: UploadFile = File(...)):
    """
    Загрузка нового регламента. Файл сохраняется в data/documents/ и ставится
    в фоновую очередь на индексацию (docling -> чанкинг -> эмбеддинги -> Qdrant).
    Ответ приходит сразу (202) с идентификатором задачи; прогресс —
    через GET /documents/jobs/{job_id}. Тяжёлый разбор PDF не держит запрос.
    """
    content = await file.read()
    try:
        filepath = indexing.save_uploaded_file(file.filename, content)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    job = indexing.enqueue_document(filepath)
    return JSONResponse(status_code=202, content=job)


@app.get("/documents/jobs/{job_id}", dependencies=admin_only)
async def get_document_job(job_id: str):
    """Статус фоновой индексации загруженного документа."""
    job = indexing.get_index_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Задача не найдена")
    return job


@app.delete("/documents/{filename}", dependencies=admin_only)
async def remove_document(filename: str):
    """Удаляет документ: векторы из Qdrant, оригинал из data/documents, кэш docling."""
    existed = indexing.delete_document(filename)
    if not existed:
        raise HTTPException(status_code=404, detail="Документ не найден")
    return {"filename": filename, "deleted": True}


@app.get("/folders", dependencies=admin_only)
async def get_folders():
    """
    Смысловые папки базы знаний с числом документов. Папки создаёт LLM при
    загрузке документа — вручную их никто не заводит.
    """
    return {"folders": indexing.folder_stats()}


# ---------- Вопросы без ответа (эскалация человеку) ----------
class ResolveRequest(BaseModel):
    answer: str


@app.get("/questions", dependencies=admin_only)
async def get_questions(status: str | None = "open"):
    """Очередь вопросов сотрудников, на которые ассистент не ответил сам."""
    return {"questions": questions.list_all(status=status or None),
            "open_count": questions.count_open()}


@app.post("/questions/{qid}/resolve", dependencies=admin_only)
async def resolve_question(qid: str, req: ResolveRequest, actor: dict = Depends(require_admin)):
    """Администратор отвечает на вопрос — ответ уходит в личный кабинет сотрудника."""
    try:
        entry = questions.resolve(qid, req.answer, actor.get("full_name") or actor.get("username"))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if entry is None:
        raise HTTPException(status_code=404, detail="Вопрос не найден")
    return entry


# ---------- Конструктор плана адаптации ----------
class PlanRequest(BaseModel):
    title: str | None = None
    role: str | None = None
    description: str | None = None
    start_date: str | None = None
    timezone: str | None = None
    stages: list = []


@app.get("/catalog", dependencies=admin_only)
async def get_catalog():
    """Каталог этапов и шаблонов подэтапов — из него человек собирает план."""
    return planner.load_catalog()


@app.get("/plans", dependencies=admin_only)
async def get_plans():
    return {"plans": planner.list_plans()}


@app.post("/plans", dependencies=admin_only)
async def create_plan(req: PlanRequest):
    plan = planner.normalize_plan(req.model_dump())
    planner.save_plan(plan)
    return plan


@app.get("/plans/{plan_id}", dependencies=admin_only)
async def get_plan(plan_id: str):
    plan = planner.load_plan(plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail="План не найден")
    return {"plan": plan, "schedule_preview": planner.resolve_schedule(plan)}


@app.put("/plans/{plan_id}", dependencies=admin_only)
async def update_plan(plan_id: str, req: PlanRequest):
    existing = planner.load_plan(plan_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="План не найден")
    payload = req.model_dump()
    payload["created_at"] = existing.get("created_at")
    plan = planner.normalize_plan(payload, plan_id=plan_id)
    planner.save_plan(plan)
    return plan


@app.delete("/plans/{plan_id}", dependencies=admin_only)
async def remove_plan(plan_id: str):
    if not planner.delete_plan(plan_id):
        raise HTTPException(status_code=404, detail="План не найден")
    return {"plan_id": plan_id, "deleted": True}


@app.post("/plans/{plan_id}/duplicate", dependencies=admin_only)
async def duplicate_plan(plan_id: str, title: str | None = None):
    """Копия плана под смежную должность — дальше редактируется как обычно."""
    plan = planner.duplicate_plan(plan_id, title)
    if plan is None:
        raise HTTPException(status_code=404, detail="План не найден")
    return plan


@app.post("/plans/{plan_id}/generate", dependencies=admin_only)
async def generate_plan(plan_id: str):
    """
    Запускает фоновую генерацию автоответов по всем подэтапам.
    Возвращает описание задачи — прогресс тянуть через GET /jobs/{job_id}.
    """
    plan = planner.load_plan(plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail="План не найден")
    if not any(s.get("substages") for s in plan.get("stages") or []):
        raise HTTPException(status_code=400, detail="В плане нет ни одного подэтапа")
    return planner.start_generation(plan)


@app.get("/jobs/{job_id}", dependencies=admin_only)
async def get_job(job_id: str):
    job = planner.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Задача не найдена")
    return job


@app.get("/plans/{plan_id}/schedule", dependencies=admin_only)
async def get_schedule(plan_id: str):
    schedule = planner.load_schedule(plan_id)
    if schedule is None:
        raise HTTPException(status_code=404, detail="Расписание ещё не сгенерировано")
    return schedule


@app.post("/plans/{plan_id}/messages/{message_id}/regenerate", dependencies=admin_only)
async def regenerate_message(plan_id: str, message_id: str):
    """Перегенерация одного подэтапа — без прогона всего плана."""
    plan = planner.load_plan(plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail="План не найден")
    message = planner.regenerate_one(plan, message_id)
    if message is None:
        raise HTTPException(status_code=404, detail="Подэтап не найден в плане")
    return message


EXPORT_FILES = {
    "plan.md": ("text/markdown; charset=utf-8", "План в формате для LLM"),
    "plan.json": ("application/json", "Канонический план"),
    "schedule.md": ("text/markdown; charset=utf-8", "Расписание с готовыми ответами"),
    "schedule.json": ("application/json", "Расписание для мессенджеров и приложения"),
}


@app.get("/plans/{plan_id}/export/{name}", dependencies=admin_only)
async def export_plan(plan_id: str, name: str):
    if name not in EXPORT_FILES:
        raise HTTPException(status_code=400, detail=f"Доступны: {', '.join(EXPORT_FILES)}")
    path = planner.plan_dir(plan_id) / name
    if not path.exists():
        raise HTTPException(status_code=404, detail="Файл ещё не сформирован")
    media_type, _ = EXPORT_FILES[name]
    return FileResponse(path, media_type=media_type, filename=f"{plan_id}_{name}")


# ---------- Пользователи: профили, роли, назначение планов ----------
class UserRequest(BaseModel):
    full_name: str
    username: str | None = None
    password: str | None = None
    position: str | None = None
    department: str | None = None
    contact: str | None = None
    mentor: str | None = None
    manager: str | None = None
    plan_id: str | None = None
    start_date: str | None = None
    status: str | None = None
    notes: str | None = None


class RoleRequest(BaseModel):
    role: str


class ActiveRequest(BaseModel):
    active: bool


class TargetCredentialsRequest(BaseModel):
    username: str | None = None
    password: str | None = None


def _target_user(user_id: str, actor: dict) -> dict:
    """Находит пользователя и проверяет, что актор вправе его менять."""
    target = users.get_user(user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="Пользователь не найден")
    try:
        users.ensure_can_manage(actor, target)
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    return target


@app.get("/users", dependencies=admin_only)
async def get_users():
    plans = {p["plan_id"]: p for p in planner.list_plans()}
    result = []
    for user in users.list_users():
        plan = plans.get(user.get("plan_id"))
        result.append({
            **user,
            "plan_title": plan["title"] if plan else None,
            "plan_generated": bool(plan and plan.get("generated")),
            "has_account": bool(user.get("username")),
        })
    return {"users": result}


@app.post("/users", dependencies=admin_only)
async def create_user(req: UserRequest, actor: dict = Depends(require_admin)):
    """
    Заведение сотрудника администратором. Логин и временный пароль необязательны:
    профиль можно создать заранее, а доступ выдать позже.
    """
    try:
        user = users.create_user(req.model_dump(), actor=actor, role=users.ROLE_EMPLOYEE,
                                 must_change_credentials=bool(req.password))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return users.public_view(user)


@app.get("/users/{user_id}", dependencies=admin_only)
async def get_user(user_id: str):
    user = users.get_user(user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="Пользователь не найден")
    return users.public_view(user)


@app.put("/users/{user_id}")
async def update_user(user_id: str, req: UserRequest, actor: dict = Depends(require_admin)):
    """Правка профиля и назначение плана адаптации с датой выхода."""
    _target_user(user_id, actor)
    user = users.update_profile(user_id, req.model_dump())
    return users.public_view(user)


@app.delete("/users/{user_id}", dependencies=owner_only)
async def remove_user(user_id: str, actor: dict = Depends(require_owner)):
    """Удаление пользователя — только главный администратор."""
    if user_id == actor["id"]:
        raise HTTPException(status_code=400, detail="Нельзя удалить самого себя")
    try:
        deleted = users.delete_user(user_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not deleted:
        raise HTTPException(status_code=404, detail="Пользователь не найден")
    auth.drop_user_sessions(user_id)
    return {"user_id": user_id, "deleted": True}


@app.post("/users/{user_id}/role", dependencies=owner_only)
async def change_user_role(user_id: str, req: RoleRequest, actor: dict = Depends(require_owner)):
    """Назначить администратором или убрать из администраторов — только главный."""
    if user_id == actor["id"]:
        raise HTTPException(status_code=400, detail="Нельзя изменить собственную роль")
    try:
        user = users.set_role(user_id, req.role)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    # Права изменились — пусть перезайдёт с актуальной ролью
    auth.drop_user_sessions(user_id)
    return users.public_view(user)


@app.post("/users/{user_id}/active")
async def change_user_active(user_id: str, req: ActiveRequest, actor: dict = Depends(require_admin)):
    """
    Подтверждение регистрации и блокировка доступа. Администратор может
    активировать и блокировать сотрудников, главный администратор — кого угодно.
    """
    _target_user(user_id, actor)
    if user_id == actor["id"]:
        raise HTTPException(status_code=400, detail="Нельзя заблокировать самого себя")
    try:
        user = users.set_active(user_id, req.active)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not req.active:
        auth.drop_user_sessions(user_id)
    return users.public_view(user)


@app.post("/users/{user_id}/credentials")
async def set_user_credentials(user_id: str, req: TargetCredentialsRequest,
                               actor: dict = Depends(require_admin)):
    """
    Выдача логина и/или временного пароля. Пользователь при следующем входе
    обязан задать свои учётные данные.
    """
    _target_user(user_id, actor)
    try:
        if req.username:
            users.set_username(user_id, req.username)
        if req.password:
            users.set_password(user_id, req.password, must_change=True)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if req.password:
        auth.drop_user_sessions(user_id)
    return users.public_view(users.get_user(user_id))


@app.post("/users/{user_id}/transfer-ownership", dependencies=owner_only)
async def transfer_ownership(user_id: str, actor: dict = Depends(require_owner)):
    """Передача роли главного администратора. Прежний владелец остаётся админом."""
    try:
        new_owner = users.transfer_ownership(actor["id"], user_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    # Роли поменялись у обоих — обе сессии переоформляются входом заново
    auth.drop_user_sessions(user_id)
    auth.drop_user_sessions(actor["id"])
    return users.public_view(new_owner)


@app.get("/users/{user_id}/schedule", dependencies=admin_only)
async def get_user_schedule(user_id: str):
    """
    Персональное расписание: план-шаблон, пересчитанный на дату выхода этого
    сотрудника, с подстановкой плейсхолдеров. Считается на лету — при правке
    плана или даты выхода расписание всегда актуальное.
    """
    user = users.get_user(user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="Пользователь не найден")
    try:
        return adaptation.build_employee_schedule(user)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


EMPLOYEE_EXPORTS = {"schedule.json", "schedule.md"}


@app.get("/users/{user_id}/export/{name}", dependencies=admin_only)
async def export_user_schedule(user_id: str, name: str):
    if name not in EMPLOYEE_EXPORTS:
        raise HTTPException(status_code=400, detail=f"Доступны: {', '.join(EMPLOYEE_EXPORTS)}")

    employee = users.get_user(user_id)
    if employee is None:
        raise HTTPException(status_code=404, detail="Пользователь не найден")
    try:
        schedule = adaptation.build_employee_schedule(employee)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    if name == "schedule.json":
        body = json.dumps(schedule, ensure_ascii=False, indent=2)
        media_type = "application/json"
    else:
        body = planner.render_schedule_md(schedule)
        media_type = "text/markdown; charset=utf-8"

    # ФИО кириллицей в заголовок напрямую не положить (HTTP-заголовки — latin-1),
    # поэтому ASCII-запаска плюс RFC 5987 filename* с процентным кодированием
    pretty = f"{(employee.get('full_name') or 'employee').replace(' ', '_')}_{name}"
    return Response(
        content=body,
        media_type=media_type,
        headers={"Content-Disposition":
                 f'attachment; filename="{user_id}_{name}"; '
                 f"filename*=UTF-8''{quote(pretty)}"},
    )


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
