from __future__ import annotations


SUPPORTED_LANGUAGES: dict[str, str] = {
    "en": "English",
    "ru": "Russian",
    "es": "Spanish",
    "fr": "French",
    "ar": "Arabic",
    "tr": "Turkish",
    "uk": "Ukrainian",
    "de": "German",
    "pt": "Portuguese",
    "it": "Italian",
    "zh": "Chinese",
    "ja": "Japanese",
    "ko": "Korean",
    "pl": "Polish",
    "hi": "Hindi",
    "bn": "Bengali",
    "id": "Indonesian",
    "vi": "Vietnamese",
    "nl": "Dutch",
    "fa": "Persian",
    "ro": "Romanian",
}

LANGUAGE_CODE_SUGGESTIONS: dict[str, str] = {
    "eng": "en",
    "english": "en",
    "en-us": "en",
    "en-gb": "en",
    "rus": "ru",
    "russian": "ru",
    "spa": "es",
    "spanish": "es",
    "fre": "fr",
    "french": "fr",
    "ger": "de",
    "deu": "de",
    "german": "de",
    "arabic": "ar",
    "ara": "ar",
    "eg": "ar",
    "egypt": "ar",
    "egyptian": "ar",
    "ua": "uk",
    "ukr": "uk",
    "ukrainian": "uk",
    "portuguese": "pt",
    "por": "pt",
    "pt-br": "pt",
    "brazilian": "pt",
    "chinese": "zh",
    "mandarin": "zh",
    "cn": "zh",
    "japanese": "ja",
    "korean": "ko",
    "polish": "pl",
    "hindi": "hi",
    "bengali": "bn",
    "indonesian": "id",
    "vietnamese": "vi",
    "dutch": "nl",
    "persian": "fa",
    "farsi": "fa",
    "romanian": "ro",
    "romana": "ro",
    "română": "ro",
    "roumanian": "ro",
}


def normalize_language_code(value: str) -> str:
    normalized = value.strip().lower().replace("_", "-")
    if "-" in normalized:
        normalized = normalized.split("-", 1)[0]
    return normalized


def get_language_display_name(code: str) -> str:
    normalized = normalize_language_code(code)
    return SUPPORTED_LANGUAGES.get(normalized, normalized.upper())


def is_supported_language(code: str) -> bool:
    return normalize_language_code(code) in SUPPORTED_LANGUAGES


def suggest_language_code(value: str) -> str | None:
    raw_normalized = value.strip().lower().replace("_", "-")
    suggestion = LANGUAGE_CODE_SUGGESTIONS.get(raw_normalized)
    if suggestion is not None:
        return suggestion

    normalized = normalize_language_code(value)
    if normalized in SUPPORTED_LANGUAGES:
        return normalized
    return LANGUAGE_CODE_SUGGESTIONS.get(normalized)


def format_supported_languages() -> str:
    return ", ".join(f"{name} ({code})" for code, name in SUPPORTED_LANGUAGES.items())
