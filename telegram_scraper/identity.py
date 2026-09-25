"""Typed peer identity, independent of mutable Telegram links and titles."""
from .storage import StoreError


def peer_identity(entity):
    values = entity if isinstance(entity, dict) else entity.to_dict() if hasattr(entity, "to_dict") else vars(entity)
    constructor = values.get("_", type(entity).__name__)
    if constructor in {"Channel", "ChannelForbidden", "PeerChannel", "InputPeerChannel"}:
        kind, key = "channel", "channel_id"
    elif constructor in {"Chat", "ChatForbidden", "PeerChat", "InputPeerChat"}:
        kind, key = "chat", "chat_id"
    else:
        raise StoreError("Telegram did not provide an identifiable channel or group.")
    number = values.get("id", values.get(key))
    if type(number) is not int or number <= 0:
        raise StoreError("Telegram did not provide a valid peer ID.")
    return {"kind": kind, "id": number}
