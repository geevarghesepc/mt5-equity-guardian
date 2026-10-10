import html
import logging
import queue
import threading
import time

import requests

logger = logging.getLogger(__name__)

class Notifier:
    def __init__(self, config, max_queue_size=100, dedup_window_sec=15.0):
        self._queue = queue.Queue(maxsize=max_queue_size)
        self._stop_event = threading.Event()
        self._worker = threading.Thread(target=self._worker_loop, daemon=True, name="telegram-notifier")
        self._recent_messages: dict[str, float] = {}
        self._dedup_window_sec = dedup_window_sec
        self.apply_config(config)
        self._worker.start()

    def apply_config(self, config):
        tg_conf = config.get("telegram", {})
        self.token = tg_conf.get("token", "")
        self.chat_id = str(tg_conf.get("chat_id", ""))
        self.enabled = bool(self.token and self.chat_id)

    def send(self, message):
        """Enqueue a Telegram message with rate-limiting and bounded queue."""
        if not self.enabled or not message:
            return

        now = time.time()
        last_sent = self._recent_messages.get(message, 0.0)
        if now - last_sent < self._dedup_window_sec:
            logger.debug("Suppressing duplicate notification within dedup window: %s", message)
            return

        # Prune old entries
        if len(self._recent_messages) > 200:
            self._recent_messages = {
                m: t for m, t in self._recent_messages.items() if now - t < self._dedup_window_sec
            }
        self._recent_messages[message] = now

        try:
            self._queue.put_nowait(message)
        except queue.Full:
            logger.warning("Notification queue full (size %d); dropping message: %s", self._queue.maxsize, message)

    def shutdown(self):
        self._stop_event.set()
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            pass
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
        escaped_message = html.escape(message, quote=False)
        payload = {
            "chat_id": self.chat_id,
            "text": escaped_message,
            "parse_mode": "HTML",
        }
        try:
            resp = requests.post(url, json=payload, timeout=5)
            if not resp.ok:
                logger.error("Telegram notification failed: %s", resp.text)
        except Exception as exc:
            logger.error("Exception sending telegram notification: %s", exc)
