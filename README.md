# Bot TV — Backend

Backend бота на FastAPI.

## Локальный запуск

1. Создайте виртуальное окружение:

```bash
python -m venv venv
```

2. Активируйте его:

**Windows (PowerShell):**
```powershell
.\venv\Scripts\Activate.ps1
```

**Linux / macOS:**
```bash
source venv/bin/activate
```

3. Установите зависимости:

```bash
pip install -r requirements.txt
```

4. Скопируйте `.env.example` в `.env` и заполните переменные при необходимости:

```bash
cp .env.example .env
```

5. Запустите сервер:

```bash
uvicorn app.main:app --reload
```

Сервер будет доступен по адресу http://127.0.0.1:8000. Проверка: GET http://127.0.0.1:8000/health → `{"status": "ok"}`.
