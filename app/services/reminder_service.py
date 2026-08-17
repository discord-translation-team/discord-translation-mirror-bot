from __future__ import annotations

import calendar
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, time, timedelta
import re

import discord
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Reminder
from app.mention_safety import sanitize_mentions


REPEAT_VALUES = {"once", "daily", "weekly", "monthly"}
MENTION_VALUES = {"none", "everyone", "here"}
MAX_REMINDERS_PER_GUILD = 100
MAX_EVERY = 365
MAX_OFFSET_MINUTES = 10_080
WEEKDAYS = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}


class ReminderValidationError(ValueError):
    pass


@dataclass(frozen=True)
class ReminderInput:
    guild_id: int
    channel_id: int
    created_by_user_id: int
    title: str
    message: str
    repeats: str
    event_time_utc: time
    event_date: date | None
    weekday: int | None
    day_of_month: int | None
    every: int
    offset_minutes: int
    start_date: date | None
    mention_mode: str


def parse_utc_time(value: str) -> time:
    try:
        parsed = datetime.strptime(value.strip(), "%H:%M").time()
    except ValueError as exc:
        raise ReminderValidationError("Time must use HH:MM in UTC, for example 17:50.") from exc
    return parsed


def parse_iso_date(value: str | None, field_name: str) -> date | None:
    if value is None or not value.strip():
        return None
    try:
        return date.fromisoformat(value.strip())
    except ValueError as exc:
        raise ReminderValidationError(f"{field_name} must use YYYY-MM-DD (UTC).") from exc


def parse_weekday(value: str | None) -> int | None:
    if value is None:
        return None
    result = WEEKDAYS.get(value.strip().lower())
    if result is None:
        raise ReminderValidationError("Choose a weekday from Monday through Sunday.")
    return result


def _combine(day: date, event_time: time, offset_minutes: int) -> datetime:
    return datetime.combine(day, event_time) - timedelta(minutes=offset_minutes)


def _month_index(day: date) -> int:
    return day.year * 12 + day.month - 1


def _month_from_index(value: int) -> tuple[int, int]:
    return value // 12, value % 12 + 1


def next_fire_at(reminder: ReminderInput | Reminder, *, after: datetime) -> datetime | None:
    if after.tzinfo is not None:
        after = after.astimezone(UTC).replace(tzinfo=None)
    repeats = reminder.repeats
    offset = reminder.offset_minutes

    if repeats == "once":
        if reminder.event_date is None:
            return None
        candidate = _combine(reminder.event_date, reminder.event_time_utc, offset)
        return candidate if candidate > after else None

    anchor = reminder.start_date or after.date()
    if repeats == "daily":
        elapsed = max(0, (after.date() - anchor).days)
        step = reminder.every
        day = anchor + timedelta(days=(elapsed // step) * step)
        candidate = _combine(day, reminder.event_time_utc, offset)
        while candidate <= after:
            day += timedelta(days=step)
            candidate = _combine(day, reminder.event_time_utc, offset)
        return candidate

    if repeats == "weekly":
        assert reminder.weekday is not None
        first = anchor + timedelta(days=(reminder.weekday - anchor.weekday()) % 7)
        step_days = reminder.every * 7
        elapsed = max(0, (after.date() - first).days)
        day = first + timedelta(days=(elapsed // step_days) * step_days)
        candidate = _combine(day, reminder.event_time_utc, offset)
        while candidate <= after:
            day += timedelta(days=step_days)
            candidate = _combine(day, reminder.event_time_utc, offset)
        return candidate

    if repeats == "monthly":
        assert reminder.day_of_month is not None
        anchor_index = _month_index(anchor)
        current_index = _month_index(after.date())
        periods = max(0, (current_index - anchor_index) // reminder.every)
        index = anchor_index + periods * reminder.every
        for _ in range(0, 2400):
            year, month = _month_from_index(index)
            last_day = calendar.monthrange(year, month)[1]
            if reminder.day_of_month <= last_day:
                day = date(year, month, reminder.day_of_month)
                if day >= anchor:
                    candidate = _combine(day, reminder.event_time_utc, offset)
                    if candidate > after:
                        return candidate
            index += reminder.every
        raise RuntimeError("Could not calculate a monthly reminder occurrence.")

    raise ReminderValidationError("Unsupported repeats value.")


def validate_input(value: ReminderInput, *, now: datetime) -> None:
    if value.repeats not in REPEAT_VALUES:
        raise ReminderValidationError("Repeats must be once, daily, weekly, or monthly.")
    if value.mention_mode not in MENTION_VALUES:
        raise ReminderValidationError("Mention must be none, everyone, or here.")
    if not 1 <= len(value.title.strip()) <= 100:
        raise ReminderValidationError("Title must contain 1 to 100 characters.")
    if not 1 <= len(value.message.strip()) <= 1800:
        raise ReminderValidationError("Message must contain 1 to 1800 characters.")
    if not 1 <= value.every <= MAX_EVERY:
        raise ReminderValidationError(f"Every must be between 1 and {MAX_EVERY}.")
    if not 0 <= value.offset_minutes <= MAX_OFFSET_MINUTES:
        raise ReminderValidationError("Offset minutes must be between 0 and 10080.")
    if value.repeats == "once" and value.event_date is None:
        raise ReminderValidationError("Date is required when repeats is once.")
    if value.repeats != "once" and value.event_date is not None:
        raise ReminderValidationError("Date can only be used when repeats is once.")
    if value.repeats == "weekly" and value.weekday is None:
        raise ReminderValidationError("Weekday is required for weekly reminders.")
    if value.repeats != "weekly" and value.weekday is not None:
        raise ReminderValidationError("Weekday can only be used for weekly reminders.")
    if value.repeats == "monthly" and not (value.day_of_month and 1 <= value.day_of_month <= 31):
        raise ReminderValidationError("Day of month must be from 1 to 31 for monthly reminders.")
    if value.repeats != "monthly" and value.day_of_month is not None:
        raise ReminderValidationError("Day of month can only be used for monthly reminders.")
    if value.repeats == "once" and value.start_date is not None:
        raise ReminderValidationError("Start date can only be used for repeating reminders.")
    if next_fire_at(value, after=now) is None:
        raise ReminderValidationError("The reminder fire time must be in the future.")


def safe_text(value: str) -> str:
    return sanitize_mentions(discord.utils.remove_markdown(value.strip()))


def build_message_content(reminder: Reminder) -> tuple[str, discord.AllowedMentions]:
    lines = [safe_text(reminder.title), safe_text(reminder.message)]
    mention = None
    if reminder.mention_mode == "everyone":
        mention = "@everyone"
    elif reminder.mention_mode == "here":
        mention = "@here"
    if mention:
        lines.extend(["", mention])
    return "\n".join(lines), discord.AllowedMentions(everyone=mention is not None, users=False, roles=False)


class ReminderService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def list_for_guild(self, guild_id: int) -> list[Reminder]:
        result = await self.session.execute(
            select(Reminder).where(Reminder.guild_id == guild_id).order_by(Reminder.next_fire_at_utc, Reminder.id)
        )
        return list(result.scalars())

    async def get(self, guild_id: int, reminder_id: int) -> Reminder | None:
        result = await self.session.execute(
            select(Reminder).where(Reminder.guild_id == guild_id, Reminder.id == reminder_id)
        )
        return result.scalar_one_or_none()

    async def add(self, value: ReminderInput, *, now: datetime | None = None) -> Reminder:
        now = now or datetime.now(UTC).replace(tzinfo=None)
        if value.repeats != "once" and value.start_date is None:
            value = replace(value, start_date=now.date())
        validate_input(value, now=now)
        count = await self.session.scalar(select(func.count()).select_from(Reminder).where(Reminder.guild_id == value.guild_id))
        if (count or 0) >= MAX_REMINDERS_PER_GUILD:
            raise ReminderValidationError(f"A server can have at most {MAX_REMINDERS_PER_GUILD} reminders.")
        reminder = Reminder(
            **value.__dict__,
            next_fire_at_utc=next_fire_at(value, after=now),
        )
        self.session.add(reminder)
        await self.session.commit()
        await self.session.refresh(reminder)
        return reminder

    async def update(self, reminder: Reminder, value: ReminderInput, *, now: datetime | None = None) -> Reminder:
        now = now or datetime.now(UTC).replace(tzinfo=None)
        if value.repeats != "once" and value.start_date is None:
            value = replace(value, start_date=now.date())
        validate_input(value, now=now)
        for key, field_value in value.__dict__.items():
            setattr(reminder, key, field_value)
        reminder.next_fire_at_utc = next_fire_at(value, after=now)
        await self.session.commit()
        await self.session.refresh(reminder)
        return reminder

    async def remove(self, guild_id: int, reminder_id: int) -> Reminder | None:
        reminder = await self.get(guild_id, reminder_id)
        if reminder is None:
            return None
        await self.session.delete(reminder)
        await self.session.commit()
        return reminder


def schedule_label(reminder: Reminder) -> str:
    at = reminder.event_time_utc.strftime("%H:%M")
    if reminder.repeats == "once":
        return f"once {reminder.event_date.isoformat()} {at} UTC"
    if reminder.repeats == "daily":
        unit = "day" if reminder.every == 1 else f"{reminder.every} days"
        return f"every {unit} at {at} UTC"
    if reminder.repeats == "weekly":
        day = calendar.day_name[reminder.weekday or 0]
        unit = "week" if reminder.every == 1 else f"{reminder.every} weeks"
        return f"every {unit} on {day} at {at} UTC"
    unit = "month" if reminder.every == 1 else f"{reminder.every} months"
    return f"every {unit} on day {reminder.day_of_month} at {at} UTC"


def autocomplete_label(reminder: Reminder) -> str:
    title = re.sub(r"\s+", " ", reminder.title).strip()
    return f"{reminder.id} · {title} · {schedule_label(reminder)}"[:100]
