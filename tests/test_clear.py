from __future__ import annotations

import unittest

from octocop.duration import (
    MAX_CLEAR_MESSAGES,
    MAX_CLEAR_SECONDS,
    DurationError,
    parse_clear_amount,
)


class ParseClearAmountTests(unittest.TestCase):
    def test_bare_numbers_are_message_counts(self) -> None:
        self.assertEqual(parse_clear_amount("10"), ("count", 10))
        self.assertEqual(parse_clear_amount(" 1 "), ("count", 1))
        self.assertEqual(parse_clear_amount(str(MAX_CLEAR_MESSAGES)), ("count", MAX_CLEAR_MESSAGES))

    def test_durations_are_seconds(self) -> None:
        self.assertEqual(parse_clear_amount("30s"), ("seconds", 30))
        self.assertEqual(parse_clear_amount("5m"), ("seconds", 300))
        self.assertEqual(parse_clear_amount("1h"), ("seconds", MAX_CLEAR_SECONDS))
        self.assertEqual(parse_clear_amount("59m30s"), ("seconds", 3570))

    def test_time_is_capped_at_one_hour(self) -> None:
        for value in ("61m", "2h", "3601s"):
            with self.assertRaises(DurationError, msg=value):
                parse_clear_amount(value)

    def test_days_and_weeks_are_refused_with_a_clear_message(self) -> None:
        for value in ("1d", "2w"):
            with self.assertRaises(DurationError) as caught:
                parse_clear_amount(value)
            self.assertIn("1 hour", str(caught.exception))

    def test_counts_are_bounded_and_junk_is_refused(self) -> None:
        for value in ("0", str(MAX_CLEAR_MESSAGES + 1), "", "   ", "abc", "5x", "-3"):
            with self.assertRaises(DurationError, msg=value):
                parse_clear_amount(value)


if __name__ == "__main__":
    unittest.main()
