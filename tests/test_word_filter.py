from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from octocop.cogs.words import WordFilterCog
from octocop.database import Database
from octocop.wordfilter import WordFilter, normalize_word


class WordFilterMatchingTests(unittest.TestCase):
    def test_whole_word_case_insensitive(self) -> None:
        f = WordFilter(["Ban", "bad phrase"])
        self.assertEqual(f.find("you are BAN!"), "ban")
        self.assertEqual(f.find("ban."), "ban")
        self.assertIsNone(f.find("banana"))
        self.assertIsNone(f.find("urban legend"))
        self.assertEqual(f.find("what a Bad   Phrase that is"), "bad phrase")
        self.assertIsNone(f.find(""))
        self.assertIsNone(WordFilter().find("anything"))

    def test_add_remove_and_listing(self) -> None:
        f = WordFilter()
        self.assertTrue(f.add("  Word "))
        self.assertFalse(f.add("word"))
        self.assertFalse(f.add("   "))
        self.assertEqual(f.words, ["word"])
        self.assertEqual(f.find("WORD"), "word")
        self.assertTrue(f.remove("WORD"))
        self.assertFalse(f.remove("word"))
        self.assertIsNone(f.find("word"))

    def test_hyphen_space_and_joined_forms_all_match(self) -> None:
        f = WordFilter(["Porch-monkey", "sand nigger"])
        self.assertEqual(f.words, ["porch monkey", "sand nigger"])
        for text in ("porch monkey", "PORCH-MONKEY", "porchmonkey", "porch_monkey", "porch.monkey"):
            self.assertEqual(f.find(f"you {text}!"), "porch monkey", text)
        self.assertEqual(f.find("sandnigger"), "sand nigger")
        self.assertIsNone(f.find("porch monkeys"))
        self.assertTrue(f.remove("porchmonkey".replace("porchmonkey", "porch-monkey")))

    def test_default_list_loads_sorted_and_deduplicated(self) -> None:
        from octocop.default_banned_words import DEFAULT_BANNED_WORDS
        self.assertEqual(list(DEFAULT_BANNED_WORDS), sorted(set(DEFAULT_BANNED_WORDS)))
        f = WordFilter(list(DEFAULT_BANNED_WORDS))
        self.assertEqual(f.words, list(DEFAULT_BANNED_WORDS))
        self.assertEqual(f.find("what a Camel-Jockey"), "camel jockey")
        self.assertEqual(f.find("mentally retarded"), "mentally retarded")
        self.assertIsNone(f.find("the retardant coating"))

    def test_regex_metacharacters_are_literal(self) -> None:
        f = WordFilter(["c++", "a.b"])
        self.assertEqual(f.find("learn c++ today"), "c++")
        self.assertIsNone(f.find("aXb"))
        self.assertEqual(normalize_word("  Héllo\tWORLD "), "héllo world")


class WordFilterDatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def test_words_persist_per_guild(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Database(Path(temp_dir) / "octobot.db")
            await database.connect()
            try:
                self.assertTrue(await database.add_banned_word(1, "alpha", 9))
                self.assertFalse(await database.add_banned_word(1, "alpha", 9))
                self.assertTrue(await database.add_banned_word(1, "beta", 9))
                self.assertTrue(await database.add_banned_word(2, "gamma", 9))
                self.assertEqual(await database.list_banned_words(1), ["alpha", "beta"])
                self.assertTrue(await database.remove_banned_word(1, "alpha"))
                self.assertFalse(await database.remove_banned_word(1, "alpha"))
                self.assertEqual(await database.list_banned_words(1), ["beta"])
                self.assertEqual(await database.list_banned_words(2), ["gamma"])
            finally:
                await database.close()


class WordFilterCogTests(unittest.TestCase):
    def test_group_exposes_list_add_remove(self) -> None:
        names = sorted(command.name for command in WordFilterCog.__cog_app_commands__)
        self.assertEqual(names, ["add", "list", "remove"])
        self.assertEqual(WordFilterCog.__cog_group_name__, "word")


if __name__ == "__main__":
    unittest.main()
