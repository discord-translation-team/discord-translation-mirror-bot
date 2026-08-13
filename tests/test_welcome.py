from __future__ import annotations

from pathlib import Path
from io import BytesIO
import tempfile
import unittest

from PIL import Image
from sqlalchemy import inspect, text

from app.database import Database
from app.services.welcome_banner_renderer import (
    BACKGROUND_SHADE_ALPHA,
    BANNER_SIZE,
    BORDER_WIDTH,
    CORNER_RADIUS,
    WelcomeBannerError,
    WelcomeBannerRenderer,
)
from app.services.welcome_service import DEFAULT_WELCOME_ACCENT_COLOR, MAX_WELCOME_IMAGE_BYTES, WelcomeService


class FakeChannel:
    def __init__(self, channel_id: int, name: str) -> None:
        self.id = channel_id
        self.name = name


class FakeGuild:
    id = 123
    name = "Test Community"
    channels = {
        20: FakeChannel(20, "choose-language"),
        30: FakeChannel(30, "rules"),
        40: FakeChannel(40, "help[desk]"),
    }

    def get_channel(self, channel_id: int):
        return self.channels.get(channel_id)


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

        self.assertIn("<@456>", payload.embed.description)
        self.assertIn("[#rules](https://discord.com/channels/123/30)", payload.embed.description)
        self.assertIn("@\u200beveryone", payload.embed.description)
        self.assertIn("<@\u200b999>", payload.embed.description)
        self.assertEqual(payload.embed.color.value, int(DEFAULT_WELCOME_ACCENT_COLOR, 16))
        self.assertEqual(len(payload.view.children), 1)
        self.assertEqual(payload.view.children[0].url, "https://discord.com/channels/123/20")
        self.assertEqual(payload.embed.image.url, "attachment://welcome-banner.png")

        payload_without_broken_button = await service.build_payload(
            setting,
            FakeMember(),
            include_button=False,
        )
        self.assertIsNone(payload_without_broken_button.view)

    async def test_normalizes_channel_url_and_preserves_invalid_references(self) -> None:
        async with self.database.session() as session:
            service = WelcomeService(session)
            setting = await service.save_setting(
                guild_id=123,
                welcome_channel_id=10,
                message_template=(
                    "Read https://discord.com/channels/123/40, "
                    "missing <#999>, external https://discord.com/channels/456/30"
                ),
                image_bytes=self._image_bytes((1200, 420), "green"),
                image_content_type="image/png",
                image_filename="welcome.png",
                button_enabled=False,
                button_label=None,
                button_channel_id=None,
            )
            payload = await service.build_payload(setting, FakeMember())

        self.assertIn(
            "[#help\\[desk\\]](https://discord.com/channels/123/40)",
            payload.embed.description,
        )
        self.assertIn("<#999>", payload.embed.description)
        self.assertIn("https://discord.com/channels/456/30", payload.embed.description)
        self.assertEqual(
            WelcomeService.invalid_channel_references(setting.message_template, FakeGuild()),
            ["https://discord.com/channels/456/30", "<#999>"],
        )

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

        self.assertEqual(payload.embed.description, "<@456>\nWelcome!")
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

        self.assertEqual(payload.embed.description, "Hello from Test Community, <@456>!")
        self.assertEqual(setting.message_template, "Hello from {server_name}, {user}!")
        self.assertEqual(setting.button_label, "New label")
        self.assertEqual(setting.welcome_channel_id, 10)
        self.assertTrue(setting.is_enabled)

    async def test_updates_accent_color_without_changing_other_fields(self) -> None:
        banner = self._image_bytes((1200, 420), "green")
        async with self.database.session() as session:
            service = WelcomeService(session)
            setting = await service.save_setting(
                guild_id=123,
                welcome_channel_id=10,
                message_template="Welcome, {user}!",
                image_bytes=banner,
                image_content_type="image/png",
                image_filename="welcome.png",
                button_enabled=True,
                button_label="Choose language",
                button_channel_id=20,
            )
            setting = await service.update_accent_color(123, "#A020F0")
            payload = await service.build_payload(setting, FakeMember())

        self.assertEqual(setting.accent_color, "A020F0")
        self.assertEqual(setting.welcome_channel_id, 10)
        self.assertEqual(setting.image_bytes, banner)
        self.assertEqual(setting.message_template, "Welcome, {user}!")
        self.assertEqual(payload.embed.color.value, 0xA020F0)

    def test_accent_color_validation(self) -> None:
        self.assertEqual(WelcomeService.normalize_accent_color(" #a020f0 "), "A020F0")
        self.assertIsNone(WelcomeService.normalize_accent_color("purple"))
        self.assertIsNone(WelcomeService.normalize_accent_color("#12345"))

    async def test_migrates_existing_welcome_table_with_default_accent(self) -> None:
        legacy_path = Path(self.temp_dir.name) / "legacy.db"
        legacy_database = Database(f"sqlite+aiosqlite:///{legacy_path.as_posix()}")
        async with legacy_database.engine.begin() as conn:
            await conn.execute(text("""
                CREATE TABLE welcome_settings (
                    id INTEGER PRIMARY KEY,
                    guild_id BIGINT NOT NULL,
                    is_enabled BOOLEAN NOT NULL,
                    welcome_channel_id BIGINT NOT NULL,
                    message_template TEXT NOT NULL,
                    image_bytes BLOB NOT NULL,
                    image_content_type VARCHAR(64) NOT NULL,
                    image_filename VARCHAR(128) NOT NULL,
                    button_enabled BOOLEAN NOT NULL,
                    button_label VARCHAR(80),
                    button_channel_id BIGINT,
                    created_at DATETIME NOT NULL,
                    updated_at DATETIME NOT NULL
                )
            """))
        await legacy_database.create_tables()
        async with legacy_database.engine.connect() as conn:
            columns = await conn.run_sync(
                lambda connection: {column["name"] for column in inspect(connection).get_columns("welcome_settings")}
            )
        self.assertIn("accent_color", columns)
        await legacy_database.close()

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
        self.assertEqual(rendered.mode, "RGBA")
        self.assertEqual(rendered.getpixel((0, 0))[3], 0)
        self.assertEqual(rendered.getpixel((BANNER_SIZE[0] // 2, 0))[3], 255)

    def test_darkens_the_entire_visible_background(self) -> None:
        result = self.renderer.render(
            banner_bytes=self._image_bytes(BANNER_SIZE, "white"),
            avatar_bytes=None,
            display_name="Member",
            server_name="Server",
        )

        rendered = Image.open(BytesIO(result)).convert("RGBA")
        expected_channel = round(255 * (255 - BACKGROUND_SHADE_ALPHA) / 255)
        background_pixel = rendered.getpixel((BANNER_SIZE[0] - CORNER_RADIUS - 20, CORNER_RADIUS + 20))
        self.assertEqual(background_pixel[3], 255)
        self.assertAlmostEqual(background_pixel[0], expected_channel, delta=1)
        self.assertAlmostEqual(background_pixel[1], expected_channel, delta=1)
        self.assertAlmostEqual(background_pixel[2], expected_channel, delta=1)

    def test_uses_accent_border_and_no_white_avatar_outline(self) -> None:
        accent = (160, 32, 240)
        result = self.renderer.render(
            banner_bytes=self._image_bytes(BANNER_SIZE, "black"),
            avatar_bytes=self._image_bytes((256, 256), "blue"),
            display_name="Member",
            server_name="Server",
            accent_color=accent,
        )

        rendered = Image.open(BytesIO(result)).convert("RGBA")
        self.assertEqual(rendered.getpixel((BANNER_SIZE[0] // 2, BORDER_WIDTH // 2))[:3], accent)
        avatar_edge = rendered.getpixel((60, 95 + 230 // 2))
        self.assertNotEqual(avatar_edge[:3], (255, 255, 255))

    def test_content_is_clipped_inside_the_colored_frame(self) -> None:
        accent = (160, 32, 240)
        result = self.renderer.render(
            banner_bytes=self._image_bytes(BANNER_SIZE, "white"),
            avatar_bytes=None,
            display_name="Member",
            server_name="Server",
            accent_color=accent,
        )

        rendered = Image.open(BytesIO(result)).convert("RGBA")
        self.assertEqual(rendered.getpixel((0, 0))[3], 0)
        self.assertEqual(rendered.getpixel((BANNER_SIZE[0] // 2, 1))[:3], accent)
        self.assertEqual(rendered.getpixel((BANNER_SIZE[0] // 2, BORDER_WIDTH - 1))[:3], accent)
        self.assertNotEqual(rendered.getpixel((BANNER_SIZE[0] // 2, BORDER_WIDTH + 1))[:3], accent)

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
