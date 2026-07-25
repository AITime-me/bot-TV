# Bot TV — Backend

Минимальный fail-closed фундамент Bot Core на FastAPI. Требуется Python 3.10+.

## Безопасные значения по умолчанию

- `BOT_MODE=OFF`
- `EMERGENCY_LOCK=true`
- токены каналов, AI, PostgreSQL и Redis для запуска в `OFF` не требуются;
- автоматические исходящие действия не разрешены ни в одном режиме.

Допустимые значения `BOT_MODE`: `OFF`, `HINTS`, `DRAFT`, `AUTO_READ`,
`AUTO_WRITE`. Неизвестное значение отклоняется при запуске.
`EMERGENCY_LOCK` имеет безусловный приоритет над режимом.
Для `EMERGENCY_LOCK` допустимы только точные значения `true` и `false`;
регистр, пробелы и псевдобулевы значения не нормализуются.

Даже `BOT_MODE=AUTO_WRITE` и `EMERGENCY_LOCK=false` пока не подключают внешние
интеграции и сами по себе не разрешают отправку сообщений.

## Локальный запуск

```bash
python -m venv venv
```

Активируйте окружение:

```powershell
# Windows PowerShell
.\venv\Scripts\Activate.ps1
```

```bash
# Linux / macOS
source venv/bin/activate
```

Установите полный закреплённый набор runtime-зависимостей и запустите
приложение:

```bash
python -m pip install -r requirements-lock.txt
uvicorn app.main:app --reload
```

`requirements.txt` фиксирует прямые production-зависимости,
`requirements-lock.txt` — проверенный полный набор прямых и транзитивных
production-зависимостей для Python 3.12. Platform-specific зависимости
закреплены в lock-файлах с environment markers и применяются только на
поддерживаемых платформах.

Приложение безопасно запускается без `.env`. При необходимости настройки
передаются через environment; `.env.example` служит только перечнем имён и
безопасных defaults.

Health endpoints:

- `GET /health` — совместимый базовый ответ;
- `GET /health/live` — процесс жив;
- `GET /health/ready` — конфигурация успешно загружена.

## Тесты

```bash
python -m pip install -r requirements-dev-lock.txt
python -m pytest -p no:cacheprovider
```

`requirements-dev.txt` фиксирует прямые dev-зависимости и подключает
`requirements.txt`; `requirements-dev-lock.txt` содержит полный
воспроизводимый набор для тестов.
