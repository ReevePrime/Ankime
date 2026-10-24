"""How common words are in Japanese, from Yomitan's frequency dictionaries: the transcript can
list an episode's new words most common first rather than most frequent in the episode. Ranks
are asked once per word and kept in user_files/frequency.json (they only change when you
change dictionaries: the player's More > Re-analyse subtitles forgets them)."""
from concurrent.futures import ThreadPoolExecutor

from ..core.store import USER_FILES, JsonFile

FREQUENCY_FILE = USER_FILES / "frequency.json"
FETCH_LIMIT = 300  # words asked for at once: an episode's worth; the rest on the next refresh
FETCH_WORKERS = 4  # lookups sent to Yomitan at the same time


class FrequencyRanks:
    def __init__(self, path=FREQUENCY_FILE):
        self._file = JsonFile(path)
        data = self._file.load({})
        if not isinstance(data, dict):
            data = {}
        # lemma -> rank (1 = most common), or None: no frequency dictionary has it
        self.ranks = {k: v for k, v in data.items() if v is None or isinstance(v, int)}

    def missing(self, lemmas):
        """The `lemmas` whose rank hasn't been asked for yet (at most FETCH_LIMIT)."""
        return [lemma for lemma in dict.fromkeys(lemmas) if lemma not in self.ranks][
            :FETCH_LIMIT]

    def update(self, found):
        """Keep {lemma: rank or None} and save them."""
        self.ranks.update(found)
        self._file.save(self.ranks)

    def clear(self):
        """Forget every rank (Yomitan's frequency dictionaries changed): asked again."""
        self.ranks = {}
        self._file.save(self.ranks)

    def sort(self, counts):
        """[(lemma, count)] most common first; words without a rank (yet) last, by count."""
        def key(item):
            lemma, count = item
            rank = self.ranks.get(lemma)
            return (rank is None, rank or 0, -count, lemma)
        return sorted(counts, key=key)


def fetch(lemmas, rank_of, workers=FETCH_WORKERS):
    """{lemma: rank or None} for `lemmas`, asking `rank_of(lemma)` (yomitan.frequency_rank)
    for each, a few at a time. Background thread. The first error is raised (Yomitan
    offline would fail them all): the lookups not started yet are dropped."""
    lemmas = list(lemmas)
    with ThreadPoolExecutor(workers) as pool:
        futures = [pool.submit(rank_of, lemma) for lemma in lemmas]
        try:
            return {lemma: future.result() for lemma, future in zip(lemmas, futures)}
        except BaseException:
            for future in futures:
                future.cancel()
            raise
