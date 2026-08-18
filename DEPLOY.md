# Развёртывание на Linux-сервере (Docker и Ollama уже установлены)

Поэтапная инструкция для сервера, где **Docker и Ollama уже есть**.
Приложение — RAG-ассистент по регламентам + конструктор планов адаптации.

Что поднимем (всё на `127.0.0.1`, наружу не торчит):
- **PostgreSQL** — аккаунты, роли, профили (Docker-контейнер, порт 5432)
- **Qdrant** — векторный поиск (Docker-контейнер, порты 6333/6334)
- **Ollama** — уже установлен, нужны модели и порт 8080
- **rag-app** — приложение FastAPI (systemd, порт 8000)

Требования: RAM 16+ ГБ (модель `qwen3:14b` ~9 ГБ без GPU), диск 30+ ГБ,
пользователь с `sudo`.

> Есть скрипт `install.sh`, делающий все шаги разом (и пропускающий уже
> установленные Docker/Ollama). Ручная инструкция ниже — если хотите контроль
> над каждым шагом.

---

## Шаг 1. Забрать код и создать окружение

```bash
git clone https://github.com/headiks/neiromaster.git
cd neiromaster
```

```bash
sudo apt-get update -y
sudo apt-get install -y python3 python3-venv python3-pip build-essential libgl1 libglib2.0-0 openssl
```

`libgl1`/`libglib2.0-0` нужны OpenCV/EasyOCR, которые docling тянет для разбора PDF.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt --extra-index-url https://download.pytorch.org/whl/cpu
```

`--extra-index-url ...whl/cpu` ставит CPU-сборку torch (нужна docling). Если на
сервере есть NVIDIA GPU с CUDA — уберите флаг, чтобы поставилась GPU-версия.

## Шаг 2. PostgreSQL в Docker

Сгенерировать пароль и записать DSN в защищённый env-файл (его читает сервис):

```bash
PG_PASS="$(openssl rand -hex 16)"
cat > .env.production <<EOF
NEIROMASTER_DB_DSN=postgresql://neiromaster:${PG_PASS}@localhost:5432/neiromaster
EOF
chmod 600 .env.production
```

Поднять контейнер (порт только на localhost, данные в томе `pg_data/`):

```bash
docker run -d --name neiromaster-pg --restart unless-stopped \
  -e POSTGRES_USER=neiromaster -e POSTGRES_PASSWORD="$PG_PASS" -e POSTGRES_DB=neiromaster \
  -p 127.0.0.1:5432:5432 \
  -v "$(pwd)/pg_data:/var/lib/postgresql/data" \
  postgres:16-alpine
```

Дождаться готовности:

```bash
until docker exec neiromaster-pg pg_isready -U neiromaster; do sleep 1; done
```

Схему таблиц приложение создаёт само при первом старте (`CREATE TABLE IF NOT EXISTS`).

## Шаг 3. Qdrant в Docker

```bash
docker run -d --name qdrant --restart unless-stopped \
  -p 127.0.0.1:6333:6333 -p 127.0.0.1:6334:6334 \
  -v "$(pwd)/qdrant_storage:/qdrant/storage" \
  qdrant/qdrant
```

## Шаг 4. Ollama: порт 8080 и модели

Код обращается к Ollama на порту **8080** (не на стандартном 11434). Переопределить
через systemd drop-in:

```bash
sudo mkdir -p /etc/systemd/system/ollama.service.d
printf '[Service]\nEnvironment="OLLAMA_HOST=127.0.0.1:8080"\n' | \
  sudo tee /etc/systemd/system/ollama.service.d/override.conf
sudo systemctl daemon-reload
sudo systemctl restart ollama
```

Скачать модели:

```bash
export OLLAMA_HOST=127.0.0.1:8080
ollama pull bge-m3        # эмбеддинги
ollama pull qwen2.5:3b    # классификация/реранк
ollama pull qwen3:14b     # генерация ответа (~9+ ГБ RAM без GPU)
```

> Слабый сервер — замените `qwen3:14b` на модель поменьше в `test_cascade.py`
> (константа `BIG_MODEL`) и скачайте её вместо `qwen3:14b`.

## Шаг 5. systemd-сервис приложения

```bash
APP_DIR="$(pwd)"; APP_USER="$(whoami)"
cat <<EOF | sudo tee /etc/systemd/system/rag-app.service
[Unit]
Description=RAG Assistant (FastAPI)
After=network.target docker.service ollama.service
Requires=docker.service ollama.service

[Service]
WorkingDirectory=$APP_DIR
EnvironmentFile=$APP_DIR/.env.production
ExecStart=$APP_DIR/.venv/bin/python $APP_DIR/app.py
Restart=on-failure
User=$APP_USER

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload
sudo systemctl enable rag-app
```

`EnvironmentFile` передаёт сервису `NEIROMASTER_DB_DSN` из `.env.production`.

## Шаг 6. Первый запуск

Разово проиндексировать документы, уже лежащие в `data/documents`:

```bash
source .venv/bin/activate
python index_documents.py
```

Запустить сайт:

```bash
sudo systemctl start rag-app
sudo systemctl status rag-app
sudo journalctl -u rag-app -f      # логи в реальном времени
```

В логах первого старта будет **логин и одноразовый пароль главного администратора**
(дубль — в `data/owner_initial_credentials.txt`). Войдите под ними на `/login` —
система сразу попросит задать свои логин и пароль.

## Шаг 7. Доступ снаружи и HTTPS

```bash
sudo ufw allow 8000/tcp        # только если нужен внешний доступ к сайту
```

Сайт: `http://<IP-сервера>:8000`. Порты Postgres (5432), Qdrant (6333) и
Ollama (8080) остаются на localhost — наружу не открывать.

**Перед публикацией наружу — HTTPS обязателен** (кука сессии ходит по HTTP
открытой). Поставьте перед приложением nginx/Caddy с TLS (Let's Encrypt),
проксируя на `127.0.0.1:8000`.

---

## Проверка, что всё поднялось

```bash
docker exec neiromaster-pg pg_isready -U neiromaster    # Postgres
curl -s http://127.0.0.1:6333/collections               # Qdrant
curl -s http://127.0.0.1:8080/api/tags                  # Ollama
sudo systemctl status rag-app                           # приложение
```

## Резервные копии

```bash
# База аккаунтов
docker exec neiromaster-pg pg_dump -U neiromaster neiromaster > backup_$(date +%F).sql
```

Переживают пересоздание контейнеров (в корне проекта): `pg_data/` (Postgres),
`qdrant_storage/` (векторы), `data/` (документы, кэш, планы).

## Обновление кода

```bash
cd neiromaster && git pull
source .venv/bin/activate && pip install -r requirements.txt
sudo systemctl restart rag-app
```

## Масштабирование (много пользователей)

- Несколько воркеров: `uvicorn app:app --workers N`. Тогда сессии (сейчас в памяти
  процесса) вынести в Redis — иначе вход на одном воркере не виден другим.
  Размер пула БД — `NEIROMASTER_DB_POOL` (по умолчанию 10); суммарно по воркерам
  не превышать `max_connections` Postgres.
- Отдельный сервер БД: поменять только `NEIROMASTER_DB_DSN` в `.env.production`,
  код не меняется.
