import os
import queue
import sys
import time
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))

from notifications import Notifier


def test_notifier_deduplication_suppresses_rapid_identical_messages():
    config = {"telegram": {"token": "fake_token", "chat_id": "12345"}}
    notifier = Notifier(config, max_queue_size=50, dedup_window_sec=2.0)
    try:
        # Patch _send_sync so worker doesn't make real network calls
        with patch.object(notifier, "_send_sync") as mock_send:
            notifier.send("Alert: circuit breaker tripped")
            notifier.send("Alert: circuit breaker tripped")
            notifier.send("Alert: circuit breaker tripped")

            time.sleep(0.1)
            # Only the first message should be queued and sent
            assert mock_send.call_count == 1
    finally:
        notifier.shutdown()


def test_notifier_distinct_messages_are_both_sent():
    config = {"telegram": {"token": "fake_token", "chat_id": "12345"}}
    notifier = Notifier(config, max_queue_size=50, dedup_window_sec=2.0)
    try:
        with patch.object(notifier, "_send_sync") as mock_send:
            notifier.send("Alert A")
            notifier.send("Alert B")

            time.sleep(0.1)
            assert mock_send.call_count == 2
    finally:
        notifier.shutdown()


def test_notifier_resends_after_dedup_window_expires():
    config = {"telegram": {"token": "fake_token", "chat_id": "12345"}}
    notifier = Notifier(config, max_queue_size=50, dedup_window_sec=0.1)
    try:
        with patch.object(notifier, "_send_sync") as mock_send:
            notifier.send("Alert: trailing stop")
            time.sleep(0.05)
            # Duplicate inside window -> suppressed
            notifier.send("Alert: trailing stop")
            time.sleep(0.15)
            # Duplicate after window expires -> sent
            notifier.send("Alert: trailing stop")

            time.sleep(0.1)
            assert mock_send.call_count == 2
    finally:
        notifier.shutdown()


def test_notifier_bounded_queue_drops_messages_on_overflow():
    config = {"telegram": {"token": "fake_token", "chat_id": "12345"}}
    notifier = Notifier(config, max_queue_size=2, dedup_window_sec=0.0)
    try:
        # Prevent worker from consuming by pausing
        with patch.object(notifier, "_send_sync", side_effect=lambda msg: time.sleep(1.0)):
            # Fill the queue
            notifier.send("Msg 1")
            notifier.send("Msg 2")
            notifier.send("Msg 3")  # Should be dropped gracefully without raising queue.Full

            assert notifier._queue.maxsize == 2
    finally:
        notifier.shutdown()


def test_notifier_escapes_html_entities_for_telegram():
    config = {"telegram": {"token": "fake_token", "chat_id": "12345"}}
    notifier = Notifier(config)
    try:
        with patch("requests.post") as mock_post:
            mock_post.return_value.ok = True
            msg = "💥 TRAILING STOP HIT! Equity 990.00 <= 995.00 & PnL < 0"
            notifier._send_sync(msg)

            assert mock_post.call_count == 1
            call_kwargs = mock_post.call_args[1]
            payload = call_kwargs["json"]
            assert payload["parse_mode"] == "HTML"
            assert "<=" not in payload["text"]
            assert "&lt;=" in payload["text"]
            assert "&amp;" in payload["text"]
            assert "&lt; 0" in payload["text"]
    finally:
        notifier.shutdown()

