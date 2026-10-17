#!/usr/bin/env bash
# Обновление кода и перезапуск бота:  sudo bash deploy/update.sh
set -euo pipefail
APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$APP_DIR"
git pull --ff-only
.venv/bin/pip install -q -r requirements.txt
chown -R bot:bot "$APP_DIR"
sudo -u bot .venv/bin/python -m pytest -q
systemctl restart deposit-bot
echo "Обновлено и перезапущено. Лог: sudo journalctl -u deposit-bot -f"
