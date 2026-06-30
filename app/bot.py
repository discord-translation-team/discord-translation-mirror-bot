from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands

from app.commands.admin import AdminCommands
from app.config import Settings, configure_logging, load_settings
from app.database import Database
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
        await self.add_cog(AdminCommands(self.database, self.translation_provider, self.webhook_service))
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

        async with self.database.session() as session:
            relay_service = RelayService(session, self.translation_provider, self.webhook_service)
            relay_service.max_message_chars = self.settings.max_message_chars
            relay_service.skip_messages_over_limit = self.settings.skip_messages_over_limit
            relay_service.default_monthly_char_limit = self.settings.default_monthly_char_limit
            await relay_service.relay_message(message)


def main() -> None:
    settings = load_settings()
    configure_logging(settings.log_level)
    logger.info(
        "translation_config",
        extra={
            "openai_api_key_present": "yes" if bool(settings.openai_api_key) else "no",
            "openai_translation_model": settings.openai_translation_model,
        },
    )
    bot = TranslationMirrorBot(settings)
    bot.run(settings.discord_bot_token, log_handler=None)


if __name__ == "__main__":
    main()
