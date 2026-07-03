from __future__ import annotations

import logging
import uuid
from datetime import datetime

import discord
from discord import app_commands
from discord.ext import commands
from sqlalchemy import delete, func, select

from app.database import Database
from app.languages import format_supported_languages, is_supported_language, suggest_language_code
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

DEFAULT_SETUP_LANGUAGES = ["ru", "en", "fr", "ar", "tr", "es", "uk"]
TRANSLATIONS_CATEGORY_NAME = "🌐 Translations"
SETUP_CHANNEL_NAME = "choose-language"
SOURCE_CHANNEL_NAME = "global-chat"


def unsupported_language_message(language: str) -> str:
    suggestion = suggest_language_code(language)
    if suggestion is not None:
        if LanguageService.normalize(language) == "eg":
            return (
                "Unsupported language code: eg. Use ar for Arabic. EG is a country code, "
                "not a language code."
            )
        return (
            f"Unsupported language code: {LanguageService.normalize(language)}. "
            f"Use {suggestion} for {LanguageService.display_name(suggestion)}."
        )
    return (
        f"Unsupported language code: {LanguageService.normalize(language)}. "
        f"Supported languages: {format_supported_languages()}"
    )


def format_language_mapping_label(language: str) -> str:
    normalized = LanguageService.normalize(language)
    if is_supported_language(normalized):
        return f"{LanguageService.display_name(normalized)} ({normalized})"
    return f"{normalized.upper()} (unsupported)"


def parse_setup_language_list(languages: str | None) -> list[str]:
    if languages is None or not languages.strip():
        return list(DEFAULT_SETUP_LANGUAGES)

    parsed: list[str] = []
    seen: set[str] = set()
    for raw_language in languages.split(","):
        if not raw_language.strip():
            continue
        language = LanguageService.normalize(raw_language)
        if not LanguageService.validate(language):
            raise ValueError(unsupported_language_message(raw_language))
        if language not in seen:
            parsed.append(language)
            seen.add(language)

    return parsed or list(DEFAULT_SETUP_LANGUAGES)


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
            await interaction.response.send_message(unsupported_language_message(target_language), ephemeral=True)
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
        elif not is_supported_language(language):
            await interaction.response.send_message(
                (
                    f"Your translation language is currently {LanguageService.normalize(language).upper()}, "
                    "which is unsupported. Please choose a supported language in #choose-language or use "
                    "/set_language."
                ),
                ephemeral=True,
            )
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
            await interaction.response.send_message(unsupported_language_message(target_language), ephemeral=True)
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

        has_unsupported = any(not is_supported_language(setting.target_language) for setting in settings)
        lines = [
            f"{format_language_mapping_label(setting.target_language)} -> <#{setting.channel_id}>"
            for setting in settings
        ]
        if has_unsupported:
            lines.append("")
            lines.append("Remove unsupported mappings with /translation_channel_remove.")
        await interaction.response.send_message("\n".join(lines), ephemeral=True)

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

        if is_supported_language(language):
            message = f"Removed {result.rowcount or 0} translation channel setting(s) for `{language}`."
        else:
            message = f"Removed legacy unsupported language mapping: {language}."
        await interaction.response.send_message(message, ephemeral=True)

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
            await interaction.response.send_message(unsupported_language_message(target_language), ephemeral=True)
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
                f"{format_language_mapping_label(setting.target_language)} -> <@&{setting.role_id}>"
                for setting in settings
            )
            + (
                "\n\nRemove unsupported mappings with /language_role_remove."
                if any(not is_supported_language(setting.target_language) for setting in settings)
                else ""
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

        if is_supported_language(language):
            message = f"Language role for {LanguageService.display_name(language)} removed."
        else:
            message = f"Removed legacy unsupported language mapping: {language}."
        await interaction.response.send_message(message, ephemeral=True)

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
            configured_languages = [LanguageService.normalize(language) for language in result.scalars().all()]

        languages = []
        for language in configured_languages:
            if is_supported_language(language):
                languages.append(language)
            else:
                logger.warning(
                    "language_setup_unsupported_mapping_skipped",
                    extra={"guild_id": interaction.guild.id, "target_language": language},
                )

        if not languages:
            await interaction.response.send_message(
                "No supported translation channels configured yet. Use /translation_channel_set first.",
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

    @app_commands.command(name="setup_server", description="Create channels, roles, mappings, and setup menu")
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True)
    async def setup_server(
        self,
        interaction: discord.Interaction,
        languages: str = "",
        source_channel: discord.TextChannel = None,
    ) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
            return

        try:
            language_codes = parse_setup_language_list(languages)
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return

        logger.info(
            "setup_server_started",
            extra={
                "guild_id": interaction.guild.id,
                "user_id": interaction.user.id,
                "language_count": len(language_codes),
            },
        )

        await interaction.response.defer(ephemeral=True)
        try:
            summary = await self._setup_server(interaction, language_codes, source_channel)
        except (discord.Forbidden, PermissionError):
            logger.warning(
                "setup_server_failed_permissions",
                extra={"guild_id": interaction.guild.id, "user_id": interaction.user.id},
            )
            await interaction.followup.send(
                "I need Manage Channels to create channels and permissions.",
                ephemeral=True,
            )
            return

        logger.info(
            "setup_server_completed",
            extra={
                "guild_id": interaction.guild.id,
                "user_id": interaction.user.id,
                "language_count": len(language_codes),
            },
        )
        await interaction.followup.send(summary, ephemeral=True)

    @app_commands.command(name="setup_check", description="Check Discord server setup for translation features")
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True)
    async def setup_check(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
            return

        report, issue_count, warning_count = await self._build_setup_check_report(interaction)
        logger.info(
            "setup_check_ran",
            extra={
                "guild_id": interaction.guild.id,
                "user_id": interaction.user.id,
                "issue_count": issue_count,
                "warning_count": warning_count,
            },
        )

        pages = self._split_setup_check_report(report)
        await interaction.response.send_message(pages[0], ephemeral=True)
        for page in pages[1:]:
            await interaction.followup.send(page, ephemeral=True)

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

    async def _setup_server(
        self,
        interaction: discord.Interaction,
        language_codes: list[str],
        source_channel: discord.TextChannel = None,
    ) -> str:
        guild = interaction.guild
        if guild is None:
            return "This command can only be used in a server."

        bot_member = self._setup_check_bot_member(interaction)
        guild_permissions = getattr(bot_member, "guild_permissions", None)
        can_manage_channels = bool(getattr(guild_permissions, "manage_channels", False))
        can_manage_roles = bool(getattr(guild_permissions, "manage_roles", False))

        if not can_manage_channels:
            logger.warning(
                "setup_server_failed_permissions",
                extra={"guild_id": guild.id, "missing_permission": "manage_channels"},
            )
            return "I need Manage Channels to create channels and permissions."

        warnings: list[str] = []
        if not can_manage_roles:
            warnings.append("I need Manage Roles to create/sync language roles.")

        category, category_created = await self._get_or_create_category(guild)
        setup_channel, setup_channel_created = await self._get_or_create_text_channel(
            guild,
            SETUP_CHANNEL_NAME,
            category,
            self._setup_channel_overwrites(guild, bot_member),
        )
        if source_channel is None:
            source_channel, source_channel_created = await self._get_or_create_text_channel(
                guild,
                SOURCE_CHANNEL_NAME,
                category,
                self._source_channel_overwrites(guild, bot_member),
            )
            source_channel_mode = "managed"
        else:
            source_channel_created = False
            source_channel_mode = "existing"
            warnings.extend(self._source_channel_permission_warnings(source_channel, bot_member))

        roles_by_language: dict[str, object | None] = {}
        role_created_by_language: dict[str, bool] = {}
        if can_manage_roles:
            for language in language_codes:
                role, created, warning = await self._get_or_create_language_role(guild, language)
                roles_by_language[language] = role
                role_created_by_language[language] = created
                if warning:
                    warnings.append(warning)
        else:
            for language in language_codes:
                roles_by_language[language] = self._find_role(guild, f"lang-{language}")
                role_created_by_language[language] = False

        channel_by_language: dict[str, object] = {}
        channel_created_by_language: dict[str, bool] = {}
        for language in language_codes:
            role = roles_by_language.get(language)
            channel, created = await self._get_or_create_text_channel(
                guild,
                f"{language}-translation",
                category,
                self._translation_channel_overwrites(guild, bot_member, role),
            )
            channel_by_language[language] = channel
            channel_created_by_language[language] = created

        async with self.database.session() as session:
            for language, channel in channel_by_language.items():
                await self._save_translation_channel_mapping(session, guild.id, language, channel.id)
                role = roles_by_language.get(language)
                if role is not None and can_manage_roles:
                    await LanguageRoleService(session).set_language_role(guild.id, language, role.id)
                logger.info(
                    "setup_server_mapping_saved",
                    extra={
                        "guild_id": guild.id,
                        "target_language": language,
                        "channel_id": channel.id,
                        "role_id": role.id if role is not None else None,
                    },
                )

        setup_languages = list(channel_by_language.keys())
        try:
            await setup_channel.send(
                embed=build_language_setup_embed(),
                view=LanguageSetupView(build_language_select_options(setup_languages)),
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.DiscordException as exc:
            warnings.append("Could not post the setup message. Check bot Send Messages permission in #choose-language.")
            logger.warning(
                "setup_server_setup_message_failed",
                extra={"guild_id": guild.id, "error_type": type(exc).__name__},
            )

        bot_top_role = getattr(bot_member, "top_role", None)
        if can_manage_roles and bot_top_role is not None:
            hierarchy_problem = any(
                role is not None and not self._role_above(bot_top_role, role)
                for role in roles_by_language.values()
            )
            if hierarchy_problem:
                warnings.append(
                    "Move the bot role above all lang-* roles in Server Settings -> Roles, otherwise role assignment will fail."
                )

        warnings.append("Delete old setup messages manually if duplicated.")
        return self._format_setup_server_summary(
            category,
            category_created,
            setup_channel,
            setup_channel_created,
            source_channel,
            source_channel_created,
            source_channel_mode,
            channel_by_language,
            channel_created_by_language,
            roles_by_language,
            role_created_by_language,
            warnings,
        )

    async def _get_or_create_category(self, guild):
        existing = self._find_category(guild, TRANSLATIONS_CATEGORY_NAME)
        if existing is not None:
            logger.info("setup_server_reused_category", extra={"guild_id": guild.id, "category_id": existing.id})
            return existing, False
        category = await guild.create_category(TRANSLATIONS_CATEGORY_NAME, reason="Translation server setup")
        logger.info("setup_server_created_category", extra={"guild_id": guild.id, "category_id": category.id})
        return category, True

    async def _get_or_create_text_channel(self, guild, name: str, category, overwrites: dict):
        existing = self._find_text_channel(guild, name)
        if existing is not None:
            if hasattr(existing, "edit"):
                await existing.edit(category=category, overwrites=overwrites, reason="Translation server setup")
            logger.info("setup_server_reused_channel", extra={"guild_id": guild.id, "channel_id": existing.id})
            return existing, False
        channel = await guild.create_text_channel(
            name,
            category=category,
            overwrites=overwrites,
            reason="Translation server setup",
        )
        logger.info("setup_server_created_channel", extra={"guild_id": guild.id, "channel_id": channel.id})
        return channel, True

    async def _get_or_create_language_role(self, guild, language: str):
        role_name = f"lang-{language}"
        existing = self._find_role(guild, role_name)
        if existing is not None:
            logger.info("setup_server_reused_role", extra={"guild_id": guild.id, "role_id": existing.id})
            return existing, False, None
        try:
            role = await guild.create_role(
                name=role_name,
                permissions=discord.Permissions.none(),
                reason="Translation server setup",
            )
        except (discord.Forbidden, PermissionError) as exc:
            logger.warning(
                "setup_server_failed_permissions",
                extra={"guild_id": guild.id, "missing_permission": "manage_roles", "error_type": type(exc).__name__},
            )
            return None, False, f"I could not create @{role_name}. Check my Manage Roles permission and role hierarchy."
        logger.info("setup_server_created_role", extra={"guild_id": guild.id, "role_id": role.id})
        return role, True, None

    async def _save_translation_channel_mapping(self, session, guild_id: int, language: str, channel_id: int) -> None:
        result = await session.execute(
            select(TranslationChannelSetting).where(
                TranslationChannelSetting.guild_id == guild_id,
                func.lower(func.trim(TranslationChannelSetting.target_language)) == language,
            )
        )
        setting = result.scalar_one_or_none()
        if setting is None:
            session.add(
                TranslationChannelSetting(
                    guild_id=guild_id,
                    target_language=language,
                    channel_id=channel_id,
                )
            )
        else:
            setting.target_language = language
            setting.channel_id = channel_id
        await session.commit()

    def _setup_channel_overwrites(self, guild, bot_member) -> dict:
        return {
            guild.default_role: discord.PermissionOverwrite(
                view_channel=True,
                send_messages=False,
                read_message_history=True,
            ),
            bot_member: discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                embed_links=True,
                read_message_history=True,
            ),
        }

    def _source_channel_overwrites(self, guild, bot_member) -> dict:
        return {
            guild.default_role: discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                read_message_history=True,
                add_reactions=True,
            ),
            bot_member: discord.PermissionOverwrite(
                view_channel=True,
                read_message_history=True,
                add_reactions=True,
            ),
        }

    def _source_channel_permission_warnings(self, source_channel, bot_member) -> list[str]:
        permissions = source_channel.permissions_for(bot_member) if bot_member is not None else None
        warnings = []
        checks = [
            ("View Channel", bool(getattr(permissions, "view_channel", False))),
            ("Read Message History", bool(getattr(permissions, "read_message_history", False))),
            ("Add Reactions", bool(getattr(permissions, "add_reactions", False))),
        ]
        for label, has_permission in checks:
            if not has_permission:
                warnings.append(f"Bot is missing {label} in {source_channel.mention}.")
        return warnings

    def _translation_channel_overwrites(self, guild, bot_member, role) -> dict:
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            bot_member: discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                embed_links=True,
                read_message_history=True,
            ),
        }
        if role is not None:
            overwrites[role] = discord.PermissionOverwrite(
                view_channel=True,
                read_message_history=True,
                send_messages=False,
                add_reactions=True,
            )
        return overwrites

    @staticmethod
    def _find_category(guild, name: str):
        return next((category for category in getattr(guild, "categories", []) if category.name == name), None)

    @staticmethod
    def _find_text_channel(guild, name: str):
        return next((channel for channel in getattr(guild, "text_channels", []) if channel.name == name), None)

    @staticmethod
    def _find_role(guild, name: str):
        return next((role for role in getattr(guild, "roles", []) if role.name == name), None)

    def _format_setup_server_summary(
        self,
        category,
        category_created: bool,
        setup_channel,
        setup_channel_created: bool,
        source_channel,
        source_channel_created: bool,
        source_channel_mode: str,
        channel_by_language: dict[str, object],
        channel_created_by_language: dict[str, bool],
        roles_by_language: dict[str, object | None],
        role_created_by_language: dict[str, bool],
        warnings: list[str],
    ) -> str:
        lines = ["Setup completed with warnings." if warnings else "Setup completed."]
        lines.extend(["", "Category:", f"{self._check_mark(True)} {category.name} ({'created' if category_created else 'reused'})"])
        lines.extend(["", "Channels:"])
        lines.append(
            f"{self._check_mark(True)} #{setup_channel.name} {setup_channel.mention} "
            f"({'created' if setup_channel_created else 'reused'})"
        )
        if source_channel_mode == "existing":
            lines.append(f"Source channel: using existing #{source_channel.name} {source_channel.mention}")
        else:
            lines.append(
                f"Source channel: {'created' if source_channel_created else 'reused'} "
                f"#{source_channel.name} {source_channel.mention}"
            )
        for language, channel in channel_by_language.items():
            lines.append(
                f"{self._check_mark(True)} #{channel.name} {channel.mention} "
                f"({'created' if channel_created_by_language[language] else 'reused'})"
            )

        lines.extend(["", "Roles:"])
        for language, role in roles_by_language.items():
            if role is None:
                lines.append(f"❌ @lang-{language} (not created)")
            else:
                lines.append(
                    f"{self._check_mark(True)} {role.mention} "
                    f"({'created' if role_created_by_language[language] else 'reused'})"
                )

        mapped_role_count = sum(1 for role in roles_by_language.values() if role is not None)
        lines.extend(
            [
                "",
                "Mappings:",
                f"{self._check_mark(True)} translation channels: {len(channel_by_language)}",
                f"{self._check_mark(True)} language roles: {mapped_role_count}",
            ]
        )

        if warnings:
            lines.extend(["", "Warnings:"])
            lines.extend(f"⚠️ {warning}" for warning in warnings)

        lines.extend(
            [
                "",
                "Next:",
                "1. Run /setup_check",
                f"2. Users choose language in {setup_channel.mention}",
                f"3. Users react {self.reaction_translate_emoji} in {source_channel.mention}",
            ]
        )
        return "\n".join(lines)

    async def _build_setup_check_report(self, interaction: discord.Interaction) -> tuple[str, int, int]:
        guild = interaction.guild
        if guild is None:
            return "This command can only be used in a server.", 1, 0

        async with self.database.session() as session:
            channel_result = await session.execute(
                select(TranslationChannelSetting).where(TranslationChannelSetting.guild_id == guild.id)
            )
            channel_settings = list(channel_result.scalars().all())
            role_result = await session.execute(
                select(LanguageRoleSetting).where(LanguageRoleSetting.guild_id == guild.id)
            )
            role_settings = list(role_result.scalars().all())

        issues: list[str] = []
        warnings: list[str] = []
        lines: list[str] = []

        provider_name = getattr(self.translation_provider, "name", None)
        provider_model = getattr(self.translation_provider, "model_name", None) or provider_name
        if not provider_name:
            issues.append("Provider is not configured.")
            provider_display = "missing"
        else:
            provider_display = f"{provider_name}/{provider_model}"

        if not self.reaction_translation_enabled:
            issues.append("Reaction translation is disabled.")

        bot_member = self._setup_check_bot_member(interaction)
        guild_permissions = getattr(bot_member, "guild_permissions", None)
        can_manage_roles = bool(getattr(guild_permissions, "manage_roles", False))
        can_manage_channels = bool(getattr(guild_permissions, "manage_channels", False))
        can_use_app_commands = bool(getattr(guild_permissions, "use_application_commands", False))

        if role_settings and not can_manage_roles:
            issues.append("Bot cannot manage roles while language role mappings exist.")

        supported_channel_languages = {
            LanguageService.normalize(setting.target_language)
            for setting in channel_settings
            if is_supported_language(setting.target_language)
        }
        supported_role_languages = {
            LanguageService.normalize(setting.target_language)
            for setting in role_settings
            if is_supported_language(setting.target_language)
        }
        if not supported_channel_languages:
            issues.append("No supported translation channels configured.")

        lines.append("**Setup Check**")
        lines.append("")
        lines.append("**Bot runtime/config**")
        lines.append(f"- On-demand channel translation: {self._check_mark(self.on_demand_channel_translation_enabled)}")
        lines.append(f"- Reaction translation: {self._check_mark(self.reaction_translation_enabled)}")
        lines.append(f"- Context menu translation: {self._check_mark(self.context_menu_translation_enabled)}")
        lines.append(f"- Legacy mirror mode: {'enabled' if self.legacy_mirror_mode_enabled else 'disabled'}")
        lines.append(f"- Reaction emoji: `{self.reaction_translate_emoji}`")
        lines.append(f"- Provider/model: `{provider_display}`")
        lines.append("")

        sendable_supported_channel_count = 0
        lines.append("**Translation channels**")
        if not channel_settings:
            lines.append("- None configured.")
        for setting in channel_settings:
            language = LanguageService.normalize(setting.target_language)
            supported = is_supported_language(language)
            channel = guild.get_channel(setting.channel_id) if hasattr(guild, "get_channel") else None
            channel_exists = channel is not None
            permissions = channel.permissions_for(bot_member) if channel_exists and bot_member is not None else None
            can_view = bool(getattr(permissions, "view_channel", False))
            can_send = bool(getattr(permissions, "send_messages", False))
            can_embed = bool(getattr(permissions, "embed_links", False))
            can_read_history = bool(getattr(permissions, "read_message_history", False))
            if supported and channel_exists and can_send:
                sendable_supported_channel_count += 1
            if not supported:
                warnings.append(f"{language.upper()} is unsupported. Remove it with /translation_channel_remove.")
            if supported and not channel_exists:
                warnings.append(f"{LanguageService.display_name(language)} ({language}) channel does not exist.")
            lines.append(
                "- "
                f"{format_language_mapping_label(language)} -> <#{setting.channel_id}>: "
                f"exists {self._check_mark(channel_exists)}, "
                f"view {self._check_mark(can_view)}, "
                f"send {self._check_mark(can_send)}, "
                f"embed {self._check_mark(can_embed)}, "
                f"history {self._check_mark(can_read_history)}"
            )
        if supported_channel_languages and sendable_supported_channel_count == 0:
            issues.append("Bot cannot send messages in any configured translation channel.")
        lines.append("")

        lines.append("**Language roles**")
        if not role_settings:
            lines.append("- None configured.")
        bot_top_role = getattr(bot_member, "top_role", None)
        for setting in role_settings:
            language = LanguageService.normalize(setting.target_language)
            role = guild.get_role(setting.role_id) if hasattr(guild, "get_role") else None
            supported = is_supported_language(language)
            if not supported:
                warnings.append(f"{language.upper()} is unsupported. Remove it with /language_role_remove.")
            if supported and role is None:
                warnings.append(f"{LanguageService.display_name(language)} ({language}) role does not exist.")
            if role is not None and bot_top_role is not None and not self._role_above(bot_top_role, role):
                issues.append(f"Bot role must be above {role.mention} in Server Settings -> Roles.")
            lines.append(
                "- "
                f"{format_language_mapping_label(language)} -> <@&{setting.role_id}>: "
                f"exists {self._check_mark(role is not None)}"
            )
        lines.append("")

        lines.append("**Bot role permissions**")
        lines.append(f"- Manage Roles: {self._check_mark(can_manage_roles)}")
        lines.append(f"- Manage Channels: {self._check_mark(can_manage_channels)}")
        lines.append(f"- Use Application Commands: {self._check_mark(can_use_app_commands)}")
        lines.append("")

        lines.append("**Completeness**")
        completeness_lines: list[str] = []
        for language in sorted(supported_channel_languages - supported_role_languages):
            warning = (
                f"{LanguageService.display_name(language)} ({language}) has a translation channel "
                "but no language role mapping."
            )
            warnings.append(warning)
            completeness_lines.append(f"- {warning}")
        for language in sorted(supported_role_languages - supported_channel_languages):
            warning = (
                f"{LanguageService.display_name(language)} ({language}) has a language role "
                "but no translation channel."
            )
            warnings.append(warning)
            completeness_lines.append(f"- {warning}")
        if completeness_lines:
            lines.extend(completeness_lines)
        else:
            lines.append("- Translation channels and language roles are paired.")
        lines.append("")

        lines.append("**Setup message**")
        lines.append("- Setup message: not tracked. Re-run /language_setup_message after changing languages.")
        lines.append("")

        lines.append("**Source channels**")
        lines.append("- Source channels are not restricted. Bot can translate from any visible channel.")
        lines.append("")

        if issues:
            summary = "❌ Not ready"
        elif warnings:
            summary = "⚠️ Needs attention"
        else:
            summary = "✅ Ready"
        lines.insert(1, f"{summary} — {len(issues)} critical issue(s), {len(warnings)} warning(s)")

        if issues:
            lines.append("**Critical issues**")
            lines.extend(f"- {issue}" for issue in issues)
            lines.append("")
        if warnings:
            lines.append("**Warnings**")
            lines.extend(f"- {warning}" for warning in warnings)

        return "\n".join(lines), len(issues), len(warnings)

    def _setup_check_bot_member(self, interaction: discord.Interaction):
        guild = interaction.guild
        if guild is None:
            return None
        member = getattr(guild, "me", None)
        if member is not None:
            return member
        client_user = getattr(interaction.client, "user", None)
        if client_user is not None and hasattr(guild, "get_member"):
            return guild.get_member(client_user.id)
        return None

    @staticmethod
    def _check_mark(value: bool) -> str:
        return "✅" if value else "❌"

    @staticmethod
    def _role_above(bot_role, target_role) -> bool:
        try:
            return bot_role > target_role
        except TypeError:
            return getattr(bot_role, "position", 0) > getattr(target_role, "position", 0)

    @staticmethod
    def _split_setup_check_report(report: str, limit: int = 1900) -> list[str]:
        if len(report) <= limit:
            return [report]
        pages: list[str] = []
        current: list[str] = []
        current_len = 0
        for line in report.splitlines():
            line_len = len(line) + 1
            if current and current_len + line_len > limit:
                pages.append("\n".join(current))
                current = []
                current_len = 0
            current.append(line)
            current_len += line_len
        if current:
            pages.append("\n".join(current))
        return pages

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
