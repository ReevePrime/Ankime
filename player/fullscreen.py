"""Fullscreen: the video fills the screen, everything else is hidden, and the subtitle panel
floats over the bottom of the video. F (or a double-click on the video) toggles it, Esc
leaves it."""
from aqt.qt import QColor, Qt, QVBoxLayout, QWidget

from ..core import config
from . import sub_panel
from .popup import floating_flags

OVERLAY_MARGIN = 32   # px between the overlay and the bottom of the screen


class SubtitleOverlay(QWidget):
    """A frameless window holding the subtitle panel over the fullscreen video. It has to
    be a window: the video is a native surface (mpv's window, or the OpenGL one it renders
    into, see video.py), which no widget can be painted over."""

    def __init__(self, parent):
        super().__init__(parent, floating_flags())
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)


class FullscreenMixin:
    def _init_fullscreen(self):
        self._fullscreen = False
        self._was_maximized = False
        self._panel_bg = None      # the panel's page background, restored on the way out
        self._margins = None       # the window's layout margins

    def _build_overlay(self):
        self.overlay = SubtitleOverlay(self)

    def _apply_subtitle_style(self):
        """The subtitle size, how the words of each status look, which words show furigana,
        how the translation shows and the pitch accent, from the settings."""
        cfg = config.get()
        css = "\n".join((sub_panel.word_css(sub_panel.word_styles(cfg)),
                         sub_panel.pitch_css(cfg["pitch_colors"])))
        self.panel.set_style(cfg["subtitle_font_size"], css, cfg["furigana"],
                             cfg["translation_display"], cfg["pitch_display"])
        self._size_panel()

    def _panel_height(self):
        cfg = config.get()
        return sub_panel.panel_height(cfg["subtitle_font_size"], self._fullscreen,
                                      furigana=cfg["furigana"] != "off",
                                      translation=cfg["translation_display"] != "off",
                                      word_scale=sub_panel.word_scale(
                                          sub_panel.word_styles(cfg)))

    def _size_panel(self):
        self.panel.setFixedHeight(self._panel_height())
        self._place_overlay()

    def toggle_fullscreen(self):
        if self._fullscreen:
            self._leave_fullscreen()
        else:
            self._enter_fullscreen()

    def _enter_fullscreen(self):
        self._hide_popup()
        self._remember_panel_widths()
        self._fullscreen = True
        self._was_maximized = self.isMaximized()
        for widget in (self.controls, self.button_box, *self._side_panels().values()):
            widget.hide()
        self._margins = self.layout().contentsMargins()
        self.layout().setContentsMargins(0, 0, 0, 0)
        self.panel.setFixedHeight(self._panel_height())
        self.overlay.layout().addWidget(self.panel)  # reparents it into the overlay
        self.panel.show()  # a widget changing parents is hidden
        self._panel_bg = self.panel.page().backgroundColor()
        self.panel.page().setBackgroundColor(QColor(0, 0, 0, 0))
        self.panel.set_fullscreen(True)
        self.showFullScreen()
        self.overlay.show()
        self._place_overlay()

    def _leave_fullscreen(self, restore_window=True):
        self._hide_popup()
        self._fullscreen = False
        self.overlay.hide()
        self.panel.set_fullscreen(False)
        if self._panel_bg is not None:
            self.panel.page().setBackgroundColor(self._panel_bg)
        self.panel.setFixedHeight(self._panel_height())
        layout = self.layout()
        layout.insertWidget(layout.indexOf(self.controls) + 1, self.panel)
        self.panel.show()
        layout.setContentsMargins(self._margins)
        self.controls.show()
        self.button_box.show()
        self.playlist_box.setVisible(self.playlist_btn.isChecked())
        self.transcript.setVisible(self.transcript_btn.isChecked())
        if restore_window:
            if self._was_maximized:
                self.showMaximized()
            else:
                self.showNormal()
        self._apply_panel_widths()

    def _window_state_changed(self):
        """The window left fullscreen some other way (the system did it): follow."""
        if self._fullscreen and not self.isFullScreen() and not self.isMinimized():
            self._leave_fullscreen(restore_window=False)

    def _place_overlay(self):
        if not self._fullscreen:
            return
        area, height = self.geometry(), self._panel_height()  # global: the window is top-level
        self.overlay.setGeometry(area.left(), area.bottom() + 1 - height - OVERLAY_MARGIN,
                                 area.width(), height)
