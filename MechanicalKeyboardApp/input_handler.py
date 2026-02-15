"""
input_handler.py — Klavye / Fare Dinleyici Katmanı v2.1 (RAM Optimized)
========================================================================
RAM optimizasyonları:
  1. pressed_keys watchdog — set >_MAX_PRESSED olursa auto-clear
     (focus kaybında release event kaçarsa set sonsuza büyüyebilirdi)
  2. _MAX_PRESSED = 30 → gerçek klavyede aynı anda 30+ tuş basılmaz
"""

from __future__ import annotations

import logging
import threading
from typing import Callable, Optional, Set

from pynput import keyboard, mouse

log = logging.getLogger("KeySim.Input")

# OPT: pressed_keys için üst sınır — focus kaybında release kaçarsa
# set büyümeye devam ederdi. 30 tuş aynı anda basılması fiziksel imkânsız.
_MAX_PRESSED = 30


# ─────────────────────────────────────────────────────────────
#  KEY NAME NORMALİZASYONU
# ─────────────────────────────────────────────────────────────
def normalize_key_name(key: keyboard.Key | keyboard.KeyCode) -> Optional[str]:
    try:
        if isinstance(key, keyboard.KeyCode) and key.char:
            return key.char.lower()
        return str(key)
    except Exception as exc:
        log.debug("Key name normalize error: %s", exc)
        return None


def normalize_button_name(button: mouse.Button) -> str:
    return str(button)


# ─────────────────────────────────────────────────────────────
#  INPUT HANDLER
# ─────────────────────────────────────────────────────────────
class InputHandler:
    """
    Klavye ve fare giriş dinleyicisi.

    enqueue_fn imzası: (key_id: str, is_mouse: bool, is_release: bool) → None
    """

    def __init__(
        self,
        enqueue_fn      : Callable[[str, bool, bool], None],
        pressed_keys    : Set[str],
        get_customizing : Callable[[], bool],
        get_repeat      : Callable[[], bool],
        get_running     : Callable[[], bool],
        get_release     : Callable[[], bool] = lambda: True,
    ) -> None:
        self._enqueue      = enqueue_fn
        self._pressed      = pressed_keys
        self._customizing  = get_customizing
        self._repeat       = get_repeat
        self._running      = get_running
        self._release      = get_release

        self._kb_listener  : Optional[keyboard.Listener] = None
        self._ms_listener  : Optional[mouse.Listener]    = None
        self._thread       : Optional[threading.Thread]  = None

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._listen_loop, name="InputHandler", daemon=True
        )
        self._thread.start()
        log.info("InputHandler started.")

    def stop(self) -> None:
        if self._kb_listener:
            self._kb_listener.stop()
        if self._ms_listener:
            self._ms_listener.stop()
        # OPT: set içeriğini temizle — thread durduğunda stale state kalmasın
        self._pressed.clear()
        log.info("InputHandler stopped.")

    # ── PRIVATE ────────────────────────────────────────────────

    def _listen_loop(self) -> None:

        def on_press(key: keyboard.Key | keyboard.KeyCode) -> Optional[bool]:
            if not self._running():
                return False
            if self._customizing():
                return None

            name = normalize_key_name(key)
            if not name:
                return None

            if not self._repeat() and name in self._pressed:
                return None

            # OPT: Watchdog — set sınırı aşıldıysa stale state var demek;
            # temizle ve devam et. Normal kullanımda hiç tetiklenmez.
            if len(self._pressed) >= _MAX_PRESSED:
                log.warning("pressed_keys overflow (%d), clearing stale state",
                            len(self._pressed))
                self._pressed.clear()

            self._pressed.add(name)
            try:
                self._enqueue(name, False, False)
            except Exception as exc:
                log.debug("on_press enqueue error: %s", exc)
            return None

        def on_release(key: keyboard.Key | keyboard.KeyCode) -> None:
            name = normalize_key_name(key)
            if not name:
                return
            was_pressed = name in self._pressed
            self._pressed.discard(name)

            if was_pressed and self._release() and not self._customizing():
                try:
                    self._enqueue(name, False, True)
                except Exception as exc:
                    log.debug("on_release enqueue error: %s", exc)

        def on_click(
            x: int, y: int,
            button: mouse.Button,
            pressed: bool
        ) -> Optional[bool]:
            if not self._running():
                return False
            if self._customizing():
                return None
            if pressed:
                name = normalize_button_name(button)
                try:
                    self._enqueue(name, True, False)
                except Exception as exc:
                    log.debug("on_click enqueue error: %s", exc)
            return None

        self._kb_listener = keyboard.Listener(
            on_press=on_press,
            on_release=on_release,
        )
        self._ms_listener = mouse.Listener(
            on_click=on_click,
        )

        with self._kb_listener, self._ms_listener:
            self._kb_listener.join()
            self._ms_listener.join()

        # OPT: listener kapandığında referansları serbest bırak
        self._kb_listener = None
        self._ms_listener = None


# ─────────────────────────────────────────────────────────────
#  TEK TUŞ YAKALAMA (Özelleştirme için)
# ─────────────────────────────────────────────────────────────
class SingleKeyCapture:
    __slots__ = ("_result", "_event")

    def __init__(self) -> None:
        self._result : Optional[str] = None
        self._event  = threading.Event()

    def wait(self, timeout: float = 30.0) -> Optional[str]:
        self._result = None
        self._event.clear()

        def on_press(key):
            name = normalize_key_name(key)
            if name:
                self._result = name
                self._event.set()
                return False

        def on_click(x, y, button, pressed):
            if pressed:
                self._result = normalize_button_name(button)
                self._event.set()
                return False

        kl = keyboard.Listener(on_press=on_press)
        ml = mouse.Listener(on_click=on_click)
        kl.start()
        ml.start()
        self._event.wait(timeout=timeout)
        kl.stop()
        ml.stop()
        return self._result
