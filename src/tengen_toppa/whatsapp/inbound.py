"""Parse Meta webhook payloads into simple inbound messages."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Inbound:
    wa_id: str
    sender: str
    kind: str  # "text" | "button"
    text: str  # message text, or button reply id (e.g. "apply:12")


def parse_webhook(payload: dict) -> list[Inbound]:
    out: list[Inbound] = []
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            for m in value.get("messages", []) or []:
                mtype = m.get("type")
                sender = m.get("from", "")
                wa_id = m.get("id", "")
                if mtype == "text":
                    out.append(Inbound(wa_id, sender, "text", m.get("text", {}).get("body", "").strip()))
                elif mtype == "interactive":
                    inter = m.get("interactive", {})
                    reply = inter.get("button_reply") or inter.get("list_reply") or {}
                    out.append(Inbound(wa_id, sender, "button", reply.get("id", "")))
                elif mtype == "button":  # quick-reply on a template message
                    btn = m.get("button", {})
                    out.append(Inbound(wa_id, sender, "text", (btn.get("payload") or btn.get("text") or "").strip()))
                else:
                    out.append(Inbound(wa_id, sender, "text", f"[unsupported message type: {mtype}]"))
    return out


def normalize_number(n: str) -> str:
    return "".join(ch for ch in n if ch.isdigit())
