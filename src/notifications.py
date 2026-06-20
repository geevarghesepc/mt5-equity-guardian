import logging
import queue
import threading

import requests

logger = logging.getLogger(__name__)


class Notifier:
    def __init__(self, config):
        self._queue = queue.Queue()
        self._stop_event = threading.Event()
        self._worker = threading.Thread(target=self._worker_loop, daemon=True, name="telegram-notifier")
        self.apply_config(config)
        self._worker.start()

    def apply_config(self, config):
        tg_conf = config.get("telegram", {})
        self.token = tg_conf.get("token", "")
        self.chat_id = str(tg_conf.get("chat_id", ""))
        self.enabled = bool(self.token and self.chat_id)

    def send(self, message):
        """Enqueue a Telegram message; delivery happens off the hot loop path."""
        if not self.enabled:
            return
        self._queue.put(message)

    def shutdown(self):
        self._stop_event.set()
        self._queue.put(None)
        self._worker.join(timeout=2)

    def _worker_loop(self):
        while not self._stop_event.is_set():
            try:
                message = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue

            if message is None:
                break

            self._send_sync(message)

    def _send_sync(self, message):
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        payload = {
            "chat_id": self.chat_id,
            "text": message,
            "parse_mode": "HTML",
        }
        try:
            resp = requests.post(url, json=payload, timeout=5)
            if not resp.ok:
                logger.error("Telegram notification failed: %s", resp.text)
        except Exception as exc:
            logger.error("Exception sending telegram notification: %s", exc)
