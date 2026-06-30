from __future__ import annotations

import re


USER_MENTION_RE = re.compile(r"<@!?(\d+)>")
ROLE_MENTION_RE = re.compile(r"<@&(\d+)>")


def sanitize_mentions(text: str) -> str:
    """Prevent Discord mention tokens from rendering as mentions."""
    sanitized = text.replace("@everyone", "@\u200beveryone")
    sanitized = sanitized.replace("@here", "@\u200bhere")
    sanitized = USER_MENTION_RE.sub(lambda match: f"<@\u200b{match.group(1)}>", sanitized)
    sanitized = ROLE_MENTION_RE.sub(lambda match: f"<@&\u200b{match.group(1)}>", sanitized)
    return sanitized
