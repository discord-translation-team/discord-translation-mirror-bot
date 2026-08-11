from __future__ import annotations

from pathlib import Path
from io import BytesIO
import tempfile
import unittest

from PIL import Image

from app.database import Database
from app.services.welcome_banner_renderer import BANNER_SIZE, WelcomeBannerError, WelcomeBannerRenderer
from app.services.welcome_service import MAX_WELCOME_IMAGE_BYTES, WelcomeService


class FakeGuild:
    id = 123
    name = "Test Community"


class FakeAvatar:
    def with_size(self, size: int):
        return self

    async def read(self) -> bytes:
        output = BytesIO()
        Image.new("RGB", (256, 256), "blue").save(output, format="PNG")
        return output.getvalue()


class FakeMember:
    id = 456
    mention = "<@456>"
    bot = False
    guild = FakeGuild()
    display_name = "New Member"
    display_avatar = FakeAvatar()


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
                image_bytes=self._image_bytes((1200, 300), "green"),
                image_content_type="image/png",
                image_filename="welcome.png",
                button_enabled=True,
                button_label="Choose language",
                button_channel_id=20,
            )
            payload = await service.build_payload(setting, FakeMember())

        self.assertIn("<@456>", payload.content)
        self.assertIn("<#30>", payload.content)
        self.assertIn("@\u200beveryone", payload.content)
        self.assertIn("<@\u200b999>", payload.content)
        self.assertEqual(len(payload.view.children), 1)
        self.assertEqual(payload.view.children[0].url, "https://discord.com/channels/123/20")
        self.assertEqual(payload.embed.image.url, "attachment://welcome-banner.png")

        payload_without_broken_button = await service.build_payload(
            setting,
            FakeMember(),
            include_button=False,
        )
        self.assertIsNone(payload_without_broken_button.view)

    async def test_build_payload_without_button_or_user_placeholder(self) -> None:
        async with self.database.session() as session:
            service = WelcomeService(session)
            setting = await service.save_setting(
                guild_id=123,
                welcome_channel_id=10,
                message_template="Welcome!",
                image_bytes=self._image_bytes((1200, 300), "green"),
                image_content_type="image/webp",
                image_filename="welcome.webp",
                button_enabled=False,
                button_label=None,
                button_channel_id=None,
            )
            payload = await service.build_payload(setting, FakeMember())

        self.assertEqual(payload.content, "<@456>\nWelcome!")
        self.assertIsNone(payload.view)

    async def test_server_name_template_and_partial_updates(self) -> None:
        original_banner = self._image_bytes((800, 200), "green")
        replacement_banner = self._image_bytes((1200, 300), "purple")
        async with self.database.session() as session:
            service = WelcomeService(session)
            setting = await service.save_setting(
                guild_id=123,
                welcome_channel_id=10,
                message_template="Welcome to {server_name}, {user}!",
                image_bytes=original_banner,
                image_content_type="image/png",
                image_filename="welcome.png",
                button_enabled=True,
                button_label="Old label",
                button_channel_id=20,
            )
            setting = await service.update_content(
                123,
                message_template="Hello from {server_name}, {user}!",
                button_label="New label",
            )
            self.assertEqual(setting.welcome_channel_id, 10)
            self.assertEqual(setting.image_bytes, original_banner)
            self.assertTrue(setting.is_enabled)

            setting = await service.update_banner(
                123,
                image_bytes=replacement_banner,
                image_content_type="image/png",
                image_filename="replacement.png",
            )
            payload = await service.build_payload(setting, FakeMember())

        self.assertEqual(payload.content, "Hello from Test Community, <@456>!")
        self.assertEqual(setting.message_template, "Hello from {server_name}, {user}!")
        self.assertEqual(setting.button_label, "New label")
        self.assertEqual(setting.welcome_channel_id, 10)
        self.assertTrue(setting.is_enabled)

    def test_image_validation(self) -> None:
        self.assertIsNone(WelcomeService.validate_image("image/png", MAX_WELCOME_IMAGE_BYTES))
        self.assertIsNotNone(WelcomeService.validate_image("text/plain", 10))
        self.assertIsNotNone(WelcomeService.validate_image("image/png", MAX_WELCOME_IMAGE_BYTES + 1))

    @staticmethod
    def _image_bytes(size: tuple[int, int], color: str) -> bytes:
        output = BytesIO()
        Image.new("RGB", size, color).save(output, format="PNG")
        return output.getvalue()


class WelcomeBannerRendererTest(unittest.TestCase):
    def setUp(self) -> None:
        self.renderer = WelcomeBannerRenderer()

    def test_renders_wide_banner_with_avatar_name_and_server(self) -> None:
        banner = self._image_bytes((500, 900), "orange")
        avatar = self._image_bytes((128, 128), "blue")
        result = self.renderer.render(
            banner_bytes=banner,
            avatar_bytes=avatar,
            display_name="Александр Example",
            server_name="Большое тестовое сообщество",
        )

        rendered = Image.open(BytesIO(result))
        self.assertEqual(rendered.size, BANNER_SIZE)
        self.assertEqual(rendered.format, "PNG")

    def test_handles_long_names_and_missing_avatar(self) -> None:
        result = self.renderer.render(
            banner_bytes=self._image_bytes((1200, 300), "black"),
            avatar_bytes=None,
            display_name="Very long display name " * 20,
            server_name="Very long server name " * 20,
        )
        self.assertGreater(len(result), 0)

    def test_rejects_small_or_invalid_banner(self) -> None:
        with self.assertRaises(WelcomeBannerError):
            self.renderer.validate_banner(b"not-an-image")
        with self.assertRaises(WelcomeBannerError):
            self.renderer.validate_banner(self._image_bytes((100, 50), "red"))

    @staticmethod
    def _image_bytes(size: tuple[int, int], color: str) -> bytes:
        output = BytesIO()
        Image.new("RGB", size, color).save(output, format="PNG")
        return output.getvalue()
