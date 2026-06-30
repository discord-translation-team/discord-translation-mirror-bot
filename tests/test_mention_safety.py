import unittest

from app.mention_safety import sanitize_mentions


class MentionSafetyTest(unittest.TestCase):
    def test_sanitizes_everyone(self) -> None:
        self.assertEqual(sanitize_mentions("@everyone test"), "@\u200beveryone test")

    def test_sanitizes_here(self) -> None:
        self.assertEqual(sanitize_mentions("@here test"), "@\u200bhere test")

    def test_sanitizes_user_mention(self) -> None:
        self.assertEqual(sanitize_mentions("<@123456789> test"), "<@\u200b123456789> test")

    def test_sanitizes_nickname_user_mention(self) -> None:
        self.assertEqual(sanitize_mentions("<@!123456789> test"), "<@\u200b123456789> test")

    def test_sanitizes_role_mention(self) -> None:
        self.assertEqual(sanitize_mentions("<@&123456789> test"), "<@&\u200b123456789> test")


if __name__ == "__main__":
    unittest.main()
