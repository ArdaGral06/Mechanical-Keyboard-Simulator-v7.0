"""
sound_pack_loader.py — JSON Soundpack Yükleyici
================================================
Mechvibes benzeri JSON soundpack formatını destekler.

Klasör yükleme mantığı:
  .json VAR    → JSON mode (tek ses + sprite/slice)
  .json YOK    → Fallback mode (dosya adı → tuş eşleme)

JSON Format (config.json):
  {
    "key_define_type": "single",
    "sound": "sound.ogg",
    "defines": {
      "1": [2894, 226],      // keycode 1 (Esc) → 2894ms'den başla, 226ms çal
      "30": [31542, 170],    // keycode 30 (A) → ...
      ...
    }
  }

Keycode mapping: JavaScript scan code → pynput normalize_key_name() formatı
  Platform agnostic: Windows, macOS, Linux hepsinde aynı mapping.

keyup davranışı: JSON mode'da keyup yok sayılır (sadece keydown çalar).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Dict, Optional

import numpy as np
import pygame

log = logging.getLogger("KeySim.PackLoader")

# ─────────────────────────────────────────────────────────────
#  SABİTLER
# ─────────────────────────────────────────────────────────────

# keybindings.json marker — pack aktif olduğunu gösterir
PACK_FOLDER_KEY = "__pack_folder__"

SUPPORTED_AUDIO: frozenset = frozenset({".ogg", ".wav", ".mp3", ".flac"})

# ─────────────────────────────────────────────────────────────
#  KATMAN 1 — JAVASCRIPT KEYCODE → PYNPUT KEY ID
#  Mechvibes/Howler.js keycode numaraları → sistem key ID
# ─────────────────────────────────────────────────────────────

# Standart JavaScript scan code mapping (platform agnostic)
_KEYCODE_MAP: Dict[int, str] = {
    # Escape + Function keys
    1:  "Key.esc",
    59: "Key.f1",    60: "Key.f2",    61: "Key.f3",    62: "Key.f4",
    63: "Key.f5",    64: "Key.f6",    65: "Key.f7",    66: "Key.f8",
    67: "Key.f9",    68: "Key.f10",   87: "Key.f11",   88: "Key.f12",
    91: "Key.f13",   92: "Key.f14",   93: "Key.f15",

    # Number row
    41: "`",         2:  "1",         3:  "2",         4:  "3",
    5:  "4",         6:  "5",         7:  "6",         8:  "7",
    9:  "8",         10: "9",         11: "0",
    12: "-",         13: "=",

    # Main keys
    14: "Key.backspace",
    15: "Key.tab",
    58: "Key.caps_lock",
    28: "Key.enter",
    57: "Key.space",

    # Letters (QWERTY layout)
    16: "q",  17: "w",  18: "e",  19: "r",  20: "t",
    21: "y",  22: "u",  23: "i",  24: "o",  25: "p",
    30: "a",  31: "s",  32: "d",  33: "f",  34: "g",
    35: "h",  36: "j",  37: "k",  38: "l",
    44: "z",  45: "x",  46: "c",  47: "v",  48: "b",
    49: "n",  50: "m",

    # Punctuation
    26: "[",   27: "]",   43: "\\",
    39: ";",   40: "'",
    51: ",",   52: ".",   53: "/",

    # Navigation cluster
    3639: "Key.print_screen",
    70:   "Key.scroll_lock",
    3653: "Key.pause",
    3666: "Key.insert",
    3667: "Key.delete",
    3655: "Key.home",
    3663: "Key.end",
    3657: "Key.page_up",
    3665: "Key.page_down",

    # Arrow keys
    57416: "Key.up",
    57419: "Key.left",
    57421: "Key.right",
    57424: "Key.down",

    # Modifiers
    42:   "Key.shift_l",
    54:   "Key.shift_r",
    29:   "Key.ctrl_l",
    3613: "Key.ctrl_r",
    56:   "Key.alt_l",
    3640: "Key.alt_r",
    3675: "Key.cmd",      # Win/Meta left
    3676: "Key.cmd_r",    # Win/Meta right
    3677: "Key.menu",

    # Numpad
    69:   "Key.num_lock",
    3637: "/",             # Numpad / (bazı sistemlerde Key.num_divide olabilir)
    55:   "*",             # Numpad *
    74:   "-",             # Numpad -
    78:   "+",             # Numpad +
    3612: "Key.enter",     # Numpad Enter
    83:   ".",             # Numpad .
    79:   "Key.num_1",
    80:   "Key.num_2",
    81:   "Key.num_3",
    75:   "Key.num_4",
    76:   "Key.num_5",
    77:   "Key.num_6",
    71:   "Key.num_7",
    72:   "Key.num_8",
    73:   "Key.num_9",
    82:   "Key.num_0",

    # Windows-specific extended codes (bazı soundpack'ler bunları kullanır)
    61010: "Key.insert",
    61011: "Key.delete",
    60999: "Key.home",
    61007: "Key.end",
    61001: "Key.page_up",
    61009: "Key.page_down",
    61000: "Key.up",
    61003: "Key.left",
    61005: "Key.right",
    61008: "Key.down",
}


def _keycode_str_to_sys(keycode_str: str) -> Optional[str]:
    """
    String keycode → pynput key ID.
    JSON'daki key'ler string olarak gelir: "1", "30", "59"
    """
    try:
        keycode = int(keycode_str)
        return _KEYCODE_MAP.get(keycode)
    except (ValueError, TypeError):
        return None


# ─────────────────────────────────────────────────────────────
#  KATMAN 2 — SES DOSYASI CACHE
# ─────────────────────────────────────────────────────────────
class _SoundFileCache:
    """Aynı ses dosyası tekrar yüklenmez."""

    def __init__(self) -> None:
        self._cache: Dict[str, pygame.mixer.Sound] = {}

    def get(self, path: str, volume: float) -> Optional[pygame.mixer.Sound]:
        if path not in self._cache:
            try:
                snd = pygame.mixer.Sound(path)
                snd.set_volume(volume)
                self._cache[path] = snd
            except Exception as exc:
                log.warning("Sound load failed (%s): %s", path, exc)
                return None
        return self._cache[path]

    def set_volume(self, volume: float) -> None:
        for snd in self._cache.values():
            snd.set_volume(volume)

    def clear(self) -> None:
        self._cache.clear()


# ─────────────────────────────────────────────────────────────
#  YARDIMCI FONKSİYONLAR
# ─────────────────────────────────────────────────────────────
def _find_audio_file(folder: Path, hint: str) -> Optional[Path]:
    """JSON'daki 'sound' alanı varsa onu dene, yoksa klasörde ara."""
    if hint:
        p = folder / hint
        if p.exists() and p.suffix.lower() in SUPPORTED_AUDIO:
            return p
        p2 = folder / Path(hint).name
        if p2.exists() and p2.suffix.lower() in SUPPORTED_AUDIO:
            return p2

    # Otomatik tarama: OGG → WAV → MP3 → FLAC
    for pattern in ("*.ogg", "*.wav", "*.mp3", "*.flac"):
        found = sorted(folder.glob(pattern))
        if found:
            return found[0]
    return None


def _slice_audio(arr: np.ndarray, start_ms: float, dur_ms: float,
                 freq: int, n_ch: int) -> Optional[pygame.mixer.Sound]:
    """Numpy int16 dizisinden belirtilen aralığı keser."""
    start_frame = int(start_ms / 1000.0 * freq)
    dur_frames  = max(1, int(dur_ms  / 1000.0 * freq))
    end_frame   = min(start_frame + dur_frames, len(arr) // n_ch)
    if start_frame >= len(arr) // n_ch:
        return None
    s = start_frame * n_ch
    e = end_frame   * n_ch
    sliced = arr[s:e]
    if len(sliced) == 0:
        return None
    return pygame.mixer.Sound(buffer=sliced.tobytes())


def peek_json_info(json_path: Path) -> dict:
    """JSON'u okuyup temel bilgileri döndür (UI için)."""
    try:
        with open(json_path, encoding="utf-8") as f:
            data = json.load(f)
        return {
            "name":       data.get("name", json_path.stem),
            "type":       data.get("key_define_type", "single"),
            "sound_file": data.get("sound", "?"),
            "key_count":  len(data.get("defines", {})),
        }
    except Exception:
        return {"name": json_path.stem, "type": "?", "sound_file": "?", "key_count": 0}


# ─────────────────────────────────────────────────────────────
#  KATMAN 3 — KEY PACK LOADER
# ─────────────────────────────────────────────────────────────
class KeyPackLoader:
    """
    JSON soundpack yükleyici.

    Dışarıdan çağrılacak metodlar:
      load_folder(folder, volume) → keybinds dict
      resolve(key_id, is_release) → Optional[pygame.Sound]
      set_volume(volume)
      unload()
    """

    def __init__(self) -> None:
        self._mode: str = ""    # "json" | "fallback" | ""
        self._file_cache = _SoundFileCache()

        # JSON mode: key_id → Sound (numpy slice'dan)
        self._json_sounds: Dict[str, pygame.mixer.Sound] = {}

        # Fallback: key_id → Sound (dosya adından)
        self._fb_sounds: Dict[str, pygame.mixer.Sound] = {}

    # ── PUBLIC ─────────────────────────────────────────────────

    def load_folder(self, folder: Path, volume: float) -> Dict[str, str]:
        """
        Klasörü tara ve uygun modu yükle.

        Sıra:
          1. Klasörde .json var mı? → JSON mode
          2. Yoksa → Fallback mode

        Returns: keybinds dict (keybinds.json'a yazılacak)
        """
        self.unload()

        # 1. JSON tespiti
        json_files = sorted(folder.glob("*.json"))
        if json_files:
            return self._load_json_pack(folder, json_files[0], volume)
        else:
            return self._load_fallback(folder, volume)

    def resolve(self, key_id: str, is_release: bool) -> Optional[pygame.mixer.Sound]:
        """
        Tek giriş noktası.

        JSON mode    : keyup → None (yok say). keydown → slice sesi.
        Fallback     : keyup → None. keydown → eşleşen dosya sesi.
        None döndürürse → engine DSP pool'a geçer.
        """
        if self._mode == "json":
            if is_release:
                return None   # JSON mode'da keyup yok
            return self._json_sounds.get(key_id)

        if self._mode == "fallback":
            if is_release:
                return None
            return self._fb_sounds.get(key_id)

        return None

    def set_volume(self, volume: float) -> None:
        """Tüm yüklü seslerin ses seviyesini güncelle."""
        for snd in self._json_sounds.values():
            snd.set_volume(volume)
        self._file_cache.set_volume(volume)

    def unload(self) -> None:
        """Tüm sesleri temizle."""
        self._mode = ""
        self._json_sounds.clear()
        self._fb_sounds.clear()
        self._file_cache.clear()

    # ── PRIVATE — YÜKLEME ──────────────────────────────────────

    def _load_json_pack(self, folder: Path, json_path: Path,
                        volume: float) -> Dict[str, str]:
        """JSON parse → sprite/slice yükle."""
        try:
            with open(json_path, encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError) as exc:
            raise RuntimeError(f"JSON okunamadı ({json_path.name}): {exc}") from exc

        if not isinstance(data, dict):
            raise RuntimeError(f"JSON kök nesnesi dict değil: {json_path.name}")

        pack_name = data.get("name", json_path.stem)
        key_define_type = data.get("key_define_type", "single")

        if key_define_type != "single":
            raise RuntimeError(
                f"Desteklenmeyen key_define_type: '{key_define_type}'\n"
                f"  Sadece 'single' desteklenmektedir."
            )

        # Ses dosyasını bul
        audio_path = _find_audio_file(folder, data.get("sound", ""))
        if audio_path is None:
            raise FileNotFoundError(
                f"Ses dosyası bulunamadı. Klasör: {folder}\n"
                f"  JSON'da 'sound': {data.get('sound', '<boş>')}"
            )

        # Tek seferde numpy'a yükle
        freq, fmt, n_ch = pygame.mixer.get_init()
        raw_snd = pygame.mixer.Sound(str(audio_path))
        raw     = raw_snd.get_raw()
        del raw_snd
        arr = np.frombuffer(raw, dtype=np.int16).copy()
        del raw

        if len(arr) == 0:
            raise RuntimeError(f"Ses dosyası boş: {audio_path}")

        defines = data.get("defines", {})
        if not defines:
            raise RuntimeError("JSON'da 'defines' bölümü boş veya yok.")

        n_loaded = 0
        n_skip   = 0

        for keycode_str, timing in defines.items():
            # Keycode → sistem key ID
            sys_key = _keycode_str_to_sys(keycode_str)
            if sys_key is None:
                log.debug("JSON: keycode tanınmadı: %s", keycode_str)
                n_skip += 1
                continue

            # Timing parse: [start_ms, duration_ms]
            if not (isinstance(timing, (list, tuple)) and len(timing) >= 2):
                log.debug("JSON: geçersiz timing formatı: %s → %s", keycode_str, timing)
                n_skip += 1
                continue

            start_ms = float(timing[0])
            dur_ms   = float(timing[1])

            # Slice
            snd = _slice_audio(arr, start_ms, dur_ms, freq, n_ch)
            if snd is None:
                log.debug("JSON: slice başarısız: keycode=%s start=%.0f dur=%.0f",
                          keycode_str, start_ms, dur_ms)
                n_skip += 1
                continue

            snd.set_volume(volume)
            self._json_sounds[sys_key] = snd
            n_loaded += 1

        del arr   # Ana numpy dizisi freed

        self._mode = "json"
        skip_str = f", {n_skip} atlandı" if n_skip else ""
        print(f"   [JSON Pack] '{pack_name}' · {audio_path.name}")
        print(f"               {n_loaded} keys loaded{skip_str}")

        # keybinds.json için: sadece pack marker
        return {PACK_FOLDER_KEY: str(folder)}

    def _load_fallback(self, folder: Path, volume: float) -> Dict[str, str]:
        """Klasörde JSON yok → dosya adından tuş eşleme."""
        from sound_mapper import FILENAME_TO_KEY

        keybinds: Dict[str, str] = {}

        for file in sorted(folder.iterdir()):
            if file.suffix.lower() not in SUPPORTED_AUDIO:
                continue
            stem   = file.stem.lower().strip()
            norm   = stem.replace(" ", "_").replace("-", "_")
            key_id = FILENAME_TO_KEY.get(stem) or FILENAME_TO_KEY.get(norm)

            if key_id is None:
                continue

            snd = self._file_cache.get(str(file), volume)
            if snd:
                self._fb_sounds[key_id] = snd
                keybinds[key_id] = str(file)

        self._mode = "fallback"
        print(f"   [Fallback] {len(self._fb_sounds)} keys matched")
        return keybinds
