import unittest

from app.translation.output_cleaner import clean_translation_output


class OutputCleanerTest(unittest.TestCase):
    def test_removes_whole_message_wrapper(self) -> None:
        self.assertEqual(
            clean_translation_output("<message>\nПривет, ребята!\n</message>"),
            "Привет, ребята!",
        )

    def test_preserves_inline_message_tags(self) -> None:
        self.assertEqual(
            clean_translation_output("Переведи <message> как тег, пожалуйста."),
            "Переведи <message> как тег, пожалуйста.",
        )

    def test_removes_surrounding_fence_with_language(self) -> None:
        self.assertEqual(
            clean_translation_output("```text\nПривет, ребята!\n```"),
            "Привет, ребята!",
        )

    def test_removes_surrounding_quotes(self) -> None:
        self.assertEqual(clean_translation_output('"Привет, ребята!"'), "Привет, ребята!")

    def test_preserves_markdown_line_breaks_and_emoji(self) -> None:
        text = "**Привет**, ребята!\n😄 Не цензурировать."
        self.assertEqual(clean_translation_output(text), text)


if __name__ == "__main__":
    unittest.main()
