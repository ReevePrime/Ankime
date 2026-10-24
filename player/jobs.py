"""Background work for the player whose result can go stale. Each job may belong to a scope
("episode": the subtitles of the video being opened); invalidating the scope drops the
results of the jobs started in it before, and closing the player drops every result. The
`done` callbacks then don't each have to check that the player is still open and still on
the same video."""


def _taskman_run(work, done):
    from aqt import mw
    mw.taskman.run_in_background(work, done)


class Jobs:
    def __init__(self, run_in_background=_taskman_run):
        self._run = run_in_background
        self._generations = {}  # scope -> number of times it was invalidated
        self._closed = False

    def run(self, work, done, scope=None):
        """work() in the background, then done(future) on the main thread, unless `scope`
        was invalidated or the player closed meanwhile."""
        generation = self._generations.get(scope, 0)

        def finished(future):
            if not self._closed and self._generations.get(scope, 0) == generation:
                done(future)

        self._run(work, finished)

    def invalidate(self, scope):
        """Drop the results of the jobs started in `scope` so far."""
        self._generations[scope] = self._generations.get(scope, 0) + 1

    def close(self):
        """The player closed: drop every result still to come."""
        self._closed = True
