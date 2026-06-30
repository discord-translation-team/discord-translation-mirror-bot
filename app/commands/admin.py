from __future__ import annotations

import logging
from datetime import datetime

import discord
from discord import app_commands
from discord.ext import commands
from sqlalchemy import func, select

from app.database import Database
from app.mention_safety import sanitize_mentions
from app.models import ChannelRoute, GuildUsageMonthly
from app.services.language_service import LanguageService
from app.services.webhook_service import WebhookService
from app.translation.base import TranslationProvider, TranslationProviderError

logger = logging.getLogger(__name__)


class AdminCommands(commands.Cog):
    def __init__(
        self,
        database: Database,
        translation_provider: TranslationProvider,
        webhook_service: WebhookService,
    ) -> None:
        self.database = database
        self.translation_provider = translation_provider
        self.webhook_service = webhook_service

    @app_commands.command(name="translate_setup", description="Create or update a translation mirror route")
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True)
    async def setup(
        self,
        interaction: discord.Interaction,
        source_channel: discord.TextChannel,
        target_channel: discord.TextChannel,
        target_language: str,
    ) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
            return

        language = LanguageService.normalize(target_language)
        if not LanguageService.validate(language):
            await interaction.response.send_message("Target language must look like `ru`, `en`, or `pt-br`.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        webhook = await self.webhook_service.create_or_reuse(target_channel)
        if webhook.token is None:
            await interaction.followup.send("Could not create a usable webhook in the target channel.", ephemeral=True)
            return

        async with self.database.session() as session:
            existing = await session.execute(
                select(ChannelRoute).where(
                    ChannelRoute.guild_id == interaction.guild.id,
                    ChannelRoute.source_channel_id == source_channel.id,
                    ChannelRoute.target_language == language,
                )
            )
            route = existing.scalar_one_or_none()
            if route is None:
                route = ChannelRoute(
                    guild_id=interaction.guild.id,
                    source_channel_id=source_channel.id,
                    target_channel_id=target_channel.id,
                    target_language=language,
                    webhook_id=webhook.id,
                    webhook_token=webhook.token,
                    is_active=True,
                )
                session.add(route)
            else:
                route.target_channel_id = target_channel.id
                route.webhook_id = webhook.id
                route.webhook_token = webhook.token
                route.is_active = True
            await session.commit()

        logger.info(
            "route_configured",
            extra={
                "guild_id": interaction.guild.id,
                "source_channel_id": source_channel.id,
                "target_channel_id": target_channel.id,
                "target_language": language,
            },
        )
        await interaction.followup.send(
            f"Translation route active: {source_channel.mention} -> {target_channel.mention} (`{language}`).",
            ephemeral=True,
        )

    @app_commands.command(name="translate_list", description="List active translation mirror routes")
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True)
    async def list_routes(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
            return

        async with self.database.session() as session:
            result = await session.execute(
                select(ChannelRoute).where(
                    ChannelRoute.guild_id == interaction.guild.id,
                    ChannelRoute.is_active.is_(True),
                )
            )
            routes = result.scalars().all()

        if not routes:
            await interaction.response.send_message("No active translation routes configured.", ephemeral=True)
            return

        lines = []
        for route in routes:
            lines.append(
                f"<#{route.source_channel_id}> -> <#{route.target_channel_id}> (`{route.target_language}`)"
            )
        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    @app_commands.command(name="translate_remove", description="Remove a translation mirror route")
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True)
    async def remove(
        self,
        interaction: discord.Interaction,
        source_channel: discord.TextChannel,
        target_language: str,
    ) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
            return

        language = LanguageService.normalize(target_language)
        async with self.database.session() as session:
            result = await session.execute(
                select(ChannelRoute).where(
                    ChannelRoute.guild_id == interaction.guild.id,
                    ChannelRoute.source_channel_id == source_channel.id,
                    ChannelRoute.target_language == language,
                    ChannelRoute.is_active.is_(True),
                )
            )
            route = result.scalar_one_or_none()
            if route is None:
                await interaction.response.send_message("No active matching route found.", ephemeral=True)
                return
            route.is_active = False
            await session.commit()

        logger.info(
            "route_removed",
            extra={
                "guild_id": interaction.guild.id,
                "source_channel_id": source_channel.id,
                "target_language": language,
            },
        )
        await interaction.response.send_message(
            f"Removed route for {source_channel.mention} -> `{language}`.",
            ephemeral=True,
        )

    @app_commands.command(name="translate_status", description="Show translation bot status")
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True)
    async def status(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
            return

        db_ok = await self.database.healthcheck()
        async with self.database.session() as session:
            count_result = await session.execute(
                select(func.count(ChannelRoute.id)).where(
                    ChannelRoute.guild_id == interaction.guild.id,
                    ChannelRoute.is_active.is_(True),
                )
            )
            route_count = count_result.scalar_one()
            usage_result = await session.execute(
                select(GuildUsageMonthly).where(
                    GuildUsageMonthly.guild_id == interaction.guild.id,
                    GuildUsageMonthly.month == datetime.utcnow().strftime("%Y-%m"),
                    GuildUsageMonthly.provider == self.translation_provider.name,
                    GuildUsageMonthly.model == self._provider_model(),
                )
            )
            usage = usage_result.scalar_one_or_none()

        await interaction.response.send_message(
            "\n".join(
                [
                    "Translation Mirror Bot is running.",
                    f"Provider: `{self.translation_provider.name}`",
                    f"Model: `{self._provider_model()}`",
                    f"Active routes: `{route_count}`",
                    f"Monthly input tokens: `{usage.input_tokens_used if usage else 0}`",
                    f"Monthly output tokens: `{usage.output_tokens_used if usage else 0}`",
                    f"Monthly character count: `{usage.characters_used if usage else 0}`",
                    f"Database: `{'ok' if db_ok else 'unavailable'}`",
                ]
            ),
            ephemeral=True,
        )

    @app_commands.command(name="translate_test", description="Preview a translation without saving anything")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def test(self, interaction: discord.Interaction, text: str, target_language: str) -> None:
        language = LanguageService.normalize(target_language)
        if not LanguageService.validate(language):
            await interaction.response.send_message("Target language must look like `ru`, `en`, or `pt-br`.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        try:
            translated = await self.translation_provider.translate(text, language)
        except TranslationProviderError as exc:
            logger.error(
                "translate_test_failed",
                extra={
                    "guild_id": interaction.guild.id if interaction.guild else None,
                    **exc.log_extra(),
                },
            )
            await interaction.followup.send(
                f"{exc.provider.title()} API error: {exc.error_summary}",
                ephemeral=True,
            )
            return
        except Exception as exc:
            logger.error(
                "translate_test_failed",
                extra={
                    "guild_id": interaction.guild.id if interaction.guild else None,
                    "provider": self.translation_provider.name,
                    "model": self._provider_model(),
                    "error_type": type(exc).__name__,
                },
            )
            await interaction.followup.send("Translation failed. Please check the bot logs and provider configuration.", ephemeral=True)
            return

        await interaction.followup.send(
            sanitize_mentions(translated.translated_text),
            allowed_mentions=discord.AllowedMentions.none(),
            ephemeral=True,
        )

    def _provider_model(self) -> str:
        return self.translation_provider.model_name or self.translation_provider.name

    async def cog_app_command_error(
        self,
        interaction: discord.Interaction,
        error: app_commands.AppCommandError,
    ) -> None:
        await on_app_command_error(interaction, error)


async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
    if isinstance(error, app_commands.MissingPermissions):
        message = "You need the Manage Server permission to use translation admin commands."
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
