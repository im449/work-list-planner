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
MVP-версия учебного проекта. Основная функциональность работает, развёрнута на VPS (work-list.ru). Без автоматических тестов.
