from __future__ import annotations

from app.languages import get_language_display_name, is_supported_language, normalize_language_code


class LanguageService:
    @staticmethod
    def normalize(language: str) -> str:
        return normalize_language_code(language)

    @staticmethod
    def validate(language: str) -> bool:
        return is_supported_language(language)

    @staticmethod
    def display_name(language: str) -> str:
        return get_language_display_name(language)
