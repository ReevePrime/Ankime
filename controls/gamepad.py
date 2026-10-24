"""Gamepad bindings: a button (or two held together, "LB+A") runs a player action, like a
hotkey. Buttons have Xbox names ("A", "LB", "LS Left" for the left stick pushed left, "Up"
for the d-pad); a PlayStation controller's are shown with its own symbols. A controller
that is neither gets "Button 5" names, bound the same way from the settings.

Qt has no gamepad module in Anki's Qt 6, so the controllers are read with what the system
has: XInput, then winmm for the others (a PlayStation controller without Steam or
DS4Windows) on Windows, the joystick devices on Linux, SDL2 on macOS if it is installed
(`brew install sdl2`). Each is polled from a timer on the main thread (see
player/gamepad.py); nothing here needs Qt."""
import ctypes
import logging
import os
import re
import struct
import sys

from ..core import config
from . import hotkeys

log = logging.getLogger(__name__)

# What a binding can run: the hotkey actions, and Esc's (close the popup, leave fullscreen),
# which has no hotkey setting of its own.
ACTIONS = {**hotkeys.ACTIONS, "escape": "Close the definition / leave fullscreen"}
# Held down, these run again and again: moving through the words, scrolling, the volume.
REPEATING = {"word_previous", "word_next", "popup_scroll_up", "popup_scroll_down",
             "volume_down", "volume_up", "sub_earlier", "sub_later"}
REPEAT_DELAY = 0.4      # s before a held button repeats
REPEAT_INTERVAL = 0.12  # s between repeats
SCAN_INTERVAL = 3.0     # s between looks for a controller while none is connected
STICK_PRESS = 0.55      # how far a stick goes (0..1) to count as pushed that way
STICK_RELEASE = 0.35    # and back to count as let go: no flicker in between
TRIGGER_PRESS = 0.5
TRIGGER_RELEASE = 0.3

STICK_DIRECTIONS = ("Up", "Down", "Left", "Right")
BUTTONS = (["A", "B", "X", "Y", "LB", "RB", "LT", "RT", "Back", "Start", "Guide", "LS", "RS"]
           + list(STICK_DIRECTIONS)
           + [f"{stick} {d}" for stick in ("LS", "RS") for d in STICK_DIRECTIONS]
           + ["Touchpad"])
_ARROWS = {"Up": "↑", "Down": "↓", "Left": "←", "Right": "→"}
# kind of controller -> its names for the buttons that differ from the Xbox ones
_LABELS = {
    "xbox": {"Back": "View", "Start": "Menu", "Guide": "Xbox"},
    "playstation": {"A": "✕", "B": "○", "X": "□", "Y": "△", "LB": "L1", "RB": "R1",
                    "LT": "L2", "RT": "R2", "Back": "Share", "Start": "Options", "Guide": "PS",
                    "LS": "L3", "RS": "R3"},
}
_STICK_NAMES = {"xbox": ("LS", "RS"), "playstation": ("L3", "R3")}


def label(button, kind="xbox"):
    """What a controller of `kind` calls `button`: "A" is "✕" on a PlayStation one; the
    d-pad and the sticks get arrows ("D-pad ↑", "LS ←")."""
    names = _LABELS.get(kind, _LABELS["xbox"])
    if button in _ARROWS:
        return f"D-pad {_ARROWS[button]}"
    stick, _, direction = button.partition(" ")
    if direction in _ARROWS:
        left, right = _STICK_NAMES.get(kind, _STICK_NAMES["xbox"])
        return f"{left if stick == 'LS' else right} {_ARROWS[direction]}"
    return names.get(button, button)


def combo_label(combo, kind="xbox"):
    """"LB+A" -> "LB + A" ("L1 + ✕" on a PlayStation controller); "" stays ""."""
    return " + ".join(label(b, kind) for b in parse(combo))


def parse(combo):
    """"LB+A" -> ("LB", "A"), "A" -> ("A",), "" -> (). Button names have no "+"."""
    return tuple(part.strip() for part in combo.split("+") if part.strip()) if combo else ()


def get(cfg=None):
    """action -> button or "LB+A" combination ("" = unbound)."""
    buttons = (cfg or config.get())["gamepad_buttons"]
    return {action: buttons.get(action, "") for action in ACTIONS}


# --- from buttons pressed to actions ----------------------------------------------------

class Mapper:
    """Turns what is held on the controller, read again and again, into actions. A button
    runs its action when pressed, again and again while held if it is a REPEATING one. A
    combination ("LB+A") runs when its second button is pressed while the first is held;
    the first, a modifier, then runs its own action (if any) when let go, and only if no
    combination was made while it was held."""

    def __init__(self, bindings, repeating=REPEATING):
        self._plain = {}   # button -> action
        self._chords = {}  # (modifier, button) -> action
        for action, combo in bindings.items():
            buttons = parse(combo)
            if len(buttons) == 1:
                self._plain[buttons[0]] = action
            elif len(buttons) == 2:
                self._chords[buttons] = action
        self._modifiers = {modifier for modifier, _ in self._chords}
        self._repeating = repeating
        self._held = frozenset()
        self._used = set()   # modifiers held for a combination: they don't run on release
        self._repeat = None  # [button, action, when it runs next]

    def reset(self, pressed=frozenset()):
        """Forget what was going on: the player lost focus. Buttons held now do nothing
        until let go and pressed again."""
        self._held = frozenset(pressed)
        self._used = set(pressed) & self._modifiers
        self._repeat = None

    def update(self, pressed, now):
        """What is held now (a set of button names), at time `now` (seconds): the actions
        to run, in order."""
        pressed = frozenset(pressed)
        fired = []
        # modifiers first, for a combination pressed all at once
        new = sorted(pressed - self._held, key=lambda b: (b not in self._modifiers, b))
        down = set(self._held & pressed)
        for button in new:
            modifier = next((m for m in sorted(down) if (m, button) in self._chords), None)
            if modifier is not None:
                self._used.add(modifier)
                self._press(button, self._chords[modifier, button], now, fired)
            elif button not in self._modifiers and button in self._plain:
                self._press(button, self._plain[button], now, fired)
            down.add(button)
        for button in sorted(self._held - pressed):
            if button in self._modifiers:
                if button not in self._used and button in self._plain:
                    fired.append(self._plain[button])
                self._used.discard(button)
            if self._repeat and self._repeat[0] == button:
                self._repeat = None
        if self._repeat and now >= self._repeat[2] and self._repeat[0] in pressed:
            fired.append(self._repeat[1])
            self._repeat[2] = now + REPEAT_INTERVAL
        self._held = pressed
        return fired

    def _press(self, button, action, now, fired):
        fired.append(action)
        if action in self._repeating:
            self._repeat = [button, action, now + REPEAT_DELAY]


class Capture:
    """Records a binding in the settings: one button, or two ("LB+A": the first held while
    the second is pressed). Done once everything is let go."""

    def __init__(self, held=frozenset()):
        self._ignore = set(held)  # held when the capture began: not part of it
        self._order = []

    def feed(self, pressed):
        """What is held now. The binding once it is complete, else None."""
        self._ignore &= set(pressed)
        for button in sorted(set(pressed) - self._ignore):
            if button not in self._order:
                self._order.append(button)
        if self._order and not set(pressed) - self._ignore:
            return "+".join(self._order[:2])
        return None


# --- turning axes into buttons ----------------------------------------------------------

def stick_direction(prefix, x, y, held):
    """The way a stick at (x, y) (-1..1 each, y down) is pushed, as a button name ("LS Left")
    or None. One way at a time, the strongest; `held` (the buttons held at the last read)
    keeps a direction until the stick is mostly back, so it doesn't flicker."""
    amounts = {"Left": -x, "Right": x, "Up": -y, "Down": y}
    strongest = max(amounts.values())
    for direction, amount in amounts.items():
        if f"{prefix} {direction}" in held and amount > STICK_RELEASE \
                and amount >= 0.8 * strongest:
            return f"{prefix} {direction}"
    direction = max(amounts, key=amounts.get)
    return f"{prefix} {direction}" if amounts[direction] > STICK_PRESS else None


def axes_to_buttons(axes, held):
    """Buttons for the analog parts: "LX" / "LY" / "RX" / "RY" (-1..1, y down) are the
    sticks, "LT" / "RT" (0..1) the triggers. Missing axes are skipped."""
    out = set()
    for prefix, x, y in (("LS", "LX", "LY"), ("RS", "RX", "RY")):
        if x in axes and y in axes:
            direction = stick_direction(prefix, axes[x], axes[y], held)
            if direction:
                out.add(direction)
    for trigger in ("LT", "RT"):
        if trigger in axes:
            limit = TRIGGER_RELEASE if trigger in held else TRIGGER_PRESS
            if axes[trigger] > limit:
                out.add(trigger)
    return out


def hat_to_buttons(degrees):
    """A d-pad reported as an angle (0 up, 90 right, clockwise; None centred): its arrows,
    two of them on a diagonal."""
    if degrees is None:
        return set()
    out = set()
    for name, centre in (("Up", 0), ("Right", 90), ("Down", 180), ("Left", 270)):
        if abs((degrees - centre + 180) % 360 - 180) < 67.5:
            out.add(name)
    return out


def _unit(value, low, high):
    """`value` in low..high -> -1..1."""
    return 0.0 if high <= low else max(-1.0, min(1.0, (value - low) * 2 / (high - low) - 1))


def _is_playstation(name):
    return bool(re.search(r"sony|playstation|dualsense|dualshock|wireless controller", name,
                          re.IGNORECASE))


class Pad:
    """One controller. Subclasses read it (_read); poll() turns that into button names."""
    name = "Gamepad"
    kind = "generic"   # "xbox" | "playstation" | "generic": which names to show

    def __init__(self):
        self._held = frozenset()

    def poll(self):
        """The buttons held, or None once the controller is gone."""
        state = self._read()
        if state is None:
            return None
        buttons, axes = state
        self._held = frozenset(set(buttons) | axes_to_buttons(axes, self._held))
        return self._held

    def _read(self):
        """(set of buttons, {axis: value}) or None if disconnected."""
        raise NotImplementedError

    def close(self):
        pass


# --- Windows: XInput ---------------------------------------------------------------------

# XINPUT_GAMEPAD_* bit -> button
XINPUT_BUTTONS = {0x0001: "Up", 0x0002: "Down", 0x0004: "Left", 0x0008: "Right",
                  0x0010: "Start", 0x0020: "Back", 0x0040: "LS", 0x0080: "RS",
                  0x0100: "LB", 0x0200: "RB", 0x1000: "A", 0x2000: "B", 0x4000: "X",
                  0x8000: "Y"}


class _XInputGamepad(ctypes.Structure):
    _fields_ = [("buttons", ctypes.c_ushort), ("left_trigger", ctypes.c_ubyte),
                ("right_trigger", ctypes.c_ubyte), ("lx", ctypes.c_short),
                ("ly", ctypes.c_short), ("rx", ctypes.c_short), ("ry", ctypes.c_short)]


class _XInputState(ctypes.Structure):
    _fields_ = [("packet", ctypes.c_uint32), ("pad", _XInputGamepad)]


def xinput_state(gp):
    """An XINPUT_GAMEPAD -> (buttons, axes). XInput's y goes up."""
    buttons = {name for bit, name in XINPUT_BUTTONS.items() if gp.buttons & bit}
    axes = {"LX": _unit(gp.lx, -32768, 32767), "LY": -_unit(gp.ly, -32768, 32767),
            "RX": _unit(gp.rx, -32768, 32767), "RY": -_unit(gp.ry, -32768, 32767),
            "LT": gp.left_trigger / 255, "RT": gp.right_trigger / 255}
    return buttons, axes


class XInputPad(Pad):
    kind = "xbox"

    def __init__(self, dll, index):
        super().__init__()
        self._dll, self._index = dll, index
        self.name = "Xbox controller" + (f" {index + 1}" if index else "")

    def _read(self):
        state = _XInputState()
        if self._dll.XInputGetState(self._index, ctypes.byref(state)) != 0:
            return None
        return xinput_state(state.pad)


class XInput:
    """Xbox controllers, and any that Steam or DS4Windows present as one."""

    def __init__(self):
        self._dll = None
        for name in ("xinput1_4", "xinput1_3", "xinput9_1_0"):
            try:
                self._dll = ctypes.WinDLL(name)
                break
            except OSError:
                continue

    def scan(self):
        if self._dll is None:
            return []
        pads = []
        for index in range(4):
            pad = XInputPad(self._dll, index)
            if pad.poll() is not None:
                pads.append(pad)
        return pads


# --- Windows: winmm, for the controllers XInput doesn't know --------------------------------

JOY_RETURNALL = 0xFF
JOYCAPS_HASZ, JOYCAPS_HASR, JOYCAPS_HASPOV = 0x01, 0x02, 0x10
SONY = 0x054C
SONY_NAMES = {0x05C4: "DualShock 4", 0x09CC: "DualShock 4", 0x0BA0: "DualShock 4",
              0x0CE6: "DualSense", 0x0DF2: "DualSense Edge"}
# A PlayStation controller's buttons in DirectInput, by number from 0: square, cross,
# circle, triangle, L1, R1, L2, R2, Share / Create, Options, L3, R3, PS, touchpad.
SONY_BUTTONS = ["X", "A", "B", "Y", "LB", "RB", "LT", "RT", "Back", "Start", "LS", "RS",
                "Guide", "Touchpad"]


class _JoyInfoEx(ctypes.Structure):
    _fields_ = [(name, ctypes.c_uint32) for name in (
        "size", "flags", "x", "y", "z", "r", "u", "v", "buttons", "button_number", "pov",
        "reserved1", "reserved2")]


class _JoyCaps(ctypes.Structure):
    _fields_ = ([("mid", ctypes.c_ushort), ("pid", ctypes.c_ushort),
                 ("pname", ctypes.c_wchar * 32)]
                + [(name, ctypes.c_uint) for name in (
                    "xmin", "xmax", "ymin", "ymax", "zmin", "zmax", "num_buttons",
                    "period_min", "period_max", "rmin", "rmax", "umin", "umax", "vmin", "vmax",
                    "caps", "max_axes", "num_axes", "max_buttons")]
                + [("regkey", ctypes.c_wchar * 32), ("oem_vxd", ctypes.c_wchar * 260)])


def winmm_state(info, caps, button_names):
    """A JOYINFOEX -> (buttons, axes). X / Y is the left stick, Z / R the right one (on
    PlayStation controllers and most others); the d-pad is the point-of-view hat. Buttons
    without a name in `button_names` are "Button <n>", from 1 like Windows numbers them."""
    buttons = {button_names[i] if i < len(button_names) else f"Button {i + 1}"
               for i in range(32) if info.buttons >> i & 1}
    axes = {"LX": _unit(info.x, caps.xmin, caps.xmax), "LY": _unit(info.y, caps.ymin, caps.ymax)}
    if caps.caps & JOYCAPS_HASZ and caps.caps & JOYCAPS_HASR:
        axes["RX"] = _unit(info.z, caps.zmin, caps.zmax)
        axes["RY"] = _unit(info.r, caps.rmin, caps.rmax)
    if caps.caps & JOYCAPS_HASPOV and info.pov <= 36000:
        buttons |= hat_to_buttons(info.pov / 100)
    return buttons, axes


class WinmmPad(Pad):
    def __init__(self, dll, index, caps):
        super().__init__()
        self._dll, self._index, self._caps = dll, index, caps
        self._info = _JoyInfoEx(size=ctypes.sizeof(_JoyInfoEx), flags=JOY_RETURNALL)
        if caps.mid == SONY:
            self.kind, self._names = "playstation", SONY_BUTTONS
            self.name = SONY_NAMES.get(caps.pid, "PlayStation controller")
        else:
            self._names = []
            self.name = f"Controller {caps.mid:04x}:{caps.pid:04x}"

    def _read(self):
        if self._dll.joyGetPosEx(self._index, ctypes.byref(self._info)) != 0:
            return None
        return winmm_state(self._info, self._caps, self._names)


class Winmm:
    """The other controllers: DirectInput ones, like a PlayStation controller on its own."""

    def __init__(self):
        try:
            self._dll = ctypes.WinDLL("winmm")
        except OSError:
            self._dll = None

    def scan(self):
        if self._dll is None:
            return []
        pads = []
        info = _JoyInfoEx(size=ctypes.sizeof(_JoyInfoEx), flags=JOY_RETURNALL)
        for index in range(min(self._dll.joyGetNumDevs(), 16)):
            if self._dll.joyGetPosEx(index, ctypes.byref(info)) != 0:
                continue
            caps = _JoyCaps()
            if self._dll.joyGetDevCapsW(index, ctypes.byref(caps), ctypes.sizeof(caps)) == 0:
                pads.append(WinmmPad(self._dll, index, caps))
        return pads


# --- Linux: /dev/input/js* -----------------------------------------------------------------

JS_EVENT_BUTTON, JS_EVENT_AXIS, JS_EVENT_INIT = 0x01, 0x02, 0x80
_JS_EVENT = struct.Struct("IhBB")  # time, value, type, number
KEY_MAX, BTN_MISC, ABS_CNT = 0x2FF, 0x100, 0x40
# evdev key code -> button. 0x133 / 0x134 are BTN_X / BTN_Y: the Xbox driver (xpad) gives
# its X and Y buttons those codes, the PlayStation drivers the top and left buttons (the
# same codes, named BTN_NORTH / BTN_WEST): see joydev_buttons.
EVDEV_BUTTONS = {0x130: "A", 0x131: "B", 0x133: "X", 0x134: "Y", 0x136: "LB", 0x137: "RB",
                 0x138: "LT", 0x139: "RT", 0x13A: "Back", 0x13B: "Start", 0x13C: "Guide",
                 0x13D: "LS", 0x13E: "RS", 0x220: "Up", 0x221: "Down", 0x222: "Left",
                 0x223: "Right"}
# evdev axis code -> axis; 0x10 / 0x11 (the hat) are the d-pad
EVDEV_AXES = {0x00: "LX", 0x01: "LY", 0x02: "LT", 0x03: "RX", 0x04: "RY", 0x05: "RT",
              0x10: "HX", 0x11: "HY"}


def _ioc_read(nr, size):
    """Linux's _IOR('j', nr, size)."""
    return (2 << 30) | (size << 16) | (ord("j") << 8) | nr


def joydev_buttons(codes, playstation):
    """Button names for a joystick device's button numbers, from their evdev key codes."""
    names = dict(EVDEV_BUTTONS)
    if playstation:
        names[0x133], names[0x134] = "Y", "X"  # triangle on top, square on the left
    return [names.get(code, f"Button {i + 1}") for i, code in enumerate(codes)]


class JoydevPad(Pad):
    def __init__(self, path):
        super().__init__()
        import fcntl
        self._fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        try:
            name = bytearray(128)
            fcntl.ioctl(self._fd, _ioc_read(0x13, len(name)), name)
            self.name = name.split(b"\0")[0].decode("utf-8", "replace") or "Gamepad"
            counts = bytearray(1)
            fcntl.ioctl(self._fd, _ioc_read(0x12, 1), counts)  # JSIOCGBUTTONS
            buttons = counts[0]
            fcntl.ioctl(self._fd, _ioc_read(0x11, 1), counts)  # JSIOCGAXES
            axes = counts[0]
            btn_map = bytearray(2 * (KEY_MAX - BTN_MISC + 1))
            fcntl.ioctl(self._fd, _ioc_read(0x34, len(btn_map)), btn_map)  # JSIOCGBTNMAP
            ax_map = bytearray(ABS_CNT)
            fcntl.ioctl(self._fd, _ioc_read(0x32, len(ax_map)), ax_map)  # JSIOCGAXMAP
        except OSError:
            os.close(self._fd)
            raise
        playstation = _is_playstation(self.name)
        self.kind = "playstation" if playstation else (
            "xbox" if re.search(r"x-?box|xinput", self.name, re.IGNORECASE) else "generic")
        codes = struct.unpack(f"{len(btn_map) // 2}H", btn_map)[:buttons]
        self._button_names = joydev_buttons(codes, playstation)
        self._axis_names = [EVDEV_AXES.get(code) for code in ax_map[:axes]]
        self._buttons = set()
        self._axes = {}

    def apply(self, data):
        """Take in joystick events (a multiple of 8 bytes)."""
        for _time, value, kind, number in _JS_EVENT.iter_unpack(data):
            kind &= ~JS_EVENT_INIT
            if kind == JS_EVENT_BUTTON and number < len(self._button_names):
                name = self._button_names[number]
                (self._buttons.add if value else self._buttons.discard)(name)
            elif kind == JS_EVENT_AXIS and number < len(self._axis_names):
                axis = self._axis_names[number]
                if axis:
                    self._axes[axis] = value / 32767

    def _read(self):
        while True:
            try:
                data = os.read(self._fd, 64 * _JS_EVENT.size)
            except BlockingIOError:
                break
            except OSError:
                return None  # unplugged
            if not data:
                break
            self.apply(data[:len(data) - len(data) % _JS_EVENT.size])
        axes = {name: value for name, value in self._axes.items() if name in (
            "LX", "LY", "RX", "RY")}
        for trigger in ("LT", "RT"):  # -1 let go .. 1 pulled in
            if trigger in self._axes:
                axes[trigger] = (self._axes[trigger] + 1) / 2
        buttons = set(self._buttons)
        x, y = self._axes.get("HX", 0), self._axes.get("HY", 0)
        buttons |= {name for name, on in (("Left", x < -0.5), ("Right", x > 0.5),
                                          ("Up", y < -0.5), ("Down", y > 0.5)) if on}
        return buttons, axes

    def close(self):
        try:
            os.close(self._fd)
        except OSError:
            pass


class Joydev:
    def __init__(self):
        self._open = {}  # path -> JoydevPad, so a rescan doesn't open one twice

    def scan(self):
        pads = []
        try:
            paths = sorted(f"/dev/input/{n}" for n in os.listdir("/dev/input")
                           if re.fullmatch(r"js\d+", n))
        except OSError:
            return []
        for path in paths:
            pad = self._open.get(path)
            if pad is None or pad.poll() is None:
                if pad is not None:
                    pad.close()
                try:
                    pad = self._open[path] = JoydevPad(path)
                except OSError:  # no permission to read it, or just unplugged
                    self._open.pop(path, None)
                    continue
            pads.append(pad)
        return pads


# --- macOS (and anywhere else it's found): SDL2 ---------------------------------------------

SDL_INIT_GAMECONTROLLER = 0x2000
SDL_BUTTONS = ["A", "B", "X", "Y", "Back", "Guide", "Start", "LS", "RS", "LB", "RB",
               "Up", "Down", "Left", "Right"]
SDL_PLAYSTATION_TYPES = {3, 4, 7}  # SDL_CONTROLLER_TYPE_PS3, _PS4, _PS5
_SDL_PATHS = ["/opt/homebrew/lib/libSDL2.dylib", "/usr/local/lib/libSDL2.dylib",
              "libSDL2-2.0.so.0"]


class SdlPad(Pad):
    def __init__(self, sdl, controller):
        super().__init__()
        self._sdl, self._gc = sdl, controller
        name = sdl.SDL_GameControllerName(controller)
        self.name = name.decode("utf-8", "replace") if name else "Gamepad"
        try:
            playstation = sdl.SDL_GameControllerGetType(controller) in SDL_PLAYSTATION_TYPES
        except AttributeError:  # SDL before 2.0.12
            playstation = _is_playstation(self.name)
        self.kind = "playstation" if playstation else "xbox"

    def _read(self):
        sdl, gc = self._sdl, self._gc
        if not gc or not sdl.SDL_GameControllerGetAttached(gc):
            return None
        buttons = {name for i, name in enumerate(SDL_BUTTONS)
                   if sdl.SDL_GameControllerGetButton(gc, i)}
        axis = [sdl.SDL_GameControllerGetAxis(gc, i) / 32767 for i in range(6)]
        return buttons, {"LX": axis[0], "LY": axis[1], "RX": axis[2], "RY": axis[3],
                         "LT": axis[4], "RT": axis[5]}

    def close(self):
        if self._gc:  # closed when gone, and again when looking for controllers
            self._sdl.SDL_GameControllerClose(self._gc)
            self._gc = None


class Sdl:
    main_thread = True  # SDL is read, and looked through, on the thread it started on

    def __init__(self):
        self._sdl = None
        import ctypes.util
        for path in [*_SDL_PATHS, ctypes.util.find_library("SDL2")]:
            if not path:
                continue
            try:
                sdl = ctypes.CDLL(path)
            except OSError:
                continue
            sdl.SDL_GameControllerOpen.restype = ctypes.c_void_p
            sdl.SDL_GameControllerName.restype = ctypes.c_char_p
            sdl.SDL_GameControllerName.argtypes = [ctypes.c_void_p]
            for fn in ("SDL_GameControllerGetAttached", "SDL_GameControllerClose",
                       "SDL_GameControllerGetType"):
                if hasattr(sdl, fn):
                    getattr(sdl, fn).argtypes = [ctypes.c_void_p]
            sdl.SDL_GameControllerGetButton.argtypes = [ctypes.c_void_p, ctypes.c_int]
            sdl.SDL_GameControllerGetAxis.argtypes = [ctypes.c_void_p, ctypes.c_int]
            sdl.SDL_GameControllerGetAxis.restype = ctypes.c_int16
            # read the controllers while the focus is in Qt's windows, not SDL's (it has none)
            sdl.SDL_SetHint(b"SDL_JOYSTICK_ALLOW_BACKGROUND_EVENTS", b"1")
            if sdl.SDL_Init(SDL_INIT_GAMECONTROLLER) == 0:
                self._sdl = sdl
                break
        self._pads = []

    def update(self):
        if self._sdl is not None:
            self._sdl.SDL_GameControllerUpdate()

    def scan(self):
        if self._sdl is None:
            return []
        for pad in self._pads:
            pad.close()
        self._pads = []
        self.update()
        for index in range(self._sdl.SDL_NumJoysticks()):
            if self._sdl.SDL_IsGameController(index):
                controller = self._sdl.SDL_GameControllerOpen(index)
                if controller:
                    self._pads.append(SdlPad(self._sdl, controller))
        return list(self._pads)


def _backends():
    """The ways of reading controllers on this system, the preferred first: only the first
    one that finds any is used, so a controller seen two ways doesn't act twice."""
    if sys.platform == "win32":
        return [XInput(), Winmm()]
    if sys.platform.startswith("linux"):
        return [Joydev()]
    if sys.platform == "darwin":
        return [Sdl()]
    return []


class Gamepads:
    """The controllers connected, read together: poll() is what is held on any of them.
    With `run_in_background(work, done)` (Anki's taskman), looking for them is done there:
    on Windows it takes a few milliseconds each time, and the first time much longer."""

    def __init__(self, backends=None, run_in_background=None):
        self._backends = _backends() if backends is None else backends
        self._run = None if any(getattr(b, "main_thread", False) for b in self._backends)             else run_in_background
        self.pads = []
        self._next_scan = 0.0
        self._scanning = False
        self._closed = False

    @property
    def kind(self):
        """Which button names to show: those of the first controller."""
        return self.pads[0].kind if self.pads else "xbox"

    def names(self):
        return [pad.name for pad in self.pads]

    def poll(self, now):
        """The buttons held now, on any controller. While there is none, look for one every
        SCAN_INTERVAL seconds, not on every read."""
        if not self.pads:
            if now >= self._next_scan and not self._scanning:
                self._next_scan = now + SCAN_INTERVAL
                if self._run is None:
                    self.pads = self._find()
                else:
                    self._scanning = True
                    self._run(self._find, self._found)
            if not self.pads:
                return frozenset()
        for backend in self._backends:
            if hasattr(backend, "update"):
                backend.update()
        held = set()
        for pad in list(self.pads):
            try:
                buttons = pad.poll()
            except OSError:
                buttons = None
            if buttons is None:
                self.pads.remove(pad)
                pad.close()
            else:
                held |= buttons
        return frozenset(held)

    def _find(self):
        for backend in self._backends:
            try:
                pads = backend.scan()
            except Exception as e:
                log.warning("couldn't look for controllers with %s: %s",
                            type(backend).__name__, e)
                continue
            if pads:
                return pads
        return []

    def _found(self, future):
        """Back on the main thread from looking in the background."""
        self._scanning = False
        try:
            pads = future.result()
        except Exception as e:
            log.warning("couldn't look for controllers: %s", e)
            return
        if self._closed or self.pads:
            for pad in pads:
                if pad not in self.pads:
                    pad.close()
            return
        self.pads = pads

    def close(self):
        self._closed = True
        for pad in self.pads:
            pad.close()
        self.pads = []
