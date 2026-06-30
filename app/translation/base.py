from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass(frozen=True)
class TranslationResult:
    translated_text: str
    detected_source_language: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None


class TranslationProvider(ABC):
    name: str
    model_name: str | None = None

    @abstractmethod
    async def translate(
        self,
        text: str,
        target_language: str,
        source_language: str | None = None,
    ) -> TranslationResult:
        raise NotImplementedError


class TranslationProviderError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        provider: str,
        model: str | None,
        error_type: str,
        error_summary: str,
        status: str | int | None = None,
        code: str | int | None = None,
    ) -> None:
        super().__init__(message)
        self.provider = provider
        self.model = model
        self.error_type = error_type
        self.error_summary = error_summary
        self.status = status
        self.code = code

    def log_extra(self) -> dict[str, str | int | None]:
        return {
            "provider": self.provider,
            "model": self.model,
            "error_type": self.error_type,
            "error_summary": self.error_summary,
            "status": self.status,
            "code": self.code,
        }
