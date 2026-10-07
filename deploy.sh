#!/bin/bash
# Обновление planner-app на сервере: подтянуть код, перезапустить сервис, проверить
set -e

APP_DIR=/var/www/planner-app
SERVICE=planner.service

cd "$APP_DIR"

echo "=== Подтягиваем код ==="
git pull

echo "=== Перезапускаем сервис ==="
systemctl restart "$SERVICE"

echo "=== Проверяем ==="
sleep 2
HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8000/)
if [ "$HTTP_CODE" = "200" ]; then
    echo "OK: сайт отвечает (HTTP $HTTP_CODE)"
else
    echo "ОШИБКА: сайт ответил $HTTP_CODE — смотри логи:"
    echo "journalctl -u $SERVICE -n 20 --no-pager"
    exit 1
fi
