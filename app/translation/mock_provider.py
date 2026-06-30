from __future__ import annotations

from app.translation.base import TranslationProvider, TranslationResult


class MockTranslationProvider(TranslationProvider):
    name = "mock"
    model_name = "mock"

    async def translate(
        self,
        text: str,
        target_language: str,
        source_language: str | None = None,
    ) -> TranslationResult:
        return TranslationResult(translated_text=f"[{target_language}] {text}")
