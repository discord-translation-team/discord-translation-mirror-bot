from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

import discord
from sqlalchemy import select

from app.database import Database
from app.models import ChannelCleanupRule, Reminder, ReminderExecution
from app.services.cleanup_service import CleanupService
from app.services.reminder_service import build_message_content, next_fire_at


logger = logging.getLogger(__name__)
MAX_DUE_PER_GUILD_PER_TICK = 5


class ScheduledTaskService:
    def __init__(self, database: Database, bot: discord.Client) -> None:
        self.database = database
        self.bot = bot

    async def run_once(self, *, now: datetime | None = None) -> None:
        now = now or datetime.now(UTC).replace(tzinfo=None)
        await self._run_reminders(now)
        await self._run_cleanups(now)

    async def _run_reminders(self, now: datetime) -> None:
        async with self.database.session() as session:
            result = await session.execute(
                select(Reminder)
                .where(Reminder.next_fire_at_utc <= now)
                .order_by(Reminder.guild_id, Reminder.next_fire_at_utc, Reminder.id)
            )
            due = list(result.scalars())

        selected: list[Reminder] = []
        counts: dict[int, int] = {}
        for reminder in due:
            count = counts.get(reminder.guild_id, 0)
            if count >= MAX_DUE_PER_GUILD_PER_TICK:
                continue
            counts[reminder.guild_id] = count + 1
            selected.append(reminder)

        for reminder in selected:
            await self._deliver_reminder(reminder.id, now)

    async def _deliver_reminder(self, reminder_id: int, now: datetime) -> None:
        async with self.database.session() as session:
            reminder = await session.get(Reminder, reminder_id)
            if reminder is None or reminder.next_fire_at_utc > now:
                return
            scheduled_for = reminder.next_fire_at_utc
            existing = await session.execute(
                select(ReminderExecution).where(
                    ReminderExecution.reminder_id == reminder.id,
                    ReminderExecution.scheduled_for_utc == scheduled_for,
                )
            )
            execution = existing.scalar_one_or_none()
            if execution is not None:
                if execution.status == "sent":
                    return
                if execution.status == "claimed" and execution.attempted_at_utc > now - timedelta(minutes=5):
                    return
                execution.status = "claimed"
                execution.attempted_at_utc = now
                execution.error_type = None
            else:
                execution = ReminderExecution(
                    reminder_id=reminder.id,
                    scheduled_for_utc=scheduled_for,
                    status="claimed",
                    attempted_at_utc=now,
                )
                session.add(execution)
            await session.commit()

            guild = self.bot.get_guild(reminder.guild_id)
            channel = guild.get_channel(reminder.channel_id) if guild else None
            if not isinstance(channel, discord.TextChannel):
                await self._mark_failed(session, execution, "ChannelNotFound")
                return

            content, allowed_mentions = build_message_content(reminder)
            try:
                await channel.send(content, allowed_mentions=allowed_mentions)
            except discord.DiscordException as exc:
                await self._mark_failed(session, execution, type(exc).__name__)
                return

            execution.status = "sent"
            execution.error_type = None
            if reminder.repeats == "once":
                await session.delete(reminder)
            else:
                reminder.next_fire_at_utc = next_fire_at(reminder, after=now)
            await session.commit()
            logger.info(
                "reminder_sent",
                extra={
                    "guild_id": reminder.guild_id,
                    "channel_id": reminder.channel_id,
                    "reminder_id": reminder.id,
                    "scheduled_for_utc": scheduled_for.isoformat(),
                },
            )

    @staticmethod
    async def _mark_failed(session, execution: ReminderExecution, error_type: str) -> None:
        execution.status = "failed"
        execution.error_type = error_type
        await session.commit()
        logger.warning(
            "reminder_send_failed",
            extra={
                "reminder_id": execution.reminder_id,
                "scheduled_for_utc": execution.scheduled_for_utc.isoformat(),
                "error_type": error_type,
            },
        )

    async def _run_cleanups(self, now: datetime) -> None:
        current_date = now.date()
        async with self.database.session() as session:
            result = await session.execute(
                select(ChannelCleanupRule).where(
                    (ChannelCleanupRule.last_run_date_utc.is_(None))
                    | (ChannelCleanupRule.last_run_date_utc < current_date)
                )
            )
            rule_ids = [rule.id for rule in result.scalars()]

        for rule_id in rule_ids:
            async with self.database.session() as session:
                rule = await session.get(ChannelCleanupRule, rule_id)
                if rule is None or rule.last_run_date_utc == current_date:
                    continue
                guild = self.bot.get_guild(rule.guild_id)
                channel = guild.get_channel(rule.channel_id) if guild else None
                if not isinstance(channel, discord.TextChannel):
                    logger.warning(
                        "cleanup_channel_not_found",
                        extra={"guild_id": rule.guild_id, "channel_id": rule.channel_id, "cleanup_rule_id": rule.id},
                    )
                    continue
                try:
                    deleted_count = await CleanupService.purge_channel(channel)
                except discord.DiscordException as exc:
                    logger.warning(
                        "cleanup_failed",
                        extra={
                            "guild_id": rule.guild_id,
                            "channel_id": rule.channel_id,
                            "cleanup_rule_id": rule.id,
                            "error_type": type(exc).__name__,
                        },
                    )
                    continue
                rule.last_run_date_utc = current_date
                await session.commit()
                logger.info(
                    "cleanup_completed",
                    extra={
                        "guild_id": rule.guild_id,
                        "channel_id": rule.channel_id,
                        "cleanup_rule_id": rule.id,
                        "deleted_count": deleted_count,
                    },
                )
