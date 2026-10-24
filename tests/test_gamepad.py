import struct
from types import SimpleNamespace

from ankime.controls import gamepad, hotkeys
from ankime.core import config


def press(mapper, *steps):
    """Run `mapper` over `steps` ((time, buttons held)), returning every action fired."""
    fired = []
    for now, held in steps:
        fired += mapper.update(set(held), now)
    return fired


# --- bindings ------------------------------------------------------------------------

def test_default_bindings_are_unique_known_buttons_and_cover_every_action():
    defaults = config.DEFAULTS["gamepad_buttons"]
    assert set(defaults) == set(gamepad.ACTIONS)
    combos = [c for c in defaults.values() if c]
    assert len(combos) == len(set(combos))
    for combo in combos:
        assert all(b in gamepad.BUTTONS for b in gamepad.parse(combo))
    assert set(gamepad.REPEATING) <= set(gamepad.ACTIONS)


def test_every_action_is_a_hotkey_action_or_escape():
    assert set(gamepad.ACTIONS) - set(hotkeys.ACTIONS) == {"escape"}


def test_get_reads_the_given_config():
    cfg = config.merge({"gamepad_buttons": {"mine_line": "LB+A"}})
    assert gamepad.get(cfg)["mine_line"] == "LB+A"
    assert gamepad.get(cfg)["escape"] == config.DEFAULTS["gamepad_buttons"]["escape"]


def test_parse_and_labels():
    assert gamepad.parse("LB+A") == ("LB", "A")
    assert gamepad.parse("LS Left") == ("LS Left",)
    assert gamepad.parse("") == ()
    assert gamepad.combo_label("LB+A") == "LB + A"
    assert gamepad.combo_label("LB+A", "playstation") == "L1 + ✕"
    assert gamepad.label("LS Left") == "LS ←"
    assert gamepad.label("RS Up", "playstation") == "R3 ↑"
    assert gamepad.label("Down") == "D-pad ↓"
    assert gamepad.label("Start") == "Menu"
    assert gamepad.label("Button 7", "generic") == "Button 7"


# --- the mapper --------------------------------------------------------------------------

def test_a_press_fires_once():
    m = gamepad.Mapper({"mine_line": "A", "escape": "B"})
    assert press(m, (0, ["A"]), (0.1, ["A"]), (1, ["A"]), (1.1, []), (1.2, ["B"])) == [
        "mine_line", "escape"]


def test_repeating_actions_repeat_while_held():
    m = gamepad.Mapper({"word_next": "LS Right"})
    steps = [(t / 100, ["LS Right"]) for t in range(0, 70, 2)] + [(0.7, [])]
    fired = press(m, *steps)
    # once, then after REPEAT_DELAY every REPEAT_INTERVAL
    assert fired == ["word_next"] * 4  # at 0, then 0.4, 0.52, 0.64
    assert press(m, (1, []), (2, [])) == []


def test_a_combination_and_its_modifier():
    m = gamepad.Mapper({"rewind": "LB", "mine_line": "LB+A", "add_word_card": "A"})
    # LB held, A pressed: the combination, and LB does nothing on release
    assert press(m, (0, ["LB"]), (0.1, ["LB", "A"]), (0.2, ["LB"]), (0.3, [])) == ["mine_line"]
    # LB alone runs its own action, when let go
    assert press(m, (1, ["LB"]), (1.5, [])) == ["rewind"]
    # A alone is still A
    assert press(m, (2, ["A"]), (2.1, [])) == ["add_word_card"]
    # both at once count as the combination
    assert press(m, (3, ["A", "LB"]), (3.1, [])) == ["mine_line"]


def test_combination_needs_its_modifier_first():
    m = gamepad.Mapper({"mine_line": "LB+A", "add_word_card": "A"})
    assert press(m, (0, ["A"]), (0.1, ["A", "LB"]), (0.2, [])) == ["add_word_card"]


def test_reset_ignores_what_is_held():
    m = gamepad.Mapper({"mine_line": "A", "rewind": "LB", "forward": "LB+RB"})
    m.reset({"A", "LB"})
    assert press(m, (0, ["A", "LB"]), (0.1, [])) == []
    assert press(m, (1, ["A"])) == ["mine_line"]


def test_capture():
    c = gamepad.Capture()
    assert c.feed({"LB"}) is None
    assert c.feed({"LB", "A"}) is None
    assert c.feed({"A"}) is None
    assert c.feed(set()) == "LB+A"
    c = gamepad.Capture()
    assert c.feed({"Y"}) is None and c.feed(set()) == "Y"
    # what was held when the capture began (the click) doesn't count
    c = gamepad.Capture({"A"})
    assert c.feed({"A"}) is None and c.feed(set()) is None
    assert c.feed({"B"}) is None and c.feed(set()) == "B"


# --- axes ---------------------------------------------------------------------------------

def test_sticks_one_way_at_a_time_without_flicker():
    assert gamepad.stick_direction("LS", 0.9, 0.2, set()) == "LS Right"
    assert gamepad.stick_direction("LS", 0.1, -0.9, set()) == "LS Up"
    assert gamepad.stick_direction("LS", 0.4, 0, set()) is None
    # held: kept until the stick is mostly back
    assert gamepad.stick_direction("LS", 0.4, 0, {"LS Right"}) == "LS Right"
    assert gamepad.stick_direction("LS", 0.2, 0, {"LS Right"}) is None
    # rolled round to another way
    assert gamepad.stick_direction("LS", 0.4, 0.9, {"LS Right"}) == "LS Down"


def test_axes_to_buttons_triggers():
    assert gamepad.axes_to_buttons({"LT": 0.6, "RT": 0.4}, set()) == {"LT"}
    assert gamepad.axes_to_buttons({"LT": 0.4}, {"LT"}) == {"LT"}
    assert gamepad.axes_to_buttons({"RX": 0, "RY": 1}, set()) == {"RS Down"}


def test_hat():
    assert gamepad.hat_to_buttons(None) == set()
    assert gamepad.hat_to_buttons(0) == {"Up"}
    assert gamepad.hat_to_buttons(45) == {"Up", "Right"}
    assert gamepad.hat_to_buttons(270) == {"Left"}
    assert gamepad.hat_to_buttons(315) == {"Up", "Left"}


# --- reading controllers ------------------------------------------------------------------

def test_xinput_state():
    gp = SimpleNamespace(buttons=0x1000 | 0x0100 | 0x0002, left_trigger=255, right_trigger=0,
                         lx=-32768, ly=0, rx=0, ry=32767)
    buttons, axes = gamepad.xinput_state(gp)
    assert buttons == {"A", "LB", "Down"}
    assert gamepad.axes_to_buttons(axes, set()) == {"LS Left", "RS Up", "LT"}


def test_winmm_playstation_controller():
    caps = SimpleNamespace(xmin=0, xmax=65535, ymin=0, ymax=65535, zmin=0, zmax=65535,
                           rmin=0, rmax=65535,
                           caps=gamepad.JOYCAPS_HASZ | gamepad.JOYCAPS_HASR
                           | gamepad.JOYCAPS_HASPOV)
    info = SimpleNamespace(buttons=0b10 | 1 << 4 | 1 << 20, x=65535, y=32767, z=32767,
                           r=32767, pov=18000)
    buttons, axes = gamepad.winmm_state(info, caps, gamepad.SONY_BUTTONS)
    # cross is A, L1 is LB, a button past the known ones gets a number
    assert buttons == {"A", "LB", "Button 21", "Down"}
    assert gamepad.axes_to_buttons(axes, set()) == {"LS Right"}
    info.pov = 65535  # centred
    assert gamepad.winmm_state(info, caps, [])[0] == {"Button 2", "Button 5", "Button 21"}


def test_joydev_button_names():
    codes = [0x130, 0x131, 0x133, 0x134, 0x136, 0x2C0]
    assert gamepad.joydev_buttons(codes, False) == ["A", "B", "X", "Y", "LB", "Button 6"]
    assert gamepad.joydev_buttons(codes, True)[:4] == ["A", "B", "Y", "X"]


def test_joydev_events():
    pad = gamepad.JoydevPad.__new__(gamepad.JoydevPad)
    gamepad.Pad.__init__(pad)
    pad._button_names = ["A", "B"]
    pad._axis_names = ["LX", "LY", "LT", "HX", "HY"]
    pad._buttons, pad._axes = set(), {}
    event = struct.Struct("IhBB")
    pad.apply(event.pack(0, 1, gamepad.JS_EVENT_BUTTON | gamepad.JS_EVENT_INIT, 0)
              + event.pack(0, 1, gamepad.JS_EVENT_BUTTON, 1)
              + event.pack(0, 0, gamepad.JS_EVENT_BUTTON, 1)
              + event.pack(0, 32767, gamepad.JS_EVENT_AXIS, 0)
              + event.pack(0, -32767, gamepad.JS_EVENT_AXIS, 2)
              + event.pack(0, -32767, gamepad.JS_EVENT_AXIS, 4))
    assert pad._buttons == {"A"}
    pad._read = lambda: ({"A", "Up"}, {"LX": 1.0, "LY": 0.0, "LT": 0.0})
    assert pad.poll() == {"A", "Up", "LS Right"}


class FakePad(gamepad.Pad):
    def __init__(self, name, states):
        super().__init__()
        self.name, self._states, self.closed = name, list(states), False

    def _read(self):
        return self._states.pop(0) if self._states else None

    def close(self):
        self.closed = True


class FakeBackend:
    def __init__(self, *found):
        self.found, self.scans = list(found), 0

    def scan(self):
        self.scans += 1
        return self.found.pop(0) if self.found else []


def test_gamepads_scan_merge_and_disconnect():
    pad1 = FakePad("one", [({"A"}, {}), ({"A"}, {})])
    pad2 = FakePad("two", [({"B"}, {})])
    empty, backend = FakeBackend(), FakeBackend([pad1, pad2])
    pads = gamepad.Gamepads([empty, backend])
    assert pads.poll(0) == {"A", "B"}
    assert pads.names() == ["one", "two"]
    assert pads.poll(0.1) == {"A"}  # two is gone
    assert pad2.closed and pads.names() == ["one"]
    pads.poll(0.2)
    assert pads.names() == [] and pad1.closed
    # not looked for again until SCAN_INTERVAL has passed
    pads.poll(1)
    assert backend.scans == 1
    pads.poll(0.2 + gamepad.SCAN_INTERVAL)
    assert backend.scans == 2


def test_the_first_backend_that_finds_one_wins():
    first = FakeBackend([FakePad("xinput", [({"A"}, {})])])
    second = FakeBackend([FakePad("winmm", [({"A"}, {})])])
    pads = gamepad.Gamepads([first, second])
    pads.poll(0)
    assert pads.names() == ["xinput"] and second.scans == 0


def test_a_backend_that_fails_is_skipped():
    class Broken:
        def scan(self):
            raise OSError("no")

    pads = gamepad.Gamepads([Broken(), FakeBackend([FakePad("ok", [({"X"}, {})])])])
    assert pads.poll(0) == {"X"}


def test_looking_in_the_background():
    queued = []
    pad = FakePad("one", [({"A"}, {})])
    pads = gamepad.Gamepads([FakeBackend([pad])],
                            run_in_background=lambda work, done: queued.append((work, done)))
    assert pads.poll(0) == set() and len(queued) == 1
    assert pads.poll(0.1) == set() and len(queued) == 1  # one look at a time
    work, done = queued.pop()
    done(SimpleNamespace(result=work))
    assert pads.names() == ["one"] and pads.poll(0.2) == {"A"}


def test_found_after_closing_is_closed():
    queued = []
    pad = FakePad("one", [])
    pads = gamepad.Gamepads([FakeBackend([pad])],
                            run_in_background=lambda work, done: queued.append((work, done)))
    pads.poll(0)
    pads.close()
    work, done = queued.pop()
    done(SimpleNamespace(result=work))
    assert pads.names() == [] and pad.closed


def test_sdl_is_looked_through_on_the_main_thread():
    backend = FakeBackend([FakePad("sdl", [({"A"}, {})])])
    backend.main_thread = True
    pads = gamepad.Gamepads([backend], run_in_background=lambda work, done: 1 / 0)
    assert pads.poll(0) == {"A"}
