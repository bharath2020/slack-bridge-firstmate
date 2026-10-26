import unittest

from slack_bridge import text as T


class TextTest(unittest.TestCase):
    def test_escape_only_control_characters(self):
        self.assertEqual(T.escape("*a* <@U1> & _b_"), "*a* &lt;@U1&gt; &amp; _b_")
        self.assertEqual(T.unescape(T.escape("x < y & z > w")), "x < y & z > w")

    def test_normalize_command(self):
        self.assertEqual(T.normalize_command("  New\n Session!! "), "new session")

    def test_short_text_is_one_chunk(self):
        self.assertEqual(T.split("hello"), ["hello"])
        self.assertEqual(T.split("\n\n"), [])

    def test_split_respects_limit_and_keeps_content(self):
        words = " ".join(f"w{i}" for i in range(3000))
        chunks = T.split(words, limit=500)
        self.assertTrue(all(len(c) <= 500 for c in chunks))
        self.assertEqual(" ".join(chunks).split(), words.split())

    def test_unbroken_text_is_hard_split(self):
        chunks = T.split("x" * 1200, limit=500)
        self.assertTrue(all(len(c) <= 500 for c in chunks))
        self.assertEqual("".join(chunks), "x" * 1200)

    def test_code_fence_is_closed_and_reopened(self):
        body = "intro\n```\n" + "\n".join(f"line {i}" for i in range(200)) + "\n```\nend"
        chunks = T.split(body, limit=400)
        self.assertGreater(len(chunks), 1)
        for c in chunks:
            self.assertLessEqual(len(c), 400)
            self.assertEqual(c.count("```") % 2, 0, c)


if __name__ == "__main__":
    unittest.main()
