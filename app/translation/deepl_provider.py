from __future__ import annotations

from app.translation.base import TranslationProvider, TranslationResult


class DeepLTranslationProvider(TranslationProvider):
    name = "deepl"
    model_name = None

    async def translate(
        self,
        text: str,
        target_language: str,
        source_language: str | None = None,
    ) -> TranslationResult:
        raise NotImplementedError("DeepL translation is intentionally not implemented in the MVP")
