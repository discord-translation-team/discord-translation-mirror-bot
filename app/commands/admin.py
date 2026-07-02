from __future__ import annotations

import logging
import uuid
from datetime import datetime

import discord
from discord import app_commands
from discord.ext import commands
from sqlalchemy import delete, func, select

from app.database import Database
from app.mention_safety import sanitize_mentions
from app.models import (
    ChannelRoute,
    GuildUsageMonthly,
    LanguageRoleSetting,
    TranslationChannelSetting,
    UserLanguageSetting,
)
from app.services.language_service import LanguageService
from app.services.language_role_service import LanguageRoleService
from app.services.on_demand_translation_service import OnDemandTranslationService
from app.services.webhook_service import WebhookService
from app.translation.base import TranslationProvider, TranslationProviderError
from app.translation.output_cleaner import clean_translation_output
from app.ui.language_setup import LanguageSetupView, build_language_select_options, build_language_setup_embed

logger = logging.getLogger(__name__)


class AdminCommands(commands.Cog):
    def __init__(
        self,
        database: Database,
        translation_provider: TranslationProvider,
        webhook_service: WebhookService,
        max_message_chars: int,
        skip_messages_over_limit: bool,
        legacy_mirror_mode_enabled: bool,
        on_demand_channel_translation_enabled: bool,
        reaction_translation_enabled: bool,
        context_menu_translation_enabled: bool,
        reaction_translate_emoji: str,
        default_monthly_char_limit: int,
    ) -> None:
        self.database = database
        self.translation_provider = translation_provider
        self.webhook_service = webhook_service
        self.max_message_chars = max_message_chars
        self.skip_messages_over_limit = skip_messages_over_limit
        self.legacy_mirror_mode_enabled = legacy_mirror_mode_enabled
        self.on_demand_channel_translation_enabled = on_demand_channel_translation_enabled
        self.reaction_translation_enabled = reaction_translation_enabled
        self.context_menu_translation_enabled = context_menu_translation_enabled
        self.reaction_translate_emoji = reaction_translate_emoji
        self.default_monthly_char_limit = default_monthly_char_limit

    @app_commands.command(name="set_language", description="Set your translation target language")
    @app_commands.guild_only()
    async def set_language(self, interaction: discord.Interaction, target_language: str) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
            return

        language = LanguageService.normalize(target_language)
        if not LanguageService.validate(language):
            await interaction.response.send_message("Target language must look like `ru`, `en`, or `pt-br`.", ephemeral=True)
            return

        async with self.database.session() as session:
            result = await session.execute(
                select(UserLanguageSetting).where(
                    UserLanguageSetting.guild_id == interaction.guild.id,
                    UserLanguageSetting.user_id == interaction.user.id,
                )
            )
            setting = result.scalar_one_or_none()
            if setting is None:
                session.add(
                    UserLanguageSetting(
                        guild_id=interaction.guild.id,
                        user_id=interaction.user.id,
                        target_language=language,
                    )
                )
            else:
                setting.target_language = language
            await session.commit()
            role_sync = await LanguageRoleService(session).sync_member_language_role(
                interaction.guild,
                interaction.user,
                language,
            )

        if role_sync.status == LanguageRoleService.PERMISSIONS_FAILED:
            message = (
                f"Your language was saved as {LanguageService.display_name(language)}, but I could not update "
                "your Discord role. Please ask an admin to check my Manage Roles permission."
            )
        elif role_sync.status in {
            LanguageRoleService.MISSING_ROLE_MAPPING,
            LanguageRoleService.MISSING_DISCORD_ROLE,
        }:
            message = (
                f"Done — your translation language is now {LanguageService.display_name(language)}. "
                "React with 🌐 to translate messages. If you cannot see your translation channel, ask an admin "
                "to configure language roles."
            )
        else:
            message = (
                f"Done — your translation language is now {LanguageService.display_name(language)}. "
                "React with 🌐 to translate messages."
            )

        await interaction.response.send_message(message, ephemeral=True)

    @app_commands.command(name="my_language", description="Show your configured translation language")
    @app_commands.guild_only()
    async def my_language(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
            return

        async with self.database.session() as session:
            result = await session.execute(
                select(UserLanguageSetting.target_language).where(
                    UserLanguageSetting.guild_id == interaction.guild.id,
                    UserLanguageSetting.user_id == interaction.user.id,
                )
            )
            language = result.scalar_one_or_none()

        if language is None:
            await interaction.response.send_message("No language set. Use `/set_language target_language:ru`.", ephemeral=True)
        else:
            await interaction.response.send_message(f"Your translation language is `{language}`.", ephemeral=True)

    @app_commands.command(name="translation_channel_set", description="Set a language-specific translation channel")
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True)
    async def translation_channel_set(
        self,
        interaction: discord.Interaction,
        target_language: str,
        channel: discord.TextChannel,
    ) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
            return

        language = LanguageService.normalize(target_language)
        if not LanguageService.validate(language):
            await interaction.response.send_message("Target language must look like `ru`, `en`, or `pt-br`.", ephemeral=True)
            return

        async with self.database.session() as session:
            result = await session.execute(
                select(TranslationChannelSetting).where(
                    TranslationChannelSetting.guild_id == interaction.guild.id,
                    func.lower(func.trim(TranslationChannelSetting.target_language)) == language,
                )
            )
            setting = result.scalar_one_or_none()
            if setting is None:
                session.add(
                    TranslationChannelSetting(
                        guild_id=interaction.guild.id,
                        target_language=language,
                        channel_id=channel.id,
                    )
                )
            else:
                setting.channel_id = channel.id
            await session.commit()

        await interaction.response.send_message(
            f"Translations for `{language}` will be posted to {channel.mention}.",
            ephemeral=True,
        )

    @app_commands.command(name="translation_channel_list", description="List language-specific translation channels")
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True)
    async def translation_channel_list(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
            return

        async with self.database.session() as session:
            result = await session.execute(
                select(TranslationChannelSetting).where(TranslationChannelSetting.guild_id == interaction.guild.id)
            )
            settings = result.scalars().all()

        if not settings:
            await interaction.response.send_message("No translation channels configured.", ephemeral=True)
            return

        await interaction.response.send_message(
            "\n".join(f"`{setting.target_language}` -> <#{setting.channel_id}>" for setting in settings),
            ephemeral=True,
        )

    @app_commands.command(name="translation_channel_remove", description="Remove a language translation channel")
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True)
    async def translation_channel_remove(self, interaction: discord.Interaction, target_language: str) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
            return

        language = LanguageService.normalize(target_language)
        async with self.database.session() as session:
            result = await session.execute(
                delete(TranslationChannelSetting).where(
                    TranslationChannelSetting.guild_id == interaction.guild.id,
                    func.lower(func.trim(TranslationChannelSetting.target_language)) == language,
                )
            )
            await session.commit()

        await interaction.response.send_message(
            f"Removed {result.rowcount or 0} translation channel setting(s) for `{language}`.",
            ephemeral=True,
        )

    @app_commands.command(name="language_role_set", description="Set a language-specific Discord role")
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True)
    async def language_role_set(
        self,
        interaction: discord.Interaction,
        target_language: str,
        role: discord.Role,
    ) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
            return

        language = LanguageService.normalize(target_language)
        if not LanguageService.validate(language):
            await interaction.response.send_message("Target language must look like `ru`, `en`, or `pt-br`.", ephemeral=True)
            return

        async with self.database.session() as session:
            await LanguageRoleService(session).set_language_role(interaction.guild.id, language, role.id)

        await interaction.response.send_message(
            f"Language role for {LanguageService.display_name(language)} set to {role.mention}.",
            ephemeral=True,
        )

    @app_commands.command(name="language_role_list", description="List language-specific Discord roles")
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True)
    async def language_role_list(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
            return

        async with self.database.session() as session:
            settings = await LanguageRoleService(session).list_language_roles(interaction.guild.id)

        if not settings:
            await interaction.response.send_message("No language roles configured.", ephemeral=True)
            return

        await interaction.response.send_message(
            "\n".join(
                f"{LanguageService.display_name(setting.target_language)} "
                f"({LanguageService.normalize(setting.target_language)}) -> <@&{setting.role_id}>"
                for setting in settings
            ),
            ephemeral=True,
        )

    @app_commands.command(name="language_role_remove", description="Remove a language-specific Discord role")
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True)
    async def language_role_remove(self, interaction: discord.Interaction, target_language: str) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
            return

        language = LanguageService.normalize(target_language)
        async with self.database.session() as session:
            await LanguageRoleService(session).remove_language_role(interaction.guild.id, language)

        await interaction.response.send_message(
            f"Language role for {LanguageService.display_name(language)} removed.",
            ephemeral=True,
        )

    @app_commands.command(name="language_setup_message", description="Post a persistent language setup dropdown")
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True)
    async def language_setup_message(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
    ) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
            return

        async with self.database.session() as session:
            result = await session.execute(
                select(TranslationChannelSetting.target_language).where(
                    TranslationChannelSetting.guild_id == interaction.guild.id,
                )
            )
            languages = [LanguageService.normalize(language) for language in result.scalars().all()]

        if not languages:
            await interaction.response.send_message(
                "No translation channels configured yet. Use /translation_channel_set first.",
                ephemeral=True,
            )
            return

        options = build_language_select_options(languages)
        await channel.send(
            embed=build_language_setup_embed(),
            view=LanguageSetupView(options),
            allowed_mentions=discord.AllowedMentions.none(),
        )
        logger.info(
            "language_setup_message_posted",
            extra={
                "guild_id": interaction.guild.id,
                "channel_id": channel.id,
                "configured_language_count": len(options),
            },
        )
        await interaction.response.send_message(
            f"Language setup message posted in {channel.mention}.",
            ephemeral=True,
        )

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
            exact_match = await session.execute(
                select(ChannelRoute).where(
                    ChannelRoute.guild_id == interaction.guild.id,
                    ChannelRoute.source_channel_id == source_channel.id,
                    ChannelRoute.target_channel_id == target_channel.id,
                    ChannelRoute.target_language == language,
                )
            )
            route = exact_match.scalars().first()
            if route is not None and route.is_active:
                await interaction.followup.send("This route already exists.", ephemeral=True)
                return

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
            routes = result.scalars().all()
            if not routes:
                await interaction.response.send_message("No active matching route found.", ephemeral=True)
                return
            for route in routes:
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
            f"Deactivated {len(routes)} route(s) for {source_channel.mention} -> `{language}`.",
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
            channel_count_result = await session.execute(
                select(func.count(TranslationChannelSetting.id)).where(
                    TranslationChannelSetting.guild_id == interaction.guild.id,
                )
            )
            translation_channel_count = channel_count_result.scalar_one()
            role_count_result = await session.execute(
                select(func.count(LanguageRoleSetting.id)).where(
                    LanguageRoleSetting.guild_id == interaction.guild.id,
                )
            )
            language_role_count = role_count_result.scalar_one()
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
                    f"Legacy mirror mode enabled: `{'yes' if self.legacy_mirror_mode_enabled else 'no'}`",
                    f"On-demand channel translation enabled: `{'yes' if self.on_demand_channel_translation_enabled else 'no'}`",
                    f"Reaction translation enabled: `{'yes' if self.reaction_translation_enabled else 'no'}`",
                    f"Reaction emoji: `{self.reaction_translate_emoji}`",
                    f"Context menu enabled: `{'yes' if self.context_menu_translation_enabled else 'no'}`",
                    "Language setup menu enabled: `true`",
                    f"Provider: `{self.translation_provider.name}`",
                    f"Model: `{self._provider_model()}`",
                    f"Active legacy routes: `{route_count}`",
                    f"Configured translation channels: `{translation_channel_count}`",
                    f"Configured language roles: `{language_role_count}`",
                    f"Monthly input tokens: `{usage.input_tokens_used if usage else 0}`",
                    f"Monthly output tokens: `{usage.output_tokens_used if usage else 0}`",
                    f"Monthly character count: `{usage.characters_used if usage else 0}`",
                    f"Max message chars: `{self.max_message_chars}`",
                    f"Skip over limit: `{'yes' if self.skip_messages_over_limit else 'no'}`",
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
            sanitize_mentions(clean_translation_output(translated.translated_text)),
            allowed_mentions=discord.AllowedMentions.none(),
            ephemeral=True,
        )

    def _provider_model(self) -> str:
        return self.translation_provider.model_name or self.translation_provider.name

    def _on_demand_service(self, session) -> OnDemandTranslationService:
        service = OnDemandTranslationService(session, self.translation_provider, self.webhook_service)
        service.max_message_chars = self.max_message_chars
        service.skip_messages_over_limit = self.skip_messages_over_limit
        service.default_monthly_char_limit = self.default_monthly_char_limit
        return service

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


async def translate_context_menu_callback(
    interaction: discord.Interaction,
    message: discord.Message,
) -> None:
    if interaction.guild is None:
        await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
        return

    bot = interaction.client
    settings = getattr(bot, "settings", None)
    database = getattr(bot, "database", None)
    translation_provider = getattr(bot, "translation_provider", None)
    webhook_service = getattr(bot, "webhook_service", None)
    if (
        settings is None
        or database is None
        or translation_provider is None
        or webhook_service is None
        or not settings.on_demand_channel_translation_enabled
        or not settings.context_menu_translation_enabled
    ):
        await interaction.response.send_message("On-demand translation is disabled.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)
    async with database.session() as session:
        service = OnDemandTranslationService(session, translation_provider, webhook_service)
        service.max_message_chars = settings.max_message_chars
        service.skip_messages_over_limit = settings.skip_messages_over_limit
        service.default_monthly_char_limit = settings.default_monthly_char_limit
        result = await service.publish_for_user(
            message,
            interaction.user.id,
            trigger="context_menu",
            request_id=uuid.uuid4().hex,
        )

    if result.status == "posted":
        await interaction.followup.send(f"Translation posted to <#{result.target_channel_id}>.", ephemeral=True)
    elif result.status == "duplicate":
        await interaction.followup.send(f"Already translated in <#{result.target_channel_id}>.", ephemeral=True)
    elif result.status == "missing_language":
        await interaction.followup.send("Set your language first with `/set_language target_language:ru`.", ephemeral=True)
    elif result.status == "missing_channel":
        await interaction.followup.send(
            f"No translation channel configured for `{result.target_language}`.",
            ephemeral=True,
        )
    elif result.status == "empty_message":
        await interaction.followup.send("That message has no text to translate.", ephemeral=True)
    else:
        await interaction.followup.send("Translation could not be posted.", ephemeral=True)


translate_context_menu = app_commands.ContextMenu(
    name="Translate",
    callback=translate_context_menu_callback,
)


def register_translate_context_menu(tree: app_commands.CommandTree) -> None:
    if tree.get_command("Translate", type=discord.AppCommandType.message) is not None:
        return
    try:
        tree.add_command(translate_context_menu)
    except app_commands.CommandAlreadyRegistered:
        return
