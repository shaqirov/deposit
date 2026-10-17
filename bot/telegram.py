"""Уведомления в Telegram.

Узнать chat_id: напишите своему боту любое сообщение и выполните
    python -m bot.telegram <TOKEN>
"""
import json
import logging
import sys
import time
import urllib.request

log = logging.getLogger(__name__)
API = "https://api.telegram.org/bot{token}/{method}"
ERROR_INTERVAL_S = 3600  # об ошибках — не чаще раза в час


def call(token, method, payload=None, timeout=10):
    data = json.dumps(payload or {}).encode()
    req = urllib.request.Request(API.format(token=token, method=method), data=data,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


class Notifier:
    """Отправка сообщений. Без токена ничего не делает; сбои Telegram не мешают торговле."""

    def __init__(self, token, chat_id):
        self.token = token
        self.chat_id = chat_id
        self.last_error_s = 0.0

    @property
    def enabled(self):
        return bool(self.token and self.chat_id)

    def send(self, text):
        if not self.enabled:
            return
        try:
            call(self.token, "sendMessage", {"chat_id": self.chat_id, "text": text})
        except Exception as e:
            log.warning("Telegram: не удалось отправить сообщение: %s", e)

    def error(self, text):
        now = time.time()
        if now - self.last_error_s >= ERROR_INTERVAL_S:
            self.last_error_s = now
            self.send(f"⚠️ Ошибка: {text}\nСледующие ошибки — не чаще раза в час, подробности в логе.")


def main():
    if len(sys.argv) != 2:
        raise SystemExit("Использование: python -m bot.telegram <TOKEN>")
    token = sys.argv[1]
    updates = call(token, "getUpdates").get("result", [])
    chats = {u["message"]["chat"]["id"]: u["message"]["chat"].get("first_name", "")
             for u in updates if "message" in u}
    if not chats:
        raise SystemExit("Сообщений нет. Напишите своему боту в Telegram любое сообщение "
                         "и запустите команду ещё раз.")
    for chat_id, name in chats.items():
        print(f"TELEGRAM_CHAT_ID={chat_id}   ({name})")
        call(token, "sendMessage", {"chat_id": chat_id,
                                    "text": "✅ Связь с торговым ботом настроена"})


if __name__ == "__main__":
    main()
