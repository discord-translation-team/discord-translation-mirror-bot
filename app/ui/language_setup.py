from __future__ import annotations

import logging

import discord
from sqlalchemy import func, select

from app.models import TranslationChannelSetting, UserLanguageSetting
from app.services.language_service import LanguageService

logger = logging.getLogger(__name__)

LANGUAGE_SELECT_CUSTOM_ID = "language_select_menu:v1"


class LanguageSetupView(discord.ui.View):
    def __init__(self, options: list[discord.SelectOption] | None = None) -> None:
        super().__init__(timeout=None)
        self.add_item(LanguageSelect(options or []))


class LanguageSelect(discord.ui.Select):
    def __init__(self, options: list[discord.SelectOption]) -> None:
        if not options:
            options = [
                discord.SelectOption(
                    label="Unavailable",
                    value="unavailable",
                    description="Ask an admin to post a fresh setup menu.",
                )
            ]
        super().__init__(
            custom_id=LANGUAGE_SELECT_CUSTOM_ID,
            placeholder="Select your translation language",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("This menu can only be used in a server.", ephemeral=True)
            return

        language = LanguageService.normalize(self.values[0])
        if language == "unavailable":
            await interaction.response.send_message(
                "This language is no longer available. Please ask an admin to update the setup message.",
                ephemeral=True,
            )
            return

        database = getattr(interaction.client, "database", None)
        if database is None:
            await interaction.response.send_message("Language setup is temporarily unavailable.", ephemeral=True)
            return

        async with database.session() as session:
            channel_result = await session.execute(
                select(TranslationChannelSetting).where(
                    TranslationChannelSetting.guild_id == interaction.guild.id,
                    func.lower(func.trim(TranslationChannelSetting.target_language)) == language,
                )
            )
            if channel_result.scalar_one_or_none() is None:
                logger.info(
                    "language_selection_rejected_missing_channel",
                    extra={
                        "guild_id": interaction.guild.id,
                        "user_id": interaction.user.id,
                        "target_language": language,
                    },
                )
                await interaction.response.send_message(
                    "This language is no longer available. Please ask an admin to update the setup message.",
                    ephemeral=True,
                )
                return

            setting_result = await session.execute(
                select(UserLanguageSetting).where(
                    UserLanguageSetting.guild_id == interaction.guild.id,
                    UserLanguageSetting.user_id == interaction.user.id,
                )
            )
            setting = setting_result.scalar_one_or_none()
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

        logger.info(
            "language_selected",
            extra={
                "guild_id": interaction.guild.id,
                "user_id": interaction.user.id,
                "target_language": language,
            },
        )
        await interaction.response.send_message(
            (
                f"Done — your translation language is now {LanguageService.display_name(language)}. "
                "React with 🌐 to translate messages."
            ),
            ephemeral=True,
        )


def build_language_setup_embed() -> discord.Embed:
    embed = discord.Embed(
        title="🌐 Choose your translation language",
        description=(
            "1. Select your language below.\n"
            "2. React with 🌐 to any message you want translated.\n"
            "3. The translation will appear in your language channel.\n\n"
            "You can change your language anytime by selecting a different option here."
        ),
    )
    embed.set_footer(text="If your language is missing, ask an admin to add a translation channel.")
    return embed


def build_language_select_options(languages: list[str]) -> list[discord.SelectOption]:
    unique_languages = sorted({LanguageService.normalize(language) for language in languages})
    return [
        discord.SelectOption(
            label=LanguageService.display_name(language),
            value=language,
        )
        for language in unique_languages[:25]
    ]
