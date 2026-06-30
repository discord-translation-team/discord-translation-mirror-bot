from __future__ import annotations

from app.mention_safety import sanitize_mentions
from app.translation.output_cleaner import clean_translation_output


def build_translated_message_body(translated_text: str, original_message_url: str) -> str:
    safe_translated_text = sanitize_mentions(clean_translation_output(translated_text))
    return f"{safe_translated_text}\n\n[Original]({original_message_url})"

