from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
import logging

import discord
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.mention_safety import sanitize_mentions
from app.models import WelcomeSetting

logger = logging.getLogger(__name__)

MAX_WELCOME_IMAGE_BYTES = 2 * 1024 * 1024
SUPPORTED_WELCOME_IMAGE_TYPES = {"image/png", "image/jpeg", "image/webp", "image/gif"}


@dataclass(frozen=True)
class WelcomePayload:
    content: str
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

    @staticmethod
    def validate_image(content_type: str | None, size: int) -> str | None:
        if content_type not in SUPPORTED_WELCOME_IMAGE_TYPES:
            return "Загрузите изображение PNG, JPEG, WEBP или GIF."
        if size <= 0 or size > MAX_WELCOME_IMAGE_BYTES:
            return "Размер welcome-картинки не должен превышать 2 МБ."
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

    @staticmethod
    def build_payload(
        setting: WelcomeSetting,
        member: discord.Member,
        *,
        include_button: bool = True,
    ) -> WelcomePayload:
        safe_template = sanitize_mentions(setting.message_template)
        mention = member.mention
        content = safe_template.replace("{user}", mention)
        if "{user}" not in safe_template:
            content = f"{mention}\n{content}"

        filename = WelcomeService._safe_filename(setting.image_filename, setting.image_content_type)
        file = discord.File(BytesIO(setting.image_bytes), filename=filename)
        embed = discord.Embed()
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
            content=content,
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

        payload = self.build_payload(setting, member, include_button=include_button)
        try:
            await channel.send(
                content=payload.content,
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
    def _safe_filename(filename: str, content_type: str) -> str:
        extension = {
            "image/png": ".png",
            "image/jpeg": ".jpg",
            "image/webp": ".webp",
            "image/gif": ".gif",
        }.get(content_type, ".png")
        stem = "".join(character for character in filename.rsplit(".", 1)[0] if character.isalnum() or character in "-_")
        return f"{stem or 'welcome'}{extension}"
