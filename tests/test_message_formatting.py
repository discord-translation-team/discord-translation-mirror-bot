import unittest

from app.message_formatting import build_translated_message_body


class MessageFormattingTest(unittest.TestCase):
    def test_final_translated_message_format(self) -> None:
        self.assertEqual(
            build_translated_message_body(
                "Привет, ребята!",
                "https://discord.com/channels/1/2/3",
            ),
            "Привет, ребята!\n\n[Original](https://discord.com/channels/1/2/3)",
        )

    def test_final_format_cleans_and_sanitizes_text(self) -> None:
        self.assertEqual(
            build_translated_message_body(
                "<message>\n@everyone привет\n</message>",
                "https://discord.com/channels/1/2/3",
            ),
            "@\u200beveryone привет\n\n[Original](https://discord.com/channels/1/2/3)",
        )


if __name__ == "__main__":
    unittest.main()
