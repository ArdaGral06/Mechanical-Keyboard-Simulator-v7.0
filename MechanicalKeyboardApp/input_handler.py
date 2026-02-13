"""
input_handler.py — Klavye / Fare Dinleyici Katmanı
====================================================
Bu modül YALNIZCA input eventlerini yakalar ve AudioEngine'e iletir.
pygame.mixer veya herhangi bir ses işlemi YAPILMAZ.

Sorumluluklar:
  • Klavye tuş basma/bırakma olaylarını dinle
  • Fare tıklama olaylarını dinle
  • is_customizing bayrağına göre sesi engelle
  • Basılı tutulan tuşları takip et (repeat_mode için)
  • Tuş adını normalize et (KeyCode.char → lower, Key.space → "Key.space")
"""

from __future__ import annotations

import logging
import threading
from typing import Callable, Optional, Set

from pynput import keyboard, mouse

log = logging.getLogger("KeySim.Input")


# ─────────────────────────────────────────────────────────────
#  KEY NAME NORMALİZASYONU
# ─────────────────────────────────────────────────────────────
def normalize_key_name(key: keyboard.Key | keyboard.KeyCode) -> Optional[str]:
    """
    pynput Key nesnesini tutarlı string'e dönüştür.
    
    KeyCode (normal karakter) → key.char.lower()   → "a", "1", "ş" ...
    Key (özel tuş)            → str(key)            → "Key.space", "Key.enter" ...
    Geçersiz                  → None
    """
    try:
        if isinstance(key, keyboard.KeyCode) and key.char:
            return key.char.lower()
        return str(key)
    except Exception as exc:
        log.debug("Key name normalize error: %s", exc)
        return None


def normalize_button_name(button: mouse.Button) -> str:
    """Mouse.Button → string. Örn: Button.left → "Button.left" """
    return str(button)


# ─────────────────────────────────────────────────────────────
#  INPUT HANDLER
# ─────────────────────────────────────────────────────────────
class InputHandler:
    """
    Klavye ve fare giriş dinleyicisi.

    Tasarım ilkesi: 'Sadece üret, asla tüketme.'
      Listener callback'leri yalnızca engine.enqueue_play() çağırır.
      Ses çalma, DSP, mixer — hiçbiri bu sınıfın sorumluluğunda değil.

    Kullanım:
      handler = InputHandler(engine=..., state=...)
      handler.start()
      ...
      handler.stop()
    """

    def __init__(
        self,
        enqueue_fn     : Callable[[str, bool], None],
        pressed_keys   : Set[str],
        get_customizing: Callable[[], bool],
        get_repeat     : Callable[[], bool],
        get_running    : Callable[[], bool],
    ) -> None:
        """
        enqueue_fn      : AudioEngine.enqueue_play — ses çalma isteği
        pressed_keys    : shared set — basılı tutulan tuşları izler
        get_customizing : bool dönen callable — tuş yakalanırken ses engelle
        get_repeat      : bool dönen callable — repeat mode durumu
        get_running     : bool dönen callable — uygulama hâlâ çalışıyor mu
        """
        self._enqueue      = enqueue_fn
        self._pressed      = pressed_keys
        self._customizing  = get_customizing
        self._repeat       = get_repeat
        self._running      = get_running

        self._kb_listener  : Optional[keyboard.Listener] = None
        self._ms_listener  : Optional[mouse.Listener]    = None
        self._thread       : Optional[threading.Thread]  = None

    def start(self) -> None:
        """Arka plan thread'inde dinleyicileri başlat."""
        self._thread = threading.Thread(
            target=self._listen_loop, name="InputHandler", daemon=True
        )
        self._thread.start()
        log.info("InputHandler started.")

    def stop(self) -> None:
        """Dinleyicileri durdur."""
        if self._kb_listener:
            self._kb_listener.stop()
        if self._ms_listener:
            self._ms_listener.stop()
        log.info("InputHandler stopped.")

    # ── PRIVATE ────────────────────────────────────────────────

    def _listen_loop(self) -> None:
        """Keyboard + Mouse listener'ları başlat ve join et."""

        def on_press(key: keyboard.Key | keyboard.KeyCode) -> Optional[bool]:
            if not self._running():
                return False
            if self._customizing():
                return None  # özelleştirme modunda sessiz

            name = normalize_key_name(key)
            if not name:
                return None

            # Repeat mode kapalıysa ve tuş zaten basılıysa → ses çalma
            if not self._repeat() and name in self._pressed:
                return None

            self._pressed.add(name)
            try:
                self._enqueue(name, False)
            except Exception as exc:
                log.debug("on_press enqueue error: %s", exc)
            return None

        def on_release(key: keyboard.Key | keyboard.KeyCode) -> None:
            name = normalize_key_name(key)
            if name:
                self._pressed.discard(name)

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
                    self._enqueue(name, True)
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


# ─────────────────────────────────────────────────────────────
#  TEK TUŞ YAKALAMA (Özelleştirme için)
# ─────────────────────────────────────────────────────────────
class SingleKeyCapture:
    """
    Kullanıcının bastığı tek tuş veya fare butonunu yakalar.
    Özelleştirme (custom binding) işleminde kullanılır.

    Kullanım:
      capture = SingleKeyCapture()
      key_name = capture.wait(timeout=30.0)
    """

    def __init__(self) -> None:
        self._result : Optional[str]   = None
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
