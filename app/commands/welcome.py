from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands

from app.database import Database
from app.services.welcome_service import MAX_WELCOME_IMAGE_BYTES, WelcomeService
from app.services.welcome_banner_renderer import WelcomeBannerError, WelcomeBannerRenderer

logger = logging.getLogger(__name__)


class WelcomeSetupModal(discord.ui.Modal, title="Настройка welcome-сообщения"):
    message = discord.ui.TextInput(
        label="Текст приветствия",
        placeholder="Добро пожаловать, {user}! Правила: <#ID канала>",
        style=discord.TextStyle.paragraph,
        max_length=1900,
        required=True,
    )
    button_label = discord.ui.TextInput(
        label="Название кнопки",
        placeholder="Выбрать язык",
        max_length=80,
        required=False,
    )

    def __init__(
        self,
        *,
        database: Database,
        welcome_channel: discord.TextChannel,
        image_bytes: bytes,
        image_content_type: str,
        image_filename: str,
        button_enabled: bool,
        button_channel: discord.TextChannel | None,
        current_message: str | None = None,
        current_button_label: str | None = None,
    ) -> None:
        super().__init__()
        self.database = database
        self.welcome_channel = welcome_channel
        self.image_bytes = image_bytes
        self.image_content_type = image_content_type
        self.image_filename = image_filename
        self.button_enabled = button_enabled
        self.button_channel = button_channel
        if current_message:
            self.message.default = current_message
        if button_enabled:
            self.button_label.default = current_button_label or "Выбрать язык"
        else:
            self.remove_item(self.button_label)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("Настройка доступна только на сервере.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        async with self.database.session() as session:
            service = WelcomeService(session)
            setting = await service.save_setting(
                guild_id=interaction.guild.id,
                welcome_channel_id=self.welcome_channel.id,
                message_template=str(self.message),
                image_bytes=self.image_bytes,
                image_content_type=self.image_content_type,
                image_filename=self.image_filename,
                button_enabled=self.button_enabled,
                button_label=str(self.button_label) if self.button_enabled else None,
                button_channel_id=self.button_channel.id if self.button_channel else None,
            )
            payload = await service.build_payload(setting, interaction.user)

        await interaction.followup.send(
            embed=payload.embed,
            file=payload.file,
            view=payload.view,
            allowed_mentions=discord.AllowedMentions.none(),
            ephemeral=True,
        )


class WelcomeEditModal(discord.ui.Modal, title="Редактирование welcome-сообщения"):
    message = discord.ui.TextInput(
        label="Текст приветствия",
        style=discord.TextStyle.paragraph,
        max_length=1900,
        required=True,
    )
    button_label = discord.ui.TextInput(
        label="Название кнопки",
        max_length=80,
        required=False,
    )

    def __init__(self, *, database: Database, current) -> None:
        super().__init__()
        self.database = database
        self.message.default = current.message_template
        if current.button_enabled:
            self.button_label.default = current.button_label or "Выбрать язык"
        else:
            self.remove_item(self.button_label)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("Редактирование доступно только на сервере.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        async with self.database.session() as session:
            service = WelcomeService(session)
            setting = await service.update_content(
                interaction.guild.id,
                message_template=str(self.message),
                button_label=str(self.button_label) if self.button_label in self.children else None,
            )
            if setting is None:
                await interaction.followup.send(
                    "Welcome ещё не настроен. Используйте `/welcome setup`.",
                    ephemeral=True,
                )
                return
            payload = await service.build_payload(setting, interaction.user)

        await interaction.followup.send(
            embed=payload.embed,
            file=payload.file,
            view=payload.view,
            allowed_mentions=discord.AllowedMentions.none(),
            ephemeral=True,
        )


class WelcomeCommands(commands.GroupCog, group_name="welcome", group_description="Настройка приветствий"):
    def __init__(self, database: Database) -> None:
        self.database = database

    @app_commands.command(name="setup", description="Настроить welcome-сообщение")
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.describe(
        welcome_channel="Канал для приветствий",
        image="Картинка PNG, JPEG, WEBP или GIF (до 5 МБ)",
        button_enabled="Добавить кнопку перехода в языковой канал",
        language_channel="Канал, который откроет кнопка",
    )
    async def setup(
        self,
        interaction: discord.Interaction,
        welcome_channel: discord.TextChannel,
        image: discord.Attachment,
        button_enabled: bool = True,
        language_channel: discord.TextChannel | None = None,
    ) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("Настройка доступна только на сервере.", ephemeral=True)
            return
        if button_enabled and language_channel is None:
            await interaction.response.send_message("Выберите языковой канал", ephemeral=True)
            return
        if welcome_channel.guild.id != interaction.guild.id or (
            language_channel is not None and language_channel.guild.id != interaction.guild.id
        ):
            await interaction.response.send_message("Выберите канал этого сервера.", ephemeral=True)
            return

        image_error = WelcomeService.validate_image(image.content_type, image.size)
        if image_error:
            await interaction.response.send_message(image_error, ephemeral=True)
            return

        bot_member = interaction.guild.me
        if bot_member is None:
            await interaction.response.send_message("Не удалось проверить права бота.", ephemeral=True)
            return
        missing_permissions = WelcomeService.validate_channel_permissions(welcome_channel, bot_member)
        if missing_permissions:
            await interaction.response.send_message(
                "В welcome-канале не хватает прав: " + ", ".join(missing_permissions),
                ephemeral=True,
            )
            return

        try:
            image_bytes = await image.read()
        except discord.DiscordException:
            await interaction.response.send_message("Не удалось загрузить картинку. Попробуйте ещё раз.", ephemeral=True)
            return

        if len(image_bytes) != image.size or len(image_bytes) > MAX_WELCOME_IMAGE_BYTES:
            await interaction.response.send_message("Размер welcome-картинки не должен превышать 5 МБ.", ephemeral=True)
            return

        try:
            WelcomeBannerRenderer().validate_banner(image_bytes)
        except WelcomeBannerError:
            await interaction.response.send_message(
                "Баннер повреждён или слишком маленький. Используйте изображение не меньше 400×100 px.",
                ephemeral=True,
            )
            return

        async with self.database.session() as session:
            current = await WelcomeService(session).get_setting(interaction.guild.id)

        modal = WelcomeSetupModal(
            database=self.database,
            welcome_channel=welcome_channel,
            image_bytes=image_bytes,
            image_content_type=image.content_type or "",
            image_filename=image.filename,
            button_enabled=button_enabled,
            button_channel=language_channel,
            current_message=current.message_template if current else None,
            current_button_label=current.button_label if current else None,
        )
        await interaction.response.send_modal(modal)

    @app_commands.command(name="edit", description="Изменить текст welcome без повторной настройки")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def edit(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("Команда доступна только на сервере.", ephemeral=True)
            return
        async with self.database.session() as session:
            current = await WelcomeService(session).get_setting(interaction.guild.id)
        if current is None:
            await interaction.response.send_message(
                "Welcome ещё не настроен. Используйте `/welcome setup`.",
                ephemeral=True,
            )
            return
        await interaction.response.send_modal(WelcomeEditModal(database=self.database, current=current))

    @app_commands.command(name="banner", description="Заменить только welcome-баннер")
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.describe(image="Новый баннер PNG, JPEG, WEBP или GIF (до 5 МБ)")
    async def banner(self, interaction: discord.Interaction, image: discord.Attachment) -> None:
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("Команда доступна только на сервере.", ephemeral=True)
            return
        async with self.database.session() as session:
            current = await WelcomeService(session).get_setting(interaction.guild.id)
        if current is None:
            await interaction.response.send_message(
                "Welcome ещё не настроен. Используйте `/welcome setup`.",
                ephemeral=True,
            )
            return
        image_error = WelcomeService.validate_image(image.content_type, image.size)
        if image_error:
            await interaction.response.send_message(image_error, ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        try:
            image_bytes = await image.read()
            WelcomeBannerRenderer().validate_banner(image_bytes)
        except discord.DiscordException:
            await interaction.followup.send("Не удалось загрузить баннер. Попробуйте ещё раз.", ephemeral=True)
            return
        except WelcomeBannerError:
            await interaction.followup.send(
                "Баннер повреждён или слишком маленький. Используйте изображение не меньше 400×100 px.",
                ephemeral=True,
            )
            return
        if len(image_bytes) != image.size or len(image_bytes) > MAX_WELCOME_IMAGE_BYTES:
            await interaction.followup.send("Размер welcome-баннера не должен превышать 5 МБ.", ephemeral=True)
            return

        async with self.database.session() as session:
            service = WelcomeService(session)
            setting = await service.update_banner(
                interaction.guild.id,
                image_bytes=image_bytes,
                image_content_type=image.content_type or "",
                image_filename=image.filename,
            )
            if setting is None:
                await interaction.followup.send(
                    "Welcome ещё не настроен. Используйте `/welcome setup`.",
                    ephemeral=True,
                )
                return
            payload = await service.build_payload(setting, interaction.user)

        await interaction.followup.send(
            embed=payload.embed,
            file=payload.file,
            view=payload.view,
            allowed_mentions=discord.AllowedMentions.none(),
            ephemeral=True,
        )

    @app_commands.command(name="preview", description="Показать текущее welcome-сообщение")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def preview(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("Команда доступна только на сервере.", ephemeral=True)
            return
        async with self.database.session() as session:
            service = WelcomeService(session)
            setting = await service.get_setting(interaction.guild.id)
            if setting is None:
                await interaction.response.send_message("Welcome ещё не настроен. Используйте `/welcome setup`.", ephemeral=True)
                return
            payload = await service.build_payload(setting, interaction.user)

        await interaction.response.send_message(
            embed=payload.embed,
            file=payload.file,
            view=payload.view,
            allowed_mentions=discord.AllowedMentions.none(),
            ephemeral=True,
        )

    @app_commands.command(name="color", description="Изменить цвет welcome-карточки")
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.describe(color="HEX-цвет, например #5865F2")
    async def color(self, interaction: discord.Interaction, color: str) -> None:
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("Команда доступна только на сервере.", ephemeral=True)
            return
        normalized = WelcomeService.normalize_accent_color(color)
        if normalized is None:
            await interaction.response.send_message(
                "Укажите цвет в формате HEX `#RRGGBB`, например `#5865F2`.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)
        async with self.database.session() as session:
            service = WelcomeService(session)
            setting = await service.update_accent_color(interaction.guild.id, normalized)
            if setting is None:
                await interaction.followup.send(
                    "Welcome ещё не настроен. Используйте `/welcome setup`.",
                    ephemeral=True,
                )
                return
            payload = await service.build_payload(setting, interaction.user)

        await interaction.followup.send(
            embed=payload.embed,
            file=payload.file,
            view=payload.view,
            allowed_mentions=discord.AllowedMentions.none(),
            ephemeral=True,
        )

    @app_commands.command(name="status", description="Проверить welcome-настройку и права")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def status(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("Команда доступна только на сервере.", ephemeral=True)
            return
        async with self.database.session() as session:
            setting = await WelcomeService(session).get_setting(interaction.guild.id)
        if setting is None:
            await interaction.response.send_message("Welcome не настроен. Используйте `/welcome setup`.", ephemeral=True)
            return

        issues: list[str] = []
        channel = interaction.guild.get_channel(setting.welcome_channel_id)
        if not isinstance(channel, discord.TextChannel):
            issues.append("welcome-канал удалён или недоступен")
        elif interaction.guild.me is not None:
            missing = WelcomeService.validate_channel_permissions(channel, interaction.guild.me)
            if missing:
                issues.append("не хватает прав: " + ", ".join(missing))
        if setting.button_enabled and not isinstance(
            interaction.guild.get_channel(setting.button_channel_id or 0), discord.TextChannel
        ):
            issues.append("языковой канал кнопки удалён или недоступен")
        invalid_links = WelcomeService.invalid_channel_references(setting.message_template, interaction.guild)
        if invalid_links:
            issues.append("некорректные ссылки на каналы: " + ", ".join(invalid_links))

        state = "включён" if setting.is_enabled else "отключён"
        lines = [f"Welcome **{state}**.", f"Канал: <#{setting.welcome_channel_id}>."]
        lines.append(f"Цвет: `#{setting.accent_color}`.")
        if setting.button_enabled:
            lines.append(f"Кнопка: **{setting.button_label or 'Выбрать язык'}** → <#{setting.button_channel_id}>.")
        else:
            lines.append("Кнопка отключена.")
        lines.append("Проблем не найдено." if not issues else "Проблемы:\n- " + "\n- ".join(issues))
        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    @app_commands.command(name="enable", description="Включить welcome-сообщения")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def enable(self, interaction: discord.Interaction) -> None:
        await self._set_enabled(interaction, True)

    @app_commands.command(name="disable", description="Отключить welcome-сообщения без удаления настройки")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def disable(self, interaction: discord.Interaction) -> None:
        await self._set_enabled(interaction, False)

    async def _set_enabled(self, interaction: discord.Interaction, enabled: bool) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("Команда доступна только на сервере.", ephemeral=True)
            return
        async with self.database.session() as session:
            setting = await WelcomeService(session).set_enabled(interaction.guild.id, enabled)
        if setting is None:
            await interaction.response.send_message("Welcome ещё не настроен. Используйте `/welcome setup`.", ephemeral=True)
            return
        action = "включён" if enabled else "отключён"
        await interaction.response.send_message(f"Welcome {action}.", ephemeral=True)
