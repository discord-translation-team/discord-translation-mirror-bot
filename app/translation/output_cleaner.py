from __future__ import annotations

import re


FENCE_RE = re.compile(r"^```[A-Za-z0-9_-]*\s*\n(?P<body>.*)\n?```$", re.DOTALL)
XML_WRAPPER_RE = re.compile(r"^<(?P<tag>[A-Za-z][A-Za-z0-9_-]*)>\s*\n?(?P<body>.*?)\n?</(?P=tag)>$", re.DOTALL)


def clean_translation_output(text: str) -> str:
    cleaned = text.strip()
    cleaned = _remove_surrounding_fence(cleaned).strip()
    cleaned = _remove_surrounding_xml_wrapper(cleaned).strip()
    cleaned = _remove_surrounding_quotes(cleaned).strip()
    return cleaned


def _remove_surrounding_fence(text: str) -> str:
    match = FENCE_RE.fullmatch(text)
    if not match:
        return text
    return match.group("body")


def _remove_surrounding_xml_wrapper(text: str) -> str:
    match = XML_WRAPPER_RE.fullmatch(text)
    if not match:
        return text
    return match.group("body")


def _remove_surrounding_quotes(text: str) -> str:
    quote_pairs = (('"', '"'), ("'", "'"), ("\u201c", "\u201d"), ("\u2018", "\u2019"))
    for start, end in quote_pairs:
        if text.startswith(start) and text.endswith(end) and len(text) >= len(start) + len(end):
            return text[len(start) : -len(end)]
    return text

