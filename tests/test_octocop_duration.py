import unittest

from octocop.duration import DurationError, format_duration, parse_duration


class DurationTests(unittest.TestCase):
    def test_single_units(self):
        self.assertEqual(parse_duration("30s"), 30)
        self.assertEqual(parse_duration("15m"), 900)
        self.assertEqual(parse_duration("1h"), 3600)
        self.assertEqual(parse_duration("2d"), 172800)
        self.assertEqual(parse_duration("1w"), 604800)

    def test_combined(self):
        self.assertEqual(parse_duration("1h30m"), 5400)
        self.assertEqual(parse_duration("1w3d2h"), 871200)

    def test_case_and_spaces(self):
        self.assertEqual(parse_duration("1H 30M"), 5400)

    def test_rejects_invalid(self):
        for value in ("", "1x", "h", "1hour", "0m", "1h-nope"):
            with self.subTest(value=value):
                with self.assertRaises(DurationError):
                    parse_duration(value)

    def test_rejects_over_discord_limit(self):
        with self.assertRaises(DurationError):
            parse_duration("4w1s")

    def test_formats(self):
        self.assertEqual(format_duration(0), "0s")
        self.assertEqual(format_duration(5400), "1h 30m")
        self.assertEqual(format_duration(604861), "1w 1m 1s")


if __name__ == "__main__":
    unittest.main()
