from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

import discord
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.message_formatting import build_translated_message_body
from app.models import (
    OnDemandTranslationMapping,
    TranslationChannelSetting,
    UserLanguageSetting,
)
from app.services.language_service import LanguageService
from app.services.relay_service import RelayService
from app.services.webhook_service import WebhookService
from app.translation.base import TranslationProvider, TranslationProviderError

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class OnDemandResult:
    status: str
    target_channel_id: int | None = None
    target_language: str | None = None


class OnDemandTranslationService:
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

    async def publish_for_user(
        self,
        message: discord.Message,
        user_id: int,
        trigger: str = "on_demand",
    ) -> OnDemandResult:
        if message.guild is None or message.author.bot or message.webhook_id is not None:
            return OnDemandResult("ignored")

        if not message.content:
            logger.info(
                "on_demand_translation_skipped_empty_message",
                extra={
                    "guild_id": message.guild.id,
                    "source_channel_id": message.channel.id,
                    "message_id": message.id,
                    "user_id": user_id,
                },
            )
            return OnDemandResult("empty_message")

        source_char_count = len(message.content)
        if self.skip_messages_over_limit and source_char_count > self.max_message_chars:
            logger.warning(
                "on_demand_translation_skipped_over_limit",
                extra={
                    "guild_id": message.guild.id,
                    "source_channel_id": message.channel.id,
                    "message_id": message.id,
                    "message_chars": source_char_count,
                    "max_message_chars": self.max_message_chars,
                },
            )
            return OnDemandResult("over_limit")

        language = await self._user_language(message.guild.id, user_id)
        if language is None:
            return OnDemandResult("missing_language")
        language = LanguageService.normalize(language)

        logger.info(
            "on_demand_translation_request_received",
            extra={
                "guild_id": message.guild.id,
                "original_channel_id": message.channel.id,
                "original_message_id": message.id,
                "user_id": user_id,
                "target_language": language,
                "trigger": trigger,
            },
        )

        channel_setting = await self._translation_channel(message.guild.id, language)
        if channel_setting is None:
            logger.warning(
                "on_demand_translation_channel_missing",
                extra={"guild_id": message.guild.id, "target_language": language},
            )
            return OnDemandResult("missing_channel", target_language=language)

        target_channel = await self._target_text_channel(message.guild, channel_setting.channel_id)
        if target_channel is None:
            logger.warning(
                "on_demand_target_channel_not_found",
                extra={
                    "guild_id": message.guild.id,
                    "target_language": language,
                    "target_channel_id": channel_setting.channel_id,
                },
            )
            return OnDemandResult("missing_channel", target_language=language)

        relay = self._relay_helper()
        original_url = relay._original_message_url(message.guild.id, message.channel.id, message.id)
        reservation = await self._reserve_mapping(
            guild_id=message.guild.id,
            original_message_id=message.id,
            original_channel_id=message.channel.id,
            target_language=language,
            target_channel_id=target_channel.id,
            original_message_url=original_url,
            created_by_user_id=user_id,
        )
        if reservation.status == "duplicate":
            return OnDemandResult(
                "duplicate",
                target_channel_id=reservation.target_channel_id,
                target_language=language,
            )

        try:
            translation = await relay._translate_with_cache(message.content, language)
        except TranslationProviderError as exc:
            logger.error(
                "on_demand_translation_failed",
                extra={
                    "guild_id": message.guild.id,
                    "source_channel_id": message.channel.id,
                    "message_id": message.id,
                    "user_id": user_id,
                    **exc.log_extra(),
                },
            )
            await self._remove_mapping_reservation(reservation.mapping_id)
            return OnDemandResult("translation_failed", target_channel_id=target_channel.id, target_language=language)
        except Exception as exc:
            logger.error(
                "on_demand_translation_failed",
                extra={
                    "guild_id": message.guild.id,
                    "source_channel_id": message.channel.id,
                    "message_id": message.id,
                    "user_id": user_id,
                    "provider": self.translation_provider.name,
                    "model": self._provider_model(),
                    "error_type": type(exc).__name__,
                },
            )
            await self._remove_mapping_reservation(reservation.mapping_id)
            return OnDemandResult("translation_failed", target_channel_id=target_channel.id, target_language=language)

        try:
            translated_message = await target_channel.send(
                build_translated_message_body(translation.translated_text, original_url),
                allowed_mentions=discord.AllowedMentions.none(),
            )
            mapping = await self.session.get(OnDemandTranslationMapping, reservation.mapping_id)
            if mapping is None:
                raise RuntimeError("On-demand translation reservation disappeared before completion")
            mapping.translated_message_id = translated_message.id
            mapping.updated_at = datetime.utcnow()
            if not translation.from_cache:
                await relay._track_usage(message.guild.id, translation, source_char_count)
            await self.session.commit()
        except Exception as exc:
            logger.error(
                "on_demand_translation_send_or_complete_failed",
                extra={
                    "guild_id": message.guild.id,
                    "original_channel_id": message.channel.id,
                    "original_message_id": message.id,
                    "user_id": user_id,
                    "target_language": language,
                    "target_channel_id": target_channel.id,
                    "error_type": type(exc).__name__,
                },
            )
            await self._remove_mapping_reservation(reservation.mapping_id)
            return OnDemandResult("translation_failed", target_channel_id=target_channel.id, target_language=language)

        logger.info(
            "on_demand_translation_completed",
            extra={
                "guild_id": message.guild.id,
                "original_channel_id": message.channel.id,
                "original_message_id": message.id,
                "target_channel_id": target_channel.id,
                "target_language": language,
                "created_by_user_id": user_id,
            },
        )
        return OnDemandResult("posted", target_channel_id=target_channel.id, target_language=language)

    async def sync_edited_message(self, message: discord.Message) -> int:
        if message.guild is None or message.author.bot or message.webhook_id is not None:
            return 0

        mappings = await self._mappings_for_message(message.guild.id, message.id)
        edited_count = 0
        relay = self._relay_helper()

        for mapping in mappings:
            target_channel = await self._target_text_channel(message.guild, mapping.target_channel_id)
            if target_channel is None:
                logger.warning(
                    "on_demand_edit_target_channel_missing",
                    extra={"guild_id": mapping.guild_id, "mapping_id": mapping.id},
                )
                continue

            try:
                target_language = LanguageService.normalize(mapping.target_language)
                translation = await relay._translate_with_cache(message.content, target_language)
                if mapping.translated_message_id is None:
                    continue
                translated_message = await target_channel.fetch_message(mapping.translated_message_id)
                await translated_message.edit(
                    content=build_translated_message_body(translation.translated_text, mapping.original_message_url),
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except discord.NotFound:
                logger.warning(
                    "on_demand_translated_message_missing_on_edit",
                    extra={
                        "guild_id": mapping.guild_id,
                        "mapping_id": mapping.id,
                        "translated_message_id": mapping.translated_message_id,
                    },
                )
                continue
            except TranslationProviderError as exc:
                logger.error(
                    "on_demand_edit_translation_failed",
                    extra={
                        "guild_id": mapping.guild_id,
                        "mapping_id": mapping.id,
                        **exc.log_extra(),
                    },
                )
                continue
            except Exception as exc:
                logger.warning(
                    "on_demand_translated_message_edit_failed",
                    extra={"guild_id": mapping.guild_id, "mapping_id": mapping.id, "error_type": type(exc).__name__},
                )
                continue

            mapping.updated_at = datetime.utcnow()
            if not translation.from_cache:
                await relay._track_usage(message.guild.id, translation, len(message.content))
            edited_count += 1

        await self.session.commit()
        return edited_count

    async def sync_deleted_message(self, guild: discord.Guild, original_message_id: int) -> int:
        mappings = await self._mappings_for_message(guild.id, original_message_id)
        deleted_count = 0
        mapping_ids: list[int] = []

        for mapping in mappings:
            target_channel = await self._target_text_channel(guild, mapping.target_channel_id)
            if target_channel is None:
                logger.warning(
                    "on_demand_delete_target_channel_missing",
                    extra={"guild_id": mapping.guild_id, "mapping_id": mapping.id},
                )
                mapping_ids.append(mapping.id)
                continue

            try:
                if mapping.translated_message_id is None:
                    mapping_ids.append(mapping.id)
                    continue
                translated_message = await target_channel.fetch_message(mapping.translated_message_id)
                await translated_message.delete()
                deleted_count += 1
            except discord.NotFound:
                logger.warning(
                    "on_demand_translated_message_missing_on_delete",
                    extra={
                        "guild_id": mapping.guild_id,
                        "mapping_id": mapping.id,
                        "translated_message_id": mapping.translated_message_id,
                    },
                )
            except Exception as exc:
                logger.warning(
                    "on_demand_translated_message_delete_failed",
                    extra={"guild_id": mapping.guild_id, "mapping_id": mapping.id, "error_type": type(exc).__name__},
                )
                continue

            mapping_ids.append(mapping.id)

        if mapping_ids:
            await self.session.execute(
                delete(OnDemandTranslationMapping).where(OnDemandTranslationMapping.id.in_(mapping_ids))
            )
        await self.session.commit()
        return deleted_count

    async def _user_language(self, guild_id: int, user_id: int) -> str | None:
        result = await self.session.execute(
            select(UserLanguageSetting.target_language).where(
                UserLanguageSetting.guild_id == guild_id,
                UserLanguageSetting.user_id == user_id,
            )
        )
        language = result.scalar_one_or_none()
        return LanguageService.normalize(language) if language else None

    async def _translation_channel(self, guild_id: int, target_language: str) -> TranslationChannelSetting | None:
        target_language = LanguageService.normalize(target_language)
        result = await self.session.execute(
            select(TranslationChannelSetting).where(
                TranslationChannelSetting.guild_id == guild_id,
                func.lower(func.trim(TranslationChannelSetting.target_language)) == target_language,
            )
        )
        return result.scalar_one_or_none()

    async def _mapping(
        self,
        guild_id: int,
        original_message_id: int,
        target_language: str,
    ) -> OnDemandTranslationMapping | None:
        target_language = LanguageService.normalize(target_language)
        result = await self.session.execute(
            select(OnDemandTranslationMapping).where(
                OnDemandTranslationMapping.guild_id == guild_id,
                OnDemandTranslationMapping.original_message_id == original_message_id,
                func.lower(func.trim(OnDemandTranslationMapping.target_language)) == target_language,
            )
        )
        return result.scalar_one_or_none()

    async def _reserve_mapping(
        self,
        *,
        guild_id: int,
        original_message_id: int,
        original_channel_id: int,
        target_language: str,
        target_channel_id: int,
        original_message_url: str,
        created_by_user_id: int,
    ) -> "ReservationResult":
        target_language = LanguageService.normalize(target_language)
        existing = await self._mapping(guild_id, original_message_id, target_language)
        if existing is not None:
            logger.info(
                "on_demand_translation_duplicate_skipped",
                extra={
                    "guild_id": guild_id,
                    "original_channel_id": original_channel_id,
                    "original_message_id": original_message_id,
                    "user_id": created_by_user_id,
                    "target_language": target_language,
                    "target_channel_id": existing.target_channel_id,
                },
            )
            return ReservationResult(status="duplicate", mapping_id=existing.id, target_channel_id=existing.target_channel_id)

        mapping = OnDemandTranslationMapping(
            guild_id=guild_id,
            original_message_id=original_message_id,
            original_channel_id=original_channel_id,
            target_language=target_language,
            target_channel_id=target_channel_id,
            translated_message_id=None,
            original_message_url=original_message_url,
            created_by_user_id=created_by_user_id,
        )
        self.session.add(mapping)
        try:
            await self.session.commit()
        except IntegrityError:
            await self.session.rollback()
            existing = await self._mapping(guild_id, original_message_id, target_language)
            logger.info(
                "on_demand_translation_duplicate_skipped",
                extra={
                    "guild_id": guild_id,
                    "original_channel_id": original_channel_id,
                    "original_message_id": original_message_id,
                    "user_id": created_by_user_id,
                    "target_language": target_language,
                    "target_channel_id": existing.target_channel_id if existing else target_channel_id,
                },
            )
            return ReservationResult(
                status="duplicate",
                mapping_id=existing.id if existing else None,
                target_channel_id=existing.target_channel_id if existing else target_channel_id,
            )

        logger.info(
            "on_demand_translation_reservation_created",
            extra={
                "guild_id": guild_id,
                "original_channel_id": original_channel_id,
                "original_message_id": original_message_id,
                "user_id": created_by_user_id,
                "target_language": target_language,
                "target_channel_id": target_channel_id,
                "mapping_id": mapping.id,
            },
        )
        return ReservationResult(status="reserved", mapping_id=mapping.id, target_channel_id=target_channel_id)

    async def _remove_mapping_reservation(self, mapping_id: int | None) -> None:
        if mapping_id is None:
            return
        await self.session.rollback()
        await self.session.execute(delete(OnDemandTranslationMapping).where(OnDemandTranslationMapping.id == mapping_id))
        await self.session.commit()
        logger.warning("on_demand_translation_failed_reservation_removed", extra={"mapping_id": mapping_id})

    async def _mappings_for_message(self, guild_id: int, original_message_id: int) -> list[OnDemandTranslationMapping]:
        result = await self.session.execute(
            select(OnDemandTranslationMapping).where(
                OnDemandTranslationMapping.guild_id == guild_id,
                OnDemandTranslationMapping.original_message_id == original_message_id,
            )
        )
        return list(result.scalars().all())

    async def _target_text_channel(self, guild: discord.Guild, channel_id: int) -> discord.TextChannel | None:
        channel = guild.get_channel(channel_id)
        if isinstance(channel, discord.TextChannel):
            return channel
        try:
            fetched = await guild.fetch_channel(channel_id)
        except discord.DiscordException:
            return None
        return fetched if isinstance(fetched, discord.TextChannel) else None

    def _relay_helper(self) -> RelayService:
        relay = RelayService(self.session, self.translation_provider, self.webhook_service)
        relay.max_message_chars = self.max_message_chars
        relay.skip_messages_over_limit = self.skip_messages_over_limit
        relay.default_monthly_char_limit = self.default_monthly_char_limit
        return relay

    def _provider_model(self) -> str:
        return self.translation_provider.model_name or self.translation_provider.name


@dataclass(frozen=True)
class ReservationResult:
    status: str
    mapping_id: int | None
    target_channel_id: int
