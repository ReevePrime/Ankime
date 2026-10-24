"""The video surface. By default mpv draws each frame into an OpenGL window Qt made, through
mpv's render API (libmpv/render.h): Qt owns the surface, so it works wherever Qt does, Wayland
and macOS included. The older way, mpv drawing into a native window of its own by handle (wid),
is kept as a fallback: the video_output setting, see mpv_controller.video_output.

The OpenGL surface is a window in a window container rather than a QOpenGLWidget: Qt
composites all the widgets of a window with one graphics API, and Anki sets that to Direct3D on
Windows and Metal on macOS for its web views (the subtitle panel is one). A QOpenGLWidget
beside them isn't drawn; a window container has a surface of its own."""
import sys

from aqt.qt import (QEvent, QOffscreenSurface, QOpenGLContext, QSurface, QSurfaceFormat, Qt,
                    QTimer, QVBoxLayout, QWidget, QWindow, pyqtSignal)

from ..core.model import SetupError

CURSOR_HIDE_MS = 1000  # the pointer still over the video this long is hidden, as mpv does


class NativeVideo(QWidget):
    """The fallback: a native window mpv is given the handle of, and draws into itself. Mouse
    and keys over it go to mpv, which has the same bindings (mpv_controller)."""

    native = True

    def __init__(self):
        super().__init__()
        self.setAttribute(Qt.WidgetAttribute.WA_NativeWindow)
        self.setStyleSheet("background: black;")

    def attach(self, player):
        pass  # mpv was given winId() when it was created

    def release(self):
        pass


class GLVideo(QWidget):
    """Holds the OpenGL window mpv renders into, which lets the mouse through to this widget:
    mpv sees no mouse here, so the wheel and double-click are passed on as mpv keys (the same
    bindings apply as in a native window of mpv's own), and the pointer is hidden after a
    second without moving, as mpv would."""

    native = False

    def __init__(self):
        super().__init__()
        self._player = None  # MpvController
        self.setStyleSheet("background: black;")
        self.setMouseTracking(True)
        self.surface = _MpvWindow()
        container = QWidget.createWindowContainer(self.surface, self)
        container.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        container.setMouseTracking(True)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(container)
        self._cursor_timer = QTimer(self)
        self._cursor_timer.setSingleShot(True)
        self._cursor_timer.setInterval(CURSOR_HIDE_MS)
        self._cursor_timer.timeout.connect(lambda: self.setCursor(Qt.CursorShape.BlankCursor))

    def attach(self, player):
        """Draw `player`'s video (an MpvController made without a window handle). Raises
        SetupError if mpv can't draw here."""
        self.surface.attach(player)
        self._player = player

    def release(self):
        """Free mpv's render context: before mpv is terminated, which it can't outlive."""
        self._player = None
        self.surface.release()

    def wheelEvent(self, event):
        delta = event.angleDelta().y()
        if delta and self._player is not None:
            self._player.press("WHEEL_UP" if delta > 0 else "WHEEL_DOWN")
        event.accept()

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self._player is not None:
            self._player.press("MBTN_LEFT_DBL")
        event.accept()

    def mouseMoveEvent(self, event):
        self.unsetCursor()
        self._cursor_timer.start()
        super().mouseMoveEvent(event)

    def leaveEvent(self, event):
        self._cursor_timer.stop()
        self.unsetCursor()
        super().leaveEvent(event)


class _MpvWindow(QWindow):
    """mpv renders into this window, on the main thread, when it says a frame is ready. Its
    GL context is made when mpv is attached, before anything plays: with vo=libmpv, a file
    loaded before the render context exists plays without its video. The mouse goes through
    the window, to the GLVideo below (and drops to the player window): it never takes the
    keyboard from the player's hotkeys."""

    _frame_ready = pyqtSignal()  # from mpv's threads

    def __init__(self):
        super().__init__()
        self.setSurfaceType(QSurface.SurfaceType.OpenGLSurface)
        self.setFlag(Qt.WindowType.WindowTransparentForInput)
        self._gl = None            # QOpenGLContext, mpv's and this window's
        self._offscreen = None     # to make it current before the window is shown
        self._ctx = None           # mpv's MpvRenderContext
        self._proc_address = None  # the ctypes callback, kept alive as long as the context
        self._frame_ready.connect(self._on_frame_ready, Qt.ConnectionType.QueuedConnection)

    def attach(self, player):
        gl = QOpenGLContext(self)
        if sys.platform == "darwin":  # macOS gives OpenGL 2.1 unless a core profile is asked for
            fmt = QSurfaceFormat.defaultFormat()
            fmt.setVersion(3, 2)
            fmt.setProfile(QSurfaceFormat.OpenGLContextProfile.CoreProfile)
            gl.setFormat(fmt)
        if not gl.create():
            raise SetupError("mpv can't draw the video: no OpenGL context could be made")
        self.setFormat(gl.format())
        self._offscreen = QOffscreenSurface()
        self._offscreen.setFormat(gl.format())
        self._offscreen.create()
        if not gl.makeCurrent(self._offscreen):
            raise SetupError("mpv can't draw the video: the OpenGL context can't be used")
        module = player.mpv_module
        self._proc_address = module.MpvGlGetProcAddressFn(_get_proc_address)
        try:
            self._ctx = module.MpvRenderContext(
                player.mpv, "opengl", opengl_init_params={"get_proc_address": self._proc_address})
        except Exception as e:
            raise SetupError(f"mpv can't draw the video with OpenGL: {e}") from e
        finally:
            gl.doneCurrent()
        self._gl = gl
        self._ctx.update_cb = self._frame_ready.emit

    def release(self):
        ctx, self._ctx = self._ctx, None
        if ctx is not None:
            ctx.update_cb = None
            self._gl.makeCurrent(self._offscreen)  # mpv frees its GL objects in its context
            ctx.free()
            self._gl.doneCurrent()
        if self._offscreen is not None:
            self._offscreen.destroy()

    def _on_frame_ready(self):
        if self._ctx is not None and self._ctx.update():
            self.requestUpdate()

    def event(self, event):
        if event.type() == QEvent.Type.UpdateRequest:
            self._render()
            return True
        return super().event(event)

    def exposeEvent(self, event):
        self._render()

    def resizeEvent(self, event):
        self.requestUpdate()

    def _render(self):
        if self._ctx is None or not self.isExposed() or not self._gl.makeCurrent(self):
            return
        ratio = self.devicePixelRatio()
        self._ctx.render(flip_y=True, opengl_fbo={
            "w": round(self.width() * ratio), "h": round(self.height() * ratio),
            "fbo": self._gl.defaultFramebufferObject()})
        self._gl.swapBuffers(self)
        self._ctx.report_swap()  # helps mpv time the frames to the display


def _get_proc_address(_ctx, name):
    """mpv's get_proc_address: Qt finds GL functions on every platform (wglGetProcAddress,
    for one, doesn't give the GL 1.1 ones)."""
    ctx = QOpenGLContext.currentContext()
    address = ctx.getProcAddress(name) if ctx is not None else None
    return int(address) if address is not None else 0


def create(output):
    """The video widget for `output` ("opengl" or "window", see
    mpv_controller.video_output)."""
    return GLVideo() if output == "opengl" else NativeVideo()
