"""Push notifications: Telegram (default) and/or ntfy."""
from __future__ import annotations

import html
import logging
import os

from curl_cffi import requests

log = logging.getLogger(__name__)


class Notifier:
    def __init__(self):
        self.tg_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        self.tg_chat = os.getenv("TELEGRAM_CHAT_ID", "").strip()
        self.ntfy_topic = os.getenv("NTFY_TOPIC", "").strip()
        self.ntfy_server = os.getenv("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
        if not (self.tg_token and self.tg_chat) and not self.ntfy_topic:
            log.warning("No notifier configured: set TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID and/or NTFY_TOPIC. "
                        "Alerts will only be printed.")

    def send(self, title: str, lines: list[str], link: str = "", urgent: bool = False) -> None:
        log.info("ALERT %s | %s", title, " / ".join(lines))
        if self.tg_token and self.tg_chat:
            self._telegram(title, lines, link)
        if self.ntfy_topic:
            self._ntfy(title, lines, link, urgent)

    def _telegram(self, title, lines, link):
        text = f"<b>{html.escape(title)}</b>\n" + "\n".join(html.escape(l) for l in lines)
        if link:
            text += f'\n\n<a href="{html.escape(link)}">Open on BookMyShow</a>'
        for chunk in _chunks(text, 4000):
            try:
                r = requests.post(f"https://api.telegram.org/bot{self.tg_token}/sendMessage",
                                  json={"chat_id": self.tg_chat, "text": chunk, "parse_mode": "HTML",
                                        "disable_web_page_preview": True}, timeout=20)
                if r.status_code != 200:
                    log.error("Telegram %s: %s", r.status_code, r.text[:300])
            except Exception as e:
                log.error("Telegram send failed: %s", e)

    def _ntfy(self, title, lines, link, urgent):
        # JSON publishing keeps emoji/UTF-8 titles intact.
        payload = {"topic": self.ntfy_topic, "title": title, "message": "\n".join(lines),
                   "priority": 5 if urgent else 3, "tags": ["movie_camera"]}
        if link:
            payload["click"] = link
        try:
            requests.post(self.ntfy_server, json=payload, timeout=20)
        except Exception as e:
            log.error("ntfy send failed: %s", e)


def _chunks(s, n):
    while s:
        yield s[:n]
        s = s[n:]
