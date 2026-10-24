"""Opening a video: finding its subtitle files (or using one chosen by hand; a web video's
are fetched by yt-dlp), choosing the tracks once mpv has loaded it, and tokenizing the whole
episode in the background."""
import html
import logging
from pathlib import Path

from aqt import mw
from aqt.qt import QFileDialog
from aqt.utils import showWarning, tooltip

from ..cards import media
from ..core import config
from ..language import knowledge, tokenizer
from ..subs import online, session, subtitles

log = logging.getLogger(__name__)


class EpisodeMixin:
    def _init_episode(self):
        self._video = None
        self._sidecars = (None, None)  # (source, target) subtitle files of the video loading
        self._episode_files = None     # (video, source, target) last given to _load_episode
        self._tracks_chosen = None     # the video _select_subtitle_tracks last ran for

    @staticmethod
    def _wants_target():
        """Are target-language subtitles needed: for a card field, or to show them?"""
        cfg = config.get()
        return bool(cfg["translation_field"]) or cfg["translation_display"] != "off"

    def _find_sidecars(self, video):
        cfg = config.get()
        if online.is_url(video):  # fetched once it opens, unless one was chosen by hand
            chosen = self.playlist.subtitles.get(video)
            return (Path(chosen) if chosen and Path(chosen).is_file() else None), None
        return subtitles.find_sidecars(video, cfg["source_lang"], cfg["target_lang"],
                                       self.playlist.subtitles.get(video))

    def open_video(self, path):
        source, target = self._find_sidecars(path)
        self._sidecars = (source, target)
        self._jobs.invalidate("episode")  # the last video's subtitles no longer apply
        self._stop_batch("another video was opened")
        self.refresh_knowledge()
        self._remember_position()  # of the video we are leaving
        self.log.flush()
        self._clear_loop()
        self._hide_popup()
        self._apply_saved_sub_delay(path)
        self._video = path
        self._jump_span = self._chosen_span = None
        self.session.new_episode()
        self.panel.set_translation("")
        self.transcript.set_lines([], 0, [])
        self.transcript.set_words([])
        # without a source file, an embedded track is tried once the video is loaded
        self._load_episode(path, source, target if self._wants_target() else None)
        self.player.play(path, self.playlist.positions.get(path))
        if online.is_url(path) and source is None:
            self._fetch_online_subtitles(path)

    def _fetch_online_subtitles(self, url):
        """Get a web video's subtitles (the site's own, else its automatic captions) and its
        title with yt-dlp, in the background while mpv starts playing it. Once they are
        there they are used as files next to a video would be."""
        cfg = config.get()
        target_lang = cfg["target_lang"] if self._wants_target() else None
        self.score_label.setText("Getting the subtitles...")

        def done(future):
            self.score_label.setText("")
            try:
                title, source, target = future.result()
            except Exception as e:
                log.warning("couldn't get the subtitles of %s: %s", url, e)
                self.score_label.setText("No subtitles")
                showWarning(f"Couldn't get the video's subtitles:\n\n{e}", parent=self)
                return
            if title and self.playlist.titles.get(url) != title:
                self.playlist.titles[url] = title
                self._save_playlist()
                self._relabel_playlist()
            if source is None:
                tooltip("This video has no subtitles in the language you are learning. More "
                        "> Load subtitles... picks a file.", period=6000)
                return
            self._sidecars = (source, target)
            if self._tracks_chosen == url:  # mpv was quicker: add them now
                self._select_subtitle_tracks()
            self._load_episode(url, str(source), target)

        self._jobs.run(lambda: online.fetch_subtitles(
            url, media.scratch_dir(), cfg["source_lang"], target_lang, cfg["ffmpeg_path"]),
            done, scope="episode")

    def load_subtitle_file(self):
        """Use a subtitle file of your choice as the video's source-language subtitles (a
        Jimaku download, a file in a Subs folder...), from now on."""
        if not self._has_video():
            tooltip("Open a video first")
            return
        path, _ = QFileDialog.getOpenFileName(
            self, "Load subtitle file", str(Path(self._video).parent),
            f"Subtitles ({' '.join('*' + e for e in subtitles.SUB_EXTS)});;All files (*)")
        if path:
            self._use_subtitle_file(self._video, path)

    def _use_subtitle_file(self, video, path, label="Subtitles", note=""):
        """Use `path` as `video`'s source-language subtitles from now on, at once if it is
        the video playing (a download may finish once another one plays). `note` (HTML)
        goes in the tooltip after the file's name."""
        path = Path(path)
        self.playlist.subtitles[video] = str(path)
        self._save_playlist()
        if not self._has_video() or video != self._video:
            return
        self._jobs.invalidate("episode")  # the subtitles found first no longer apply
        self._stop_batch("other subtitles were loaded")  # its lines are the old file's
        self._sidecars = (path, self._sidecars[1])
        self.player.add_subtitle(path, select=True)
        self.session.new_episode()
        self.transcript.set_lines([], 0, [])
        self.transcript.set_words([])
        self._episode_lemmas.pop(video, None)
        target = self._sidecars[1] if self._wants_target() else None
        self._load_episode(video, str(path), target)
        tooltip(f"{label}: {html.escape(path.name)}{note}")

    def _select_subtitle_tracks(self):
        """Once the video is loaded: load the sidecar files and choose the source-language
        track for the panel and (if a translation field is set) the target-language one.
        Without a source sidecar, an embedded track is used."""
        p = self.player
        if p is None:
            return
        self._tracks_chosen = self._video
        cfg = config.get()
        source, target = self._sidecars
        want_target = self._wants_target()
        for path in (source, target if want_target else None):
            if path:
                p.add_subtitle(path)
        all_tracks = p.track_list
        self._populate_audio_tracks(all_tracks)
        subs = [t for t in all_tracks if t.get("type") == "sub"]
        tracks = [t for t in subs if t.get("codec") not in subtitles.IMAGE_CODECS]

        def pick(path, lang, exclude=None):
            for t in tracks:
                if path and Path(t.get("external-filename") or "").name == path.name:
                    return t["id"]
            codes = subtitles.lang_codes(lang)
            return next((t["id"] for t in tracks
                         if (t.get("lang") or "").lower() in codes and t["id"] != exclude), None)

        sid = pick(source, cfg["source_lang"])
        if sid is None and not source:
            # an embedded track with no language tag is most likely the one being learned
            sid = next((t["id"] for t in tracks
                        if (t.get("lang") or "und").lower() == "und"), None)
        if sid is not None:
            p.select("sid", sid)
        elif len(subs) > len(tracks):
            tooltip("The subtitles in this video are images (PGS / DVD): they can't be read.")
        elif not source and not online.is_url(self._video):  # a web video's are on their way
            tooltip("No subtitles found: no matching file next to the video, none embedded. "
                    "More > Search Jimaku... downloads some, More > Load subtitles... picks a "
                    "file.", period=6000)
        secondary = pick(target, cfg["target_lang"], exclude=sid) if want_target else None
        p.select("secondary-sid", secondary)
        self._on_pause_changed(p.paused)  # a file can start playing without a pause change
        self._update_seek_marks()  # the duration is known now

        if sid is not None and not source:
            track = next(t for t in tracks if t["id"] == sid)
            if not track.get("external") and track.get("ff-index") is not None:
                self._load_embedded_episode(track["ff-index"])

    def _load_embedded_episode(self, stream_index):
        """Extract an embedded subtitle track so the whole episode can be tokenized."""
        video = self._video

        def done(future):
            try:
                path = future.result()
            except Exception as e:
                log.warning("couldn't extract subtitle stream %s of %s: %s", stream_index,
                            video, e)
                self.score_label.setText("Score unavailable")
                self.score_label.setToolTip(f"Couldn't extract the embedded subtitles:\n{e}")
                return
            self._load_episode(video, str(path))

        self.score_label.setText("Reading the subtitles...")
        self._jobs.run(lambda: media.extract_subtitles(video, stream_index), done,
                       scope="episode")

    def _load_episode(self, video, sub, target=None):
        """Tokenize the whole subtitle file in the background (cached on disk): the lines
        then show instantly, and the known/learning/new share of the episode can be
        computed."""
        self.score_label.setText("")
        self._episode_files = (video, sub, target)
        if not sub:
            return
        key = tokenizer.analysis_key()
        self.score_label.setText("Analysing the subtitles...")

        def progress(n, total):  # background thread
            mw.taskman.run_on_main(lambda: self._show_analysing(video, n, total))

        def tokenize_many(texts):
            return tokenizer.tokenize_many(texts, progress=progress)

        def work():
            events, tokens = session.tokenize_file(sub, key, tokenize_many, video)
            try:
                target_events = subtitles.load_events(target) if target else []
            except Exception:
                target_events = []  # the translation is a nice-to-have
            return events, tokens, target_events

        def done(future):
            try:
                events, tokens, target_events = future.result()
            except Exception as e:
                log.warning("couldn't load the episode's subtitles %s: %s", sub, e)
                offline = self._yomitan_failed(e)  # if so, tokenized again once it is back
                self.score_label.setText("Score unavailable" + (": Yomitan offline"
                                                                if offline else ""))
                self.score_label.setToolTip(f"Couldn't tokenize the subtitle file:\n{e}")
                return
            self.session.tokens.update(tokens)
            self._learn_tokens(tokens.values())
            self.session.events, self.session.target_events = events, target_events
            self._episode_lemmas[video] = knowledge.content_lemmas(
                t for text in {text for _, _, text in events} for t in tokens.get(text, ()))
            self.score_label.setText("")  # no longer analysing; the score, if it has words
            self._update_score()
            self._update_badge()  # the line on screen may be i+1 now that its words are known
            self._update_seek_marks()
            self._refresh_transcript_lines()
            self._refresh_transcript_status()
            self._seek_to_library_hit()

        self._jobs.run(work, done, scope="episode")

    def _show_analysing(self, video, n, total):
        """How far tokenizing the episode has got: `n` of its `total` lines (the line filter
        and i+1 need them)."""
        if self.player is not None and self._video == video and n < total:
            self.score_label.setText(f"Analysing the subtitles... {n}/{total} lines")
