from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands

from app.commands.admin import AdminCommands, register_translate_context_menu
from app.config import Settings, configure_logging, load_settings
from app.database import Database
from app.services.on_demand_translation_service import OnDemandTranslationService
from app.services.relay_service import RelayService
from app.services.webhook_service import WebhookService
from app.translation.base import TranslationProvider
from app.translation.gemini_provider import GeminiTranslationProvider
from app.translation.mock_provider import MockTranslationProvider
from app.translation.openai_provider import OpenAITranslationProvider

logger = logging.getLogger(__name__)


def build_translation_provider(settings: Settings) -> TranslationProvider:
    if settings.translation_provider == "mock":
        return MockTranslationProvider()
    if settings.translation_provider == "gemini":
        return GeminiTranslationProvider(
            api_key=settings.gemini_api_key,
            model_name=settings.gemini_translation_model,
        )
    if settings.translation_provider == "openai":
        return OpenAITranslationProvider(
            api_key=settings.openai_api_key,
            model_name=settings.openai_translation_model,
            quality_model_name=settings.openai_translation_quality_model,
        )
    raise RuntimeError("TRANSLATION_PROVIDER must be one of: mock, gemini, openai")


class TranslationMirrorBot(commands.Bot):
    def __init__(self, settings: Settings) -> None:
        intents = discord.Intents.default()
        intents.guilds = True
        intents.messages = True
        intents.message_content = True
        if hasattr(intents, "webhooks"):
            intents.webhooks = True
        super().__init__(command_prefix="!", intents=intents)

        self.settings = settings
        self.database = Database(settings.database_url)
        self.translation_provider = build_translation_provider(settings)
        self.webhook_service = WebhookService()
        self.tree.on_error = self.on_tree_error

    async def setup_hook(self) -> None:
        await self.database.create_tables()
        await self.add_cog(
            AdminCommands(
                self.database,
                self.translation_provider,
                self.webhook_service,
                max_message_chars=self.settings.max_message_chars,
                skip_messages_over_limit=self.settings.skip_messages_over_limit,
                legacy_mirror_mode_enabled=self.settings.legacy_mirror_mode_enabled,
                on_demand_channel_translation_enabled=self.settings.on_demand_channel_translation_enabled,
                reaction_translation_enabled=self.settings.reaction_translation_enabled,
                context_menu_translation_enabled=self.settings.context_menu_translation_enabled,
                reaction_translate_emoji=self.settings.reaction_translate_emoji,
                default_monthly_char_limit=self.settings.default_monthly_char_limit,
            )
        )
        if self.settings.context_menu_translation_enabled:
            register_translate_context_menu(self.tree)
        synced = await self.tree.sync()
        logger.info("slash_commands_synced", extra={"count": len(synced)})

    async def on_tree_error(
        self,
        interaction: discord.Interaction,
        error: app_commands.AppCommandError,
    ) -> None:
        if isinstance(error, app_commands.TransformerError):
            message = (
                "I could not resolve that channel. Please select the channel from Discord's channel picker, "
                "for example #global-original, not plain text."
            )
        else:
            logger.error(
                "app_command_failed",
                exc_info=(type(error), error, error.__traceback__),
            )
            message = "Something went wrong while running that command."

        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)

    async def close(self) -> None:
        await self.database.close()
        await super().close()

    async def on_ready(self) -> None:
        logger.info(
            "bot_ready",
            extra={
                "bot_user_id": self.user.id if self.user else None,
                "guild_count": len(self.guilds),
                "guilds": [{"id": guild.id, "name": guild.name} for guild in self.guilds],
                "provider": self.settings.translation_provider,
            },
        )

    async def on_message(self, message: discord.Message) -> None:
        if message.guild is None or message.author.bot or message.webhook_id is not None:
            return

        if not self.settings.legacy_mirror_mode_enabled:
            return

        async with self.database.session() as session:
            await self._relay_service(session).relay_message(message)

    async def on_message_edit(self, before: discord.Message, after: discord.Message) -> None:
        if after.guild is None or after.author.bot or after.webhook_id is not None:
            return

        async with self.database.session() as session:
            if self.settings.on_demand_channel_translation_enabled:
                await self._on_demand_service(session).sync_edited_message(after)
            if self.settings.legacy_mirror_mode_enabled:
                await self._relay_service(session).sync_edited_message(after)

    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent) -> None:
        if payload.guild_id is None:
            return

        guild = self.get_guild(payload.guild_id)
        if guild is None:
            logger.warning(
                "delete_sync_guild_not_found",
                extra={"guild_id": payload.guild_id, "message_id": payload.message_id},
            )
            return

        async with self.database.session() as session:
            if self.settings.on_demand_channel_translation_enabled:
                await self._on_demand_service(session).sync_deleted_message(guild, payload.message_id)
            if self.settings.legacy_mirror_mode_enabled:
                await self._relay_service(session).sync_deleted_message(guild, payload.message_id)

    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent) -> None:
        if (
            payload.guild_id is None
            or payload.user_id == (self.user.id if self.user else None)
            or str(payload.emoji) != self.settings.reaction_translate_emoji
            or not self.settings.on_demand_channel_translation_enabled
            or not self.settings.reaction_translation_enabled
        ):
            return

        channel = self.get_channel(payload.channel_id)
        if channel is None:
            try:
                channel = await self.fetch_channel(payload.channel_id)
            except discord.DiscordException as exc:
                logger.warning(
                    "reaction_translation_channel_fetch_failed",
                    extra={
                        "guild_id": payload.guild_id,
                        "channel_id": payload.channel_id,
                        "message_id": payload.message_id,
                        "error_type": type(exc).__name__,
                    },
                )
                return
        if not isinstance(channel, discord.TextChannel):
            return

        try:
            message = await channel.fetch_message(payload.message_id)
        except discord.DiscordException as exc:
            logger.warning(
                "reaction_translation_message_fetch_failed",
                extra={
                    "guild_id": payload.guild_id,
                    "channel_id": payload.channel_id,
                    "message_id": payload.message_id,
                    "error_type": type(exc).__name__,
                },
            )
            return

        async with self.database.session() as session:
            result = await self._on_demand_service(session).publish_for_user(
                message,
                payload.user_id,
                trigger="reaction",
            )

        if result.status == "missing_language" and self.settings.on_demand_setup_dm_fallback:
            try:
                user = self.get_user(payload.user_id) or await self.fetch_user(payload.user_id)
                await user.send("Set your translation language in the server with `/set_language target_language:ru`.")
            except discord.DiscordException:
                pass

    def _relay_service(self, session) -> RelayService:
        relay_service = RelayService(session, self.translation_provider, self.webhook_service)
        relay_service.max_message_chars = self.settings.max_message_chars
        relay_service.skip_messages_over_limit = self.settings.skip_messages_over_limit
        relay_service.default_monthly_char_limit = self.settings.default_monthly_char_limit
        return relay_service

    def _on_demand_service(self, session) -> OnDemandTranslationService:
        service = OnDemandTranslationService(session, self.translation_provider, self.webhook_service)
        service.max_message_chars = self.settings.max_message_chars
        service.skip_messages_over_limit = self.settings.skip_messages_over_limit
        service.default_monthly_char_limit = self.settings.default_monthly_char_limit
        return service


def main() -> None:
    settings = load_settings()
    configure_logging(settings.log_level)
    logger.info(
        "translation_config",
        extra={
            "openai_api_key_present": "yes" if bool(settings.openai_api_key) else "no",
            "openai_translation_model": settings.openai_translation_model,
            "legacy_mirror_mode_enabled": "yes" if settings.legacy_mirror_mode_enabled else "no",
            "on_demand_channel_translation_enabled": "yes"
            if settings.on_demand_channel_translation_enabled
            else "no",
            "reaction_translation_enabled": "yes" if settings.reaction_translation_enabled else "no",
            "context_menu_translation_enabled": "yes" if settings.context_menu_translation_enabled else "no",
        },
    )
    bot = TranslationMirrorBot(settings)
    bot.run(settings.discord_bot_token, log_handler=None)


if __name__ == "__main__":
    main()
