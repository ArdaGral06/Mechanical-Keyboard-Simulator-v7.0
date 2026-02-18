"""
input_handler.py — Klavye / Fare Dinleyici Katmanı v2.2 (Enhanced Release)
============================================================================
v2.2 YENİLİKLERİ:
  1. Press timestamp tracking — her tuş basışının zamanını kaydet
  2. Release duration hesaplama — basılı kalma süresini ölç
  3. WPM-aware release volume — hızlı yazımda release hafifler
  4. Last key tracking — aynı/farklı tuş geçişi için varyasyon desteği

RAM optimizasyonları (v2.1'den devam):
  1. pressed_keys watchdog — set >_MAX_PRESSED olursa auto-clear
  2. _MAX_PRESSED = 30 → gerçek klavyede aynı anda 30+ tuş basılmaz
  3. press_times bounded dict — max 40 entry (watchdog)
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Dict, Optional, Set

from pynput import keyboard, mouse

log = logging.getLogger("KeySim.Input")

# OPT: pressed_keys icin ust sinir
_MAX_PRESSED = 30
# OPT: press_times dict icin ust sinir - memory leak prevention
_MAX_PRESS_TIMES = 40

# -----------------------------------------------------------------
#  MODIFIER NORMALIZE MAP
#  pynput bazi sistemlerde generic Key.shift / Key.ctrl / Key.alt
#  gonderir; canonical forma normalize et -> binding tutarliligi.
# -----------------------------------------------------------------
_MODIFIER_NORMALIZE: Dict[str, str] = {
    "Key.shift": "Key.shift_l",
    "Key.ctrl":  "Key.ctrl_l",
    "Key.alt":   "Key.alt_l",
}


# -----------------------------------------------------------------
#  KEY NAME NORMALIZASYONU
# -----------------------------------------------------------------
def normalize_key_name(key: keyboard.Key | keyboard.KeyCode) -> Optional[str]:
    """
    Tus adini normalise et.

    FIX v2.2.1:
      - KeyCode(vk=N, char=None): pynput bazi Windows sistemlerinde
        ozel tuslari bare KeyCode olarak gonderir (LShift -> vk=160).
        Bu durumda Key enum'unda eslesen uyeyi ara; bulamazsan None.
      - Generic modifier alias: Key.shift -> Key.shift_l,
        Key.ctrl -> Key.ctrl_l, Key.alt -> Key.alt_l
    """
    try:
        if isinstance(key, keyboard.KeyCode):
            if key.char:
                return key.char.lower()
            # KeyCode with no char: vk ile Key enum'da eslestir (FIX: LShift bug)
            vk = getattr(key, "vk", None)
            if vk is not None:
                for k in keyboard.Key:
                    try:
                        kv = k.value
                        if isinstance(kv, keyboard.KeyCode) and getattr(kv, "vk", None) == vk:
                            name = str(k)
                            return _MODIFIER_NORMALIZE.get(name, name)
                    except Exception:
                        pass
            return None  # tanimsiz KeyCode

        # keyboard.Key enum uyesi
        name = str(key)
        return _MODIFIER_NORMALIZE.get(name, name)

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

    v2.2 GELİŞMELER:
      • Press duration tracking — basılı kalma süresini ölçer
      • Last key tracking — ardışık tuş geçişlerini izler
      • WPM callback — release volume WPM'e göre ayarlanır

    enqueue_fn imzası: (key_id: str, is_mouse: bool, is_release: bool, 
                        duration: float, last_key: str) → None
    """

    def __init__(
        self,
        enqueue_fn      : Callable[[str, bool, bool, float, str], None],
        pressed_keys    : Set[str],
        get_customizing : Callable[[], bool],
        get_repeat      : Callable[[], bool],
        get_running     : Callable[[], bool],
        get_release     : Callable[[], bool] = lambda: True,
        get_wpm         : Callable[[], float] = lambda: 0.0,
    ) -> None:
        self._enqueue      = enqueue_fn
        self._pressed      = pressed_keys
        self._customizing  = get_customizing
        self._repeat       = get_repeat
        self._running      = get_running
        self._release      = get_release
        self._get_wpm      = get_wpm

        # CHANGE 1: Press timestamp tracking — {key_id: press_time}
        # Basılı kalma süresini ölçmek için her tuşun basıldığı anı kaydet
        self._press_times : Dict[str, float] = {}
        
        # CHANGE 2: Last pressed key tracking — aynı/farklı tuş varyasyonu için
        self._last_key : str = ""
        
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
        self._pressed.clear()
        self._press_times.clear()  # CHANGE 1: timestamp dict temizle
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

            # OPT: Watchdog — set sınırı aşıldıysa stale state var demek
            if len(self._pressed) >= _MAX_PRESSED:
                log.warning("pressed_keys overflow (%d), clearing stale state",
                            len(self._pressed))
                self._pressed.clear()
                self._press_times.clear()

            # CHANGE 1: Press timestamp kaydet
            now = time.monotonic()
            self._press_times[name] = now
            
            # OPT: press_times watchdog — dict'in sınırsız büyümesini engelle
            if len(self._press_times) > _MAX_PRESS_TIMES:
                # En eski 10 entry'yi temizle (sorted by timestamp)
                sorted_items = sorted(self._press_times.items(), key=lambda x: x[1])
                for old_key, _ in sorted_items[:10]:
                    self._press_times.pop(old_key, None)

            self._pressed.add(name)
            
            # CHANGE 2: Last key güncelle (press eventi)
            prev_key = self._last_key
            self._last_key = name
            
            try:
                # Press: duration=0.0, last_key=previous key
                self._enqueue(name, False, False, 0.0, prev_key)
            except Exception as exc:
                log.debug("on_press enqueue error: %s", exc)
            return None

        def on_release(key: keyboard.Key | keyboard.KeyCode) -> None:
            name = normalize_key_name(key)
            if not name:
                return
            
            was_pressed = name in self._pressed
            self._pressed.discard(name)
            
            # CHANGE 1: Duration hesapla
            duration = 0.0
            if name in self._press_times:
                press_time = self._press_times.pop(name)
                duration = time.monotonic() - press_time
                # Sanitize: fiziksel imkansız değerler (>10s veya <0) filtrele
                if duration < 0.0 or duration > 10.0:
                    duration = 0.1  # fallback: ortalama basış süresi

            if was_pressed and self._release() and not self._customizing():
                try:
                    # Release: duration=basılı kalma süresi, last_key=current
                    self._enqueue(name, False, True, duration, self._last_key)
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
                # CHANGE 2: Mouse click'te de last key güncelle
                prev_key = self._last_key
                self._last_key = name
                try:
                    # Mouse: duration=0.0, last_key=previous key
                    self._enqueue(name, True, False, 0.0, prev_key)
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
