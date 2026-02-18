"""
main.py — Giriş Noktası & Komut Döngüsü v2.2 (Enhanced Realism)
==================================================================
v2.2 YENİLİKLERİ:
  1. InputHandler'a WPM callback desteği — release volume WPM-aware
  2. Engine enqueue_play signature güncellendi — duration + last_key
  3. Tüm optimizasyonlar korundu

RAM optimizasyonları (v2.1'den devam):
  1. RotatingFileHandler (max 512KB, 1 backup)
  2. gc.freeze() — startup sonrası uzun yaşayan nesneler GC'den çıkar
  3. GC threshold ayarı
  4. gc.disable() audio loop süresince (engine içinde)
"""

from __future__ import annotations

import gc
import json
import logging
import logging.handlers
import os
import signal
import sys
from pathlib import Path
from typing import Optional

from engine        import AudioEngine
from input_handler import InputHandler, SingleKeyCapture
from ui            import STRINGS, select_language, update_ui
from sound_mapper  import interactive_custom_flow

# ─────────────────────────────────────────────────────────────
#  LOGLAMA — RotatingFileHandler
# ─────────────────────────────────────────────────────────────
logging.basicConfig(
    level   = logging.WARNING,
    format  = "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers= [logging.handlers.RotatingFileHandler(
        "keyboard_sim.log",
        maxBytes    = 512 * 1024,
        backupCount = 1,
        encoding    = "utf-8",
    )],
)
log = logging.getLogger("KeySim.Main")

_CONFIG_PATH  = Path(__file__).parent / "config.json"
_PRESETS_PATH = Path(__file__).parent / "presets.json"


def _load_json(path: Path, what: str) -> dict:
    if not path.exists():
        print(f"[FATAL] {what} dosyası bulunamadı: {path}")
        sys.exit(1)
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except json.JSONDecodeError as exc:
        print(f"[FATAL] {what} parse hatası: {exc}")
        sys.exit(1)


def _load_bindings(cfg: dict) -> dict:
    path = Path(cfg["bindings_file"])
    if not path.exists() or path.stat().st_size == 0:
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, ValueError) as exc:
        log.warning("Bindings file malformed, starting fresh. (%s)", exc)
        return {}
    except Exception as exc:
        log.error("Bindings load error: %s", exc)
        return {}


def _save_bindings(cfg: dict, bindings: dict) -> None:
    path = Path(cfg["bindings_file"])
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(bindings, f, indent=4, ensure_ascii=False)
    except Exception as exc:
        log.error("Bindings save error: %s", exc)


# ─────────────────────────────────────────────────────────────
#  UYGULAMA DURUMU
# ─────────────────────────────────────────────────────────────
class AppState:
    """
    __slots__ ile her instance field için dict overhead yok.
    """
    __slots__ = (
        "running", "is_customizing", "repeat_mode",
        "last_action", "lang", "pressed_keys", "bindings",
    )

    def __init__(self) -> None:
        self.running        : bool = True
        self.is_customizing : bool = False
        self.repeat_mode    : bool = False
        self.last_action    : str  = ""
        self.lang           : str  = "en"
        self.pressed_keys   : set  = set()
        self.bindings       : dict = {}


# ─────────────────────────────────────────────────────────────
#  KOMUT İŞLEYİCİ
# ─────────────────────────────────────────────────────────────
def handle_command(
    raw    : str,
    state  : AppState,
    engine : AudioEngine,
    cfg    : dict,
) -> str:
    s   = STRINGS[state.lang]
    cmd = raw.strip().lower()

    if cmd in ("exit", "q", "quit", "çık"):
        state.running = False
        return ""

    if cmd in ("c", "custom", "özelleştir"):
        state.is_customizing = True
        new_bindings = interactive_custom_flow(
            lang             = state.lang,
            current_bindings = state.bindings,
        )
        if new_bindings is not None:
            state.bindings = new_bindings
            _save_bindings(cfg, state.bindings)
            engine._key_bindings = state.bindings
            print(f"\n  {s['reloading']}")
            engine.reload_sounds()
            state.last_action = f"Bound: {len(new_bindings)} ses"
            state.is_customizing = False
            return s["custom_success"]
        state.is_customizing = False
        return s["custom_cancel"]

    if cmd in ("r", "repeat", "tekrar"):
        state.repeat_mode = not state.repeat_mode
        notif = s["rep_on"] if state.repeat_mode else s["rep_off"]
        state.last_action = notif
        return notif

    if cmd:
        try:
            val = float(cmd)
            if 1.0 < val <= 100.0:
                val /= 100.0
            if 0.0 <= val <= 1.0:
                engine.update_volume(val)
                state.last_action = f"{s['vol_changed']}: %{int(val * 100)}"
                return s["vol_success"]
            return s["invalid"]
        except ValueError:
            return s["invalid"]

    return ""


# ─────────────────────────────────────────────────────────────
#  ANA FONKSİYON v2.2
# ─────────────────────────────────────────────────────────────
def main() -> None:
    cfg     = _load_json(_CONFIG_PATH,  "Config")
    presets = _load_json(_PRESETS_PATH, "Presets")

    state             = AppState()
    state.lang        = select_language()
    state.bindings    = _load_bindings(cfg)
    state.repeat_mode = cfg["app"]["default_repeat"]
    cfg["app"]["language"] = state.lang

    s = STRINGS[state.lang]

    # ── Audio Engine + ses havuzları ──────────────────────────
    engine = AudioEngine(cfg=cfg, presets=presets, key_bindings=state.bindings)
    engine.reload_sounds()
    engine.start()

    # ── GC OPTİMİZASYONLARI ───────────────────────────────────
    gc.collect()
    gc.freeze()

    # CHANGE v2.2: InputHandler'a WPM callback ekle
    # Release volume WPM'e göre ayarlanacak
    def get_wpm() -> float:
        """Engine'deki WPM değerini döndür."""
        base, _ = engine._wpm.burst_wpm()
        return base

    handler = InputHandler(
        enqueue_fn      = engine.enqueue_play,
        pressed_keys    = state.pressed_keys,
        get_customizing = lambda: state.is_customizing,
        get_repeat      = lambda: state.repeat_mode,
        get_running     = lambda: state.running,
        get_release     = lambda: cfg["app"].get("release_enabled", True),
        get_wpm         = get_wpm,  # CHANGE: WPM callback
    )
    handler.start()

    def _sig_handler(sig, frame):
        state.running = False

    signal.signal(signal.SIGINT,  _sig_handler)
    signal.signal(signal.SIGTERM, _sig_handler)

    notification = s["start"]

    while state.running:
        try:
            update_ui(
                lang          = state.lang,
                volume        = engine.volume,
                active_voices = engine.active_voices,
                polyphony     = cfg["engine"]["polyphony"],
                repeat_mode   = state.repeat_mode,
                last_action   = state.last_action,
                notification  = notification,
            )
            notification = ""
            raw = input().strip()
            notification = handle_command(raw, state, engine, cfg)

        except KeyboardInterrupt:
            state.running = False
            break
        except EOFError:
            break

    print(f"\n  {s['closing']}")
    handler.stop()
    engine.stop()

    gc.unfreeze()
    gc.collect()

    log.info("Application exited cleanly.")
    sys.exit(0)


if __name__ == "__main__":
    main()
