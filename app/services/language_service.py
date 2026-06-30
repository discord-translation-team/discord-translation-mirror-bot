from __future__ import annotations


class LanguageService:
    @staticmethod
    def normalize(language: str) -> str:
        return language.strip().lower()

    @staticmethod
    def validate(language: str) -> bool:
        normalized = LanguageService.normalize(language)
        return 2 <= len(normalized) <= 16 and normalized.replace("-", "").isalpha()

