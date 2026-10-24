"""Known words over time, for the immersion log's chart: how many words were known at the end
of each day, worked out again from what the collection already records. A card's review log
has the interval each review gave it, so the day it reached the known interval (and the day a
lapse took it back under) is known; a word marked known has the time it was marked. Today's
count is the one the player shows (Knowledge.known_words).

With FSRS, a card counts as known by its stability now but by its interval in the past (the
review log has no stability): the two are close at the usual desired retention."""
import datetime

from ..core.immersion import study_day

REVIEW_CHUNK = 500  # card ids per query
FILTERED = 3        # revlog type: a review in a filtered deck; ivl 0 = not rescheduled


def _ordinal(day):
    return datetime.date.fromisoformat(day).toordinal()


def read_reviews(col, card_ids, rollover):
    """{card id: [(day ordinal, interval it gave, revlog type)]}, oldest first. Intervals
    under a day (learning steps) are negative seconds, as in the log."""
    reviews = {}
    ids = sorted(card_ids)
    for i in range(0, len(ids), REVIEW_CHUNK):
        chunk = ",".join(str(int(card_id)) for card_id in ids[i:i + REVIEW_CHUNK])
        for card_id, stamp, ivl, kind in col.db.all(
                f"select cid, id, ivl, type from revlog where cid in ({chunk}) order by id"):
            day = study_day(datetime.datetime.fromtimestamp(stamp / 1000), rollover)
            reviews.setdefault(card_id, []).append((_ordinal(day), ivl, kind))
    return reviews


def card_spans(reviews, known_now, threshold, today):
    """[(first day, day after the last)] as ordinals, the days a card was known: a day
    counts by its last review, and today by what the card is now. A card known now without
    reviews (imported known) is known since None: the start of the history."""
    known = {}
    for day, ivl, kind in reviews:
        if kind == FILTERED and ivl == 0:
            continue  # reviewed without being rescheduled: nothing changed
        known[min(day, today)] = ivl >= threshold
    if known_now and not known:
        return [(None, today + 1)]
    known[today] = known_now
    spans, start = [], None
    for day in sorted(known):
        if known[day] and start is None:
            start = day
        elif not known[day] and start is not None:
            spans.append((start, day))
            start = None
    if start is not None:
        spans.append((start, today + 1))
    return spans


def history(threshold, cards, marks, reviews, today, rollover):
    """[("YYYY-MM-DD", words known at the end of it)] for every day from the first one a
    word was known to `today` ("YYYY-MM-DD"). `cards` is {card id: (its days now, its
    words)}, `marks` {word: time marked known, 0 if not known}, `reviews` as read_reviews
    gives. A word is known on a day if one of its cards was, or it was marked by then."""
    end = _ordinal(today)
    spans = {}
    for card_id, (days_now, words) in cards.items():
        found = card_spans(reviews.get(card_id, ()), days_now >= threshold, threshold, end)
        if found:
            for word in words:
                spans.setdefault(word, []).extend(found)
    for word, stamp in marks.items():
        day = (min(_ordinal(study_day(datetime.datetime.fromtimestamp(stamp), rollover)), end)
               if stamp > 0 else None)
        spans.setdefault(word, []).append((day, end + 1))
    starts = [s for found in spans.values() for s, _ in found if s is not None]
    first = min(starts, default=end)
    changes = [0] * (end - first + 2)  # +1 the day a word becomes known, -1 the day it isn't
    for found in spans.values():
        merged_end = None
        for s, e in sorted((first if s is None else s, e) for s, e in found):
            if merged_end is not None and s <= merged_end:  # overlaps the span before
                if e > merged_end:  # it goes on longer: move that span's end
                    changes[merged_end - first] += 1
                    changes[e - first] -= 1
                    merged_end = e
                continue
            changes[s - first] += 1
            changes[e - first] -= 1
            merged_end = e
    out, known = [], 0
    for i in range(end - first + 1):
        known += changes[i]
        out.append((datetime.date.fromordinal(first + i).isoformat(), known))
    return out


def known_history(col, inputs, today, rollover):
    """history() of the collection, from Knowledge.history_inputs(). Reads the review log:
    run it in the background (QueryOp)."""
    threshold, cards, marks = inputs
    reviews = read_reviews(col, cards, rollover)
    return history(threshold, cards, marks, reviews, today, rollover)
