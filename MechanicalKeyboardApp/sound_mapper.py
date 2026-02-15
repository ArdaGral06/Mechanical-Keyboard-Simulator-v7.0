"""
sound_mapper.py — Ses Dosyası ↔ Tuş Eşleştirme Katmanı
=========================================================
İki mod destekler:

  1. Tek Dosya Modu:
       Kullanıcı bir .wav seçer → opsiyonel olarak dosya adından tuş
       otomatik tahmin edilir → onaylar veya başka tuşa atar.

  2. Klasör Modu:
       Kullanıcı klasör seçer → içindeki .wav dosyaları taranır →
       her dosya adı bilinen bir tuş adıyla eşleştirilir →
       eşleşmeyen dosyalar rapor edilir → onay alınır → toplu atama.

Desteklenen dosya adı örnekleri (uzantısız, büyük/küçük harf fark etmez):
  space, enter, return, backspace, tab, esc, escape,
  shift, shift_l, shift_r, ctrl, ctrl_l, ctrl_r, alt, alt_l, alt_r,
  caps, caps_lock, delete, del, insert, ins,
  home, end, page_up, page_down, pgup, pgdn, pgdown,
  up, down, left, right, arrow_up, arrow_down, arrow_left, arrow_right,
  f1..f12, a..z, 0..9,
  mouse_left, left_click, mouse_right, right_click, mouse_middle, middle_click,
  num0..num9, numpad0..numpad9,
  plus, minus, equals, period, comma, semicolon, slash, backslash,
  quote, backtick, tilde, ...
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

log = logging.getLogger("KeySim.SoundMapper")


# ─────────────────────────────────────────────────────────────
#  DOSYA ADI → TUŞTANMLAYICI HARİTASI
# ─────────────────────────────────────────────────────────────
# Anahtar  : dosya adı (uzantısız, lowercase) — birden fazla alias desteklenir
# Değer    : pynput tuş tanımlayıcısı (InputHandler'ın normalize ettiği format)

_ALIASES: List[Tuple[str, str]] = [
    # ── Boşluk / Enter / Büyük tuşlar ──────────────────────────
    ("space",       "Key.space"),
    ("enter",       "Key.enter"),
    ("return",      "Key.enter"),
    ("ret",         "Key.enter"),
    ("backspace",   "Key.backspace"),
    ("bs",          "Key.backspace"),
    ("back",        "Key.backspace"),
    ("tab",         "Key.tab"),
    ("escape",      "Key.escape"),
    ("esc",         "Key.escape"),
    ("delete",      "Key.delete"),
    ("del",         "Key.delete"),
    ("insert",      "Key.insert"),
    ("ins",         "Key.insert"),
    ("caps_lock",   "Key.caps_lock"),
    ("caps",        "Key.caps_lock"),
    ("capslock",    "Key.caps_lock"),

    # ── Modifier tuşlar ────────────────────────────────────────
    ("shift",       "Key.shift_l"),
    ("shift_l",     "Key.shift_l"),
    ("shift_left",  "Key.shift_l"),
    ("lshift",      "Key.shift_l"),
    ("shift_r",     "Key.shift_r"),
    ("shift_right", "Key.shift_r"),
    ("rshift",      "Key.shift_r"),

    ("ctrl",        "Key.ctrl_l"),
    ("ctrl_l",      "Key.ctrl_l"),
    ("ctrl_left",   "Key.ctrl_l"),
    ("control",     "Key.ctrl_l"),
    ("lctrl",       "Key.ctrl_l"),
    ("ctrl_r",      "Key.ctrl_r"),
    ("ctrl_right",  "Key.ctrl_r"),
    ("rctrl",       "Key.ctrl_r"),

    ("alt",         "Key.alt_l"),
    ("alt_l",       "Key.alt_l"),
    ("alt_left",    "Key.alt_l"),
    ("lalt",        "Key.alt_l"),
    ("alt_r",       "Key.alt_r"),
    ("alt_right",   "Key.alt_r"),
    ("ralt",        "Key.alt_r"),
    ("altgr",       "Key.alt_gr"),
    ("alt_gr",      "Key.alt_gr"),

    # ── Navigasyon ─────────────────────────────────────────────
    ("home",        "Key.home"),
    ("end",         "Key.end"),
    ("page_up",     "Key.page_up"),
    ("pageup",      "Key.page_up"),
    ("pgup",        "Key.page_up"),
    ("pg_up",       "Key.page_up"),
    ("page_down",   "Key.page_down"),
    ("pagedown",    "Key.page_down"),
    ("pgdn",        "Key.page_down"),
    ("pgdown",      "Key.page_down"),
    ("pg_down",     "Key.page_down"),

    # ── Ok tuşları ─────────────────────────────────────────────
    ("up",          "Key.up"),
    ("arrow_up",    "Key.up"),
    ("arrowup",     "Key.up"),
    ("down",        "Key.down"),
    ("arrow_down",  "Key.down"),
    ("arrowdown",   "Key.down"),
    ("left",        "Key.left"),
    ("arrow_left",  "Key.left"),
    ("arrowleft",   "Key.left"),
    ("right",       "Key.right"),
    ("arrow_right", "Key.right"),
    ("arrowright",  "Key.right"),

    # ── Sistem tuşları ─────────────────────────────────────────
    ("print_screen","Key.print_screen"),
    ("printscreen", "Key.print_screen"),
    ("prtsc",       "Key.print_screen"),
    ("scroll_lock", "Key.scroll_lock"),
    ("scrolllock",  "Key.scroll_lock"),
    ("num_lock",    "Key.num_lock"),
    ("numlock",     "Key.num_lock"),
    ("pause",       "Key.pause"),
    ("menu",        "Key.menu"),
    ("win",         "Key.cmd"),
    ("cmd",         "Key.cmd"),
    ("super",       "Key.cmd"),
    ("win_r",       "Key.cmd_r"),
    ("cmd_r",       "Key.cmd_r"),

    # ── Fare ───────────────────────────────────────────────────
    ("mouse_left",    "Button.left"),
    ("left_click",    "Button.left"),
    ("lclick",        "Button.left"),
    ("mouse_right",   "Button.right"),
    ("right_click",   "Button.right"),
    ("rclick",        "Button.right"),
    ("mouse_middle",  "Button.middle"),
    ("middle_click",  "Button.middle"),
    ("mclick",        "Button.middle"),
    ("scroll_up",     "Button.scroll_up"),
    ("scroll_down",   "Button.scroll_down"),

    # ── Numpad ─────────────────────────────────────────────────
    *[(f"num{i}",    f"Key.num_{i}") for i in range(10)],
    *[(f"numpad{i}", f"Key.num_{i}") for i in range(10)],
    *[(f"kp{i}",     f"Key.num_{i}") for i in range(10)],

    # ── Noktalama / Semboller ──────────────────────────────────
    ("period",        "."),
    ("dot",           "."),
    ("comma",         ","),
    ("semicolon",     ";"),
    ("colon",         ":"),
    ("slash",         "/"),
    ("backslash",     "\\"),
    ("quote",         "'"),
    ("doublequote",   '"'),
    ("apostrophe",    "'"),
    ("backtick",      "`"),
    ("tilde",         "~"),
    ("exclamation",   "!"),
    ("at",            "@"),
    ("hash",          "#"),
    ("dollar",        "$"),
    ("percent",       "%"),
    ("caret",         "^"),
    ("ampersand",     "&"),
    ("asterisk",      "*"),
    ("star",          "*"),
    ("minus",         "-"),
    ("hyphen",        "-"),
    ("underscore",    "_"),
    ("plus",          "+"),
    ("equals",        "="),
    ("equal",         "="),
    ("open_bracket",  "["),
    ("close_bracket", "]"),
    ("open_brace",    "{"),
    ("close_brace",   "}"),
    ("open_paren",    "("),
    ("close_paren",   ")"),
    ("pipe",          "|"),
    ("less",          "<"),
    ("greater",       ">"),
    ("question",      "?"),
]

# F1–F12 tuşları
_ALIASES += [(f"f{i}", f"Key.f{i}") for i in range(1, 13)]

# a–z (tek harf dosya adları)
_ALIASES += [(c, c) for c in "abcdefghijklmnopqrstuvwxyz"]

# 0–9 (tek rakam dosya adları)
_ALIASES += [(str(d), str(d)) for d in range(10)]

# Büyük harf varyantları (A.wav → a)
_ALIASES += [(c.upper(), c) for c in "abcdefghijklmnopqrstuvwxyz"]

# Sözlük olarak derle — son alias kazanır (genel → özel sıra)
FILENAME_TO_KEY: Dict[str, str] = {alias: key for alias, key in _ALIASES}


# ─────────────────────────────────────────────────────────────
#  YARDIMCI: TKİNTER KONTROLÜ
# ─────────────────────────────────────────────────────────────
def _has_tkinter() -> bool:
    """tkinter ve görsel display kullanılabilir mi?"""
    try:
        import tkinter as tk
        root = tk.Tk()
        root.withdraw()
        root.destroy()
        return True
    except Exception:
        return False


# ─────────────────────────────────────────────────────────────
#  DOSYA / KLASÖR SEÇİCİ (GUI)
# ─────────────────────────────────────────────────────────────
def pick_file_gui(title: str = "Select .wav file") -> Optional[Path]:
    """
    tkinter filedialog ile tek .wav dosyası seç.
    Döner: seçilen dosyanın Path'i, iptal → None.
    """
    try:
        import tkinter as tk
        from tkinter import filedialog
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        path = filedialog.askopenfilename(
            title      = title,
            filetypes  = [("WAV files", "*.wav"), ("All files", "*.*")],
            parent     = root,
        )
        root.destroy()
        return Path(path) if path else None
    except Exception as exc:
        log.warning("GUI file picker error: %s", exc)
        return None


def pick_folder_gui(title: str = "Select sound folder") -> Optional[Path]:
    """
    tkinter filedialog ile klasör seç.
    Döner: seçilen klasörün Path'i, iptal → None.
    """
    try:
        import tkinter as tk
        from tkinter import filedialog
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        path = filedialog.askdirectory(title=title, parent=root)
        root.destroy()
        return Path(path) if path else None
    except Exception as exc:
        log.warning("GUI folder picker error: %s", exc)
        return None


# ─────────────────────────────────────────────────────────────
#  DOSYA ADI → TUŞTANMLAYICI TAHMİN
# ─────────────────────────────────────────────────────────────
def guess_key_from_filename(wav_path: Path) -> Optional[str]:
    """
    Dosya adından tuş tanımlayıcısını tahmin et.

    Örnekler:
      space.wav       → "Key.space"
      Enter.wav       → "Key.enter"
      W.wav           → "w"
      backspace.wav   → "Key.backspace"
      mouse_left.wav  → "Button.left"
      f5.wav          → "Key.f5"
      Bilinmeyen.wav  → None
    """
    stem = wav_path.stem.lower().strip()
    # Doğrudan eşleşme
    if stem in FILENAME_TO_KEY:
        return FILENAME_TO_KEY[stem]
    # Boşlukları alt çizgiye çevir (örn. "shift left.wav")
    stem_norm = stem.replace(" ", "_").replace("-", "_")
    if stem_norm in FILENAME_TO_KEY:
        return FILENAME_TO_KEY[stem_norm]
    return None


# ─────────────────────────────────────────────────────────────
#  KLASÖR TARAMA
# ─────────────────────────────────────────────────────────────
class FolderScanResult:
    """Klasör tarama sonucu."""

    def __init__(self) -> None:
        self.matched  : Dict[str, Path] = {}   # key_id  → wav_path
        self.unmatched: List[Path]      = []   # eşleşemeyen dosyalar
        self.total    : int             = 0

    def summary(self) -> str:
        lines = [
            f"  Toplam .wav: {self.total}",
            f"  Eşlenen    : {len(self.matched)}",
            f"  Eşlenemeyen: {len(self.unmatched)}",
        ]
        if self.matched:
            lines.append("\n  ✔ Eşlenen tuşlar:")
            for kid, p in sorted(self.matched.items()):
                lines.append(f"     {p.name:<28} → {kid}")
        if self.unmatched:
            lines.append("\n  ✘ Eşlenemeyen dosyalar (atlandı):")
            for p in self.unmatched:
                lines.append(f"     {p.name}")
        return "\n".join(lines)


def scan_folder(folder: Path) -> FolderScanResult:
    """
    Klasördeki tüm .wav dosyalarını tara ve otomatik eşleştir.

    Döner: FolderScanResult (matched, unmatched, total)
    """
    result = FolderScanResult()
    wav_files = sorted(folder.glob("*.wav")) + sorted(folder.glob("*.WAV"))

    # Tekrar edenler kaldır (case-insensitive sistemlerde glob ikisi de döner)
    seen: set = set()
    unique_wavs: List[Path] = []
    for p in wav_files:
        key = p.stem.lower()
        if key not in seen:
            seen.add(key)
            unique_wavs.append(p)

    result.total = len(unique_wavs)

    for wav in unique_wavs:
        key_id = guess_key_from_filename(wav)
        if key_id:
            result.matched[key_id] = wav
        else:
            result.unmatched.append(wav)

    return result


# ─────────────────────────────────────────────────────────────
#  İNTERAKTİF ATAMA AKIŞI
# ─────────────────────────────────────────────────────────────
def interactive_custom_flow(
    lang          : str,
    current_bindings: Dict[str, str],
) -> Optional[Dict[str, str]]:
    """
    Kullanıcıya iki mod sunar:
      [1] Tek dosya seç → tuşa ata
      [2] Klasör seç → otomatik toplu atama

    Döner: güncellenmiş bindings dict, iptal → None.
    """
    from ui import STRINGS
    s = STRINGS.get(lang, STRINGS["en"])

    use_gui = _has_tkinter()

    print("\n" + "─" * 50)
    print("  Ses Atama Modu / Sound Binding Mode")
    print("─" * 50)
    print("  [1] Tek dosya seç  (.wav)")
    print("  [2] Klasör seç    (otomatik toplu atama)")
    print("  [0] İptal")
    print("─" * 50)

    choice = input("  Seçim / Choice (0/1/2): ").strip()

    if choice == "0" or choice == "":
        return None

    new_bindings = dict(current_bindings)

    # ── MOD 1: Tek Dosya ──────────────────────────────────────
    if choice == "1":
        wav_path = _pick_single_wav(use_gui, s)
        if not wav_path:
            print(f"\n  {s['custom_cancel']}")
            return None

        # Dosya adından tuş tahmini
        guessed = guess_key_from_filename(wav_path)
        if guessed:
            print(f"\n  Dosya adından tahmin: {wav_path.name}  →  [{guessed}]")
            confirm = input("  Onayla / Confirm? (Enter=Evet, n=Hayır): ").strip().lower()
            if confirm != "n":
                new_bindings[guessed] = str(wav_path)
                print(f"\n  ✔ {wav_path.name} → {guessed}")
                return new_bindings

        # Tahmin yok veya reddedildi → tuşa bas
        print(f"\n  {s['custom_press_key']}")
        from input_handler import SingleKeyCapture
        captured = SingleKeyCapture().wait(timeout=30.0)
        if not captured:
            print(f"  {s['custom_cancel']}")
            return None
        new_bindings[captured] = str(wav_path)
        print(f"\n  ✔ {wav_path.name} → {captured}")
        return new_bindings

    # ── MOD 2: Klasör Otomatik Tarama ────────────────────────
    if choice == "2":
        folder = _pick_folder(use_gui)
        if not folder:
            print(f"\n  {s['custom_cancel']}")
            return None

        print(f"\n  Taranıyor: {folder}")
        result = scan_folder(folder)

        if result.total == 0:
            print("  [!] Bu klasörde .wav dosyası bulunamadı.")
            return None

        print(result.summary())

        if not result.matched:
            print("\n  [!] Hiçbir dosya tanınan bir tuş adıyla eşleşmedi.")
            print("      Dosya adlarını şu şekilde düzenleyin: space.wav, enter.wav, a.wav ...")
            return None

        print("\n" + "─" * 50)
        confirm = input("  Bunları kaydet? / Save these? (Enter=Evet, n=Hayır): ").strip().lower()
        if confirm == "n":
            print(f"  {s['custom_cancel']}")
            return None

        for key_id, wav_path in result.matched.items():
            new_bindings[key_id] = str(wav_path)

        print(f"\n  ✔ {len(result.matched)} ses atandı.")
        return new_bindings

    return None


# ─────────────────────────────────────────────────────────────
#  YARDIMCI: DOSYA / KLASÖR SEÇİMİ (GUI + fallback)
# ─────────────────────────────────────────────────────────────
def _pick_single_wav(use_gui: bool, s: dict) -> Optional[Path]:
    """GUI varsa dosya seçici aç, yoksa manuel yol al."""
    if use_gui:
        print("  [Dosya seçici pencere açılıyor / File picker opening...]")
        path = pick_file_gui("WAV dosyası seçin / Select WAV file")
        if path and path.exists() and path.suffix.lower() == ".wav":
            return path
        if path:
            print(f"  [!] {s['custom_error']}")
            return None
        # Pencereyi kapattıysa manuel dene
    print(f"\n  {s['custom_enter_path']}")
    raw = input("  Yol/Path: ").strip().strip('"').strip("'")
    if not raw:
        return None
    p = Path(raw)
    if p.exists() and p.suffix.lower() == ".wav":
        return p
    print(f"  [!] {s['custom_error']}")
    return None


def _pick_folder(use_gui: bool) -> Optional[Path]:
    """GUI varsa klasör seçici aç, yoksa manuel yol al."""
    if use_gui:
        print("  [Klasör seçici pencere açılıyor / Folder picker opening...]")
        path = pick_folder_gui("Ses klasörünü seçin / Select sound folder")
        if path and path.is_dir():
            return path
        if path:
            print("  [!] Geçersiz klasör.")
            return None
    print("\n  Klasör yolunu girin / Enter folder path:")
    raw = input("  Yol/Path: ").strip().strip('"').strip("'")
    if not raw:
        return None
    p = Path(raw)
    if p.is_dir():
        return p
    print("  [!] Klasör bulunamadı.")
    return None
