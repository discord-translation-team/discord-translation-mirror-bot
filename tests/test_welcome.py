from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from app.database import Database
from app.services.welcome_service import MAX_WELCOME_IMAGE_BYTES, WelcomeService


class FakeGuild:
    id = 123


class FakeMember:
    id = 456
    mention = "<@456>"
    bot = False
    guild = FakeGuild()


class WelcomeServiceTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        db_path = Path(self.temp_dir.name) / "test.db"
        self.database = Database(f"sqlite+aiosqlite:///{db_path.as_posix()}")
        await self.database.create_tables()

    async def asyncTearDown(self) -> None:
        await self.database.close()
        self.temp_dir.cleanup()

    async def test_saves_one_setting_per_guild_and_updates_it(self) -> None:
        async with self.database.session() as session:
            service = WelcomeService(session)
            first = await service.save_setting(
                guild_id=123,
                welcome_channel_id=10,
                message_template="Hello, {user}",
                image_bytes=b"first",
                image_content_type="image/png",
                image_filename="first.png",
                button_enabled=True,
                button_label="Choose language",
                button_channel_id=20,
            )
            first_id = first.id
            second = await service.save_setting(
                guild_id=123,
                welcome_channel_id=11,
                message_template="Welcome, {user}",
                image_bytes=b"second",
                image_content_type="image/jpeg",
                image_filename="second.jpg",
                button_enabled=False,
                button_label=None,
                button_channel_id=None,
            )

            self.assertEqual(second.id, first_id)
            self.assertEqual(second.welcome_channel_id, 11)
            self.assertFalse(second.button_enabled)
            self.assertIsNone(second.button_channel_id)

    async def test_build_payload_preserves_channel_link_and_only_inserts_joining_user(self) -> None:
        async with self.database.session() as session:
            service = WelcomeService(session)
            setting = await service.save_setting(
                guild_id=123,
                welcome_channel_id=10,
                message_template="Hello {user}! Read <#30>. @everyone <@999>",
                image_bytes=b"image",
                image_content_type="image/png",
                image_filename="welcome.png",
                button_enabled=True,
                button_label="Choose language",
                button_channel_id=20,
            )
            payload = service.build_payload(setting, FakeMember())

        self.assertIn("<@456>", payload.content)
        self.assertIn("<#30>", payload.content)
        self.assertIn("@\u200beveryone", payload.content)
        self.assertIn("<@\u200b999>", payload.content)
        self.assertEqual(len(payload.view.children), 1)
        self.assertEqual(payload.view.children[0].url, "https://discord.com/channels/123/20")
        self.assertEqual(payload.embed.image.url, "attachment://welcome.png")

        payload_without_broken_button = service.build_payload(setting, FakeMember(), include_button=False)
        self.assertIsNone(payload_without_broken_button.view)

    async def test_build_payload_without_button_or_user_placeholder(self) -> None:
        async with self.database.session() as session:
            service = WelcomeService(session)
            setting = await service.save_setting(
                guild_id=123,
                welcome_channel_id=10,
                message_template="Welcome!",
                image_bytes=b"image",
                image_content_type="image/webp",
                image_filename="welcome.webp",
                button_enabled=False,
                button_label=None,
                button_channel_id=None,
            )
            payload = service.build_payload(setting, FakeMember())

        self.assertEqual(payload.content, "<@456>\nWelcome!")
        self.assertIsNone(payload.view)

    def test_image_validation(self) -> None:
        self.assertIsNone(WelcomeService.validate_image("image/png", MAX_WELCOME_IMAGE_BYTES))
        self.assertIsNotNone(WelcomeService.validate_image("text/plain", 10))
        self.assertIsNotNone(WelcomeService.validate_image("image/png", MAX_WELCOME_IMAGE_BYTES + 1))
