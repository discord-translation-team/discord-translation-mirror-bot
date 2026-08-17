from __future__ import annotations

from datetime import date

import discord
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ChannelCleanupRule


MAX_CLEANUP_RULES_PER_GUILD = 25


class CleanupValidationError(ValueError):
    pass


class CleanupService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def list_for_guild(self, guild_id: int) -> list[ChannelCleanupRule]:
        result = await self.session.execute(
            select(ChannelCleanupRule)
            .where(ChannelCleanupRule.guild_id == guild_id)
            .order_by(ChannelCleanupRule.id)
        )
        return list(result.scalars())

    async def get(self, guild_id: int, rule_id: int) -> ChannelCleanupRule | None:
        result = await self.session.execute(
            select(ChannelCleanupRule).where(
                ChannelCleanupRule.guild_id == guild_id,
                ChannelCleanupRule.id == rule_id,
            )
        )
        return result.scalar_one_or_none()

    async def add(
        self,
        *,
        guild_id: int,
        channel_id: int,
        created_by_user_id: int,
        current_date_utc: date,
    ) -> ChannelCleanupRule:
        existing = await self.session.execute(
            select(ChannelCleanupRule).where(
                ChannelCleanupRule.guild_id == guild_id,
                ChannelCleanupRule.channel_id == channel_id,
            )
        )
        if existing.scalar_one_or_none() is not None:
            raise CleanupValidationError("This channel already has a cleanup rule.")
        count = await self.session.scalar(
            select(func.count()).select_from(ChannelCleanupRule).where(ChannelCleanupRule.guild_id == guild_id)
        )
        if (count or 0) >= MAX_CLEANUP_RULES_PER_GUILD:
            raise CleanupValidationError(f"A server can have at most {MAX_CLEANUP_RULES_PER_GUILD} cleanup rules.")
        rule = ChannelCleanupRule(
            guild_id=guild_id,
            channel_id=channel_id,
            created_by_user_id=created_by_user_id,
            last_run_date_utc=current_date_utc,
        )
        self.session.add(rule)
        await self.session.commit()
        await self.session.refresh(rule)
        return rule

    async def update_channel(self, rule: ChannelCleanupRule, channel_id: int) -> ChannelCleanupRule:
        existing = await self.session.execute(
            select(ChannelCleanupRule).where(
                ChannelCleanupRule.guild_id == rule.guild_id,
                ChannelCleanupRule.channel_id == channel_id,
                ChannelCleanupRule.id != rule.id,
            )
        )
        if existing.scalar_one_or_none() is not None:
            raise CleanupValidationError("This channel already has a cleanup rule.")
        rule.channel_id = channel_id
        await self.session.commit()
        await self.session.refresh(rule)
        return rule

    async def remove(self, guild_id: int, rule_id: int) -> ChannelCleanupRule | None:
        rule = await self.get(guild_id, rule_id)
        if rule is None:
            return None
        await self.session.delete(rule)
        await self.session.commit()
        return rule

    @staticmethod
    def missing_permissions(channel: discord.TextChannel, member: discord.Member) -> list[str]:
        permissions = channel.permissions_for(member)
        required = {
            "view_channel": "View Channel",
            "read_message_history": "Read Message History",
            "manage_messages": "Manage Messages",
        }
        return [label for attr, label in required.items() if not getattr(permissions, attr, False)]

    @staticmethod
    async def purge_channel(channel: discord.TextChannel) -> int:
        deleted = await channel.purge(limit=None, check=lambda message: not message.pinned, bulk=True)
        return len(deleted)
