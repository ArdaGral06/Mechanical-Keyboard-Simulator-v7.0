"""
main.py — Giriş Noktası & Komut Döngüsü
=========================================
Modülleri bağlar ve komut döngüsünü çalıştırır.

Bağımlılık akışı (bağımlılık tersine çevrilmez):
  main.py
    ├── config.json  → AppConfig
    ├── presets.json → DSP Presets
    ├── engine.py    → AudioEngine   (ses havuzu + audio thread)
    ├── input_handler.py → InputHandler (klavye/fare → engine.enqueue_play)
    └── ui.py        → update_ui()   (terminal çıktısı)

Graceful shutdown sırası:
  1. app_running = False
  2. InputHandler.stop()   → listener'lar durur
  3. AudioEngine.stop()    → kuyruk boşalır, fade-out, mixer.quit()
  4. sys.exit(0)
"""

from __future__ import annotations

import json
import logging
import os
import signal
import sys
from pathlib import Path
from typing import Optional

# ── Proje modülleri ──────────────────────────────────────────────────────────
from engine        import AudioEngine
from input_handler import InputHandler, SingleKeyCapture
from ui            import STRINGS, select_language, update_ui
from sound_mapper  import interactive_custom_flow

# ─────────────────────────────────────────────────────────────
#  LOGLAMA KURULUMU (diğer modüller import etmeden önce)
# ─────────────────────────────────────────────────────────────
logging.basicConfig(
    level   = logging.WARNING,
    format  = "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers= [logging.FileHandler("keyboard_sim.log", encoding="utf-8")],
)
log = logging.getLogger("KeySim.Main")


# ─────────────────────────────────────────────────────────────
#  YAPILANDIRMA YÜKLEME
# ─────────────────────────────────────────────────────────────
_CONFIG_PATH  = Path(__file__).parent / "config.json"
_PRESETS_PATH = Path(__file__).parent / "presets.json"


def _load_json(path: Path, what: str) -> dict:
    """JSON dosyasını yükle. Hata durumunda açıklayıcı mesaj ver."""
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
    """
    key_bindings.json yükle.
    Dosya yoksa, boşsa (0 byte) veya bozuksa boş dict döndür.
    """
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
    Mutable uygulama durumu — tek yerde tutulur.
    Thread güvenliği: main thread dışından YAZILMAZ.
    Okunabilir: herhangi bir thread'den (GIL korumalı primitifler).
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
    """
    Kullanıcı komutunu işle.
    Dönen değer: bildirim metni (UI'da gösterilecek).
    """
    s   = STRINGS[state.lang]
    cmd = raw.strip().lower()

    # ── ÇIKIŞ ──────────────────────────────────────────────
    if cmd in ("exit", "q", "quit", "çık"):
        state.running = False
        return ""

    # ── ÖZELLEŞTİRME ───────────────────────────────────────
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

    # ── TEKRAR MODU ─────────────────────────────────────────
    if cmd in ("r", "repeat", "tekrar"):
        state.repeat_mode = not state.repeat_mode
        notif = s["rep_on"] if state.repeat_mode else s["rep_off"]
        state.last_action = notif
        return notif

    # ── SES SEVİYESİ ────────────────────────────────────────
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
#  ANA FONKSİYON
# ─────────────────────────────────────────────────────────────
def main() -> None:
    # ── Config yükle ─────────────────────────────────────────
    cfg     = _load_json(_CONFIG_PATH,  "Config")
    presets = _load_json(_PRESETS_PATH, "Presets")

    # ── Durum oluştur ─────────────────────────────────────────
    state          = AppState()
    state.lang     = select_language()
    state.bindings = _load_bindings(cfg)
    state.repeat_mode = cfg["app"]["default_repeat"]
    cfg["app"]["language"] = state.lang

    s = STRINGS[state.lang]

    # ── Audio Engine ──────────────────────────────────────────
    engine = AudioEngine(cfg=cfg, presets=presets, key_bindings=state.bindings)
    engine.reload_sounds()
    engine.start()

    # ── Input Handler ─────────────────────────────────────────
    handler = InputHandler(
        enqueue_fn      = engine.enqueue_play,
        pressed_keys    = state.pressed_keys,
        get_customizing = lambda: state.is_customizing,
        get_repeat      = lambda: state.repeat_mode,
        get_running     = lambda: state.running,
    )
    handler.start()

    # ── SIGINT / SIGTERM ──────────────────────────────────────
    def _sig_handler(sig, frame):
        state.running = False

    signal.signal(signal.SIGINT,  _sig_handler)
    signal.signal(signal.SIGTERM, _sig_handler)

    # ── Komut Döngüsü ─────────────────────────────────────────
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
            # Stdin kapandı (headless çalışma)
            break

    # ── Graceful Shutdown ─────────────────────────────────────
    print(f"\n  {s['closing']}")
    handler.stop()
    engine.stop()
    log.info("Application exited cleanly.")
    sys.exit(0)


if __name__ == "__main__":
    main()
