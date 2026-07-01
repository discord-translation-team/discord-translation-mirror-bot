import tempfile
import unittest
from pathlib import Path

from app.commands.admin import AdminCommands
from app.database import Database
from app.models import TranslationChannelSetting, UserLanguageSetting
from app.services.language_service import LanguageService
from app.ui.language_setup import LanguageSelect, build_language_select_options, build_language_setup_embed


class FakeResponse:
    def __init__(self) -> None:
        self.messages: list[tuple[str, bool]] = []

    async def send_message(self, content: str, ephemeral: bool = False, **kwargs) -> None:
        self.messages.append((content, ephemeral))


class FakeInteraction:
    def __init__(self, database: Database, guild_id: int = 111, user_id: int = 222) -> None:
        self.guild = type("Guild", (), {"id": guild_id})()
        self.user = type("User", (), {"id": user_id})()
        self.client = type("Client", (), {"database": database})()
        self.response = FakeResponse()


class FakeChannel:
    id = 333
    mention = "<#333>"

    def __init__(self) -> None:
        self.sent: list[dict[str, object]] = []

    async def send(self, content: str | None = None, embed=None, view=None, allowed_mentions=None):
        self.sent.append({"content": content, "embed": embed, "view": view})


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

    def test_setup_options_use_display_names(self) -> None:
        options = build_language_select_options(["ru", "EN", "sv"])
        labels = {option.value: option.label for option in options}
        self.assertEqual(labels["ru"], "Russian")
        self.assertEqual(labels["en"], "English")
        self.assertEqual(labels["sv"], "SV")

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
            [("No translation channels configured yet. Use /translation_channel_set first.", True)],
        )

    async def test_selecting_language_creates_user_setting(self) -> None:
        await self._add_translation_channel("ru")
        interaction = FakeInteraction(self.database)
        select = LanguageSelect(build_language_select_options(["ru"]))
        select._values = ["ru"]

        await select.callback(interaction)

        async with self.database.session() as session:
            setting = await session.get(UserLanguageSetting, 1)

        self.assertIsNotNone(setting)
        self.assertEqual(setting.target_language, "ru")
        self.assertEqual(
            interaction.response.messages,
            [("Done — your translation language is now Russian. React with 🌐 to translate messages.", True)],
        )

    async def test_selecting_language_updates_user_setting(self) -> None:
        await self._add_translation_channel("en")
        async with self.database.session() as session:
            session.add(UserLanguageSetting(guild_id=111, user_id=222, target_language="ru"))
            await session.commit()

        interaction = FakeInteraction(self.database)
        select = LanguageSelect(build_language_select_options(["en"]))
        select._values = ["en"]

        await select.callback(interaction)

        async with self.database.session() as session:
            setting = await session.get(UserLanguageSetting, 1)

        self.assertEqual(setting.target_language, "en")
        self.assertEqual(
            interaction.response.messages,
            [("Done — your translation language is now English. React with 🌐 to translate messages.", True)],
        )

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

    async def _add_translation_channel(self, language: str) -> None:
        async with self.database.session() as session:
            session.add(TranslationChannelSetting(guild_id=111, target_language=language, channel_id=999))
            await session.commit()


if __name__ == "__main__":
    unittest.main()
