"""Subtitle panel: an AnkiWebView with one <span> per word. Definitions open in the
floating popup (popup.py), next to the word."""
import html
import json
import re

from aqt.webview import AnkiWebView

from ..core import config

# Japanese fonts on Windows, macOS and Linux. With lang="ja" on the page as well, kanji get
# their Japanese glyph shapes rather than the Chinese ones of a font found first.
JA_FONTS = ('"Yu Gothic UI", "Meiryo", "Hiragino Sans", "Hiragino Kaku Gothic ProN", '
            '"Noto Sans CJK JP", "Noto Sans JP", sans-serif')
FULLSCREEN_SCALE = 1.2  # the subtitles are larger over the video
# Behind the escaping of everything shown: nothing from outside Anki's own server (no remote
# images that would tell a server what you look up), no plugins, no frames, no forms. The
# popup uses it too, behind sanitize.clean_html.
CSP = ("<meta http-equiv=\"Content-Security-Policy\" content=\"img-src 'self' data:; "
       "media-src 'self' data:; object-src 'none'; frame-src 'none'; "
       "form-action 'none'\">")
# (word status, the setting holding its style, label). A style is {"color", "background"
# (#rrggbb, "" for the default), "bold", "italic", "underline" (UNDERLINES), "size" (% of
# the subtitle's)}: see word_css.
WORD_STYLES = [("new", "new_word_style", "New words"),
               ("learning", "learning_word_style", "Learning words"),
               ("known", "known_word_style", "Known words")]
# (config value, label): the line under a word of a status, if any
UNDERLINES = [("none", "No underline"), ("solid", "Underline"), ("dotted", "Dotted"),
              ("dashed", "Dashed"), ("wavy", "Wavy"), ("double", "Double")]
WORD_SIZES = (50, 200)  # % of the subtitle's size a word's size can be


def _style(bold=False, underline="none", size=100):
    return {"color": "", "background": "", "bold": bold, "italic": False,
            "underline": underline, "size": size}


# (name, tooltip, {status: style}) for the settings' word style presets, besides the
# defaults. They keep Ankime's colours and add what tells the statuses apart without them,
# for colour-blind users: a line's shape, bold, a size.
WORD_STYLE_PRESETS = [
    ("Colour-blind: underlines",
     "New words bold and underlined, learning words with a dotted underline, known words plain",
     {"new": _style(True, "solid"), "learning": _style(underline="dotted"),
      "known": _style()}),
    ("Colour-blind: wavy and dashed",
     "New words bold with a wavy underline, learning words with a dashed one, known words plain",
     {"new": _style(True, "wavy"), "learning": _style(underline="dashed"),
      "known": _style()}),
    ("Colour-blind: larger new words",
     "New words bold, underlined and a fifth larger, learning words with a dotted underline, "
     "known words plain: easy to tell apart from across the room",
     {"new": _style(True, "solid", 120), "learning": _style(underline="dotted"),
      "known": _style()}),
]
HEX_COLOR = re.compile(r"#[0-9a-fA-F]{6}")
# (config value, label): which words show their reading above them
FURIGANA = [("off", "None"), ("new", "New words"), ("unknown", "New and learning words"),
            ("all", "Every word")]
# (config value, label): the translation under the subtitle
TRANSLATION_DISPLAY = [("off", "Hidden"), ("blur", "Blurred until hovered"), ("on", "Shown")]
TRANSLATION_SCALE = 0.6  # its size, relative to the subtitle's
# (config value, label): how words show their pitch-accent category (language/pitch.py).
# "color" puts the category's colour over the word status's; "line" keeps the status colours
# and draws a line of the category's colour under the word.
PITCH_DISPLAY = [("off", "Off"), ("color", "Colour the words"),
                 ("line", "Coloured line under the words")]
# category -> (colour on a light page, colour in night mode and fullscreen), unless the
# pitch_colors setting gives one. The usual pitch-accent colours (heiban blue, atamadaka
# red, nakadaka orange, odaka green, kifuku purple); the light ones are 4.5:1 against white.
PITCH_COLORS = {"heiban": ("#1864ab", "#4dabf7"), "atamadaka": ("#c92a2a", "#ff6b6b"),
                "nakadaka": ("#c2410c", "#ff922b"), "odaka": ("#237032", "#69db7c"),
                "kifuku": ("#7048e8", "#b197fc")}

PAGE = """
<style>
  html, body { margin: 0; height: 100%; background: transparent; user-select: none; }
  body { display: flex; flex-direction: column; font-family: $FONTS; }
  #sub { font-size: var(--sub-size, 28px); text-align: center; padding: 10px 12px 2px; line-height: 1.6; min-height: 1.6em; }
  .w { border-radius: 4px; cursor: pointer; padding: 0 1px; }
  .w:hover, .w.sel { background: rgba(80, 140, 255, .35); }
  /* known words stay plain. On a light page these are 5:1 against white; Anki's night mode
     and fullscreen use lighter shades of the same colours. */
  .w.new { color: #c2410c; font-weight: bold; }
  .w.learning { color: #1971c2; }
  :is(.nightMode, .night_mode, .night-mode) .w.new { color: #ff922b; }
  :is(.nightMode, .night_mode, .night-mode) .w.learning { color: #4dabf7; }
  /* furigana: laid out for every word with a reading (so lines don't jump when a word
     changes status), shown for the words the setting picks */
  rt { font-size: .5em; font-weight: normal; visibility: hidden; }
  body.furi-off rt { display: none; }
  body.furi-all rt, body.furi-new .w.new rt,
  body.furi-unknown .w.new rt, body.furi-unknown .w.learning rt { visibility: visible; }
  #tr { font-size: calc(var(--sub-size, 28px) * $TR_SCALE); text-align: center;
        padding: 0 12px; opacity: .85; }
  #tr:empty, body.tr-off #tr { display: none; }
  body.tr-blur #tr { filter: blur(6px); transition: filter .15s; }
  body.tr-blur #tr:hover { filter: none; }
  /* pitch accent: each category's colour in --pitch (see pitch_css for custom ones), used
     as the body class the setting picks says; over the status colours and word styles */
$PITCH_CSS
  html body.pitch-color .w:is($PITCHED) { color: var(--pitch) !important; }
  html body.pitch-line .w:is($PITCHED) { border-bottom: 3px solid var(--pitch); }
  #sub.i1 { box-shadow: inset 4px 0 0 #2b8a3e; }
  .tag { position: fixed; top: 8px; padding: 1px 10px; border-radius: 10px; font-size: 13px;
         font-weight: bold; color: white; }
  .tag:empty { display: none; }
  #badge { right: 12px; background: #2b8a3e; }  /* 4.4:1 against the white text */
  #range { left: 12px; background: #7048e8; }
  #msg { text-align: center; font-size: 14px; padding: 0 12px; }
  .err { color: #d9534f; }
  #status { position: fixed; bottom: 6px; left: 0; right: 0; text-align: center; font-size: 13px; }
  #status:empty { display: none; }
  #status .off { color: #d9534f; font-weight: bold; }
  #status button { font-size: 12px; padding: 0 8px; cursor: pointer; }
  /* fullscreen: over the video */
  body.fs { background: rgba(0, 0, 0, .55); color: #fff; }
  body.fs #sub { font-size: var(--fs-size, 34px); text-shadow: 0 0 3px #000, 0 0 6px #000; }
  body.fs .w.new { color: #ff922b; }
  body.fs .w.learning { color: #74c0fc; }
  body.fs #tr { font-size: calc(var(--fs-size, 34px) * $TR_SCALE); text-shadow: 0 0 3px #000; }
</style>
<style id="word-styles">/* the settings' styles for each word status, see word_css */</style>
<div id="sub"></div>
<div id="tr"></div>
<div id="badge" class="tag"></div>
<div id="range" class="tag" title="Lines the next card takes besides this one"></div>
<div id="msg"></div>
<div id="status"></div>
<script>
  document.documentElement.lang = "ja";
  let timer = null;
  const sub = document.getElementById("sub"), msg = document.getElementById("msg");
  const badge = document.getElementById("badge"), range = document.getElementById("range");
  const status = document.getElementById("status"), tr = document.getElementById("tr");

  function setSub(h) { sub.innerHTML = h; msg.innerHTML = ""; setBadge(""); setRange(""); }
  function setBadge(t) { badge.textContent = t; sub.classList.toggle("i1", t === "i+1"); }
  function setRange(t) { range.textContent = t; }
  function setMsg(h) { msg.innerHTML = h; }
  function setStatus(h) { status.innerHTML = h; }   // stays across lines, unlike msg
  function setTranslation(t) { tr.textContent = t; }
  function setFullscreen(on) { document.body.classList.toggle("fs", on); }
  function setStyle(size, fsSize, wordCss, furigana, translation, pitchDisplay) {
    const body = document.body;
    body.style.setProperty("--sub-size", size + "px");
    body.style.setProperty("--fs-size", fsSize + "px");
    document.getElementById("word-styles").textContent = wordCss;
    for (const [value] of FURIGANA_VALUES) body.classList.toggle("furi-" + value, value === furigana);
    for (const [value] of TRANSLATION_VALUES)
      body.classList.toggle("tr-" + value, value === translation);
    for (const [value] of PITCH_VALUES)
      body.classList.toggle("pitch-" + value, value === pitchDisplay);
  }
  // classes[i] is the status ("new" | "learning" | "known" | "other" | "") of the i-th
  // word span ("other" is a name or fragment with no dictionary form: no colour); pitches[i]
  // its pitch-accent category ("" for none known)
  const STATUSES = ["new", "learning", "known", "other"];
  const PITCHES = PITCH_CLASSES;
  function restyle(classes, pitches) {
    sub.querySelectorAll(".w").forEach((el, i) => {
      el.classList.remove(...STATUSES, ...PITCHES);
      if (classes[i]) el.classList.add(classes[i]);
      if (pitches && pitches[i]) el.classList.add("p-" + pitches[i]);
    });
  }
  function lookup(el) {
    document.querySelectorAll(".w.sel").forEach(e => e.classList.remove("sel"));
    el.classList.add("sel");
    const r = el.getBoundingClientRect();
    pycmd("lookup:" + JSON.stringify([el.dataset.w, el.dataset.b, [r.left, r.top, r.width, r.height],
                                      Number(el.dataset.i)]));
  }
  // keyboard word cursor: step over the words (not punctuation) of the line
  function moveSel(delta) {
    const words = [...sub.querySelectorAll(".w")].filter(
      el => STATUSES.some(s => el.classList.contains(s)));
    if (!words.length) return;
    const at = words.findIndex(el => el.classList.contains("sel"));
    const next = at < 0 ? (delta > 0 ? 0 : words.length - 1)
                        : Math.min(Math.max(at + delta, 0), words.length - 1);
    lookup(words[next]);
  }
  // the selected word's definition again (the first word's if none is selected)
  function lookupSel() {
    const el = sub.querySelector(".w.sel");
    if (el) lookup(el); else moveSel(1);
  }
  sub.addEventListener("mouseover", e => {
    const el = e.target.closest(".w");
    if (!el) return;
    clearTimeout(timer);
    timer = setTimeout(() => lookup(el), 150);   // hover intent: ignore fly-bys
  });
  sub.addEventListener("mouseout", e => { if (e.target.closest(".w")) clearTimeout(timer); });
  document.addEventListener("click", e => {
    const b = e.target.closest("button[data-cmd]");
    if (b) { pycmd(b.dataset.cmd); return; }
    const el = e.target.closest(".w");
    clearTimeout(timer);
    if (el) lookup(el); else pycmd("dismiss");
  });
  window.onerror = (m, s, l) => console.log("ankime panel error: " + m + " @" + l);
</script>
"""


def _default_pitch_css():
    """The categories' default colours, as the --pitch of their words: lighter in night mode
    and fullscreen."""
    rules = []
    for name, (light, dark) in PITCH_COLORS.items():
        rules.append(f"  .w.p-{name} {{ --pitch: {light}; }}")
        rules.append(f"  :is(.nightMode, .night_mode, .night-mode, .fs) .w.p-{name} "
                     f"{{ --pitch: {dark}; }}")
    return "\n".join(rules)


PAGE = PAGE.replace("$FONTS", JA_FONTS).replace("$TR_SCALE", str(TRANSLATION_SCALE)).replace(
    "$PITCH_CSS", _default_pitch_css()).replace(
    "$PITCHED", ", ".join(f".p-{name}" for name in PITCH_COLORS)).replace(
    "FURIGANA_VALUES", json.dumps(FURIGANA)).replace(
    "TRANSLATION_VALUES", json.dumps(TRANSLATION_DISPLAY)).replace(
    "PITCH_VALUES", json.dumps(PITCH_DISPLAY)).replace(
    "PITCH_CLASSES", json.dumps([f"p-{name}" for name in PITCH_COLORS]))


def word_styles(cfg):
    """{word status: its style} from the settings (WORD_STYLES)."""
    return {status: cfg[key] for status, key, _ in WORD_STYLES}


def word_size(style):
    try:
        size = int(style.get("size", 100))
    except (TypeError, ValueError):
        return 100
    return min(max(size, WORD_SIZES[0]), WORD_SIZES[1])


def _color(style, key):
    color = style.get(key)
    return color if isinstance(color, str) and HEX_COLOR.fullmatch(color) else ""


def word_css(styles):
    """CSS putting each status's style ({status: style}, see WORD_STYLES) over the page's
    own: bold, italic, an underline, a size, and a colour and background where they aren't
    "" (the default colours, which are lighter in night mode and fullscreen). Only values
    of the right kind get in, so a hand-edited setting can't put any other CSS on the
    page; a style that isn't a dict leaves its status as the page has it."""
    rules = []
    for status, style in styles.items():
        if not isinstance(style, dict):
            continue
        underline = style.get("underline")
        decls = [f"font-weight: {'bold' if style.get('bold') else 'normal'}",
                 f"font-style: {'italic' if style.get('italic') else 'normal'}",
                 f"text-decoration: underline {underline} 2px; text-underline-offset: .2em"
                 if underline in dict(UNDERLINES) and underline != "none"
                 else "text-decoration: none"]
        if (size := word_size(style)) != 100:
            decls.append(f"font-size: {size}%")
        if color := _color(style, "color"):
            decls.append(f"color: {color} !important")  # over night mode's and fullscreen's
        rules.append(f"html body .w.{status} {{ {'; '.join(decls)}; }}")
        if background := _color(style, "background"):  # the hover highlight still shows
            rules.append(f"html body .w.{status}:not(:hover):not(.sel) "
                         f"{{ background: {background}; }}")
    return "\n".join(rules)


def pitch_css(colors):
    """CSS giving the pitch-accent categories the colours of the pitch_colors setting
    ({category: "#rrggbb", or "" for the default}), in every mode. Only colours get in."""
    if not isinstance(colors, dict):
        return ""
    return "\n".join(f"html body .w.p-{name} {{ --pitch: {color} !important; }}"
                     for name in PITCH_COLORS if (color := _color(colors, name)))


def word_pitches(tokens, category):
    """Pitch-accent category of each word span render_sub emits, in order ("" for none).
    `category(token)` gives a word's."""
    return [category(token) for token in tokens if token[0] != "\n"]


def word_scale(styles):
    """How much larger than the subtitle's size the largest word style is (1 at most 100%)."""
    sizes = [word_size(s) for s in styles.values() if isinstance(s, dict)]
    return max([100, *sizes]) / 100


def panel_height(font_size, fullscreen=False, furigana=False, translation=False,
                 word_scale=1.0):
    """Height in px of the panel holding two subtitle lines of `font_size` px (larger in
    fullscreen) plus the message and status lines: 150 at the default 28. Furigana above
    the lines, two translation lines under them and words made larger (`word_scale`) take
    more."""
    if fullscreen:
        font_size *= FULLSCREEN_SCALE
    line_size = font_size * max(word_scale, 1.0)
    height = 3.2 * line_size + 60
    if furigana:
        height += 0.8 * line_size
    if translation:
        height += 2 * 1.4 * TRANSLATION_SCALE * font_size
    return round(height)


def word_classes(tokens, classify):
    """Status class of each word span render_sub emits, in order. `classify` is
    Knowledge.token_status."""
    return [classify(surface, base, alts) for surface, _, base, alts in tokens
            if surface != "\n"]


def _ruby(surface, furigana):
    """The word as HTML, its segments that have a reading in <ruby> (CSS decides which
    words show it)."""
    if not furigana or isinstance(furigana, str):
        return html.escape(surface)
    return "".join(
        f"<ruby>{html.escape(text)}<rt>{html.escape(reading)}</rt></ruby>"
        if reading and reading != text else html.escape(text)
        for text, reading in furigana)


def render_sub(text, tokens=None, classify=None, pitches=()):
    """HTML for the subtitle line. `tokens` is [(surface, furigana, base, alts)] or None for
    plain text; `classify(surface, base, alts)` (Knowledge.token_status) gives each word's
    status class ('' for none), and `pitches` each word's pitch-accent category (see
    word_pitches) where known."""
    if not tokens:
        return html.escape(text).replace("\n", "<br>")
    statuses = word_classes(tokens, classify) if classify else []
    pitches = list(pitches)
    classes = iter(" ".join(c for c in (status, f"p-{pitches[i]}" if i < len(pitches)
                                        and pitches[i] in PITCH_COLORS else "") if c)
                   for i, status in enumerate(statuses))
    # tokens come from the joined text, so newlines can only appear inside a segment
    # data-i: the token's index, to find the word where it is in the line (see cards/notes.py)
    return "".join(
        "<br>" if surface == "\n" else
        f'<span class="w {next(classes, "")}" data-w="{html.escape(surface, quote=True)}" '
        f'data-b="{html.escape(base, quote=True)}" data-i="{i}">{_ruby(surface, furigana)}'
        '</span>'
        for i, (surface, furigana, base, _) in enumerate(tokens)
    )


def render_error(message):
    return f'<span class="err">{html.escape(message)}</span>'


OFFLINE_NOTE = ("Until it is back, the words are split from what Ankime already knows "
                "(Yomitan's earlier analyses, your cards), roughly: i+1 and the line filter "
                "wait for Yomitan.")


def render_offline(message, checking=False):
    """The status line while Yomitan is offline; `message` (why) is its tooltip."""
    action = ("<i>checking...</i>" if checking
              else '<button data-cmd="retry">Retry</button>')
    tip = html.escape(f"{message}\n\n{OFFLINE_NOTE}".strip(), quote=True)
    what = "No dictionary" if config.get()["dictionary"] == "local" else "Yomitan offline"
    return f'<span class="off" title="{tip}">{what}</span> · {action}'


class SubtitlePanel(AnkiWebView):
    """Python half of the panel. Callbacks: on_lookup(word, base, rect, index) with the
    word's rectangle in widget pixels and its index among the line's tokens, on_dismiss() for a click beside the words, on_retry() for
    the status line's Retry button."""

    def __init__(self, parent, on_lookup, on_dismiss, on_retry):
        super().__init__(parent, title="ankime subtitles")
        self._on_lookup, self._on_dismiss, self._on_retry = on_lookup, on_dismiss, on_retry
        self._ready = False
        self._pending = []  # JS queued until the page has loaded
        self.set_bridge_command(self._bridge, self)
        self.loadFinished.connect(self._loaded)
        self.setAcceptDrops(False)  # a dropped file would replace the page: the window takes it
        self.stdHtml(PAGE, head=CSP, context=self)

    def _loaded(self, ok):
        self._ready = True
        for js in self._pending:
            self.eval(js)
        self._pending.clear()

    def _bridge(self, cmd):
        if cmd.startswith("lookup:"):
            word, base, rect, index = json.loads(cmd[len("lookup:"):])
            zoom = self.zoomFactor()  # CSS pixels -> widget pixels
            self._on_lookup(word, base or "", [round(v * zoom) for v in rect],
                            index if isinstance(index, int) else None)
        elif cmd == "dismiss":
            self._on_dismiss()
        elif cmd == "retry":
            self._on_retry()

    def _call(self, fn, *args):
        js = f"{fn}({', '.join(json.dumps(a) for a in args)})"
        if self._ready:
            self.eval(js)
        else:
            self._pending = [p for p in self._pending if not p.startswith(fn + "(")] + [js]

    def set_sub(self, html_text):
        self._call("setSub", html_text)

    def set_message(self, html_text):
        self._call("setMsg", html_text)

    def set_status(self, html_text):
        self._call("setStatus", html_text)

    def set_fullscreen(self, on):
        self._call("setFullscreen", on)

    def set_style(self, font_size, word_css="", furigana="off", translation="off",
                  pitch_display="off"):
        """Subtitle size in px, how the words of each status look (CSS from word_css, and
        pitch_css), which words show furigana (FURIGANA), how the translation shows
        (TRANSLATION_DISPLAY) and the pitch accent (PITCH_DISPLAY)."""
        self._call("setStyle", font_size, round(font_size * FULLSCREEN_SCALE), word_css,
                   furigana, translation, pitch_display)

    def set_translation(self, text):
        self._call("setTranslation", text)

    def set_badge(self, text):
        self._call("setBadge", text)

    def set_range(self, text):
        self._call("setRange", text)

    def restyle(self, classes, pitches=()):
        """Each word span's status class and pitch-accent category (see word_pitches)."""
        self._call("restyle", classes, list(pitches))

    def move_selection(self, delta):
        self._call("moveSel", delta)

    def lookup_selection(self):
        """Look the selected word up again, or the line's first word."""
        self._call("lookupSel")
