#!/usr/bin/env python3
from bridge_util import MAX_PEER_BODY_CHARS, normalize_kind, short_id, utc_now


PROMPT_BODY_CONTROL_TRANSLATION = {
    codepoint: None
    for codepoint in (
        *range(0x00, 0x09),
        *range(0x0B, 0x20),
        0x7F,
        *range(0x80, 0xA0),
    )
}


def one_line(text: str) -> str:
    return " ".join(str(text).split())


def normalize_prompt_body_text(text: str) -> str:
    raw = str(text).replace("\r\n", "\n").replace("\r", "\n")
    return raw.translate(PROMPT_BODY_CONTROL_TRANSLATION)


def prompt_body(text: str) -> str:
    raw = normalize_prompt_body_text(text)
    if len(raw) > MAX_PEER_BODY_CHARS:
        raw = raw[:MAX_PEER_BODY_CHARS] + "\n[bridge truncated peer body]"
    return raw


def kind_expects_response(kind: str) -> bool:
    return kind == "request"


def build_peer_prompt(message: dict, nonce: str) -> str:
    sender = str(message["from"])
    kind = normalize_kind(message.get("kind"), "request")
    details = [f"from={sender}", f"kind={kind}"]
    if kind == "result" and message.get("reply_to"):
        details.append(f"in_reply_to={message.get('reply_to')}")
    if message.get("causal_id"):
        details.append(f"causal_id={message.get('causal_id')}")
    if message.get("aggregate_id"):
        details.append(f"aggregate_id={message.get('aggregate_id')}")
    if kind == "request":
        label = "Request"
        if message.get("requester_cleared"):
            cleared = str(message.get("requester_cleared_alias") or sender)
            hint = (
                f"Reply normally for local completion; requester {cleared} was cleared, "
                "so bridge will not deliver this reply back."
            )
        else:
            hint = "Reply normally; do not call agent_send_peer; bridge auto-returns your reply."
    elif kind == "result":
        label = "Result"
        hint = "Use locally; do not reply to peer."
    else:
        label = "Notice"
        hint = "FYI; no reply needed."

    prefix = one_line(f"[bridge:{nonce}] {' '.join(details)}. {hint}")
    return f"{prefix} {label}: {prompt_body(message['body'])}"


def make_message(
    sender: str,
    target: str,
    intent: str,
    body: str,
    causal_id: str | None = None,
    hop_count: int = 0,
    auto_return: bool | None = None,
    kind: str = "request",
    reply_to: str | None = None,
    source: str = "daemon",
) -> dict:
    kind = normalize_kind(kind)
    if auto_return is None:
        auto_return = sender != "bridge" and target != "bridge" and kind_expects_response(kind)
    return {
        "id": short_id("msg"),
        "created_ts": utc_now(),
        "updated_ts": utc_now(),
        "from": sender,
        "to": target,
        "kind": kind,
        "intent": intent,
        "body": str(body),
        "causal_id": causal_id or short_id("causal"),
        "hop_count": int(hop_count),
        "auto_return": bool(auto_return),
        "reply_to": reply_to,
        "source": source,
        "status": "pending",
        "nonce": None,
        "delivery_attempts": 0,
    }
