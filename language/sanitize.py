"""Dictionary HTML from Yomitan (glossaries, pitch accents, frequencies) is third-party content:
a dictionary can hold anything. Before it is shown in the popup or written to a card it is
rebuilt from an allowlist of tags and attributes, so it can't run script, load remote
content or navigate. Pure logic."""
import html
import re
from html.parser import HTMLParser

ALLOWED_TAGS = {
    "a", "b", "br", "code", "dd", "details", "div", "dl", "dt", "em", "hr", "i", "img", "li",
    "ol", "p", "rp", "rt", "ruby", "s", "small", "span", "strong", "sub", "summary", "sup",
    "table", "tbody", "td", "tfoot", "th", "thead", "tr", "u", "ul",
}
VOID_TAGS = {"br", "hr", "img"}
# Dropped with everything inside them, not just the tags.
DROP_CONTENT = {"script", "style", "iframe", "object", "embed", "template", "noscript",
                "svg", "math", "head", "title", "textarea", "select"}
ALLOWED_ATTRS = {"class", "lang", "title", "style", "colspan", "rowspan", "open", "alt",
                 "width", "height", "src", "href"}
# Card fields have no Content-Security-Policy, so a style must not be able to load anything:
# url(), image-set() with a bare string, image(), cross-fade()... CSS functions are therefore
# allowlisted (colours, lengths, transforms) rather than blocklisted, and a backslash, which
# can spell any of them as an escape ("\75 rl(" is url(), is refused outright.
_STYLE_FUNCTIONS = {
    "rgb", "rgba", "hsl", "hsla", "hwb", "lab", "lch", "oklab", "oklch", "color", "calc",
    "var", "min", "max", "clamp", "translate", "translatex", "translatey", "rotate", "scale",
    "scalex", "scaley", "skew", "skewx", "skewy",
}
_FUNCTION = re.compile(r"([-\w]+)\s*\(")
_UNSAFE_STYLE = re.compile(r"\\|javascript:|expression|behavior|@import", re.I)

# Declarations dropped from a style that is otherwise kept: they would let an entry leave its
# place and lie over the popup's buttons or the reviewer's (an invisible layer as big as the
# window catching every click).
_DROPPED_PROPERTIES = {"position", "z-index"}


def _safe_style(value):
    if _UNSAFE_STYLE.search(value):
        return False
    # "(" right after anything that isn't a word ("x:(…)") isn't a function: only allowed
    # functions may open one
    if value.count("(") != len(_FUNCTION.findall(value)):
        return False
    return all(name.lower() in _STYLE_FUNCTIONS for name in _FUNCTION.findall(value))


def _clean_style(value):
    """`value` without the declarations of _DROPPED_PROPERTIES, or "" if it isn't safe or
    nothing is left."""
    if not _safe_style(value):
        return ""
    kept = [d for d in value.split(";")
            if d.split(":", 1)[0].strip().lower() not in _DROPPED_PROPERTIES]
    value = ";".join(kept)
    return value if value.strip(" ;") else ""
# A media file in the collection: a plain name, no scheme, no path out of the folder.
_LOCAL_FILE = re.compile(r"^[^:/\\?#][^:\\?#]*$")


def _safe_src(value):
    value = value.strip()
    return value.startswith("data:image/") or (_LOCAL_FILE.match(value) and ".." not in value)


class _Sanitizer(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out = []
        self._open = []   # allowed tags open, to close what the source left open
        self._drop = 0    # depth inside a DROP_CONTENT tag

    def handle_starttag(self, tag, attrs):
        if tag in DROP_CONTENT:
            self._drop += 1
            return
        if self._drop or tag not in ALLOWED_TAGS:
            return
        kept = []
        for name, value in attrs:
            name, value = name.lower(), value or ""
            if name.startswith("data-"):
                pass  # inert: Yomitan's templates use them for styling
            elif name not in ALLOWED_ATTRS:
                continue
            elif name == "style" and not (value := _clean_style(value)):
                continue
            elif name == "src" and not (tag == "img" and _safe_src(value)):
                continue
            elif name == "href":
                continue  # links would navigate the popup or open things: keep the text
            kept.append(f' {name}="{html.escape(value, quote=True)}"')
        self.out.append(f"<{tag}{''.join(kept)}>")
        if tag not in VOID_TAGS:
            self._open.append(tag)

    def handle_startendtag(self, tag, attrs):
        if tag in DROP_CONTENT:
            return  # <svg/>: nothing inside it to drop, and no end tag to stop dropping
        self.handle_starttag(tag, attrs)
        if tag in self._open[-1:] and tag not in VOID_TAGS:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if tag in DROP_CONTENT:
            self._drop = max(0, self._drop - 1)
            return
        if self._drop or tag not in self._open:
            return
        while self._open:  # close the tags left open inside it
            inner = self._open.pop()
            self.out.append(f"</{inner}>")
            if inner == tag:
                break

    def handle_data(self, data):
        if not self._drop:
            self.out.append(html.escape(data, quote=False))

    def close(self):
        super().close()
        self.out.extend(f"</{tag}>" for tag in reversed(self._open))
        self._open = []


def clean_html(text):
    """`text` with only allowlisted tags and attributes left; text content is kept."""
    if not text:
        return ""
    parser = _Sanitizer()
    parser.feed(str(text))
    parser.close()
    return "".join(parser.out)
