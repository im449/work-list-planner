# work-list-planner

Канбан-планировщик задач (MVP) на стеке Python + FastAPI. Учебный проект для портфолио.

🔗 Демо: [work-list.ru](https://work-list.ru) (вход в админку: `/admin`)

## Стек
- Backend: Python, FastAPI, SQLite
- Frontend: HTML, CSS, Vanilla JS

## Возможности
- Канбан-интерфейс: создание, редактирование, перемещение и архивация задач
- Административная панель с авторизацией (`/admin`)
- Генерация PDF-отчётов о работе

## Как запустить
1. `python -m venv venv`
2. `source venv/bin/activate`
3. `pip install -r requirements.txt`
4. `uvicorn main:app --host 0.0.0.0 --port 8000`

## Статус
MVP-версия учебного проекта, развёрнута на VPS (work-list.ru). Секреты вынесены в переменные окружения, API покрыт автотестами, деплой — одной командой.
## Тесты
Запуск: `pytest tests/ -v` — покрытие авторизации и CRUD задач.

## Деплой
На сервере: `./deploy.sh` — подтягивает код, перезапускает сервис и проверяет, что сайт отвечает.
