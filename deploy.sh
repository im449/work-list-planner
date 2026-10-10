#!/bin/bash
set -euo pipefail

APP_DIR=/var/www/planner-app
SERVICE=planner.service

cd "$APP_DIR"

echo "=== Получаем изменения ==="
git pull --ff-only origin main

echo "=== Проверяем Python ==="
.venv/bin/python -m py_compile main.py

echo "=== Перезапускаем сервис ==="
systemctl restart "$SERVICE"

echo "=== Ожидаем готовности сервиса (до 30 секунд) ==="
READY=0

for ATTEMPT in $(seq 1 15); do
    if ! systemctl is-active --quiet "$SERVICE"; then
        echo "ОШИБКА: сервис не активен"
        systemctl status "$SERVICE" --no-pager || true
        journalctl -u "$SERVICE" -n 50 --no-pager || true
        exit 1
    fi

    if curl --connect-timeout 2 --max-time 3 -sS -o /dev/null \
        http://127.0.0.1:8000/ 2>/dev/null; then
        READY=1
        echo "Приложение отвечает"
        break
    fi

    echo "Приложение пока не отвечает (попытка $ATTEMPT/15)"
    sleep 2
done

if [ "$READY" -ne 1 ]; then
    echo "ОШИБКА: приложение не стало доступно за 30 секунд"
    systemctl status "$SERVICE" --no-pager || true
    journalctl -u "$SERVICE" -n 50 --no-pager || true
    exit 1
fi

for URL in / /savings.html /api/savings; do
    echo "Проверяем URL: $URL"

    CODE=$(curl --connect-timeout 5 --max-time 15 \
        -sS -o /dev/null -w "%{http_code}" \
        "http://127.0.0.1:8000$URL") || {
        echo "ОШИБКА: curl не смог обратиться к $URL"
        journalctl -u "$SERVICE" -n 30 --no-pager || true
        exit 1
    }

    echo "$URL -> HTTP $CODE"

    if [ "$URL" = "/api/savings" ]; then
        if [ "$CODE" != "200" ] && [ "$CODE" != "401" ]; then
            echo "ОШИБКА API: неожиданный HTTP $CODE"
            exit 1
        fi
    elif [ "$CODE" != "200" ]; then
        echo "ОШИБКА страницы $URL: HTTP $CODE"
        exit 1
    fi
done

echo "=== Деплой завершён ==="
