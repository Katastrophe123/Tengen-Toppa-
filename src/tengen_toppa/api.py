"""FastAPI app: WhatsApp webhook (exposed via Cloudflare Tunnel) + local health endpoint."""

from __future__ import annotations

import json
import logging

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse

from tengen_toppa.secrets import Secret, get_secret
from tengen_toppa.store.db import session
from tengen_toppa.whatsapp import router
from tengen_toppa.whatsapp.client import verify_signature
from tengen_toppa.whatsapp.inbound import parse_webhook

log = logging.getLogger(__name__)

app = FastAPI(title="Tengen-Toppa", docs_url=None, redoc_url=None, openapi_url=None)


@app.get("/health")
def health() -> dict:
    return {"ok": True}


@app.get("/whatsapp/webhook")
@app.get("/whatsapp/webhook/", include_in_schema=False)
@app.get("/", include_in_schema=False)  # tolerate a Callback URL saved without the path
def verify(
    mode: str = Query("", alias="hub.mode"),
    token: str = Query("", alias="hub.verify_token"),
    challenge: str = Query("", alias="hub.challenge"),
) -> PlainTextResponse:
    expected = get_secret(Secret.WA_VERIFY_TOKEN)
    if mode == "subscribe" and expected and token == expected:
        log.info("webhook verified by Meta")
        return PlainTextResponse(challenge)
    if not expected:
        reason = "wa_verify_token is not set on this Mac"
    elif mode != "subscribe":
        reason = f"hub.mode={mode!r} (open the URL from Meta, not a browser)"
    elif token.strip() == expected:
        reason = "verify token in Meta has extra spaces"
    else:
        reason = f"verify token mismatch (Meta sent {len(token)} chars, this Mac has {len(expected)} chars)"
    log.warning("webhook verification FAILED: %s", reason)
    raise HTTPException(status_code=403, detail=reason)


@app.post("/whatsapp/webhook")
@app.post("/whatsapp/webhook/", include_in_schema=False)
@app.post("/", include_in_schema=False)
async def inbound(request: Request) -> dict:
    app_secret = get_secret(Secret.WA_APP_SECRET)
    me = get_secret(Secret.WA_MY_NUMBER)
    raw = await request.body()
    log.info("webhook POST received (%d bytes)", len(raw))
    if not app_secret or not me:
        log.warning("webhook rejected: WhatsApp secrets not configured")
        raise HTTPException(status_code=503, detail="WhatsApp secrets not configured")
    if not verify_signature(raw, request.headers.get("X-Hub-Signature-256"), app_secret):
        log.warning("webhook rejected: bad X-Hub-Signature-256 (wa_app_secret doesn't match the app)")
        raise HTTPException(status_code=401)
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=400) from e

    msgs = parse_webhook(payload)
    statuses = [
        st.get("status")
        for e in payload.get("entry", [])
        for c in e.get("changes", [])
        for st in c.get("value", {}).get("statuses", []) or []
    ]
    log.info(
        "webhook payload: %d message(s), statuses=%s, field=%s",
        len(msgs),
        statuses,
        [c.get("field") for e in payload.get("entry", []) for c in e.get("changes", [])],
    )
    for msg in msgs:
        try:
            with session() as s:
                router.handle(s, msg, me)
        except Exception:  # never make Meta retry-storm us; errors are logged
            log.exception("failed handling inbound message %s", msg.wa_id)
    return {"ok": True}
