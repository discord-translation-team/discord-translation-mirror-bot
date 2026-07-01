import tempfile
import unittest
from pathlib import Path

import discord
from sqlalchemy import func, select

from app.database import Database
from app.models import OnDemandTranslationMapping, TranslationChannelSetting, UserLanguageSetting
from app.services.on_demand_translation_service import OnDemandTranslationService
from app.translation.base import TranslationProvider, TranslationResult


class FakeProvider(TranslationProvider):
    name = "mock-test"
    model_name = "mock-test"

    def __init__(self) -> None:
        self.calls = 0

    async def translate(
        self,
        text: str,
        target_language: str,
        source_language: str | None = None,
    ) -> TranslationResult:
        self.calls += 1
        return TranslationResult(translated_text=f"[{target_language}] {text}")


class FakeAuthor:
    bot = False


class FakeChannel:
    id = 222

    def __init__(self) -> None:
        self.sent_messages: list[str] = []

    async def send(self, content: str, allowed_mentions: discord.AllowedMentions):
        self.sent_messages.append(content)
        return type("SentMessage", (), {"id": 9000 + len(self.sent_messages)})()


class FakeGuild:
    id = 111


class FakeMessage:
    id = 333
    guild = FakeGuild()
    channel = FakeChannel()
    author = FakeAuthor()
    webhook_id = None
    content = "Hello everyone"


class OnDemandDedupeTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        db_path = Path(self.temp_dir.name) / "test.db"
        self.database = Database(f"sqlite+aiosqlite:///{db_path.as_posix()}")
        await self.database.create_tables()

    async def asyncTearDown(self) -> None:
        await self.database.close()
        self.temp_dir.cleanup()

    async def test_duplicate_requests_create_one_mapping_and_skip_provider(self) -> None:
        provider = FakeProvider()
        fake_channel = FakeChannel()

        async with self.database.session() as session:
            session.add(UserLanguageSetting(guild_id=111, user_id=444, target_language="ru"))
            session.add(TranslationChannelSetting(guild_id=111, target_language="ru", channel_id=222))
            await session.commit()

            service = OnDemandTranslationService(session, provider, webhook_service=None)
            service._target_text_channel = _fake_target_channel(fake_channel)

            first = await service.publish_for_user(FakeMessage(), user_id=444)
            second = await service.publish_for_user(FakeMessage(), user_id=444)

            count = await session.scalar(select(func.count(OnDemandTranslationMapping.id)))

        self.assertEqual(first.status, "posted")
        self.assertEqual(second.status, "duplicate")
        self.assertEqual(count, 1)
        self.assertEqual(provider.calls, 1)
        self.assertEqual(len(fake_channel.sent_messages), 1)

    async def test_language_normalization_prevents_case_and_space_duplicates(self) -> None:
        provider = FakeProvider()
        fake_channel = FakeChannel()

        async with self.database.session() as session:
            session.add(UserLanguageSetting(guild_id=111, user_id=444, target_language=" RU "))
            session.add(UserLanguageSetting(guild_id=111, user_id=555, target_language="ru "))
            session.add(TranslationChannelSetting(guild_id=111, target_language=" ru ", channel_id=222))
            await session.commit()

            service = OnDemandTranslationService(session, provider, webhook_service=None)
            service._target_text_channel = _fake_target_channel(fake_channel)

            first = await service.publish_for_user(FakeMessage(), user_id=444)
            second = await service.publish_for_user(FakeMessage(), user_id=555)

            rows = (await session.execute(select(OnDemandTranslationMapping))).scalars().all()

        self.assertEqual(first.status, "posted")
        self.assertEqual(second.status, "duplicate")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].target_language, "ru")
        self.assertEqual(provider.calls, 1)
        self.assertEqual(len(fake_channel.sent_messages), 1)


def _fake_target_channel(channel: FakeChannel):
    async def fake_target_channel(guild, channel_id: int):
        return channel

    return fake_target_channel


if __name__ == "__main__":
    unittest.main()
