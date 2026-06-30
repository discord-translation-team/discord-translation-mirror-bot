from __future__ import annotations

import hashlib
import logging
from datetime import datetime

import discord
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.mention_safety import sanitize_mentions
from app.models import ChannelRoute, GuildUsageMonthly, MessageMapping, TranslationCache
from app.services.webhook_service import WebhookService
from app.translation.base import TranslationProvider, TranslationProviderError, TranslationResult
from app.translation.output_cleaner import clean_translation_output

logger = logging.getLogger(__name__)


class RelayService:
    def __init__(
        self,
        session: AsyncSession,
        translation_provider: TranslationProvider,
        webhook_service: WebhookService,
    ) -> None:
        self.session = session
        self.translation_provider = translation_provider
        self.webhook_service = webhook_service
        self.max_message_chars = 1_500
        self.skip_messages_over_limit = True
        self.default_monthly_char_limit = 500_000

    async def relay_message(self, message: discord.Message) -> int:
        if message.guild is None or message.author.bot or message.webhook_id is not None:
            return 0

        source_char_count = len(message.content)
        if self.skip_messages_over_limit and source_char_count > self.max_message_chars:
            logger.warning(
                "message_translation_skipped_over_limit",
                extra={
                    "guild_id": message.guild.id,
                    "source_channel_id": message.channel.id,
                    "message_id": message.id,
                    "message_chars": source_char_count,
                    "max_message_chars": self.max_message_chars,
                },
            )
            return 0

        routes = await self._active_routes(message.guild.id, message.channel.id)
        if not routes:
            return 0

        sent_count = 0
        for route in routes:
            target_channel = message.guild.get_channel(route.target_channel_id)
            if not isinstance(target_channel, discord.TextChannel):
                logger.warning(
                    "target_channel_not_found",
                    extra={
                        "guild_id": route.guild_id,
                        "route_id": route.id,
                        "target_channel_id": route.target_channel_id,
                    },
                )
                continue

            webhook = await self.webhook_service.get_for_route(target_channel, route.webhook_id)
            if webhook is None:
                logger.warning(
                    "route_webhook_not_found",
                    extra={"guild_id": route.guild_id, "route_id": route.id, "webhook_id": route.webhook_id},
                )
                continue

            try:
                translation = await self._translate_with_cache(message.content, route.target_language)
            except TranslationProviderError as exc:
                logger.error(
                    "translation_failed",
                    extra={
                        "guild_id": message.guild.id,
                        "source_channel_id": message.channel.id,
                        "message_id": message.id,
                        "route_id": route.id,
                        **exc.log_extra(),
                    },
                )
                continue
            except Exception as exc:
                logger.error(
                    "translation_failed",
                    extra={
                        "guild_id": message.guild.id,
                        "source_channel_id": message.channel.id,
                        "message_id": message.id,
                        "route_id": route.id,
                        "provider": self.translation_provider.name,
                        "model": self._provider_model(),
                        "error_type": type(exc).__name__,
                    },
                )
                continue

            original_url = self._original_message_url(
                message.guild.id,
                message.channel.id,
                message.id,
            )
            body = self._translated_body(translation.translated_text, original_url)

            translated_message = await webhook.send(
                content=body,
                username=message.author.display_name,
                avatar_url=message.author.display_avatar.url,
                allowed_mentions=discord.AllowedMentions.none(),
                wait=True,
            )

            self.session.add(
                MessageMapping(
                    guild_id=message.guild.id,
                    original_message_id=message.id,
                    original_channel_id=message.channel.id,
                    target_channel_id=route.target_channel_id,
                    translated_message_id=translated_message.id,
                    target_language=route.target_language,
                    original_message_url=original_url,
                )
            )
            if not translation.from_cache:
                await self._track_usage(message.guild.id, translation, source_char_count)
            sent_count += 1

        await self.session.commit()
        if sent_count:
            logger.info(
                "message_relayed",
                extra={
                    "guild_id": message.guild.id,
                    "source_channel_id": message.channel.id,
                    "message_id": message.id,
                    "targets": sent_count,
                },
            )
        return sent_count

    async def _active_routes(self, guild_id: int, source_channel_id: int) -> list[ChannelRoute]:
        result = await self.session.execute(
            select(ChannelRoute).where(
                ChannelRoute.guild_id == guild_id,
                ChannelRoute.source_channel_id == source_channel_id,
                ChannelRoute.is_active.is_(True),
            )
        )
        return list(result.scalars().all())

    async def _translate_with_cache(self, text: str, target_language: str) -> "CachedTranslationResult":
        source_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
        provider = self.translation_provider.name
        model = self._provider_model()
        cached = await self.session.execute(
            select(TranslationCache).where(
                TranslationCache.source_text_hash == source_hash,
                TranslationCache.target_language == target_language,
                TranslationCache.provider == provider,
                TranslationCache.model == model,
            )
        )
        cache_row = cached.scalar_one_or_none()
        if cache_row:
            return CachedTranslationResult(
                translated_text=cache_row.translated_text,
                detected_source_language=cache_row.source_language,
                input_tokens=None,
                output_tokens=None,
                from_cache=True,
            )

        result = await self.translation_provider.translate(text, target_language)
        translated_text = clean_translation_output(result.translated_text)
        self.session.add(
            TranslationCache(
                source_text_hash=source_hash,
                source_language=result.detected_source_language,
                target_language=target_language,
                provider=provider,
                model=model,
                translated_text=translated_text,
            )
        )
        return CachedTranslationResult(
            translated_text=translated_text,
            detected_source_language=result.detected_source_language,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            from_cache=False,
        )

    async def _track_usage(
        self,
        guild_id: int,
        translation: "CachedTranslationResult",
        source_char_count: int,
    ) -> None:
        month = datetime.utcnow().strftime("%Y-%m")
        provider = self.translation_provider.name
        model = self._provider_model()
        result = await self.session.execute(
            select(GuildUsageMonthly).where(
                GuildUsageMonthly.guild_id == guild_id,
                GuildUsageMonthly.month == month,
                GuildUsageMonthly.provider == provider,
                GuildUsageMonthly.model == model,
            )
        )
        usage = result.scalar_one_or_none()
        if usage is None:
            usage = GuildUsageMonthly(
                guild_id=guild_id,
                month=month,
                provider=provider,
                model=model,
                characters_used=0,
                input_tokens_used=0,
                output_tokens_used=0,
                monthly_limit=self.default_monthly_char_limit,
            )
            self.session.add(usage)

        usage.characters_used += source_char_count
        usage.input_tokens_used += translation.input_tokens or 0
        usage.output_tokens_used += translation.output_tokens or 0

    def _provider_model(self) -> str:
        return self.translation_provider.model_name or self.translation_provider.name

    @staticmethod
    def _original_message_url(guild_id: int, source_channel_id: int, message_id: int) -> str:
        return f"https://discord.com/channels/{guild_id}/{source_channel_id}/{message_id}"

    @staticmethod
    def _translated_body(translated_text: str, original_message_url: str) -> str:
        safe_translated_text = sanitize_mentions(clean_translation_output(translated_text))
        return f"{safe_translated_text}\n\n[Original]({original_message_url})"


class CachedTranslationResult(TranslationResult):
    from_cache: bool

    def __init__(
        self,
        translated_text: str,
        detected_source_language: str | None,
        input_tokens: int | None,
        output_tokens: int | None,
        from_cache: bool,
    ) -> None:
        super().__init__(
            translated_text=translated_text,
            detected_source_language=detected_source_language,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )
        object.__setattr__(self, "from_cache", from_cache)
