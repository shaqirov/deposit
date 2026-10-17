#!/usr/bin/env bash
# Установка бота на сервер Ubuntu/Debian. Запуск из папки с кодом:
#   sudo bash deploy/install.sh
set -euo pipefail

APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"
SERVICE=deposit-bot

if [ "$(id -u)" -ne 0 ]; then
  echo "Запустите через sudo: sudo bash deploy/install.sh" >&2
  exit 1
fi

echo "==> Установка Python и зависимостей системы"
apt-get update -q
apt-get install -y -q python3 python3-venv python3-pip git curl

echo "==> Проверка доступа к Binance Futures"
code=$(curl -s -o /dev/null -w "%{http_code}" https://fapi.binance.com/fapi/v1/ping || true)
if [ "$code" = "200" ]; then
  echo "    Binance доступен"
else
  echo "    ВНИМАНИЕ: Binance ответил кодом '$code'. С серверов в некоторых странах"
  echo "    (например, США) Binance недоступен — нужен сервер в другой стране."
fi

echo "==> Пользователь bot"
id bot >/dev/null 2>&1 || useradd --system --home-dir "$APP_DIR" --shell /usr/sbin/nologin bot

echo "==> Виртуальное окружение и библиотеки"
python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install -q --upgrade pip
"$APP_DIR/.venv/bin/pip" install -q -r "$APP_DIR/requirements.txt"

if [ ! -f "$APP_DIR/.env" ]; then
  cp "$APP_DIR/.env.example" "$APP_DIR/.env"
  echo "==> Создан .env из .env.example (режим paper)"
fi
chmod 600 "$APP_DIR/.env"
chown -R bot:bot "$APP_DIR"
# git отказывается работать в папке чужого владельца без этой настройки
git config --global --add safe.directory "$APP_DIR"

echo "==> Тесты"
(cd "$APP_DIR" && sudo -u bot .venv/bin/python -m pytest -q)

echo "==> Сервис автозапуска"
sed "s|/opt/deposit|$APP_DIR|g" "$APP_DIR/deploy/$SERVICE.service" > /etc/systemd/system/$SERVICE.service
systemctl daemon-reload
systemctl enable $SERVICE >/dev/null
systemctl restart $SERVICE

sleep 5
systemctl --no-pager --lines=5 status $SERVICE || true
echo
echo "Готово. Бот работает в фоне и запускается сам после перезагрузки сервера."
echo "  Лог в реальном времени:  sudo journalctl -u $SERVICE -f"
echo "  Настройки:               sudo nano $APP_DIR/.env  (затем sudo systemctl restart $SERVICE)"
echo "  Журнал сделок:           cat $APP_DIR/journal.csv"
