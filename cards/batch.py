"""Words queued from the transcript to become cards, added one after the other. Pure logic:
the player window cuts the media and adds each card."""


class WordBatch:
    def __init__(self):
        self.queue = []   # dictionary forms still to add; the first is being added
        self.total = 0    # words queued since the batch started, for "3/10"

    def add(self, lemmas):
        """Queue words (those already queued are ignored). True if the batch was idle: the
        caller starts it with next()."""
        idle = not self.queue
        if idle:
            self.total = 0
        queued = [lemma for lemma in dict.fromkeys(lemmas) if lemma not in self.queue]
        self.queue.extend(queued)
        self.total += len(queued)
        return idle and bool(queued)

    def next(self, line_for, is_new):
        """(lemma, (line index, surface)) for the next word to add, or None once the batch
        is done. `line_for(lemma)` picks the line. Words no line has, or no longer new (mined
        meanwhile), are dropped."""
        while self.queue:
            lemma = self.queue[0]
            hit = line_for(lemma)
            if hit is not None and is_new(lemma):
                return lemma, hit
            self.queue.pop(0)
        return None

    def position(self):
        """Number of the word being added, counting from 1."""
        return self.total - len(self.queue) + 1

    def done(self, lemma):
        """`lemma` was added (or failed): move on from it."""
        if self.queue and self.queue[0] == lemma:
            self.queue.pop(0)

    def cancel(self):
        """Drop the words still queued, the one being added included; returns how many.
        finish() then reports nothing."""
        dropped = len(self.queue)
        self.queue.clear()
        self.total = 0
        return dropped

    def finish(self):
        """How many words the finished batch had (0 if it was already reported)."""
        total, self.total = self.total, 0
        return total
