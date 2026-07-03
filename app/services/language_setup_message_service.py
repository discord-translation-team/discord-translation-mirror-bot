from __future__ import annotations

from dataclasses import dataclass
import logging

import discord
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.languages import is_supported_language
from app.models import LanguageSetupMessage, TranslationChannelSetting
from app.services.language_service import LanguageService
from app.ui.language_setup import LanguageSetupView, build_language_select_options, build_language_setup_embed

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class LanguageSetupMessageRefreshResult:
    status: str
    channel_id: int
    message_id: int
    supported_languages: list[str]


class LanguageSetupMessageService:
    CREATED = "created"
    UPDATED = "updated"
    RECREATED = "recreated"

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_setup_message(self, guild_id: int) -> LanguageSetupMessage | None:
        result = await self.session.execute(
            select(LanguageSetupMessage).where(LanguageSetupMessage.guild_id == guild_id)
        )
        return result.scalar_one_or_none()

    async def save_setup_message(self, guild_id: int, channel_id: int, message_id: int) -> LanguageSetupMessage:
        setting = await self.get_setup_message(guild_id)
        if setting is None:
            setting = LanguageSetupMessage(guild_id=guild_id, channel_id=channel_id, message_id=message_id)
            self.session.add(setting)
        else:
            setting.channel_id = channel_id
            setting.message_id = message_id
        await self.session.commit()
        logger.info(
            "language_setup_message_tracking_saved",
            extra={"guild_id": guild_id, "channel_id": channel_id, "message_id": message_id},
        )
        return setting

    async def clear_setup_message(self, guild_id: int) -> int:
        result = await self.session.execute(
            delete(LanguageSetupMessage).where(LanguageSetupMessage.guild_id == guild_id)
        )
        await self.session.commit()
        return result.rowcount or 0

    async def supported_configured_languages(self, guild_id: int) -> list[str]:
        result = await self.session.execute(
            select(TranslationChannelSetting.target_language).where(
                TranslationChannelSetting.guild_id == guild_id,
            )
        )
        languages = []
        seen = set()
        for raw_language in result.scalars().all():
            language = LanguageService.normalize(raw_language)
            if not is_supported_language(language):
                logger.warning(
                    "language_setup_unsupported_mapping_skipped",
                    extra={"guild_id": guild_id, "target_language": language},
                )
                continue
            if language not in seen:
                languages.append(language)
                seen.add(language)
        return languages

    async def refresh_setup_message(
        self,
        guild: discord.Guild,
        channel: discord.TextChannel,
    ) -> LanguageSetupMessageRefreshResult:
        languages = await self.supported_configured_languages(guild.id)
        embed = build_language_setup_embed()
        view = LanguageSetupView(build_language_select_options(languages))
        tracked = await self.get_setup_message(guild.id)

        if tracked is not None:
            old_channel = guild.get_channel(tracked.channel_id)
            if old_channel is not None and tracked.channel_id == channel.id:
                try:
                    old_message = await old_channel.fetch_message(tracked.message_id)
                    await old_message.edit(embed=embed, view=view, allowed_mentions=discord.AllowedMentions.none())
                    logger.info(
                        "language_setup_message_updated",
                        extra={"guild_id": guild.id, "channel_id": channel.id, "message_id": tracked.message_id},
                    )
                    return LanguageSetupMessageRefreshResult(
                        self.UPDATED,
                        channel.id,
                        tracked.message_id,
                        languages,
                    )
                except discord.NotFound:
                    logger.warning(
                        "language_setup_message_missing",
                        extra={"guild_id": guild.id, "channel_id": tracked.channel_id, "message_id": tracked.message_id},
                    )
                except Exception as exc:
                    logger.warning(
                        "language_setup_message_missing",
                        extra={
                            "guild_id": guild.id,
                            "channel_id": tracked.channel_id,
                            "message_id": tracked.message_id,
                            "error_type": type(exc).__name__,
                        },
                    )
            elif old_channel is not None and hasattr(old_channel, "fetch_message"):
                try:
                    old_message = await old_channel.fetch_message(tracked.message_id)
                    await old_message.delete()
                except Exception:
                    pass
            else:
                logger.warning(
                    "language_setup_message_missing",
                    extra={"guild_id": guild.id, "channel_id": tracked.channel_id, "message_id": tracked.message_id},
                )

            message = await self._post_setup_message(channel, embed, view)
            await self.save_setup_message(guild.id, channel.id, message.id)
            logger.info(
                "language_setup_message_recreated",
                extra={"guild_id": guild.id, "channel_id": channel.id, "message_id": message.id},
            )
            return LanguageSetupMessageRefreshResult(self.RECREATED, channel.id, message.id, languages)

        message = await self._post_setup_message(channel, embed, view)
        await self.save_setup_message(guild.id, channel.id, message.id)
        logger.info(
            "language_setup_message_created",
            extra={"guild_id": guild.id, "channel_id": channel.id, "message_id": message.id},
        )
        return LanguageSetupMessageRefreshResult(self.CREATED, channel.id, message.id, languages)

    @staticmethod
    async def _post_setup_message(channel: discord.TextChannel, embed: discord.Embed, view: discord.ui.View):
        return await channel.send(
            embed=embed,
            view=view,
            allowed_mentions=discord.AllowedMentions.none(),
        )
