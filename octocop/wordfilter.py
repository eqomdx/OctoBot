from __future__ import annotations

import re
import unicodedata


def normalize_word(value: str) -> str:
    """Canonical form for storage and matching: casefolded, single-spaced, trimmed.

    Hyphens and underscores count as spaces, so "porch-monkey", "porch monkey" and
    "porch_monkey" are the same entry.
    """
    folded = unicodedata.normalize("NFKC", value).casefold()
    folded = re.sub(r"[-_]+", " ", folded)
    return re.sub(r"\s+", " ", folded).strip()


class WordFilter:
    """Whole-word, case-insensitive matcher over a set of banned words or phrases.

    "ban" matches "BAN", "ban!" and "ban." but not "banana" or "urban", so ordinary
    words that merely contain a banned one are left alone. The parts of a phrase may
    be joined by spaces, hyphens, underscores, dots or nothing at all, so "sand nigger"
    also catches "sand-nigger" and "sandnigger".
    """

    def __init__(self, words: list[str] | None = None):
        self._words: set[str] = set()
        self._pattern: re.Pattern[str] | None = None
        self._by_word: dict[str, re.Pattern[str]] = {}
        if words:
            for word in words:
                self._words.add(normalize_word(word))
            self._rebuild()

    @property
    def words(self) -> list[str]:
        return sorted(self._words)

    def add(self, word: str) -> bool:
        cleaned = normalize_word(word)
        if not cleaned or cleaned in self._words:
            return False
        self._words.add(cleaned)
        self._rebuild()
        return True

    def remove(self, word: str) -> bool:
        cleaned = normalize_word(word)
        if cleaned not in self._words:
            return False
        self._words.discard(cleaned)
        self._rebuild()
        return True

    @staticmethod
    def _word_regex(word: str) -> str:
        return r"[\s\-_.]*".join(re.escape(piece) for piece in word.split(" "))

    def _rebuild(self) -> None:
        if not self._words:
            self._pattern = None
            self._by_word = {}
            return
        # Longest first so a phrase wins over a word it contains.
        ordered = sorted(self._words, key=len, reverse=True)
        self._by_word = {
            word: re.compile(rf"^{self._word_regex(word)}$", re.IGNORECASE) for word in ordered
        }
        joined = "|".join(self._word_regex(word) for word in ordered)
        self._pattern = re.compile(rf"(?<!\w)(?:{joined})(?!\w)", re.IGNORECASE)

    def find(self, text: str) -> str | None:
        """Return the list entry that ``text`` contains, or None."""
        if self._pattern is None or not text:
            return None
        match = self._pattern.search(unicodedata.normalize("NFKC", text))
        if match is None:
            return None
        hit = match.group(0)
        for word, pattern in self._by_word.items():
            if pattern.match(hit):
                return word
        return normalize_word(hit)
