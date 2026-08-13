from __future__ import annotations

import asyncio
from dataclasses import dataclass
from io import BytesIO
import logging

import discord
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.mention_safety import sanitize_mentions
from app.models import WelcomeSetting
from app.services.welcome_banner_renderer import WelcomeBannerRenderer

logger = logging.getLogger(__name__)

MAX_WELCOME_IMAGE_BYTES = 5 * 1024 * 1024
MAX_AVATAR_BYTES = 2 * 1024 * 1024
AVATAR_DOWNLOAD_TIMEOUT_SECONDS = 5
SUPPORTED_WELCOME_IMAGE_TYPES = {"image/png", "image/jpeg", "image/webp", "image/gif"}
DEFAULT_WELCOME_ACCENT_COLOR = "5865F2"


@dataclass(frozen=True)
class WelcomePayload:
    embed: discord.Embed
    file: discord.File
    view: discord.ui.View | None
    allowed_mentions: discord.AllowedMentions


class WelcomeService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_setting(self, guild_id: int) -> WelcomeSetting | None:
        result = await self.session.execute(
            select(WelcomeSetting).where(WelcomeSetting.guild_id == guild_id)
        )
        return result.scalar_one_or_none()

    async def save_setting(
        self,
        *,
        guild_id: int,
        welcome_channel_id: int,
        message_template: str,
        image_bytes: bytes,
        image_content_type: str,
        image_filename: str,
        button_enabled: bool,
        button_label: str | None,
        button_channel_id: int | None,
    ) -> WelcomeSetting:
        setting = await self.get_setting(guild_id)
        if setting is None:
            setting = WelcomeSetting(guild_id=guild_id)
            self.session.add(setting)

        setting.is_enabled = True
        setting.welcome_channel_id = welcome_channel_id
        setting.message_template = message_template.strip()
        setting.image_bytes = image_bytes
        setting.image_content_type = image_content_type
        setting.image_filename = image_filename
        setting.button_enabled = button_enabled
        setting.button_label = button_label.strip() if button_enabled and button_label else None
        setting.button_channel_id = button_channel_id if button_enabled else None
        await self.session.commit()
        return setting

    async def set_enabled(self, guild_id: int, enabled: bool) -> WelcomeSetting | None:
        setting = await self.get_setting(guild_id)
        if setting is None:
            return None
        setting.is_enabled = enabled
        await self.session.commit()
        return setting

    async def update_content(
        self,
        guild_id: int,
        *,
        message_template: str,
        button_label: str | None,
    ) -> WelcomeSetting | None:
        setting = await self.get_setting(guild_id)
        if setting is None:
            return None
        setting.message_template = message_template.strip()
        if setting.button_enabled:
            setting.button_label = button_label.strip() if button_label else "Выбрать язык"
        await self.session.commit()
        return setting

    async def update_banner(
        self,
        guild_id: int,
        *,
        image_bytes: bytes,
        image_content_type: str,
        image_filename: str,
    ) -> WelcomeSetting | None:
        setting = await self.get_setting(guild_id)
        if setting is None:
            return None
        setting.image_bytes = image_bytes
        setting.image_content_type = image_content_type
        setting.image_filename = image_filename
        await self.session.commit()
        return setting

    async def update_accent_color(self, guild_id: int, accent_color: str) -> WelcomeSetting | None:
        normalized = self.normalize_accent_color(accent_color)
        if normalized is None:
            raise ValueError("Invalid accent color")
        setting = await self.get_setting(guild_id)
        if setting is None:
            return None
        setting.accent_color = normalized
        await self.session.commit()
        return setting

    @staticmethod
    def normalize_accent_color(value: str) -> str | None:
        normalized = value.strip().removeprefix("#").upper()
        if len(normalized) != 6 or any(character not in "0123456789ABCDEF" for character in normalized):
            return None
        return normalized

    @staticmethod
    def validate_image(content_type: str | None, size: int) -> str | None:
        if content_type not in SUPPORTED_WELCOME_IMAGE_TYPES:
            return "Загрузите изображение PNG, JPEG, WEBP или GIF."
        if size <= 0 or size > MAX_WELCOME_IMAGE_BYTES:
            return "Размер welcome-картинки не должен превышать 5 МБ."
        return None

    @staticmethod
    def validate_channel_permissions(channel: discord.TextChannel, bot_member: discord.Member) -> list[str]:
        permissions = channel.permissions_for(bot_member)
        required = {
            "View Channel": permissions.view_channel,
            "Send Messages": permissions.send_messages,
            "Embed Links": permissions.embed_links,
            "Attach Files": permissions.attach_files,
        }
        return [name for name, granted in required.items() if not granted]

    async def build_payload(
        self,
        setting: WelcomeSetting,
        member: discord.Member,
        *,
        include_button: bool = True,
    ) -> WelcomePayload:
        user_marker = "\x00WELCOME_USER\x00"
        safe_template = sanitize_mentions(setting.message_template).replace("{user}", user_marker)
        safe_server_name = sanitize_mentions(member.guild.name)
        content = safe_template.replace("{server_name}", safe_server_name)
        mention = member.mention
        content = content.replace(user_marker, mention)
        if user_marker not in safe_template:
            content = f"{mention}\n{content}"

        avatar_bytes = await self._read_avatar(member)
        accent_hex = self.normalize_accent_color(setting.accent_color) or DEFAULT_WELCOME_ACCENT_COLOR
        accent_rgb = tuple(bytes.fromhex(accent_hex))
        try:
            rendered_bytes = await asyncio.to_thread(
                WelcomeBannerRenderer().render,
                banner_bytes=setting.image_bytes,
                avatar_bytes=avatar_bytes,
                display_name=member.display_name,
                server_name=member.guild.name,
                accent_color=accent_rgb,
            )
            filename = "welcome-banner.png"
        except Exception as exc:
            logger.warning(
                "welcome_banner_render_failed",
                extra={"guild_id": member.guild.id, "member_id": member.id, "error_type": type(exc).__name__},
            )
            rendered_bytes = setting.image_bytes
            filename = WelcomeService._safe_filename(setting.image_filename, setting.image_content_type)

        file = discord.File(BytesIO(rendered_bytes), filename=filename)
        embed = discord.Embed(description=content, color=int(accent_hex, 16))
        embed.set_image(url=f"attachment://{filename}")

        view = None
        if include_button and setting.button_enabled and setting.button_channel_id:
            view = discord.ui.View(timeout=None)
            view.add_item(
                discord.ui.Button(
                    label=setting.button_label or "Choose language",
                    style=discord.ButtonStyle.link,
                    url=f"https://discord.com/channels/{setting.guild_id}/{setting.button_channel_id}",
                )
            )

        return WelcomePayload(
            embed=embed,
            file=file,
            view=view,
            allowed_mentions=discord.AllowedMentions(
                everyone=False,
                roles=False,
                users=[member],
                replied_user=False,
            ),
        )

    async def send_welcome(self, member: discord.Member) -> bool:
        setting = await self.get_setting(member.guild.id)
        if setting is None or not setting.is_enabled or member.bot:
            return False

        channel = member.guild.get_channel(setting.welcome_channel_id)
        if not isinstance(channel, discord.TextChannel):
            logger.warning(
                "welcome_channel_missing",
                extra={"guild_id": member.guild.id, "channel_id": setting.welcome_channel_id},
            )
            return False

        include_button = True
        if setting.button_enabled and not isinstance(
            member.guild.get_channel(setting.button_channel_id or 0), discord.TextChannel
        ):
            include_button = False
            logger.warning(
                "welcome_button_channel_missing",
                extra={"guild_id": member.guild.id, "channel_id": setting.button_channel_id},
            )

        payload = await self.build_payload(setting, member, include_button=include_button)
        try:
            await channel.send(
                embed=payload.embed,
                file=payload.file,
                view=payload.view,
                allowed_mentions=payload.allowed_mentions,
            )
        except discord.DiscordException as exc:
            logger.warning(
                "welcome_send_failed",
                extra={
                    "guild_id": member.guild.id,
                    "channel_id": channel.id,
                    "member_id": member.id,
                    "error_type": type(exc).__name__,
                },
            )
            return False

        logger.info(
            "welcome_sent",
            extra={"guild_id": member.guild.id, "channel_id": channel.id, "member_id": member.id},
        )
        return True

    @staticmethod
    async def _read_avatar(member: discord.Member) -> bytes | None:
        try:
            avatar_bytes = await asyncio.wait_for(
                member.display_avatar.with_size(256).read(),
                timeout=AVATAR_DOWNLOAD_TIMEOUT_SECONDS,
            )
        except (discord.DiscordException, asyncio.TimeoutError, OSError):
            logger.warning(
                "welcome_avatar_download_failed",
                extra={"guild_id": member.guild.id, "member_id": member.id},
            )
            return None
        if len(avatar_bytes) > MAX_AVATAR_BYTES:
            logger.warning(
                "welcome_avatar_too_large",
                extra={"guild_id": member.guild.id, "member_id": member.id, "size": len(avatar_bytes)},
            )
            return None
        return avatar_bytes

    @staticmethod
    def _safe_filename(filename: str, content_type: str) -> str:
        extension = {
            "image/png": ".png",
            "image/jpeg": ".jpg",
            "image/webp": ".webp",
            "image/gif": ".gif",
        }.get(content_type, ".png")
        stem = "".join(character for character in filename.rsplit(".", 1)[0] if character.isalnum() or character in "-_")
        return f"{stem or 'welcome'}{extension}"
