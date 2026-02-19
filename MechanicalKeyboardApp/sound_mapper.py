"""
sound_mapper.py — Ses Dosyası ↔ Tuş Eşleştirme Katmanı
=========================================================
İki mod destekler:

  1. Tek Dosya Modu:
       Kullanıcı bir .wav seçer → dosya adından tuş otomatik tahmin edilir
       → onaylar veya tuşa basarak başka tuşa atar.

  2. Klasör Modu:
       Kullanıcı klasör seçer → içindeki .wav dosyaları taranır →
       dosya adından tuş otomatik eşleştirilir → özet gösterilir
       → onay alınır → toplu atama yapılır.

Desteklenen dosya adı örnekleri (büyük/küçük harf fark etmez):
  space, enter, backspace, tab, esc, shift, ctrl, alt,
  caps_lock, delete, home, end, page_up, up, down, left, right,
  f1..f12, a..z, 0..9,
  mouse_left, mouse_right, mouse_middle, num0..num9 ...
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
    ("period",        "."),   ("dot",           "."),
    ("comma",         ","),   ("semicolon",      ";"),
    ("colon",         ":"),   ("slash",          "/"),
    ("backslash",    "\\"),   ("quote",          "'"),
    ("doublequote",  '"'),    ("apostrophe",     "'"),
    ("backtick",      "`"),   ("tilde",          "~"),
    ("exclamation",   "!"),   ("at",             "@"),
    ("hash",          "#"),   ("dollar",         "$"),
    ("percent",       "%"),   ("caret",          "^"),
    ("ampersand",     "&"),   ("asterisk",       "*"),
    ("star",          "*"),   ("minus",          "-"),
    ("hyphen",        "-"),   ("underscore",     "_"),
    ("plus",          "+"),   ("equals",         "="),
    ("equal",         "="),   ("open_bracket",   "["),
    ("close_bracket", "]"),   ("open_brace",     "{"),
    ("close_brace",   "}"),   ("open_paren",     "("),
    ("close_paren",   ")"),   ("pipe",           "|"),
    ("less",          "<"),   ("greater",        ">"),
    ("question",      "?"),
]

# F1–F12
_ALIASES += [(f"f{i}", f"Key.f{i}") for i in range(1, 13)]
# a–z (tek harf)
_ALIASES += [(c, c) for c in "abcdefghijklmnopqrstuvwxyz"]
# 0–9
_ALIASES += [(str(d), str(d)) for d in range(10)]
# Büyük harf varyantları
_ALIASES += [(c.upper(), c) for c in "abcdefghijklmnopqrstuvwxyz"]

FILENAME_TO_KEY: Dict[str, str] = {alias: key for alias, key in _ALIASES}


# ─────────────────────────────────────────────────────────────
#  TKİNTER KONTROLÜ
# ─────────────────────────────────────────────────────────────
def _has_tkinter() -> bool:
    try:
        import tkinter as tk
        root = tk.Tk()
        root.withdraw()
        root.destroy()
        return True
    except Exception:
        return False


# ─────────────────────────────────────────────────────────────
#  GUI SEÇİCİLER
# ─────────────────────────────────────────────────────────────
def pick_file_gui(title: str = "Select .wav file") -> Optional[Path]:
    try:
        import tkinter as tk
        from tkinter import filedialog
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        path = filedialog.askopenfilename(
            title     = title,
            filetypes = [("WAV files", "*.wav"), ("All files", "*.*")],
            parent    = root,
        )
        root.destroy()
        return Path(path) if path else None
    except Exception as exc:
        log.warning("GUI file picker error: %s", exc)
        return None


def pick_folder_gui(title: str = "Select sound folder") -> Optional[Path]:
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

    space.wav  → "Key.space"  |  W.wav → "w"  |  bilinmeyen.wav → None
    """
    stem = wav_path.stem.lower().strip()
    if stem in FILENAME_TO_KEY:
        return FILENAME_TO_KEY[stem]
    norm = stem.replace(" ", "_").replace("-", "_")
    return FILENAME_TO_KEY.get(norm)


# ─────────────────────────────────────────────────────────────
#  KLASÖR TARAMA
# ─────────────────────────────────────────────────────────────
class FolderScanResult:
    def __init__(self) -> None:
        self.matched  : Dict[str, Path] = {}
        self.unmatched: List[Path]      = []
        self.total    : int             = 0

    def summary(self, s: dict) -> str:
        """Lokalize özet metni üret. s = STRINGS[lang]"""
        lines = [
            f"  {s['mapper_total']}: {self.total}",
            f"  {s['mapper_matched']}: {len(self.matched)}",
            f"  {s['mapper_unmatched']}: {len(self.unmatched)}",
        ]
        if self.matched:
            lines.append(f"\n  ✔ {s['mapper_matched_keys']}:")
            for kid, p in sorted(self.matched.items()):
                lines.append(f"     {p.name:<28} → {kid}")
        if self.unmatched:
            lines.append(f"\n  ✘ {s['mapper_skipped']}:")
            for p in self.unmatched:
                lines.append(f"     {p.name}")
        return "\n".join(lines)


def scan_folder(folder: Path) -> FolderScanResult:
    """Klasördeki tüm .wav dosyalarını tara ve otomatik eşleştir."""
    result   = FolderScanResult()
    wav_files = sorted(folder.glob("*.wav")) + sorted(folder.glob("*.WAV"))

    seen: set = set()
    unique: List[Path] = []
    for p in wav_files:
        if p.stem.lower() not in seen:
            seen.add(p.stem.lower())
            unique.append(p)

    result.total = len(unique)
    for wav in unique:
        key_id = guess_key_from_filename(wav)
        if key_id:
            result.matched[key_id] = wav
        else:
            result.unmatched.append(wav)

    return result


# ─────────────────────────────────────────────────────────────
#  İNTERAKTİF ATAMA AKIŞI — ui.STRINGS ile lokalize
# ─────────────────────────────────────────────────────────────
def interactive_custom_flow(
    lang            : str,
    current_bindings: Dict[str, str],
) -> Optional[Dict[str, str]]:
    """
    Kullanıcıya iki mod sunar:
      [1] Tek dosya seç → tuşa ata
      [2] Klasör seç   → otomatik toplu atama

    Döner: güncellenmiş bindings dict, iptal → None.
    """
    from ui import STRINGS
    s       = STRINGS.get(lang, STRINGS["en"])
    use_gui = _has_tkinter()

    print("\n" + "─" * 52)
    print(f"  {s['mapper_mode_title']}")
    print("─" * 52)
    print(f"  {s['mapper_single']}")
    print(f"  {s['mapper_folder']}")
    print(f"  {s['mapper_cancel_opt']}")
    print("─" * 52)

    choice = input(f"  {s['mapper_choice']}").strip()

    if choice == "0" or choice == "":
        return None

    new_bindings = dict(current_bindings)

    # ── MOD 1: Tek Dosya ──────────────────────────────────────
    if choice == "1":
        wav_path = _pick_single_wav(use_gui, s)
        if not wav_path:
            print(f"\n  {s['custom_cancel']}")
            return None

        guessed = guess_key_from_filename(wav_path)
        if guessed:
            print(f"\n  {s['mapper_guess_found']}: {wav_path.name}  →  [{guessed}]")
            confirm = input(f"  {s['mapper_confirm_guess']}").strip().lower()
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

    # ── MOD 2: Klasör ─────────────────────────────────────────
    if choice == "2":
        folder = _pick_folder(use_gui, s)
        if not folder:
            print(f"\n  {s['custom_cancel']}")
            return None

        # ── ADIM 1: JSON TARAMA ───────────────────────────────────
        # Klasörde .json varsa → JSON pack modu (Mechvibes format)
        json_files = sorted(folder.glob("*.json"))
        if json_files:
            try:
                from sound_pack_loader import PACK_FOLDER_KEY, peek_json_info
                info = peek_json_info(json_files[0])

                print("\n" + "─" * 52)
                print(f"  ✦ {s.get('pack_detected', 'JSON soundpack tespit edildi')}: {json_files[0].name}")
                print(f"    {s.get('pack_name', 'İsim')}: {info['name']}")
                print(f"    {s.get('pack_type', 'Tip')}: {info['type']}")
                print(f"    {s.get('pack_sound', 'Ses')}: {info['sound_file']}")
                print(f"    {s.get('pack_keys', 'Key count')}: {info['key_count']}")
                print("─" * 52)
                print(f"  {s.get('pack_hint', 'Single audio + keycode sprite system (Mechvibes)')}")
                print("─" * 52)

                _confirm_suffix = "(Enter=Yes, n=No)" if lang == "en" else "(Enter=Evet, n=Hayir)"
                confirm = input(f"  {s.get('pack_use', 'Use this pack?')} {_confirm_suffix}: ").strip().lower()

                if confirm != "n":
                    # JSON pack modu aktif
                    # engine.reload_sounds() PACK_FOLDER_KEY görünce paketi yükler
                    print(f"\n  ✔ {s.get('pack_activated', 'JSON pack modu aktif edildi.')}")
                    return {PACK_FOLDER_KEY: str(folder)}

                # Kullanıcı reddetti → klasik tarama
                print(f"\n  {s.get('pack_skipped', 'Pack atlandı, klasik .wav taraması başlıyor...')}")

            except ImportError:
                pass  # sound_pack_loader.py yoksa klasik moda geç

        # ── ADIM 2: KLASİK .wav TARAMA ────────────────────────────
        print(f"\n  {s['mapper_scanning']}: {folder}")
        result = scan_folder(folder)

        if result.total == 0:
            print(f"  [!] {s['mapper_no_wav']}")
            return None

        print(result.summary(s))

        if not result.matched:
            print(f"\n  [!] {s['mapper_no_match']}")
            print(f"      {s['mapper_no_match_hint']}")
            return None

        print("\n" + "─" * 52)
        confirm = input(f"  {s['mapper_confirm_save']}").strip().lower()
        if confirm == "n":
            print(f"  {s['custom_cancel']}")
            return None

        for key_id, wav_path in result.matched.items():
            new_bindings[key_id] = str(wav_path)

        n = len(result.matched)
        print(f"\n  ✔ {n} {s['mapper_saved_n']}.")
        return new_bindings

    return None


# ─────────────────────────────────────────────────────────────
#  YARDIMCI: DOSYA / KLASÖR SEÇİMİ (GUI + fallback)
# ─────────────────────────────────────────────────────────────
def _pick_single_wav(use_gui: bool, s: dict) -> Optional[Path]:
    if use_gui:
        print(f"  [{s['mapper_opening_file']}]")
        path = pick_file_gui(s["mapper_mode_title"])
        if path and path.exists() and path.suffix.lower() == ".wav":
            return path
        if path:
            print(f"  [!] {s['custom_error']}")
            return None

    print(f"\n  {s['mapper_path_prompt']}")
    raw = input("  > ").strip().strip('"').strip("'")
    if not raw:
        return None
    p = Path(raw)
    if p.exists() and p.suffix.lower() == ".wav":
        return p
    print(f"  [!] {s['custom_error']}")
    return None


def _pick_folder(use_gui: bool, s: dict) -> Optional[Path]:
    if use_gui:
        print(f"  [{s['mapper_opening_folder']}]")
        path = pick_folder_gui(s["mapper_mode_title"])
        if path and path.is_dir():
            return path
        if path:
            print(f"  [!] {s['mapper_invalid_folder']}")
            return None

    print(f"\n  {s['mapper_folder_prompt']}")
    raw = input("  > ").strip().strip('"').strip("'")
    if not raw:
        return None
    p = Path(raw)
    if p.is_dir():
        return p
    print(f"  [!] {s['mapper_folder_nf']}")
    return None
