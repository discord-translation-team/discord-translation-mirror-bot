from __future__ import annotations

from dataclasses import dataclass
import logging

import discord
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import LanguageRoleSetting
from app.services.language_service import LanguageService

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class LanguageRoleSyncResult:
    status: str
    target_language: str
    role_id: int | None = None


class LanguageRoleService:
    SYNCED = "synced"
    MISSING_ROLE_MAPPING = "missing_role_mapping"
    PERMISSIONS_FAILED = "permissions_failed"
    MISSING_DISCORD_ROLE = "missing_discord_role"

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def set_language_role(self, guild_id: int, target_language: str, role_id: int) -> LanguageRoleSetting:
        language = LanguageService.normalize(target_language)
        result = await self.session.execute(
            select(LanguageRoleSetting).where(
                LanguageRoleSetting.guild_id == guild_id,
                func.lower(func.trim(LanguageRoleSetting.target_language)) == language,
            )
        )
        setting = result.scalar_one_or_none()
        if setting is None:
            setting = LanguageRoleSetting(guild_id=guild_id, target_language=language, role_id=role_id)
            self.session.add(setting)
        else:
            setting.target_language = language
            setting.role_id = role_id
        await self.session.commit()
        logger.info(
            "language_role_set",
            extra={"guild_id": guild_id, "target_language": language, "role_id": role_id},
        )
        return setting

    async def list_language_roles(self, guild_id: int) -> list[LanguageRoleSetting]:
        result = await self.session.execute(
            select(LanguageRoleSetting)
            .where(LanguageRoleSetting.guild_id == guild_id)
            .order_by(LanguageRoleSetting.target_language)
        )
        return list(result.scalars().all())

    async def remove_language_role(self, guild_id: int, target_language: str) -> int:
        language = LanguageService.normalize(target_language)
        result = await self.session.execute(
            delete(LanguageRoleSetting).where(
                LanguageRoleSetting.guild_id == guild_id,
                func.lower(func.trim(LanguageRoleSetting.target_language)) == language,
            )
        )
        await self.session.commit()
        removed_count = result.rowcount or 0
        logger.info(
            "language_role_removed",
            extra={"guild_id": guild_id, "target_language": language, "removed_count": removed_count},
        )
        return removed_count

    async def sync_member_language_role(
        self,
        guild: discord.Guild,
        member: discord.Member,
        target_language: str,
    ) -> LanguageRoleSyncResult:
        language = LanguageService.normalize(target_language)
        settings = await self.list_language_roles(guild.id)
        configured_role_ids = {setting.role_id for setting in settings}
        selected_setting = next(
            (
                setting
                for setting in settings
                if LanguageService.normalize(setting.target_language) == language
            ),
            None,
        )

        logger.info(
            "language_role_sync_started",
            extra={
                "guild_id": guild.id,
                "user_id": member.id,
                "target_language": language,
                "configured_role_count": len(settings),
            },
        )

        roles_by_id = {}
        for role_id in configured_role_ids:
            role = guild.get_role(role_id)
            if role is None:
                logger.warning(
                    "language_role_sync_missing_discord_role",
                    extra={"guild_id": guild.id, "user_id": member.id, "role_id": role_id},
                )
                continue
            roles_by_id[role_id] = role

        selected_role_id = selected_setting.role_id if selected_setting else None
        selected_role = roles_by_id.get(selected_role_id) if selected_role_id is not None else None
        if selected_setting is None:
            logger.info(
                "language_role_sync_skipped_no_mapping",
                extra={"guild_id": guild.id, "user_id": member.id, "target_language": language},
            )
            return LanguageRoleSyncResult(self.MISSING_ROLE_MAPPING, language)

        current_role_ids = {role.id for role in getattr(member, "roles", [])}
        roles_to_remove = [
            role
            for role_id, role in roles_by_id.items()
            if role_id != selected_role_id and role_id in current_role_ids
        ]

        try:
            if roles_to_remove:
                await member.remove_roles(*roles_to_remove, reason="Translation language role sync")
            if selected_role is None:
                return LanguageRoleSyncResult(self.MISSING_DISCORD_ROLE, language, selected_role_id)
            if selected_role.id not in current_role_ids:
                await member.add_roles(selected_role, reason="Translation language role sync")
        except (discord.Forbidden, PermissionError) as exc:
            logger.warning(
                "language_role_sync_failed_permissions",
                extra={
                    "guild_id": guild.id,
                    "user_id": member.id,
                    "target_language": language,
                    "error_type": type(exc).__name__,
                },
            )
            return LanguageRoleSyncResult(self.PERMISSIONS_FAILED, language, selected_role_id)

        logger.info(
            "language_role_sync_completed",
            extra={
                "guild_id": guild.id,
                "user_id": member.id,
                "target_language": language,
                "role_id": selected_role_id,
                "removed_role_count": len(roles_to_remove),
            },
        )
        return LanguageRoleSyncResult(self.SYNCED, language, selected_role_id)
