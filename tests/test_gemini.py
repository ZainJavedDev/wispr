import unittest

from wispr.gemini import merge_transcript


class TranscriptMergeTests(unittest.TestCase):
    def test_merges_distinct_final_segments(self) -> None:
        self.assertEqual(merge_transcript("hello", "world"), "hello world")

    def test_replaces_with_cumulative_transcript(self) -> None:
        self.assertEqual(merge_transcript("hello", "hello world"), "hello world")

    def test_ignores_duplicate_segment(self) -> None:
        self.assertEqual(merge_transcript("hello world", "world"), "hello world")


if __name__ == "__main__":
    unittest.main()

