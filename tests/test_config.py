import unittest

from app.config import normalize_database_url


class ConfigTest(unittest.TestCase):
    def test_normalizes_railway_postgres_url(self) -> None:
        self.assertEqual(
            normalize_database_url("postgresql://user:pass@example.railway.internal:5432/db"),
            "postgresql+asyncpg://user:pass@example.railway.internal:5432/db",
        )

    def test_keeps_sqlite_url(self) -> None:
        self.assertEqual(
            normalize_database_url("sqlite+aiosqlite:///./bot.db"),
            "sqlite+aiosqlite:///./bot.db",
        )

    def test_keeps_already_asyncpg_url(self) -> None:
        self.assertEqual(
            normalize_database_url("postgresql+asyncpg://user:pass@host/db"),
            "postgresql+asyncpg://user:pass@host/db",
        )


if __name__ == "__main__":
    unittest.main()
