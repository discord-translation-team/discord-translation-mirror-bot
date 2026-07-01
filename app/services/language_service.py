from __future__ import annotations


class LanguageService:
    DISPLAY_NAMES = {
        "ru": "Russian",
        "en": "English",
        "es": "Spanish",
        "de": "German",
        "fr": "French",
        "it": "Italian",
        "pt": "Portuguese",
        "pl": "Polish",
        "uk": "Ukrainian",
        "tr": "Turkish",
        "ar": "Arabic",
        "zh": "Chinese",
        "ja": "Japanese",
        "ko": "Korean",
    }

    @staticmethod
    def normalize(language: str) -> str:
        return language.strip().lower()

    @staticmethod
    def validate(language: str) -> bool:
        normalized = LanguageService.normalize(language)
        return 2 <= len(normalized) <= 16 and normalized.replace("-", "").isalpha()

    @staticmethod
    def display_name(language: str) -> str:
        normalized = LanguageService.normalize(language)
        return LanguageService.DISPLAY_NAMES.get(normalized, normalized.upper())
