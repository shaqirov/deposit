"""Telegram: уведомления и приём команд.

Узнать chat_id: напишите своему боту любое сообщение и выполните
    python -m bot.telegram <TOKEN>
"""
import json
import logging
import os
import sys
import time
import urllib.request
import uuid

log = logging.getLogger(__name__)
API = "https://api.telegram.org/bot{token}/{method}"
ERROR_INTERVAL_S = 3600  # об ошибках — не чаще раза в час
MAX_TEXT = 4000          # лимит Telegram — 4096 символов


def call(token, method, payload=None, timeout=10):
    data = json.dumps(payload or {}).encode()
    req = urllib.request.Request(API.format(token=token, method=method), data=data,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def upload(token, method, fields, file_field, path, timeout=30):
    """multipart/form-data запрос с одним файлом."""
    boundary = uuid.uuid4().hex
    parts = []
    for name, value in fields.items():
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n'
                     f'{value}\r\n'.encode())
    with open(path, "rb") as f:
        content = f.read()
    parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{file_field}"; '
                 f'filename="{os.path.basename(path)}"\r\n'
                 'Content-Type: application/octet-stream\r\n\r\n'.encode() + content + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    req = urllib.request.Request(API.format(token=token, method=method), data=b"".join(parts),
                                 headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


class Notifier:
    """Отправка сообщений и приём команд только из своего чата.

    Без токена ничего не делает; сбои Telegram не мешают торговле.
    """

    def __init__(self, token, chat_id):
        self.token = token
        self.chat_id = str(chat_id)
        self.last_error_s = 0.0
        self.last_poll_error_s = 0.0
        self.offset = None

    @property
    def enabled(self):
        return bool(self.token and self.chat_id)

    def send(self, text):
        if not self.enabled:
            return
        try:
            call(self.token, "sendMessage", {"chat_id": self.chat_id, "text": text[:MAX_TEXT]})
        except Exception as e:
            log.warning("Telegram: не удалось отправить сообщение: %s", e)

    def send_document(self, path, caption=""):
        if not self.enabled:
            return
        try:
            upload(self.token, "sendDocument", {"chat_id": self.chat_id, "caption": caption},
                   "document", path)
        except Exception as e:
            log.warning("Telegram: не удалось отправить файл: %s", e)

    def error(self, text):
        now = time.time()
        if now - self.last_error_s >= ERROR_INTERVAL_S:
            self.last_error_s = now
            self.send(f"⚠️ Ошибка: {text}\nСледующие ошибки — не чаще раза в час, подробности в /log.")

    def set_commands(self, commands):
        """Меню команд в интерфейсе Telegram: [(команда, описание), ...]."""
        if not self.enabled:
            return
        try:
            call(self.token, "setMyCommands",
                 {"commands": [{"command": c, "description": d} for c, d in commands]})
        except Exception as e:
            log.warning("Telegram: не удалось установить меню команд: %s", e)

    def skip_pending(self):
        """Пропустить команды, пришедшие пока бот не работал, — чтобы не выполнить устаревшие."""
        if not self.enabled:
            return
        try:
            result = call(self.token, "getUpdates", {"offset": -1, "timeout": 0}).get("result", [])
            if result:
                self.offset = result[-1]["update_id"] + 1
                self.ack()
        except Exception as e:
            log.warning("Telegram: не удалось получить обновления: %s", e)

    def ack(self):
        """Подтвердить полученные команды, чтобы после перезапуска они не пришли снова."""
        if self.enabled and self.offset is not None:
            try:
                call(self.token, "getUpdates", {"offset": self.offset, "timeout": 0})
            except Exception as e:
                log.warning("Telegram: не удалось подтвердить обновления: %s", e)

    def poll(self, timeout):
        """Ждать команды до timeout секунд. Возвращает список текстов из своего чата."""
        if not self.enabled:
            time.sleep(timeout)
            return []
        try:
            result = call(self.token, "getUpdates",
                          {"offset": self.offset, "timeout": timeout, "allowed_updates": ["message"]},
                          timeout=timeout + 10).get("result", [])
        except Exception as e:
            now = time.time()
            if now - self.last_poll_error_s >= ERROR_INTERVAL_S:
                self.last_poll_error_s = now
                log.warning("Telegram: не удалось получить команды: %s", e)
            time.sleep(timeout)
            return []
        texts = []
        for update in result:
            self.offset = update["update_id"] + 1
            msg = update.get("message") or {}
            if str(msg.get("chat", {}).get("id")) != self.chat_id:
                log.warning("Telegram: сообщение из чужого чата %s проигнорировано",
                            msg.get("chat", {}).get("id"))
                continue
            if msg.get("text"):
                texts.append(msg["text"].strip())
        return texts


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
