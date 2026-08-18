from __future__ import annotations

from datetime import date, datetime, time
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from sqlalchemy import inspect, text

from app.database import Database
from app.commands.reminder import ReminderCommands, parse_reminder_id
from app.services.cleanup_service import CleanupService, CleanupValidationError, parse_cleanup_time
from app.services.reminder_service import (
    ReminderInput,
    ReminderService,
    ReminderValidationError,
    build_message_content,
    next_fire_at,
    parse_iso_date,
    parse_utc_time,
    parse_weekday,
)
from app.services.scheduled_task_service import ScheduledTaskService


class FakeScheduledChannel:
    def __init__(self) -> None:
        self.sent: list[tuple[str, object]] = []

    async def send(self, content: str, *, allowed_mentions) -> None:
        self.sent.append((content, allowed_mentions))


class FakePurgeMessage:
    def __init__(self, pinned: bool) -> None:
        self.pinned = pinned


class FakePurgeChannel:
    def __init__(self) -> None:
        self.messages = [FakePurgeMessage(False), FakePurgeMessage(True), FakePurgeMessage(False)]

    async def purge(self, *, limit, check, bulk):
        return [message for message in self.messages if check(message)]


class FakeScheduledGuild:
    def __init__(self, channel: FakeScheduledChannel) -> None:
        self.channel = channel

    def get_channel(self, channel_id: int):
        return self.channel if channel_id == 456 else None


class FakeScheduledBot:
    def __init__(self, channel: FakeScheduledChannel) -> None:
        self.guild = FakeScheduledGuild(channel)

    def get_guild(self, guild_id: int):
        return self.guild if guild_id == 123 else None


def reminder_input(**overrides) -> ReminderInput:
    values = {
        "guild_id": 123,
        "channel_id": 456,
        "created_by_user_id": 789,
        "title": "Daily reset",
        "message": "Reset happens soon",
        "repeats": "daily",
        "event_time_utc": time(12, 0),
        "event_date": None,
        "weekday": None,
        "day_of_month": None,
        "every": 1,
        "offset_minutes": 0,
        "start_date": date(2026, 1, 1),
        "mention_mode": "none",
    }
    values.update(overrides)
    return ReminderInput(**values)


class ReminderScheduleTest(unittest.TestCase):
    def test_reminder_id_accepts_autocomplete_string_and_rejects_invalid_value(self) -> None:
        self.assertEqual(parse_reminder_id(" 42 "), 42)
        for value in ("", "Daily reset", "0", "-1"):
            with self.subTest(value=value), self.assertRaises(ReminderValidationError):
                parse_reminder_id(value)

    def test_parsers_require_explicit_utc_formats(self) -> None:
        self.assertEqual(parse_utc_time("17:50"), time(17, 50))
        self.assertEqual(parse_iso_date("2026-08-17", "Date"), date(2026, 8, 17))
        self.assertEqual(parse_weekday("Monday"), 0)
        with self.assertRaises(ReminderValidationError):
            parse_utc_time("5pm")
        with self.assertRaises(ReminderValidationError):
            parse_iso_date("17-08-2026", "Date")

    def test_daily_every_other_day_and_offset(self) -> None:
        value = reminder_input(every=2, offset_minutes=15)
        self.assertEqual(
            next_fire_at(value, after=datetime(2026, 1, 2, 13, 0)),
            datetime(2026, 1, 3, 11, 45),
        )

    def test_weekly_every_four_weeks(self) -> None:
        value = reminder_input(repeats="weekly", weekday=0, every=4)
        self.assertEqual(
            next_fire_at(value, after=datetime(2026, 1, 6, 0, 0)),
            datetime(2026, 2, 2, 12, 0),
        )

    def test_monthly_skips_missing_day(self) -> None:
        value = reminder_input(repeats="monthly", day_of_month=31, start_date=date(2026, 1, 1))
        self.assertEqual(
            next_fire_at(value, after=datetime(2026, 1, 31, 12, 1)),
            datetime(2026, 3, 31, 12, 0),
        )

    def test_once_in_past_has_no_next_fire(self) -> None:
        value = reminder_input(
            repeats="once",
            event_date=date(2026, 1, 1),
            start_date=None,
        )
        self.assertIsNone(next_fire_at(value, after=datetime(2026, 1, 2)))


class ReminderPersistenceTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        db_path = Path(self.temp_dir.name) / "test.db"
        self.database = Database(f"sqlite+aiosqlite:///{db_path.as_posix()}")
        await self.database.create_tables()

    async def asyncTearDown(self) -> None:
        await self.database.close()
        self.temp_dir.cleanup()

    async def test_add_list_update_remove_are_guild_scoped(self) -> None:
        async with self.database.session() as session:
            service = ReminderService(session)
            first = await service.add(reminder_input(), now=datetime(2026, 1, 1, 0, 0))
            reminder_id = first.id
            self.assertEqual(len(await service.list_for_guild(123)), 1)
            self.assertEqual(await service.list_for_guild(999), [])
            updated = await service.update(
                first,
                reminder_input(title="Changed", every=2),
                now=datetime(2026, 1, 1, 0, 0),
            )
            self.assertEqual(updated.title, "Changed")
            self.assertEqual(updated.every, 2)
            self.assertIsNone(await service.remove(999, reminder_id))
            self.assertIsNotNone(await service.remove(123, reminder_id))
            self.assertIsNone(await service.get(123, reminder_id))

    async def test_autocomplete_returns_string_id_for_discord_text_option(self) -> None:
        async with self.database.session() as session:
            reminder = await ReminderService(session).add(
                reminder_input(title="Clickable edit"),
                now=datetime(2026, 1, 1, 0, 0),
            )
        interaction = SimpleNamespace(guild=SimpleNamespace(id=123))
        choices = await ReminderCommands(self.database)._autocomplete(interaction, "Clickable")
        self.assertEqual(len(choices), 1)
        self.assertEqual(choices[0].value, str(reminder.id))

    async def test_message_mentions_only_use_selected_bottom_line(self) -> None:
        async with self.database.session() as session:
            reminder = await ReminderService(session).add(
                reminder_input(
                    title="@everyone **title**",
                    message="Hello <@123> @here *body*",
                    mention_mode="everyone",
                ),
                now=datetime(2026, 1, 1, 0, 0),
            )
            content, allowed = build_message_content(reminder)
        self.assertIn("@\u200beveryone", content.splitlines()[0])
        self.assertIn("<@\u200b123>", content)
        self.assertIn("@\u200bhere", content)
        self.assertNotIn("*", content)
        self.assertTrue(content.endswith("\n\n@everyone"))
        self.assertTrue(allowed.everyone)
        self.assertFalse(allowed.users)
        self.assertFalse(allowed.roles)

    async def test_message_preserves_unicode_and_custom_emojis(self) -> None:
        async with self.database.session() as session:
            reminder = await ReminderService(session).add(
                reminder_input(
                    title="Launch 🚀 <:party_parrot:123456789012345678>",
                    message="Team 👨‍👩‍👧‍👦 👍🏽 <a:dance_party:987654321098765432>",
                ),
                now=datetime(2026, 1, 1, 0, 0),
            )
            content, allowed = build_message_content(reminder)
        self.assertIn("🚀", content)
        self.assertIn("👨‍👩‍👧‍👦", content)
        self.assertIn("👍🏽", content)
        self.assertIn("<:party_parrot:123456789012345678>", content)
        self.assertIn("<a:dance_party:987654321098765432>", content)
        self.assertFalse(allowed.everyone)

    async def test_cleanup_rules_are_unique_per_channel_and_guild_scoped(self) -> None:
        async with self.database.session() as session:
            service = CleanupService(session)
            first = await service.add(
                guild_id=123,
                channel_id=10,
                created_by_user_id=5,
                cleanup_time_utc=time(17, 30),
                current_date_utc=date(2026, 8, 17),
            )
            self.assertEqual(first.last_run_date_utc, date(2026, 8, 17))
            self.assertEqual(first.cleanup_time_utc, time(17, 30))
            with self.assertRaises(CleanupValidationError):
                await service.add(
                    guild_id=123,
                    channel_id=10,
                    created_by_user_id=5,
                    cleanup_time_utc=time(17, 30),
                    current_date_utc=date(2026, 8, 17),
                )
            self.assertEqual(len(await service.list_for_guild(123)), 1)
            self.assertEqual(await service.list_for_guild(999), [])

    async def test_scheduler_sends_due_reminder_and_skips_old_recurrences(self) -> None:
        now = datetime(2026, 1, 10, 12, 0)
        async with self.database.session() as session:
            reminder = await ReminderService(session).add(
                reminder_input(event_time_utc=time(9, 0)),
                now=datetime(2026, 1, 1, 0, 0),
            )
            reminder.next_fire_at_utc = datetime(2026, 1, 2, 9, 0)
            reminder_id = reminder.id
            await session.commit()

        channel = FakeScheduledChannel()
        scheduler = ScheduledTaskService(self.database, FakeScheduledBot(channel))
        with patch("app.services.scheduled_task_service.discord.TextChannel", FakeScheduledChannel):
            await scheduler.run_once(now=now)

        self.assertEqual(len(channel.sent), 1)
        async with self.database.session() as session:
            updated = await ReminderService(session).get(123, reminder_id)
            self.assertIsNotNone(updated)
            self.assertEqual(updated.next_fire_at_utc, datetime(2026, 1, 11, 9, 0))

    async def test_cleanup_purge_keeps_pinned_messages(self) -> None:
        self.assertEqual(await CleanupService.purge_channel(FakePurgeChannel()), 2)

    async def test_cleanup_runs_only_after_configured_utc_time(self) -> None:
        async with self.database.session() as session:
            await CleanupService(session).add(
                guild_id=123,
                channel_id=456,
                created_by_user_id=5,
                cleanup_time_utc=time(14, 30),
                current_date_utc=date(2026, 8, 17),
            )

        channel = FakeScheduledChannel()
        scheduler = ScheduledTaskService(self.database, FakeScheduledBot(channel))
        purge = AsyncMock(return_value=3)
        with (
            patch("app.services.scheduled_task_service.discord.TextChannel", FakeScheduledChannel),
            patch("app.services.scheduled_task_service.CleanupService.purge_channel", purge),
        ):
            await scheduler.run_once(now=datetime(2026, 8, 18, 14, 29))
            purge.assert_not_awaited()
            await scheduler.run_once(now=datetime(2026, 8, 18, 14, 30))
            purge.assert_awaited_once_with(channel)

    def test_cleanup_time_requires_hh_mm(self) -> None:
        self.assertEqual(parse_cleanup_time("09:05"), time(9, 5))
        with self.assertRaises(CleanupValidationError):
            parse_cleanup_time("9am")

    async def test_existing_cleanup_table_gets_default_time_column(self) -> None:
        legacy_path = Path(self.temp_dir.name) / "legacy-cleanup.db"
        database = Database(f"sqlite+aiosqlite:///{legacy_path.as_posix()}")
        async with database.engine.begin() as connection:
            await connection.execute(
                text(
                    "CREATE TABLE channel_cleanup_rules ("
                    "id INTEGER PRIMARY KEY, guild_id BIGINT NOT NULL, channel_id BIGINT NOT NULL, "
                    "created_by_user_id BIGINT NOT NULL, last_run_date_utc DATE, "
                    "created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL)"
                )
            )
        await database.create_tables()
        async with database.engine.begin() as connection:
            columns = await connection.run_sync(
                lambda sync_connection: {
                    column["name"]: column for column in inspect(sync_connection).get_columns("channel_cleanup_rules")
                }
            )
            row = (
                await connection.execute(
                    text(
                        "INSERT INTO channel_cleanup_rules "
                        "(guild_id, channel_id, created_by_user_id, last_run_date_utc, created_at, updated_at) "
                        "VALUES (1, 2, 3, NULL, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP) "
                        "RETURNING cleanup_time_utc"
                    )
                )
            ).one()
        self.assertIn("cleanup_time_utc", columns)
        self.assertEqual(str(row[0]), "00:00:00")
        await database.close()


if __name__ == "__main__":
    unittest.main()
