"""Resolve supported Telegram links locally, without opening or joining anything.

Link shapes: https://core.telegram.org/api/links#message-links
A message/topic link selects its containing channel; capture scope is chosen
separately by Sync or Date range.
"""

import re
from urllib.parse import parse_qs, urlsplit


class ChannelLinkError(ValueError):
    pass


HELP = "Paste a Telegram channel or message link, an @username, or a numeric channel ID."
RESERVED = {"share", "msg", "joinchat", "addlist", "addstickers", "addemoji", "addtheme", "addstyle", "setlanguage", "proxy", "socks", "login", "oauth", "confirmphone", "invoice", "giftcode", "nft", "contact", "m", "call", "newbot", "bg", "iv", "boost", "c", "s"}


def _username(value):
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{3,31}", value) or value.lower() in RESERVED:
        raise ChannelLinkError(HELP)
    return "@" + value.lower()


def _invite(value):
    if not re.fullmatch(r"[A-Za-z0-9_-]{4,256}", value) or value.isdigit():
        raise ChannelLinkError("Use a channel invite link, not a phone-number link.")
    return "https://t.me/+" + value


def _private(value):
    if not re.fullmatch(r"[0-9]{1,12}", value) or not 0 < int(value) < 10**12:
        raise ChannelLinkError("That private message link has an invalid channel ID.")
    # Telethon's marked channel ID, including leading zeroes for small IDs.
    return str(-10**12 - int(value))


def normalize_channel(value):
    if not isinstance(value, str):
        raise ChannelLinkError(HELP)
    value = value.strip()
    if not value:
        return ""
    if len(value) > 2048 or re.search(r"[\s\x00-\x1f\x7f\\]", value):
        raise ChannelLinkError(HELP)
    if re.fullmatch(r"-?[0-9]+", value):
        if not 0 < abs(int(value)) < 2**63:
            raise ChannelLinkError("Enter a valid numeric channel ID.")
        return str(int(value))
    if re.fullmatch(r"@?[A-Za-z][A-Za-z0-9_]{3,31}", value):
        return _username(value.lstrip("@"))
    try:
        url = urlsplit(value if re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", value) else "https://" + value)
        if url.username or url.password or url.port is not None or url.fragment:
            raise ChannelLinkError(HELP)
        query = parse_qs(url.query, keep_blank_values=True, max_num_fields=30)
        if any(len(values) != 1 for values in query.values()):
            raise ChannelLinkError("That link contains conflicting options. Copy its original Telegram link again.")
        if url.scheme.lower() == "tg":
            if url.path not in {"", "/"}:
                raise ChannelLinkError(HELP)
            if url.hostname == "join" and set(query) == {"invite"}:
                return _invite(query["invite"][0])
            if url.hostname == "resolve" and "domain" in query:
                if any(key in query for key in ("start", "startgroup", "startapp", "game", "phone", "story", "album")):
                    raise ChannelLinkError("Use a channel or message link. Bot actions and story links are not channel archives.")
                _message_ids([query[key][0] for key in ("post", "thread") if key in query])
                return _username(query["domain"][0])
            if url.hostname == "privatepost" and "channel" in query and "post" in query:
                _message_ids([query[key][0] for key in ("post", "thread") if key in query])
                return _private(query["channel"][0])
            raise ChannelLinkError(HELP)
        if url.scheme.lower() not in {"http", "https"} or url.hostname not in {"t.me", "www.t.me", "telegram.me", "www.telegram.me", "telegram.dog", "www.telegram.dog"}:
            raise ChannelLinkError("Use a Telegram link from t.me or telegram.me.")
        parts = url.path.strip("/").split("/")
        if not parts or any(not part for part in parts):
            raise ChannelLinkError(HELP)
        if len(parts) == 1 and parts[0].startswith("+"):
            return _invite(parts[0][1:])
        if len(parts) == 2 and parts[0] == "joinchat":
            return _invite(parts[1])
        if parts[0] == "c" and len(parts) in {3, 4}:
            _message_ids(parts[2:])
            return _private(parts[1])
        if parts[0] == "s":
            parts = parts[1:]  # Public browser preview of a channel.
        if len(parts) not in {1, 2, 3}:
            raise ChannelLinkError(HELP)
        if any(key in query for key in ("start", "startgroup", "startapp", "game", "story", "album")):
            raise ChannelLinkError("Use a channel or message link. Bot actions and story links are not channel archives.")
        _message_ids(parts[1:])
        return _username(parts[0])
    except (ValueError, IndexError) as exc:
        if isinstance(exc, ChannelLinkError):
            raise
        raise ChannelLinkError(HELP) from None


def _message_ids(values):
    if any(not re.fullmatch(r"[0-9]{1,10}", value) or not 0 < int(value) < 2**31 for value in values):
        raise ChannelLinkError("That message link contains an invalid post or topic ID.")
