import tempfile
import unittest
from pathlib import Path

from sqlalchemy import select

from app.commands.admin import AdminCommands, DEFAULT_SETUP_LANGUAGES, parse_setup_language_list
from app.database import Database
from app.languages import is_supported_language, normalize_language_code, suggest_language_code
from app.models import LanguageRoleSetting, LanguageSetupMessage, TranslationChannelSetting, UserLanguageSetting
from app.services.language_service import LanguageService
from app.services.language_role_service import LanguageRoleService
from app.ui.language_setup import LanguageSelect, build_language_select_options, build_language_setup_embed


class FakeResponse:
    def __init__(self) -> None:
        self.messages: list[tuple[str, bool]] = []
        self.deferred = False

    async def send_message(self, content: str, ephemeral: bool = False, **kwargs) -> None:
        self.messages.append((content, ephemeral))

    async def defer(self, ephemeral: bool = False, **kwargs) -> None:
        self.deferred = True


class FakeFollowup:
    def __init__(self) -> None:
        self.messages: list[tuple[str, bool]] = []

    async def send(self, content: str, ephemeral: bool = False, **kwargs) -> None:
        self.messages.append((content, ephemeral))


class FakePermissions:
    def __init__(
        self,
        view_channel: bool = True,
        send_messages: bool = True,
        embed_links: bool = True,
        read_message_history: bool = True,
        add_reactions: bool = True,
        manage_roles: bool = True,
        manage_channels: bool = True,
        use_application_commands: bool = True,
    ) -> None:
        self.view_channel = view_channel
        self.send_messages = send_messages
        self.embed_links = embed_links
        self.read_message_history = read_message_history
        self.add_reactions = add_reactions
        self.manage_roles = manage_roles
        self.manage_channels = manage_channels
        self.use_application_commands = use_application_commands


class FakeRole:
    def __init__(self, role_id: int, name: str, position: int = 1) -> None:
        self.id = role_id
        self.name = name
        self.mention = f"@{name}"
        self.position = position

    def __gt__(self, other) -> bool:
        return self.position > other.position


class FakeCategory:
    def __init__(self, category_id: int, name: str) -> None:
        self.id = category_id
        self.name = name


class FakeMember:
    def __init__(
        self,
        user_id: int,
        roles: list[FakeRole] | None = None,
        fail_permissions: bool = False,
        guild_permissions: FakePermissions | None = None,
        top_role: FakeRole | None = None,
    ) -> None:
        self.id = user_id
        self.roles = roles or []
        self.added_roles: list[FakeRole] = []
        self.removed_roles: list[FakeRole] = []
        self.fail_permissions = fail_permissions
        self.guild_permissions = guild_permissions or FakePermissions()
        self.top_role = top_role or FakeRole(9999, "bot", position=100)

    async def add_roles(self, *roles: FakeRole, reason: str | None = None) -> None:
        if self.fail_permissions:
            raise PermissionError("missing manage roles")
        self.added_roles.extend(roles)
        for role in roles:
            if role.id not in {existing.id for existing in self.roles}:
                self.roles.append(role)

    async def remove_roles(self, *roles: FakeRole, reason: str | None = None) -> None:
        if self.fail_permissions:
            raise PermissionError("missing manage roles")
        self.removed_roles.extend(roles)
        remove_ids = {role.id for role in roles}
        self.roles = [role for role in self.roles if role.id not in remove_ids]


class FakeGuild:
    def __init__(
        self,
        guild_id: int = 111,
        roles: list[FakeRole] | None = None,
        channels: list[object] | None = None,
        categories: list[FakeCategory] | None = None,
        me: FakeMember | None = None,
    ) -> None:
        self.id = guild_id
        self.default_role = FakeRole(0, "@everyone", position=0)
        self.roles = [self.default_role, *(roles or [])]
        self._roles = {role.id: role for role in self.roles}
        self.text_channels = list(channels or [])
        self._channels = {channel.id: channel for channel in channels or []}
        self.categories = list(categories or [])
        self.me = me or FakeMember(999)
        self._next_id = 1000

    def get_role(self, role_id: int) -> FakeRole | None:
        return self._roles.get(role_id)

    def get_channel(self, channel_id: int):
        return self._channels.get(channel_id)

    def get_member(self, member_id: int):
        return self.me if self.me.id == member_id else None

    async def create_category(self, name: str, reason: str | None = None):
        category = FakeCategory(self._allocate_id(), name)
        self.categories.append(category)
        return category

    async def create_text_channel(self, name: str, category=None, overwrites=None, reason: str | None = None):
        channel = FakeChannel(self._allocate_id(), name=name, category=category, overwrites=overwrites)
        self.text_channels.append(channel)
        self._channels[channel.id] = channel
        return channel

    async def create_role(self, name: str, permissions=None, reason: str | None = None):
        role = FakeRole(self._allocate_id(), name, position=1)
        self.roles.append(role)
        self._roles[role.id] = role
        return role

    def _allocate_id(self) -> int:
        self._next_id += 1
        return self._next_id


class FakeInteraction:
    def __init__(
        self,
        database: Database,
        guild_id: int = 111,
        user_id: int = 222,
        guild: FakeGuild | None = None,
        user: FakeMember | None = None,
    ) -> None:
        self.guild = guild or FakeGuild(guild_id)
        self.user = user or FakeMember(user_id)
        self.client = type("Client", (), {"database": database, "user": type("User", (), {"id": 999})()})()
        self.response = FakeResponse()
        self.followup = FakeFollowup()


class FakeChannel:
    def __init__(
        self,
        channel_id: int = 333,
        permissions: FakePermissions | None = None,
        name: str | None = None,
        category=None,
        overwrites=None,
    ) -> None:
        self.id = channel_id
        self.mention = f"<#{channel_id}>"
        self.name = name or f"channel-{channel_id}"
        self.category = category
        self.overwrites = overwrites or {}
        self._permissions = permissions or FakePermissions()
        self.sent: list[dict[str, object]] = []
        self.messages: dict[int, FakeMessage] = {}
        self._next_message_id = 2000

    async def send(self, content: str | None = None, embed=None, view=None, allowed_mentions=None):
        message = FakeMessage(self._allocate_message_id(), self, embed=embed, view=view)
        self.messages[message.id] = message
        self.sent.append({"content": content, "embed": embed, "view": view, "message": message})
        return message

    def permissions_for(self, member: FakeMember) -> FakePermissions:
        return self._permissions

    async def edit(self, category=None, overwrites=None, reason: str | None = None):
        self.category = category
        self.overwrites = overwrites or {}

    async def fetch_message(self, message_id: int):
        message = self.messages.get(message_id)
        if message is None:
            raise FakeNotFound()
        return message

    def _allocate_message_id(self) -> int:
        self._next_message_id += 1
        return self._next_message_id


class FakeNotFound(Exception):
    pass


class FakeMessage:
    def __init__(self, message_id: int, channel: FakeChannel, embed=None, view=None) -> None:
        self.id = message_id
        self.channel = channel
        self.embed = embed
        self.view = view
        self.edit_count = 0
        self.deleted = False

    async def edit(self, embed=None, view=None, allowed_mentions=None):
        self.embed = embed
        self.view = view
        self.edit_count += 1

    async def delete(self):
        self.deleted = True
        self.channel.messages.pop(self.id, None)


class FakeProvider:
    name = "mock"
    model_name = "mock"


class LanguageSetupTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        db_path = Path(self.temp_dir.name) / "test.db"
        self.database = Database(f"sqlite+aiosqlite:///{db_path.as_posix()}")
        await self.database.create_tables()

    async def asyncTearDown(self) -> None:
        await self.database.close()
        self.temp_dir.cleanup()

    def test_language_display_names(self) -> None:
        self.assertEqual(LanguageService.display_name("ru"), "Russian")
        self.assertEqual(LanguageService.display_name(" EN "), "English")
        self.assertEqual(LanguageService.display_name("sv"), "SV")

    def test_language_code_normalization(self) -> None:
        self.assertEqual(LanguageService.normalize(" RU "), "ru")
        self.assertEqual(normalize_language_code("EN"), "en")
        self.assertEqual(normalize_language_code("pt-BR"), "pt")
        self.assertEqual(normalize_language_code("zh_CN"), "zh")

    def test_supported_language_allowlist(self) -> None:
        self.assertTrue(is_supported_language("en"))
        self.assertTrue(is_supported_language("ar"))
        self.assertFalse(is_supported_language("eng"))
        self.assertFalse(is_supported_language("eg"))
        self.assertFalse(is_supported_language("ua"))

    def test_language_code_suggestions(self) -> None:
        self.assertEqual(suggest_language_code("eng"), "en")
        self.assertEqual(suggest_language_code("english"), "en")
        self.assertEqual(suggest_language_code("eg"), "ar")
        self.assertEqual(suggest_language_code("ua"), "uk")
        self.assertEqual(suggest_language_code("ukrainian"), "uk")
        self.assertEqual(suggest_language_code("farsi"), "fa")

    def test_setup_server_language_parser_defaults_and_deduplicates(self) -> None:
        self.assertEqual(parse_setup_language_list(None), DEFAULT_SETUP_LANGUAGES)
        self.assertEqual(parse_setup_language_list("  "), DEFAULT_SETUP_LANGUAGES)
        self.assertEqual(parse_setup_language_list("ru,en,RU, pt-BR "), ["ru", "en", "pt"])

    def test_setup_server_language_parser_rejects_unsupported_with_suggestion(self) -> None:
        with self.assertRaisesRegex(ValueError, "Unsupported language code: eng. Use en for English."):
            parse_setup_language_list("ru,eng")

    def test_setup_options_use_display_names(self) -> None:
        options = build_language_select_options(["ru", "EN", "sv"])
        labels = {option.value: option.label for option in options}
        self.assertEqual(labels["ru"], "Russian")
        self.assertEqual(labels["en"], "English")
        self.assertNotIn("sv", labels)

    def test_select_placeholder_and_embed_copy(self) -> None:
        select = LanguageSelect(build_language_select_options(["ru"]))
        self.assertEqual(select.placeholder, "Select your translation language")
        embed = build_language_setup_embed()
        self.assertEqual(embed.title, "🌐 Choose your translation language")
        self.assertIn("React with 🌐 to any message you want translated.", embed.description)

    async def test_setup_message_refuses_without_translation_channels(self) -> None:
        cog = AdminCommands(
            self.database,
            translation_provider=None,
            webhook_service=None,
            max_message_chars=1500,
            skip_messages_over_limit=True,
            legacy_mirror_mode_enabled=False,
            on_demand_channel_translation_enabled=True,
            reaction_translation_enabled=True,
            context_menu_translation_enabled=True,
            reaction_translate_emoji="🌐",
            default_monthly_char_limit=500000,
        )
        interaction = FakeInteraction(self.database)
        channel = FakeChannel()

        await cog.language_setup_message.callback(cog, interaction, channel)

        self.assertEqual(channel.sent, [])
        self.assertEqual(
            interaction.response.messages,
            [("No supported translation channels configured yet. Use /translation_channel_set first.", True)],
        )

    async def test_translation_channel_set_rejects_unsupported_language_with_suggestion(self) -> None:
        interaction = FakeInteraction(self.database)
        channel = FakeChannel()
        cog = self._cog()

        await cog.translation_channel_set.callback(cog, interaction, "eng", channel)

        self.assertEqual(interaction.response.messages[0][0], "Unsupported language code: eng. Use en for English.")
        async with self.database.session() as session:
            result = await session.execute(select(TranslationChannelSetting))
            self.assertEqual(result.scalars().all(), [])

    async def test_translation_channel_set_rejects_country_code_eg(self) -> None:
        interaction = FakeInteraction(self.database)
        channel = FakeChannel()
        cog = self._cog()

        await cog.translation_channel_set.callback(cog, interaction, "eg", channel)

        self.assertEqual(
            interaction.response.messages[0][0],
            "Unsupported language code: eg. Use ar for Arabic. EG is a country code, not a language code.",
        )

    async def test_language_role_set_rejects_ua_with_suggestion(self) -> None:
        interaction = FakeInteraction(self.database)
        role = FakeRole(444, "lang-uk")
        cog = self._cog()

        await cog.language_role_set.callback(cog, interaction, "ua", role)

        self.assertEqual(interaction.response.messages[0][0], "Unsupported language code: ua. Use uk for Ukrainian.")
        async with self.database.session() as session:
            result = await session.execute(select(LanguageRoleSetting))
            self.assertEqual(result.scalars().all(), [])

    async def test_set_language_rejects_unsupported_language(self) -> None:
        interaction = FakeInteraction(self.database)
        cog = self._cog()

        await cog.set_language.callback(cog, interaction, "xx")

        self.assertIn("Unsupported language code: xx. Supported languages:", interaction.response.messages[0][0])
        async with self.database.session() as session:
            result = await session.execute(select(UserLanguageSetting))
            self.assertEqual(result.scalars().all(), [])

    async def test_remove_commands_allow_legacy_unsupported_languages(self) -> None:
        async with self.database.session() as session:
            session.add(TranslationChannelSetting(guild_id=111, target_language="eng", channel_id=999))
            session.add(LanguageRoleSetting(guild_id=111, target_language="eg", role_id=444))
            await session.commit()
        interaction = FakeInteraction(self.database)
        cog = self._cog()

        await cog.translation_channel_remove.callback(cog, interaction, "eng")
        await cog.language_role_remove.callback(cog, interaction, "eg")

        self.assertEqual(interaction.response.messages[0], ("Removed legacy unsupported language mapping: eng.", True))
        self.assertEqual(interaction.response.messages[1], ("Removed legacy unsupported language mapping: eg.", True))

    async def test_language_lists_mark_unsupported_legacy_mappings(self) -> None:
        async with self.database.session() as session:
            session.add(TranslationChannelSetting(guild_id=111, target_language="eng", channel_id=999))
            session.add(LanguageRoleSetting(guild_id=111, target_language="eg", role_id=444))
            await session.commit()
        interaction = FakeInteraction(self.database)
        cog = self._cog()

        await cog.translation_channel_list.callback(cog, interaction)
        await cog.language_role_list.callback(cog, interaction)

        self.assertIn("ENG (unsupported) -> <#999>", interaction.response.messages[0][0])
        self.assertIn("Remove unsupported mappings with /translation_channel_remove.", interaction.response.messages[0][0])
        self.assertIn("EG (unsupported) -> <@&444>", interaction.response.messages[1][0])
        self.assertIn("Remove unsupported mappings with /language_role_remove.", interaction.response.messages[1][0])

    async def test_setup_message_skips_unsupported_configured_mappings(self) -> None:
        await self._add_translation_channel("ru")
        await self._add_translation_channel("eng")
        interaction = FakeInteraction(self.database)
        channel = FakeChannel()
        cog = self._cog()

        await cog.language_setup_message.callback(cog, interaction, channel)

        select = channel.sent[0]["view"].children[0]
        option_values = [option.value for option in select.options]
        option_labels = [option.label for option in select.options]
        self.assertEqual(option_values, ["ru"])
        self.assertEqual(option_labels, ["Russian"])

    async def test_language_setup_message_creates_tracking_row(self) -> None:
        await self._add_translation_channel("ru")
        channel = FakeChannel()
        interaction = FakeInteraction(self.database, guild=FakeGuild(111, channels=[channel]))
        cog = self._cog()

        await cog.language_setup_message.callback(cog, interaction, channel)

        self.assertEqual(interaction.response.messages[0], (f"Language setup message posted in {channel.mention}.", True))
        async with self.database.session() as session:
            setting = await session.get(LanguageSetupMessage, 1)

        self.assertIsNotNone(setting)
        self.assertEqual(setting.channel_id, channel.id)
        self.assertEqual(setting.message_id, channel.sent[0]["message"].id)

    async def test_language_setup_message_updates_existing_message_without_duplicate(self) -> None:
        await self._add_translation_channel("ru")
        channel = FakeChannel()
        interaction = FakeInteraction(self.database, guild=FakeGuild(111, channels=[channel]))
        cog = self._cog()

        await cog.language_setup_message.callback(cog, interaction, channel)
        first_message = channel.sent[0]["message"]
        await cog.language_setup_message.callback(cog, interaction, channel)

        self.assertEqual(len(channel.sent), 1)
        self.assertEqual(first_message.edit_count, 1)
        self.assertEqual(interaction.response.messages[-1], (f"Language setup message updated in {channel.mention}.", True))

    async def test_language_setup_message_recreates_missing_message(self) -> None:
        await self._add_translation_channel("ru")
        channel = FakeChannel()
        interaction = FakeInteraction(self.database, guild=FakeGuild(111, channels=[channel]))
        cog = self._cog()

        await cog.language_setup_message.callback(cog, interaction, channel)
        first_message_id = channel.sent[0]["message"].id
        channel.messages.clear()
        await cog.language_setup_message.callback(cog, interaction, channel)

        self.assertEqual(len(channel.sent), 2)
        self.assertEqual(interaction.response.messages[-1], (f"Language setup message recreated in {channel.mention}.", True))
        async with self.database.session() as session:
            setting = await session.get(LanguageSetupMessage, 1)

        self.assertNotEqual(setting.message_id, first_message_id)

    async def test_language_setup_message_recreates_when_channel_changes(self) -> None:
        await self._add_translation_channel("ru")
        interaction = FakeInteraction(self.database)
        old_channel = FakeChannel(333, name="choose-language")
        new_channel = FakeChannel(444, name="choose-language-2")
        guild = FakeGuild(111, channels=[old_channel, new_channel])
        interaction.guild = guild
        cog = self._cog()

        await cog.language_setup_message.callback(cog, interaction, old_channel)
        old_message = old_channel.sent[0]["message"]
        await cog.language_setup_message.callback(cog, interaction, new_channel)

        self.assertTrue(old_message.deleted)
        self.assertEqual(len(new_channel.sent), 1)
        self.assertEqual(interaction.response.messages[-1], (f"Language setup message recreated in {new_channel.mention}.", True))
        async with self.database.session() as session:
            setting = await session.get(LanguageSetupMessage, 1)

        self.assertEqual(setting.channel_id, new_channel.id)

    async def test_setup_server_creates_expected_items_and_mappings(self) -> None:
        guild = FakeGuild(111)
        interaction = FakeInteraction(self.database, guild=guild)
        cog = self._cog(FakeProvider())

        await cog.setup_server.callback(cog, interaction, "ru,en")

        summary = interaction.followup.messages[0][0]
        self.assertIn("Setup completed.", summary)
        self.assertNotIn("Delete old setup messages manually", summary)
        self.assertIn("#choose-language", summary)
        self.assertIn("#global-chat", summary)
        self.assertIn("Setup message: created", summary)
        self.assertIn("@lang-ru", summary)
        self.assertIn("@lang-en", summary)
        self.assertIn("translation channels: 2", summary)
        self.assertIn("language roles: 2", summary)
        self.assertEqual([category.name for category in guild.categories], ["🌐 Translations"])
        self.assertEqual(
            [channel.name for channel in guild.text_channels],
            ["choose-language", "global-chat", "ru-translation", "en-translation"],
        )
        self.assertIn("lang-ru", [role.name for role in guild.roles])
        self.assertIn("lang-en", [role.name for role in guild.roles])
        setup_channel = next(channel for channel in guild.text_channels if channel.name == "choose-language")
        self.assertEqual(len(setup_channel.sent), 1)

        async with self.database.session() as session:
            channel_rows = (await session.execute(select(TranslationChannelSetting))).scalars().all()
            role_rows = (await session.execute(select(LanguageRoleSetting))).scalars().all()
            setup_message = await session.get(LanguageSetupMessage, 1)

        self.assertEqual({row.target_language for row in channel_rows}, {"ru", "en"})
        self.assertEqual({row.target_language for row in role_rows}, {"ru", "en"})
        self.assertEqual(setup_message.channel_id, setup_channel.id)

    async def test_setup_server_is_idempotent(self) -> None:
        guild = FakeGuild(111)
        interaction = FakeInteraction(self.database, guild=guild)
        cog = self._cog(FakeProvider())

        await cog.setup_server.callback(cog, interaction, "ru,en")
        await cog.setup_server.callback(cog, interaction, "ru,en")

        setup_channel = next(channel for channel in guild.text_channels if channel.name == "choose-language")
        self.assertEqual(len(guild.categories), 1)
        self.assertEqual(len([channel for channel in guild.text_channels if channel.name == "ru-translation"]), 1)
        self.assertEqual(len([role for role in guild.roles if role.name == "lang-ru"]), 1)
        self.assertEqual(len(setup_channel.sent), 1)
        self.assertEqual(setup_channel.sent[0]["message"].edit_count, 1)
        async with self.database.session() as session:
            channel_rows = (await session.execute(select(TranslationChannelSetting))).scalars().all()
            role_rows = (await session.execute(select(LanguageRoleSetting))).scalars().all()

        self.assertEqual(len(channel_rows), 2)
        self.assertEqual(len(role_rows), 2)

    async def test_setup_server_uses_existing_source_channel_when_provided(self) -> None:
        source_channel = FakeChannel(777, name="general")
        guild = FakeGuild(111, channels=[source_channel])
        interaction = FakeInteraction(self.database, guild=guild)
        cog = self._cog(FakeProvider())

        await cog.setup_server.callback(cog, interaction, "ru", source_channel)

        summary = interaction.followup.messages[0][0]
        self.assertIn("Source channel: using existing #general", summary)
        self.assertNotIn("#global-chat", [channel.name for channel in guild.text_channels])
        self.assertEqual(source_channel.category, None)
        self.assertEqual(source_channel.overwrites, {})

    async def test_setup_server_warns_for_existing_source_channel_missing_permissions(self) -> None:
        source_channel = FakeChannel(
            777,
            name="general",
            permissions=FakePermissions(view_channel=True, read_message_history=False, add_reactions=False),
        )
        guild = FakeGuild(111, channels=[source_channel])
        interaction = FakeInteraction(self.database, guild=guild)
        cog = self._cog(FakeProvider())

        await cog.setup_server.callback(cog, interaction, "ru", source_channel)

        summary = interaction.followup.messages[0][0]
        self.assertIn("Bot is missing Read Message History in <#777>.", summary)
        self.assertIn("Bot is missing Add Reactions in <#777>.", summary)

    async def test_setup_server_warns_for_role_hierarchy(self) -> None:
        bot_member = FakeMember(999, top_role=FakeRole(9999, "bot", position=0))
        guild = FakeGuild(111, me=bot_member)
        interaction = FakeInteraction(self.database, guild=guild)
        cog = self._cog(FakeProvider())

        await cog.setup_server.callback(cog, interaction, "ru")

        self.assertIn("Move the bot role above all lang-* roles", interaction.followup.messages[0][0])

    async def test_setup_server_rejects_unsupported_language_suggestion(self) -> None:
        interaction = FakeInteraction(self.database)
        cog = self._cog(FakeProvider())

        await cog.setup_server.callback(cog, interaction, "eng")

        self.assertEqual(interaction.response.messages[0][0], "Unsupported language code: eng. Use en for English.")

    async def test_setup_server_requires_manage_channels(self) -> None:
        bot_member = FakeMember(999, guild_permissions=FakePermissions(manage_channels=False))
        guild = FakeGuild(111, me=bot_member)
        interaction = FakeInteraction(self.database, guild=guild)
        cog = self._cog(FakeProvider())

        await cog.setup_server.callback(cog, interaction, "ru")

        self.assertEqual(
            interaction.followup.messages[0][0],
            "I need Manage Channels to create channels and permissions.",
        )
        self.assertEqual(guild.text_channels, [])

    async def test_setup_check_detects_unsupported_mappings(self) -> None:
        await self._add_translation_channel("eng")
        await self._add_language_role("eg", 444)
        role = FakeRole(444, "lang-ar")
        channel = FakeChannel(999)
        interaction = FakeInteraction(
            self.database,
            guild=FakeGuild(111, roles=[role], channels=[channel]),
        )
        cog = self._cog(FakeProvider())

        await cog.setup_check.callback(cog, interaction)

        report = interaction.response.messages[0][0]
        self.assertIn("❌ Not ready", report)
        self.assertIn("ENG is unsupported. Remove it with /translation_channel_remove.", report)
        self.assertIn("EG is unsupported. Remove it with /language_role_remove.", report)

    async def test_setup_check_detects_missing_role_mapping_for_channel(self) -> None:
        await self._add_translation_channel("ru")
        channel = FakeChannel(999)
        interaction = FakeInteraction(self.database, guild=FakeGuild(111, channels=[channel]))
        cog = self._cog(FakeProvider())

        await cog.setup_check.callback(cog, interaction)

        report = interaction.response.messages[0][0]
        self.assertIn("⚠️ Needs attention", report)
        self.assertIn("Russian (ru) has a translation channel but no language role mapping.", report)

    async def test_setup_check_detects_missing_channel_mapping_for_role(self) -> None:
        await self._add_language_role("ar", 444)
        role = FakeRole(444, "lang-ar")
        interaction = FakeInteraction(self.database, guild=FakeGuild(111, roles=[role]))
        cog = self._cog(FakeProvider())

        await cog.setup_check.callback(cog, interaction)

        report = interaction.response.messages[0][0]
        self.assertIn("❌ Not ready", report)
        self.assertIn("Arabic (ar) has a language role but no translation channel.", report)

    async def test_setup_check_reports_setup_message_not_tracked(self) -> None:
        await self._add_translation_channel("ru")
        await self._add_language_role("ru", 444)
        role = FakeRole(444, "lang-ru", position=1)
        channel = FakeChannel(999)
        bot_member = FakeMember(999, top_role=FakeRole(9999, "bot", position=100))
        interaction = FakeInteraction(
            self.database,
            guild=FakeGuild(111, roles=[role], channels=[channel], me=bot_member),
        )
        cog = self._cog(FakeProvider())

        await cog.setup_check.callback(cog, interaction)

        report = interaction.response.messages[0][0]
        self.assertIn("⚠️ Needs attention", report)
        self.assertIn("Tracked: ❌", report)
        self.assertIn("Setup message is not tracked. Run /language_setup_message channel:#choose-language.", report)
        self.assertIn("Source channels are not restricted. Bot can translate from any visible channel.", report)

    async def test_setup_check_reports_tracked_missing_setup_message(self) -> None:
        await self._add_translation_channel("ru")
        await self._add_language_role("ru", 444)
        role = FakeRole(444, "lang-ru", position=1)
        channel = FakeChannel(999)
        async with self.database.session() as session:
            session.add(LanguageSetupMessage(guild_id=111, channel_id=channel.id, message_id=12345))
            await session.commit()
        interaction = FakeInteraction(
            self.database,
            guild=FakeGuild(111, roles=[role], channels=[channel]),
        )
        cog = self._cog(FakeProvider())

        await cog.setup_check.callback(cog, interaction)

        report = interaction.response.messages[0][0]
        self.assertIn("Tracked: ✅", report)
        self.assertIn("Message exists: ❌", report)
        self.assertIn("Tracked setup message is missing. Run /language_setup_message channel:#choose-language to recreate it.", report)

    async def test_setup_check_reports_tracked_existing_setup_message(self) -> None:
        await self._add_translation_channel("ru")
        await self._add_language_role("ru", 444)
        role = FakeRole(444, "lang-ru", position=1)
        channel = FakeChannel(999)
        message = await channel.send(embed=None, view=None)
        async with self.database.session() as session:
            session.add(LanguageSetupMessage(guild_id=111, channel_id=channel.id, message_id=message.id))
            await session.commit()
        interaction = FakeInteraction(
            self.database,
            guild=FakeGuild(111, roles=[role], channels=[channel]),
        )
        cog = self._cog(FakeProvider())

        await cog.setup_check.callback(cog, interaction)

        report = interaction.response.messages[0][0]
        self.assertIn("✅ Ready", report)
        self.assertIn("Tracked: ✅", report)
        self.assertIn("Message exists: ✅", report)

    async def test_setup_cleanup_removes_unsupported_mappings(self) -> None:
        async with self.database.session() as session:
            session.add(TranslationChannelSetting(guild_id=111, target_language="eng", channel_id=999))
            session.add(LanguageRoleSetting(guild_id=111, target_language="eg", role_id=444))
            await session.commit()
        interaction = FakeInteraction(self.database, guild=FakeGuild(111))
        cog = self._cog(FakeProvider())

        await cog.setup_cleanup.callback(cog, interaction)

        summary = interaction.response.messages[0][0]
        self.assertIn("Unsupported translation mappings removed: 1", summary)
        self.assertIn("Unsupported role mappings removed: 1", summary)
        async with self.database.session() as session:
            self.assertEqual((await session.execute(select(TranslationChannelSetting))).scalars().all(), [])
            self.assertEqual((await session.execute(select(LanguageRoleSetting))).scalars().all(), [])

    async def test_setup_cleanup_removes_orphan_channel_and_role_mappings(self) -> None:
        async with self.database.session() as session:
            session.add(TranslationChannelSetting(guild_id=111, target_language="ru", channel_id=999))
            session.add(LanguageRoleSetting(guild_id=111, target_language="ru", role_id=444))
            await session.commit()
        interaction = FakeInteraction(self.database, guild=FakeGuild(111))
        cog = self._cog(FakeProvider())

        await cog.setup_cleanup.callback(cog, interaction)

        summary = interaction.response.messages[0][0]
        self.assertIn("Orphan channel mappings removed: 1", summary)
        self.assertIn("Orphan role mappings removed: 1", summary)
        async with self.database.session() as session:
            self.assertEqual((await session.execute(select(TranslationChannelSetting))).scalars().all(), [])
            self.assertEqual((await session.execute(select(LanguageRoleSetting))).scalars().all(), [])

    async def test_setup_cleanup_keeps_valid_mappings(self) -> None:
        channel = FakeChannel(999)
        role = FakeRole(444, "lang-ru")
        async with self.database.session() as session:
            session.add(TranslationChannelSetting(guild_id=111, target_language="ru", channel_id=channel.id))
            session.add(LanguageRoleSetting(guild_id=111, target_language="ru", role_id=role.id))
            await session.commit()
        interaction = FakeInteraction(self.database, guild=FakeGuild(111, roles=[role], channels=[channel]))
        cog = self._cog(FakeProvider())

        await cog.setup_cleanup.callback(cog, interaction)

        summary = interaction.response.messages[0][0]
        self.assertIn("Unsupported translation mappings removed: 0", summary)
        self.assertIn("Orphan channel mappings removed: 0", summary)
        async with self.database.session() as session:
            self.assertEqual(len((await session.execute(select(TranslationChannelSetting))).scalars().all()), 1)
            self.assertEqual(len((await session.execute(select(LanguageRoleSetting))).scalars().all()), 1)

    async def test_setup_cleanup_clears_missing_setup_message_tracking(self) -> None:
        channel = FakeChannel(999)
        async with self.database.session() as session:
            session.add(LanguageSetupMessage(guild_id=111, channel_id=channel.id, message_id=12345))
            await session.commit()
        interaction = FakeInteraction(self.database, guild=FakeGuild(111, channels=[channel]))
        cog = self._cog(FakeProvider())

        await cog.setup_cleanup.callback(cog, interaction)

        summary = interaction.response.messages[0][0]
        self.assertIn("Setup tracking cleared: yes", summary)
        self.assertIn("Run /language_setup_message or /setup_server to recreate the setup message.", summary)
        async with self.database.session() as session:
            self.assertIsNone(await session.get(LanguageSetupMessage, 1))

    async def test_setup_cleanup_keeps_existing_setup_message_tracking(self) -> None:
        channel = FakeChannel(999)
        message = await channel.send(embed=None, view=None)
        async with self.database.session() as session:
            session.add(LanguageSetupMessage(guild_id=111, channel_id=channel.id, message_id=message.id))
            await session.commit()
        interaction = FakeInteraction(self.database, guild=FakeGuild(111, channels=[channel]))
        cog = self._cog(FakeProvider())

        await cog.setup_cleanup.callback(cog, interaction)

        summary = interaction.response.messages[0][0]
        self.assertIn("Setup tracking cleared: no", summary)
        self.assertIn("Old setup messages deleted: not attempted", summary)
        async with self.database.session() as session:
            self.assertIsNotNone(await session.get(LanguageSetupMessage, 1))

    async def test_setup_check_reports_critical_role_hierarchy(self) -> None:
        await self._add_translation_channel("ru")
        await self._add_language_role("ru", 444)
        role = FakeRole(444, "lang-ru", position=50)
        channel = FakeChannel(999)
        bot_member = FakeMember(999, top_role=FakeRole(9999, "bot", position=10))
        interaction = FakeInteraction(
            self.database,
            guild=FakeGuild(111, roles=[role], channels=[channel], me=bot_member),
        )
        cog = self._cog(FakeProvider())

        await cog.setup_check.callback(cog, interaction)

        report = interaction.response.messages[0][0]
        self.assertIn("❌ Not ready", report)
        self.assertIn("Bot role must be above @lang-ru in Server Settings -> Roles.", report)

    async def test_selecting_language_creates_user_setting(self) -> None:
        await self._add_translation_channel("ru")
        role = FakeRole(444, "lang-ru")
        await self._add_language_role("ru", role.id)
        member = FakeMember(222)
        interaction = FakeInteraction(self.database, guild=FakeGuild(111, [role]), user=member)
        select = LanguageSelect(build_language_select_options(["ru"]))
        select._values = ["ru"]

        await select.callback(interaction)

        async with self.database.session() as session:
            setting = await session.get(UserLanguageSetting, 1)

        self.assertIsNotNone(setting)
        self.assertEqual(setting.target_language, "ru")
        self.assertEqual(member.added_roles, [role])
        self.assertEqual(interaction.response.messages[0][1], True)
        self.assertIn("your translation language is now Russian", interaction.response.messages[0][0])

    async def test_selecting_language_updates_user_setting(self) -> None:
        await self._add_translation_channel("en")
        role = FakeRole(555, "lang-en")
        await self._add_language_role("en", role.id)
        async with self.database.session() as session:
            session.add(UserLanguageSetting(guild_id=111, user_id=222, target_language="ru"))
            await session.commit()

        member = FakeMember(222)
        interaction = FakeInteraction(self.database, guild=FakeGuild(111, [role]), user=member)
        select = LanguageSelect(build_language_select_options(["en"]))
        select._values = ["en"]

        await select.callback(interaction)

        async with self.database.session() as session:
            setting = await session.get(UserLanguageSetting, 1)

        self.assertEqual(setting.target_language, "en")
        self.assertEqual(member.added_roles, [role])
        self.assertIn("your translation language is now English", interaction.response.messages[0][0])

    async def test_selecting_language_warns_when_role_mapping_missing(self) -> None:
        await self._add_translation_channel("ru")
        interaction = FakeInteraction(self.database)
        select = LanguageSelect(build_language_select_options(["ru"]))
        select._values = ["ru"]

        await select.callback(interaction)

        async with self.database.session() as session:
            setting = await session.get(UserLanguageSetting, 1)

        self.assertIsNotNone(setting)
        self.assertEqual(setting.target_language, "ru")
        self.assertIn("ask an admin to configure language roles", interaction.response.messages[0][0])

    async def test_selecting_language_fails_when_mapping_removed(self) -> None:
        interaction = FakeInteraction(self.database)
        select = LanguageSelect(build_language_select_options(["ru"]))
        select._values = ["ru"]

        await select.callback(interaction)

        async with self.database.session() as session:
            setting = await session.get(UserLanguageSetting, 1)

        self.assertIsNone(setting)
        self.assertEqual(
            interaction.response.messages,
            [("This language is no longer available. Please ask an admin to update the setup message.", True)],
        )

    async def test_language_role_mapping_create_update_remove_normalizes_language(self) -> None:
        async with self.database.session() as session:
            service = LanguageRoleService(session)
            await service.set_language_role(111, " RU ", 444)
            await service.set_language_role(111, "ru", 555)
            settings = await service.list_language_roles(111)

        self.assertEqual(len(settings), 1)
        self.assertEqual(settings[0].target_language, "ru")
        self.assertEqual(settings[0].role_id, 555)

        async with self.database.session() as session:
            removed_count = await LanguageRoleService(session).remove_language_role(111, " RU ")
            settings = await LanguageRoleService(session).list_language_roles(111)

        self.assertEqual(removed_count, 1)
        self.assertEqual(settings, [])

    async def test_sync_removes_old_configured_roles_and_adds_selected_role(self) -> None:
        ru_role = FakeRole(444, "lang-ru")
        en_role = FakeRole(555, "lang-en")
        await self._add_language_role("ru", ru_role.id)
        await self._add_language_role("en", en_role.id)
        member = FakeMember(222, roles=[ru_role])

        async with self.database.session() as session:
            result = await LanguageRoleService(session).sync_member_language_role(
                FakeGuild(111, [ru_role, en_role]),
                member,
                " EN ",
            )

        self.assertEqual(result.status, LanguageRoleService.SYNCED)
        self.assertEqual(member.removed_roles, [ru_role])
        self.assertEqual(member.added_roles, [en_role])
        self.assertEqual([role.id for role in member.roles], [en_role.id])

    async def test_sync_missing_role_mapping_does_not_fail(self) -> None:
        ru_role = FakeRole(444, "lang-ru")
        await self._add_language_role("ru", ru_role.id)
        member = FakeMember(222, roles=[ru_role])

        async with self.database.session() as session:
            result = await LanguageRoleService(session).sync_member_language_role(
                FakeGuild(111, [ru_role]),
                member,
                "fr",
            )

        self.assertEqual(result.status, LanguageRoleService.MISSING_ROLE_MAPPING)
        self.assertEqual(member.roles, [ru_role])

    async def test_sync_deleted_discord_role_is_handled_safely(self) -> None:
        await self._add_language_role("ru", 444)
        member = FakeMember(222)

        async with self.database.session() as session:
            result = await LanguageRoleService(session).sync_member_language_role(
                FakeGuild(111, []),
                member,
                "ru",
            )

        self.assertEqual(result.status, LanguageRoleService.MISSING_DISCORD_ROLE)
        self.assertEqual(member.added_roles, [])

    async def test_set_language_calls_role_sync(self) -> None:
        role = FakeRole(444, "lang-ru")
        await self._add_language_role("ru", role.id)
        member = FakeMember(222)
        interaction = FakeInteraction(self.database, guild=FakeGuild(111, [role]), user=member)
        cog = self._cog()

        await cog.set_language.callback(cog, interaction, " RU ")

        async with self.database.session() as session:
            setting = await session.get(UserLanguageSetting, 1)

        self.assertEqual(setting.target_language, "ru")
        self.assertEqual(member.added_roles, [role])
        self.assertIn("your translation language is now Russian", interaction.response.messages[0][0])

    async def test_set_language_reports_role_sync_permission_failure_after_saving(self) -> None:
        role = FakeRole(444, "lang-ru")
        await self._add_language_role("ru", role.id)
        member = FakeMember(222, fail_permissions=True)
        interaction = FakeInteraction(self.database, guild=FakeGuild(111, [role]), user=member)
        cog = self._cog()

        await cog.set_language.callback(cog, interaction, "ru")

        async with self.database.session() as session:
            setting = await session.get(UserLanguageSetting, 1)

        self.assertEqual(setting.target_language, "ru")
        self.assertIn("could not update your Discord role", interaction.response.messages[0][0])

    async def test_language_role_admin_commands(self) -> None:
        role = FakeRole(444, "lang-ru")
        interaction = FakeInteraction(self.database)
        cog = self._cog()

        await cog.language_role_set.callback(cog, interaction, " RU ", role)
        await cog.language_role_list.callback(cog, interaction)
        await cog.language_role_remove.callback(cog, interaction, "ru")

        self.assertEqual(interaction.response.messages[0], ("Language role for Russian set to @lang-ru.", True))
        self.assertIn("Russian (ru) -> <@&444>", interaction.response.messages[1][0])
        self.assertEqual(interaction.response.messages[2], ("Language role for Russian removed.", True))

    async def _add_translation_channel(self, language: str) -> None:
        async with self.database.session() as session:
            session.add(TranslationChannelSetting(guild_id=111, target_language=language, channel_id=999))
            await session.commit()

    async def _add_language_role(self, language: str, role_id: int) -> None:
        async with self.database.session() as session:
            session.add(LanguageRoleSetting(guild_id=111, target_language=language, role_id=role_id))
            await session.commit()

    def _cog(self, translation_provider=None) -> AdminCommands:
        return AdminCommands(
            self.database,
            translation_provider=translation_provider,
            webhook_service=None,
            max_message_chars=1500,
            skip_messages_over_limit=True,
            legacy_mirror_mode_enabled=False,
            on_demand_channel_translation_enabled=True,
            reaction_translation_enabled=True,
            context_menu_translation_enabled=True,
            reaction_translate_emoji="🌐",
            default_monthly_char_limit=500000,
        )


if __name__ == "__main__":
    unittest.main()
