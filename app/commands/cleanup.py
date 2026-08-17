from __future__ import annotations

from datetime import UTC, datetime

import discord
from discord import app_commands
from discord.ext import commands

from app.database import Database
from app.services.cleanup_service import CleanupService, CleanupValidationError


class CleanupRemoveView(discord.ui.View):
    def __init__(self, database: Database, guild_id: int, rule_id: int, owner_id: int) -> None:
        super().__init__(timeout=60)
        self.database = database
        self.guild_id = guild_id
        self.rule_id = rule_id
        self.owner_id = owner_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("Only the administrator who started this action can confirm it.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Remove cleanup rule", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        async with self.database.session() as session:
            removed = await CleanupService(session).remove(self.guild_id, self.rule_id)
        content = "Cleanup rule no longer exists." if removed is None else f"Removed cleanup rule `{removed.id}`."
        await interaction.response.edit_message(content=content, view=None)
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.edit_message(content="Removal cancelled.", view=None)
        self.stop()


class CleanupCommands(commands.GroupCog, group_name="cleanup", group_description="Manage daily channel cleanup"):
    def __init__(self, database: Database) -> None:
        self.database = database

    @staticmethod
    async def _validate_channel(interaction: discord.Interaction, channel: discord.TextChannel) -> bool:
        if interaction.guild is None or channel.guild.id != interaction.guild.id or interaction.guild.me is None:
            await interaction.response.send_message("Choose a text channel from this server.", ephemeral=True)
            return False
        missing = CleanupService.missing_permissions(channel, interaction.guild.me)
        if missing:
            await interaction.response.send_message("The bot is missing: " + ", ".join(missing), ephemeral=True)
            return False
        return True

    @app_commands.command(name="list", description="List channels cleaned daily at 00:00 UTC")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def list_rules(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("This command is only available in a server.", ephemeral=True)
            return
        async with self.database.session() as session:
            rules = await CleanupService(session).list_for_guild(interaction.guild.id)
        if not rules:
            await interaction.response.send_message("No cleanup rules configured.", ephemeral=True)
            return
        lines = [f"`{rule.id}` · <#{rule.channel_id}> · daily 00:00 UTC · keeps pinned messages" for rule in rules]
        await interaction.response.send_message("**CLEANUP RULES**\n" + "\n".join(lines), ephemeral=True)

    @app_commands.command(name="add", description="Clean all unpinned messages in a channel daily at 00:00 UTC")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def add(self, interaction: discord.Interaction, channel: discord.TextChannel) -> None:
        if not await self._validate_channel(interaction, channel):
            return
        assert interaction.guild is not None
        try:
            async with self.database.session() as session:
                rule = await CleanupService(session).add(
                    guild_id=interaction.guild.id,
                    channel_id=channel.id,
                    created_by_user_id=interaction.user.id,
                    current_date_utc=datetime.now(UTC).date(),
                )
        except CleanupValidationError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        await interaction.response.send_message(
            f"Added cleanup rule `{rule.id}` for {channel.mention}. It runs daily at 00:00 UTC and keeps pinned messages.",
            ephemeral=True,
        )

    @app_commands.command(name="edit", description="Change the channel for a cleanup rule")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def edit(self, interaction: discord.Interaction, rule_id: int, channel: discord.TextChannel) -> None:
        if interaction.guild is None or not await self._validate_channel(interaction, channel):
            return
        async with self.database.session() as session:
            service = CleanupService(session)
            rule = await service.get(interaction.guild.id, rule_id)
            if rule is None:
                await interaction.response.send_message("Cleanup rule not found.", ephemeral=True)
                return
            try:
                rule = await service.update_channel(rule, channel.id)
            except CleanupValidationError as exc:
                await interaction.response.send_message(str(exc), ephemeral=True)
                return
        await interaction.response.send_message(f"Cleanup rule `{rule.id}` now targets {channel.mention}.", ephemeral=True)

    @edit.autocomplete("rule_id")
    async def edit_autocomplete(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[int]]:
        return await self._autocomplete(interaction, current)

    @app_commands.command(name="remove", description="Remove a daily cleanup rule")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def remove(self, interaction: discord.Interaction, rule_id: int) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("This command is only available in a server.", ephemeral=True)
            return
        async with self.database.session() as session:
            rule = await CleanupService(session).get(interaction.guild.id, rule_id)
        if rule is None:
            await interaction.response.send_message("Cleanup rule not found.", ephemeral=True)
            return
        view = CleanupRemoveView(self.database, interaction.guild.id, rule.id, interaction.user.id)
        await interaction.response.send_message(
            f"Remove cleanup rule `{rule.id}` for <#{rule.channel_id}>?",
            view=view,
            ephemeral=True,
        )

    @remove.autocomplete("rule_id")
    async def remove_autocomplete(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[int]]:
        return await self._autocomplete(interaction, current)

    async def _autocomplete(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[int]]:
        if interaction.guild is None:
            return []
        async with self.database.session() as session:
            rules = await CleanupService(session).list_for_guild(interaction.guild.id)
        query = current.casefold().strip()
        choices: list[app_commands.Choice[int]] = []
        for rule in rules:
            channel = interaction.guild.get_channel(rule.channel_id)
            channel_name = getattr(channel, "name", str(rule.channel_id))
            if query and query not in str(rule.id) and query not in channel_name.casefold():
                continue
            choices.append(app_commands.Choice(name=f"{rule.id} · #{channel_name}"[:100], value=rule.id))
        return choices[:25]
