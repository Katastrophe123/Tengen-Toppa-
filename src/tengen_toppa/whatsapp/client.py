"""Thin client for the official Meta WhatsApp Cloud API."""

from __future__ import annotations

import hashlib
import hmac
import re
from dataclasses import dataclass
from pathlib import Path

import httpx

BODY_MAX = 1024  # interactive body / caption limit
TEXT_MAX = 4096
BUTTON_TITLE_MAX = 20


class WhatsAppError(RuntimeError):
    def __init__(self, status: int, detail: str):
        super().__init__(f"WhatsApp API {status}: {detail}")
        self.status = status


@dataclass(frozen=True)
class Button:
    id: str
    title: str


def clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def template_param(text: str) -> str:
    """Template params may not contain newlines/tabs or >4 consecutive spaces."""
    return clip(re.sub(r"\s+", " ", text).strip(), 1000)


def verify_signature(raw_body: bytes, header: str | None, app_secret: str) -> bool:
    if not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(app_secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header.removeprefix("sha256="))


class WhatsAppClient:
    def __init__(
        self,
        token: str,
        phone_number_id: str,
        to: str,
        graph_version: str = "v23.0",
        http: httpx.Client | None = None,
    ):
        self.base = f"https://graph.facebook.com/{graph_version}/{phone_number_id}"
        self.to = to
        self.http = http or httpx.Client(timeout=30)
        self.headers = {"Authorization": f"Bearer {token}"}

    def _post(self, path: str, **kw) -> dict:
        r = self.http.post(f"{self.base}/{path}", headers=self.headers, **kw)
        if r.status_code >= 400:
            raise WhatsAppError(r.status_code, r.text[:500])
        return r.json()

    @staticmethod
    def _message_id(resp: dict) -> str | None:
        msgs = resp.get("messages") or []
        return msgs[0].get("id") if msgs else None

    def _send(self, payload: dict) -> str | None:
        body = {"messaging_product": "whatsapp", "recipient_type": "individual", "to": self.to, **payload}
        return self._message_id(self._post("messages", json=body))

    def send_text(self, text: str) -> str | None:
        return self._send({"type": "text", "text": {"body": clip(text, TEXT_MAX), "preview_url": False}})

    def send_buttons(self, body: str, buttons: list[Button]) -> str | None:
        if not 1 <= len(buttons) <= 3:
            raise ValueError("WhatsApp reply buttons: 1-3 buttons")
        return self._send(
            {
                "type": "interactive",
                "interactive": {
                    "type": "button",
                    "body": {"text": clip(body, BODY_MAX)},
                    "action": {
                        "buttons": [
                            {"type": "reply", "reply": {"id": b.id, "title": clip(b.title, BUTTON_TITLE_MAX)}}
                            for b in buttons
                        ]
                    },
                },
            }
        )

    def upload_media(self, path: Path, mime: str = "application/pdf") -> str:
        with path.open("rb") as fh:
            resp = self._post(
                "media",
                data={"messaging_product": "whatsapp", "type": mime},
                files={"file": (path.name, fh, mime)},
            )
        return resp["id"]

    def send_document(self, path: Path, caption: str = "") -> str | None:
        media_id = self.upload_media(path)
        doc = {"id": media_id, "filename": path.name}
        if caption:
            doc["caption"] = clip(caption, BODY_MAX)
        return self._send({"type": "document", "document": doc})

    def send_image(self, path: Path, caption: str = "") -> str | None:
        media_id = self.upload_media(path, mime="image/png")
        image: dict = {"id": media_id}
        if caption:
            image["caption"] = clip(caption, BODY_MAX)
        return self._send({"type": "image", "image": image})

    def send_template(self, name: str, language: str, params: list[str]) -> str | None:
        template: dict = {"name": name, "language": {"code": language}}
        if params:
            template["components"] = [
                {"type": "body", "parameters": [{"type": "text", "text": template_param(p)} for p in params]}
            ]
        return self._send({"type": "template", "template": template})
