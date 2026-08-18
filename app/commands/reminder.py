from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

import discord
from discord import app_commands
from discord.ext import commands

from app.database import Database
from app.services.reminder_service import (
    ReminderInput,
    ReminderService,
    ReminderValidationError,
    autocomplete_label,
    parse_iso_date,
    parse_utc_time,
    parse_weekday,
    schedule_label,
)


Repeats = Literal["once", "daily", "weekly", "monthly"]
Weekday = Literal["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
MentionMode = Literal["none", "everyone", "here"]


def utc_timestamp(value: datetime) -> int:
    return int(value.replace(tzinfo=UTC).timestamp())


def parse_reminder_id(value: str) -> int:
    try:
        reminder_id = int(value.strip())
    except (TypeError, ValueError) as exc:
        raise ReminderValidationError("Choose a reminder from the list or enter its numeric ID.") from exc
    if reminder_id <= 0:
        raise ReminderValidationError("Choose a reminder from the list or enter its numeric ID.")
    return reminder_id


class ReminderRemoveView(discord.ui.View):
    def __init__(self, database: Database, guild_id: int, reminder_id: int, owner_id: int) -> None:
        super().__init__(timeout=60)
        self.database = database
        self.guild_id = guild_id
        self.reminder_id = reminder_id
        self.owner_id = owner_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("Only the administrator who started this action can confirm it.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Remove reminder", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        async with self.database.session() as session:
            removed = await ReminderService(session).remove(self.guild_id, self.reminder_id)
        if removed is None:
            await interaction.response.edit_message(content="Reminder no longer exists.", view=None)
        else:
            await interaction.response.edit_message(
                content=f"Removed reminder `{removed.id}`: {discord.utils.escape_markdown(removed.title)}",
                view=None,
            )
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.edit_message(content="Removal cancelled.", view=None)
        self.stop()


class ReminderCommands(commands.GroupCog, group_name="reminder", group_description="Manage scheduled reminders"):
    def __init__(self, database: Database) -> None:
        self.database = database

    @staticmethod
    def _missing_permissions(channel: discord.TextChannel, member: discord.Member, mention_mode: str) -> list[str]:
        permissions = channel.permissions_for(member)
        required = {"view_channel": "View Channel", "send_messages": "Send Messages"}
        missing = [label for attr, label in required.items() if not getattr(permissions, attr, False)]
        if mention_mode != "none" and not permissions.mention_everyone:
            missing.append("Mention Everyone")
        return missing

    @staticmethod
    def _input(
        *,
        guild_id: int,
        user_id: int,
        channel_id: int,
        repeats: str,
        time_value: str,
        title: str,
        message: str,
        date_value: str | None,
        weekday: str | None,
        day_of_month: int | None,
        every: int,
        offset_minutes: int,
        start_date: str | None,
        mention_mode: str,
    ) -> ReminderInput:
        return ReminderInput(
            guild_id=guild_id,
            channel_id=channel_id,
            created_by_user_id=user_id,
            title=title.strip(),
            message=message.strip(),
            repeats=repeats,
            event_time_utc=parse_utc_time(time_value),
            event_date=parse_iso_date(date_value, "Date"),
            weekday=parse_weekday(weekday),
            day_of_month=day_of_month,
            every=every,
            offset_minutes=offset_minutes,
            start_date=parse_iso_date(start_date, "Start date"),
            mention_mode=mention_mode,
        )

    @app_commands.command(name="list", description="List reminders for this server")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def list_reminders(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("This command is only available in a server.", ephemeral=True)
            return
        async with self.database.session() as session:
            reminders = await ReminderService(session).list_for_guild(interaction.guild.id)
        if not reminders:
            await interaction.response.send_message("No reminders configured.", ephemeral=True)
            return
        lines = [
            f"`{item.id}` · {discord.utils.escape_markdown(item.title)} · {schedule_label(item)}"
            f" · next <t:{utc_timestamp(item.next_fire_at_utc)}:R>"
            for item in reminders
        ]
        pages: list[str] = []
        current = "**REMINDERS**\n"
        for line in lines:
            if len(current) + len(line) + 1 > 1900:
                pages.append(current)
                current = "**REMINDERS (continued)**\n"
            current += line + "\n"
        pages.append(current)
        await interaction.response.send_message(pages[0], ephemeral=True)
        for page in pages[1:]:
            await interaction.followup.send(page, ephemeral=True)

    @app_commands.command(name="add", description="Add a scheduled reminder")
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.describe(
        channel="Channel where the reminder is sent",
        repeats="How the reminder repeats",
        time="Event time as HH:MM (UTC)",
        title="Plain title shown above the message",
        message="Plain reminder message",
        date="YYYY-MM-DD for a one-time reminder",
        weekday="Required for weekly reminders",
        day_of_month="Required for monthly reminders (1-31)",
        every="Cadence multiplier (1-365)",
        offset_minutes="Send this many minutes before the event (0-10080)",
        start_date="Optional repeating anchor as YYYY-MM-DD",
        mention_everyone_or_here="Optional ping appended below the message",
    )
    async def add(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
        repeats: Repeats,
        time: str,
        title: app_commands.Range[str, 1, 100],
        message: app_commands.Range[str, 1, 1800],
        date: str | None = None,
        weekday: Weekday | None = None,
        day_of_month: app_commands.Range[int, 1, 31] | None = None,
        every: app_commands.Range[int, 1, 365] = 1,
        offset_minutes: app_commands.Range[int, 0, 10080] = 0,
        start_date: str | None = None,
        mention_everyone_or_here: MentionMode = "none",
    ) -> None:
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("This command is only available in a server.", ephemeral=True)
            return
        if channel.guild.id != interaction.guild.id or interaction.guild.me is None:
            await interaction.response.send_message("Choose a channel from this server.", ephemeral=True)
            return
        missing = self._missing_permissions(channel, interaction.guild.me, mention_everyone_or_here)
        if missing:
            await interaction.response.send_message("The bot is missing: " + ", ".join(missing), ephemeral=True)
            return
        try:
            value = self._input(
                guild_id=interaction.guild.id,
                user_id=interaction.user.id,
                channel_id=channel.id,
                repeats=repeats,
                time_value=time,
                title=title,
                message=message,
                date_value=date,
                weekday=weekday,
                day_of_month=day_of_month,
                every=every,
                offset_minutes=offset_minutes,
                start_date=start_date,
                mention_mode=mention_everyone_or_here,
            )
            async with self.database.session() as session:
                reminder = await ReminderService(session).add(value)
        except ReminderValidationError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        await interaction.response.send_message(
            f"Added reminder `{reminder.id}`: {schedule_label(reminder)}. "
            f"Next send <t:{utc_timestamp(reminder.next_fire_at_utc)}:F>.",
            ephemeral=True,
        )

    @app_commands.command(name="edit", description="Edit an existing reminder")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def edit(
        self,
        interaction: discord.Interaction,
        reminder_id: str,
        channel: discord.TextChannel | None = None,
        repeats: Repeats | None = None,
        time: str | None = None,
        title: app_commands.Range[str, 1, 100] | None = None,
        message: app_commands.Range[str, 1, 1800] | None = None,
        date: str | None = None,
        weekday: Weekday | None = None,
        day_of_month: app_commands.Range[int, 1, 31] | None = None,
        every: app_commands.Range[int, 1, 365] | None = None,
        offset_minutes: app_commands.Range[int, 0, 10080] | None = None,
        start_date: str | None = None,
        mention_everyone_or_here: MentionMode | None = None,
    ) -> None:
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("This command is only available in a server.", ephemeral=True)
            return
        try:
            parsed_reminder_id = parse_reminder_id(reminder_id)
        except ReminderValidationError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        async with self.database.session() as session:
            service = ReminderService(session)
            current = await service.get(interaction.guild.id, parsed_reminder_id)
            if current is None:
                await interaction.response.send_message("Reminder not found.", ephemeral=True)
                return
            selected_channel = channel or interaction.guild.get_channel(current.channel_id)
            if not isinstance(selected_channel, discord.TextChannel) or interaction.guild.me is None:
                await interaction.response.send_message("Choose an available text channel from this server.", ephemeral=True)
                return
            mention_mode = mention_everyone_or_here or current.mention_mode
            missing = self._missing_permissions(selected_channel, interaction.guild.me, mention_mode)
            if missing:
                await interaction.response.send_message("The bot is missing: " + ", ".join(missing), ephemeral=True)
                return
            new_repeats = repeats or current.repeats
            try:
                resolved_date = (
                    date if date is not None else (current.event_date.isoformat() if current.event_date else None)
                ) if new_repeats == "once" else None
                resolved_weekday = (
                    weekday if weekday is not None else (
                        ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"][current.weekday]
                        if current.weekday is not None else None
                    )
                ) if new_repeats == "weekly" else None
                resolved_day_of_month = (
                    day_of_month if day_of_month is not None else current.day_of_month
                ) if new_repeats == "monthly" else None
                resolved_start_date = (
                    start_date if start_date is not None else (
                        current.start_date.isoformat() if current.start_date else None
                    )
                ) if new_repeats != "once" else None
                value = self._input(
                    guild_id=current.guild_id,
                    user_id=current.created_by_user_id,
                    channel_id=selected_channel.id,
                    repeats=new_repeats,
                    time_value=time or current.event_time_utc.strftime("%H:%M"),
                    title=title or current.title,
                    message=message or current.message,
                    date_value=resolved_date,
                    weekday=resolved_weekday,
                    day_of_month=resolved_day_of_month,
                    every=every if every is not None else current.every,
                    offset_minutes=offset_minutes if offset_minutes is not None else current.offset_minutes,
                    start_date=resolved_start_date,
                    mention_mode=mention_mode,
                )
                reminder = await service.update(current, value)
            except ReminderValidationError as exc:
                await interaction.response.send_message(str(exc), ephemeral=True)
                return
        await interaction.response.send_message(
            f"Updated reminder `{reminder.id}`: {schedule_label(reminder)}. "
            f"Next send <t:{utc_timestamp(reminder.next_fire_at_utc)}:F>.",
            ephemeral=True,
        )

    @edit.autocomplete("reminder_id")
    async def edit_autocomplete(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        return await self._autocomplete(interaction, current)

    @app_commands.command(name="remove", description="Remove a reminder")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def remove(self, interaction: discord.Interaction, reminder_id: str) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("This command is only available in a server.", ephemeral=True)
            return
        try:
            parsed_reminder_id = parse_reminder_id(reminder_id)
        except ReminderValidationError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        async with self.database.session() as session:
            reminder = await ReminderService(session).get(interaction.guild.id, parsed_reminder_id)
        if reminder is None:
            await interaction.response.send_message("Reminder not found.", ephemeral=True)
            return
        view = ReminderRemoveView(self.database, interaction.guild.id, reminder.id, interaction.user.id)
        await interaction.response.send_message(
            f"Remove reminder `{reminder.id}`: {discord.utils.escape_markdown(reminder.title)}?",
            view=view,
            ephemeral=True,
        )

    @remove.autocomplete("reminder_id")
    async def remove_autocomplete(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        return await self._autocomplete(interaction, current)

    async def _autocomplete(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        if interaction.guild is None:
            return []
        async with self.database.session() as session:
            reminders = await ReminderService(session).list_for_guild(interaction.guild.id)
        query = current.casefold().strip()
        matches = [item for item in reminders if not query or query in str(item.id) or query in item.title.casefold()]
        return [app_commands.Choice(name=autocomplete_label(item), value=str(item.id)) for item in matches[:25]]
