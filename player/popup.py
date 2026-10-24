"""Floating definition popup, shown next to the hovered word like Yomitan's."""
import html
import json

from aqt.qt import QGuiApplication, QPoint, QRect, Qt, QWidget, QVBoxLayout
from aqt.webview import AnkiWebView

from ..core import config
from ..language.sanitize import clean_html
from .sub_panel import CSP, JA_FONTS

WIDTH = 440        # px, at the default text size
MAX_HEIGHT = 380   # px, at the default text size; longer entries scroll
GAP = 6            # px between the word and the popup
FONT_SIZES = (10, 40)  # px, the popup_font_size setting's range
FOLLOW_SUBTITLES = 0   # popup_font_size: in proportion to subtitle_font_size
SCREEN_SHARE = (0.9, 0.6)  # the most of its screen's width and height the popup takes

PAGE = """
<style>
  html, body { margin: 0; background: var(--canvas, #fff); overflow-x: hidden; }
  body { border: 1px solid rgba(128,128,128,.6); box-sizing: border-box; min-height: 100vh;
         font-family: $FONTS; }
  /* every size is relative to #pop's, the popup_font_size setting (15px by default) */
  #pop { padding: .55em .8em; font-size: 15px; }
  .head { display: flex; align-items: baseline; gap: .65em; flex-wrap: wrap; }
  .expr { font-size: 1.47em; font-weight: bold; }
  .rd { opacity: .75; }
  .pitch { font-size: .87em; opacity: .85; }
  .freq { margin-left: auto; font-size: .8em; opacity: .65; }
  .actions { margin: .4em 0 .15em; display: flex; gap: .4em; flex-wrap: wrap;
             align-items: center; }
  .actions button { padding: .15em .65em; cursor: pointer; font-size: .87em; }
  .actions.more { margin: .15em 0 0; }
  .actions.more button { font-size: .8em; padding: .1em .55em; }
  .dup { font-size: .8em; font-weight: bold; padding: .1em .55em; border-radius: .7em;
         background: #c2410c; color: white; }   /* 5:1 against the white text */
  .def { margin: .4em 0 .15em; }
  .def ul, .def ol { margin: .15em 0; padding-left: 1.35em; }
  .err { color: #d9534f; }
  .dim { opacity: .6; }
  hr { border: 0; border-top: 1px solid rgba(128,128,128,.4); }
</style>
<div id="pop"></div>
<script>
  document.documentElement.lang = "ja";
  const pop = document.getElementById("pop");
  function setPop(h, size) {
    pop.style.fontSize = size + "px";
    pop.innerHTML = h;
    requestAnimationFrame(() => pycmd("size:" + Math.ceil(pop.getBoundingClientRect().height)));
  }
  pop.addEventListener("click", e => {
    const b = e.target.closest("button[data-cmd]");
    if (b) pycmd(b.dataset.cmd);
  });
  document.addEventListener("keydown", e => { if (e.key === "Escape") pycmd("close"); });
</script>
""".replace("$FONTS", JA_FONTS)


def render_entries(word, entries, marked=False, duplicates=(), hotkeys=None):
    """HTML for the dictionary entries of `word`, each with a button to add a card from it
    ("add:<entry number>"). `duplicates[i]`: a card for entry i's word exists, so it offers
    to add the sentence to it ("append:<i>") instead. `hotkeys` maps actions to keys for the
    button labels: the hotkeys act on the first entry. The expression and reading are text
    and get escaped; the glossary, pitch accents and frequencies are HTML from the
    dictionaries, kept once sanitized."""
    if not entries:
        return f'<span class="dim">No entry for {html.escape(word)}</span>'
    keys = hotkeys or {}

    def button(cmd, text, action, label=None):
        key = keys.get(action)
        name = html.escape(label or "", quote=True)
        aria = f' aria-label="{name}" title="{name}"' if label else ""
        key = f" ({html.escape(key)})" if key else ""
        return f'<button data-cmd="{cmd}"{aria}>{text}{key}</button>'

    out = []
    for i, e in enumerate(entries):
        first = i == 0
        name = html.escape(e.get("expression", "") or word)
        if i < len(duplicates) and duplicates[i]:
            actions = ('<span class="dup">You already have a card</span>'
                       + button(f"append:{i}", "Add sentence to it",
                                "add_word_card" if first else "",
                                label=None if first else f"Add the sentence to your {name} card")
                       + button(f"add:{i}", "New card anyway", "",
                                label=None if first else f"New card for {name} anyway"))
        else:
            actions = button(f"add:{i}", "Add card" if first else "Add this one",
                             "add_word_card" if first else "",
                             label=None if first else f"Add a card for {name}, this meaning")
        if first:
            actions += button("known", "Unmark known" if marked else "Mark known", "mark_known")
            actions += button("audio", "🔊", "play_word_audio", label="Play the word's audio")
        actions = f'<div class="actions{"" if first else " more"}">{actions}</div>'
        pitch = clean_html(e.get("pitch-accents", ""))
        out.append(
            f'<div class="head"><span class="expr">{html.escape(e.get("expression", ""))}</span>'
            f'<span class="rd">{html.escape(e.get("reading", ""))}</span>'
            f'{f"<span class=pitch>{pitch}</span>" if pitch else ""}'
            f'<span class="freq">{clean_html(e.get("frequencies", ""))}</span></div>'
            f'{actions}<div class="def">{clean_html(e.get("glossary-brief", ""))}</div>')
    return "<hr>".join(out)


def text_size(cfg, scale=1.0):
    """Size in px of the popup's text: popup_font_size, or with FOLLOW_SUBTITLES as much
    larger than 15 as subtitle_font_size is than 28 (the defaults), times `scale` (the
    subtitles' zoom and fullscreen's enlargement, which the popup follows)."""
    defaults = config.DEFAULTS
    try:
        size = float(cfg["popup_font_size"])
        if size == FOLLOW_SUBTITLES:
            size = (float(cfg["subtitle_font_size"]) * defaults["popup_font_size"]
                    / defaults["subtitle_font_size"])
    except (TypeError, ValueError, KeyError):
        size = defaults["popup_font_size"]  # not a number in the JSON config
    low, high = FONT_SIZES
    size = min(max(size, low), high)
    try:
        scale = min(max(float(scale), 0.25), 5.0)
    except (TypeError, ValueError):
        scale = 1.0
    return round(size * scale)


def floating_flags():
    """Window flags for a frameless window floating over the player: this popup, the
    fullscreen subtitles. A tool window, except on Wayland, where the compositor places tool
    windows where it likes: a tooltip one goes where it is put, beside its parent."""
    on_wayland = QGuiApplication.platformName() == "wayland"
    kind = Qt.WindowType.ToolTip if on_wayland else Qt.WindowType.Tool
    return kind | Qt.WindowType.FramelessWindowHint


class Popup(QWidget):
    """A frameless window owned by the player. Callbacks: on_command("add:<entry>" |
    "append:<entry>" | "known" | "audio" | "close"); the owner hides it on "close" (Esc).
    `scale()`: how much larger than the settings say the text is to be now (see text_size)."""

    def __init__(self, parent, on_command, scale=None):
        super().__init__(parent, floating_flags())
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self._on_command = on_command
        self._scale_now = scale or (lambda: 1.0)
        self._anchor = None   # the word's rectangle, global coordinates
        self._ready = False
        self._pending = None  # content waiting for the page to load
        self._scale = 1.0     # the text size, relative to the default
        self.web = AnkiWebView(self, title="ankime popup")
        self.web.set_bridge_command(self._bridge, self)
        self.web.loadFinished.connect(self._loaded)
        self.web.setAcceptDrops(False)  # a dropped file would replace the page
        self.web.stdHtml(PAGE, head=CSP, context=self)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.web)
        self.resize(WIDTH, 120)

    def _loaded(self, _ok):
        self._ready = True
        if self._pending is not None:
            self._set(self._pending)
            self._pending = None

    def _set(self, html_text):
        """Show `html_text` in the popup, in the text size of the settings."""
        size = text_size(config.get(), self._scale_now())
        self._scale = size / config.DEFAULTS["popup_font_size"]
        self.resize(self._width(), self.height())  # before the page measures its height
        self.web.eval(f"setPop({json.dumps(html_text)}, {size})")

    def _area(self):
        """The available geometry of the screen the popup is (or will be) on."""
        screen = (self.screen() if self.isVisible() else self.parentWidget().screen())
        return screen.availableGeometry() if screen else QRect(0, 0, 10 ** 5, 10 ** 5)

    def _width(self):
        # the popup's own zoom (Ctrl+wheel over it) enlarges the text too
        width = WIDTH * max(self._scale * self.web.zoomFactor(), 1.0)
        return round(min(width, self._area().width() * SCREEN_SHARE[0]))

    def _bridge(self, cmd):
        if cmd.startswith("size:"):
            zoom = self.web.zoomFactor()
            height = int(float(cmd[len("size:"):]) * zoom) + 2
            most = min(MAX_HEIGHT * self._scale * zoom, self._area().height() * SCREEN_SHARE[1])
            self.resize(self._width(), min(max(height, 40), round(most)))
            self._place()
        else:
            self._on_command(cmd)

    def show_at(self, anchor, html_text):
        """Show `html_text` next to `anchor` (a QRect in global coordinates)."""
        self._anchor = anchor
        if self._ready:
            self._set(html_text)
        else:
            self._pending = html_text
        self._place()
        self.show()
        self.raise_()

    def scroll(self, pixels):
        """Scroll the entries down by `pixels` (up if negative)."""
        if self.isVisible() and self._ready:
            self.web.eval(f"window.scrollBy({{top: {int(pixels)}, behavior: 'smooth'}})")

    def update_content(self, html_text):
        if self.isVisible() and self._ready:
            self._set(html_text)

    def _place(self):
        """Above the word if there is room on its screen, otherwise below it."""
        if self._anchor is None:
            return
        a, w, h = self._anchor, self.width(), self.height()
        area = self._area()
        x = a.center().x() - w // 2
        x = max(area.left(), min(x, area.right() - w))
        y = a.top() - GAP - h
        if y < area.top():
            y = min(a.bottom() + GAP, area.bottom() - h)
        self.move(QPoint(x, y))
