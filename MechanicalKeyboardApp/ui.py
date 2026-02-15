"""
ui.py — Terminal Arayüz Katmanı
=================================
Tüm ekran çıktısı bu modülde merkezi olarak yönetilir.
Engine, input, main katmanları bu modülü çağırır; bu modül onları çağırmaz.
"""

from __future__ import annotations

import os
from typing import Any, Dict

# ─────────────────────────────────────────────────────────────
#  DİL METİNLERİ
# ─────────────────────────────────────────────────────────────
STRINGS: Dict[str, Dict[str, str]] = {
    "tr": {
        # ── Ana arayüz ───────────────────────────────────────
        "title"              : "MEKANİK KLAVYE SİMÜLATÖRÜ v7.0",
        "subtitle"           : "Thread-Safe · Zero-Latency · Professional",
        "vol"                : "SES",
        "poly"               : "POLİFONİ",
        "last"               : "SON İŞLEM",
        "cmds"               : "KOMUTLAR",
        "cmd_vol"            : "0-100 / 0.0-1.0  →  Ses seviyesi",
        "cmd_custom"         : "c / custom        →  Tuş ata",
        "cmd_repeat"         : "r / repeat        →  Tekrar modu",
        "cmd_mute"           : "0                 →  Sessize al",
        "cmd_exit"           : "q / exit          →  Çıkış",
        "prompt"             : "Komut: ",
        "start"              : "Ses motoru hazır. Gecikme: ~11ms. Yazabilirsiniz!",
        "loading"            : "Ses havuzları oluşturuluyor...",
        "ready"              : "Hazır! (~11ms gecikme)",
        "vol_changed"        : "Ses",
        "vol_success"        : "Ses seviyesi güncellendi.",
        "invalid"            : "Geçersiz komut.",
        "custom_enter_path"  : "Ses dosyası yolunu yapıştırın (.wav):",
        "custom_press_key"   : "Atamak istediğiniz tuşa basın...",
        "custom_success"     : "Kaydedildi!",
        "custom_error"       : "Hata: Dosya bulunamadı veya .wav değil!",
        "custom_cancel"      : "İptal.",
        "waiting"            : "Bekleniyor...",
        "closing"            : "Kapatılıyor...",
        "rep_on"             : "Tekrar: AÇIK",
        "rep_off"            : "Tekrar: KAPALI",
        "reloading"          : "Yeniden yükleniyor...",
        "lang_prompt"        : "Dil / Language (tr/en): ",

        # ── Ses atama (sound_mapper) ──────────────────────────
        "mapper_mode_title"  : "Ses Atama Modu",
        "mapper_single"      : "[1] Tek dosya seç  (.wav)",
        "mapper_folder"      : "[2] Klasör seç    (otomatik toplu atama)",
        "mapper_cancel_opt"  : "[0] İptal",
        "mapper_choice"      : "Seçim (0/1/2): ",
        "mapper_opening_file": "Dosya seçici pencere açılıyor...",
        "mapper_opening_folder": "Klasör seçici pencere açılıyor...",
        "mapper_path_prompt" : "Ses dosyası yolunu yapıştırın (.wav):",
        "mapper_folder_prompt": "Klasör yolunu girin:",
        "mapper_scanning"    : "Taranıyor",
        "mapper_no_wav"      : "Bu klasörde .wav dosyası bulunamadı.",
        "mapper_no_match"    : "Hiçbir dosya tanınan bir tuş adıyla eşleşmedi.",
        "mapper_no_match_hint": "Dosya adlarını şu şekilde düzenleyin: space.wav, enter.wav, a.wav ...",
        "mapper_total"       : "Toplam .wav ",
        "mapper_matched"     : "Eşlenen    ",
        "mapper_unmatched"   : "Eşlenemeyen",
        "mapper_matched_keys": "Eşlenen tuşlar",
        "mapper_skipped"     : "Eşlenemeyen dosyalar (atlandı)",
        "mapper_confirm_save": "Bunları kaydet? (Enter=Evet, n=Hayır): ",
        "mapper_saved_n"     : "ses atandı",
        "mapper_guess_found" : "Dosya adından tahmin",
        "mapper_confirm_guess": "Onayla? (Enter=Evet, n=Hayır): ",
        "mapper_invalid_folder": "Geçersiz klasör.",
        "mapper_folder_nf"   : "Klasör bulunamadı.",
    },
    "en": {
        # ── Main UI ───────────────────────────────────────────
        "title"              : "MECHANICAL KEYBOARD SIMULATOR v7.0",
        "subtitle"           : "Thread-Safe · Zero-Latency · Professional",
        "vol"                : "VOLUME",
        "poly"               : "POLYPHONY",
        "last"               : "LAST ACTION",
        "cmds"               : "COMMANDS",
        "cmd_vol"            : "0-100 / 0.0-1.0  →  Volume",
        "cmd_custom"         : "c / custom        →  Bind key",
        "cmd_repeat"         : "r / repeat        →  Repeat mode",
        "cmd_mute"           : "0                 →  Mute",
        "cmd_exit"           : "q / exit          →  Quit",
        "prompt"             : "Command: ",
        "start"              : "Engine ready. Latency: ~11ms. Start typing!",
        "loading"            : "Building sound pools...",
        "ready"              : "Ready! (~11ms latency)",
        "vol_changed"        : "Volume",
        "vol_success"        : "Volume updated.",
        "invalid"            : "Invalid command.",
        "custom_enter_path"  : "Paste full .wav file path:",
        "custom_press_key"   : "Press the key you want to bind...",
        "custom_success"     : "Saved!",
        "custom_error"       : "Error: File not found or not .wav!",
        "custom_cancel"      : "Cancelled.",
        "waiting"            : "Waiting...",
        "closing"            : "Closing...",
        "rep_on"             : "Repeat: ON",
        "rep_off"            : "Repeat: OFF",
        "reloading"          : "Reloading...",
        "lang_prompt"        : "Dil / Language (tr/en): ",

        # ── Sound binding (sound_mapper) ──────────────────────
        "mapper_mode_title"  : "Sound Binding Mode",
        "mapper_single"      : "[1] Select single file  (.wav)",
        "mapper_folder"      : "[2] Select folder       (auto bulk assign)",
        "mapper_cancel_opt"  : "[0] Cancel",
        "mapper_choice"      : "Choice (0/1/2): ",
        "mapper_opening_file": "Opening file picker...",
        "mapper_opening_folder": "Opening folder picker...",
        "mapper_path_prompt" : "Paste full .wav file path:",
        "mapper_folder_prompt": "Enter folder path:",
        "mapper_scanning"    : "Scanning",
        "mapper_no_wav"      : "No .wav files found in this folder.",
        "mapper_no_match"    : "No files matched a recognized key name.",
        "mapper_no_match_hint": "Rename your files like: space.wav, enter.wav, a.wav ...",
        "mapper_total"       : "Total .wav  ",
        "mapper_matched"     : "Matched     ",
        "mapper_unmatched"   : "Unmatched   ",
        "mapper_matched_keys": "Matched keys",
        "mapper_skipped"     : "Unmatched files (skipped)",
        "mapper_confirm_save": "Save these? (Enter=Yes, n=No): ",
        "mapper_saved_n"     : "sounds assigned",
        "mapper_guess_found" : "Guessed from filename",
        "mapper_confirm_guess": "Confirm? (Enter=Yes, n=No): ",
        "mapper_invalid_folder": "Invalid folder.",
        "mapper_folder_nf"   : "Folder not found.",
    },
}


# ─────────────────────────────────────────────────────────────
#  ÇIZIM YARDIMCILARI
# ─────────────────────────────────────────────────────────────
def clear_screen() -> None:
    os.system("cls" if os.name == "nt" else "clear")


def _bar(value: float, length: int = 20,
         full: str = "█", empty: str = "░") -> str:
    """Yüzde değerinden ASCII progress bar oluştur."""
    n = max(0, min(length, int(length * value)))
    return full * n + empty * (length - n)


def _row(text: str, width: int) -> str:
    """Kenarları çizgili, belirtilen genişlikte tablo satırı."""
    return f" ║ {text:<{width - 2}} ║"


# ─────────────────────────────────────────────────────────────
#  ANA UI GÜNCELLEME
# ─────────────────────────────────────────────────────────────
def update_ui(
    lang          : str,
    volume        : float,
    active_voices : int,
    polyphony     : int,
    repeat_mode   : bool,
    last_action   : str,
    notification  : str = "",
) -> None:
    """
    Terminal ekranını tamamen yeniden çizer.

    Tüm durum bilgisi parametre olarak geçilir — global state erişimi yok.
    """
    clear_screen()
    s = STRINGS.get(lang, STRINGS["en"])

    vp        = int(volume * 100)
    vol_bar   = _bar(volume, 20)
    voice_bar = _bar(active_voices / max(1, polyphony), 10, "▮", "▯")
    rep_str   = s["rep_on"] if repeat_mode else s["rep_off"]
    act_str   = last_action if last_action else s["waiting"]

    W  = 58
    hr = "═" * W

    print("\n")
    print(f" ╔{hr}╗")
    print(f" ║ {s['title']:^{W}} ║")
    print(f" ║ {s['subtitle']:^{W}} ║")
    print(f" ╠{hr}╣")
    print(_row(f"🔊 {s['vol']:<12}: {vp:>3}%  [{vol_bar}]",        W + 2))
    print(_row(f"🎹 {s['poly']:<12}: {active_voices:>2}/{polyphony}  [{voice_bar}]", W + 2))
    print(_row(f"🔄 {rep_str}",                                     W + 2))
    print(f" ╠{hr}╣")
    print(_row(f"⚡ {s['last']:<12}: {act_str[:W - 18]}",            W + 2))
    print(f" ╠{hr}╣")
    print(_row(f"[ {s['cmds']} ]",                                  W + 2))
    for key in ("cmd_vol", "cmd_custom", "cmd_repeat", "cmd_mute", "cmd_exit"):
        print(_row(f"  {s[key]}",                                   W + 2))
    print(f" ╚{hr}╝")

    if notification:
        print(f"\n  ▶ {notification}")

    print(f"\n  {s['prompt']}", end="", flush=True)


# ─────────────────────────────────────────────────────────────
#  DİL SEÇİMİ
# ─────────────────────────────────────────────────────────────
def select_language() -> str:
    """Başlangıçta dil seçtir. 'tr' veya 'en' döner."""
    clear_screen()
    print("\n  ╔══════════════════════════════════════╗")
    print("  ║  MECHANICAL KEYBOARD SIMULATOR v7.0  ║")
    print("  ╚══════════════════════════════════════╝\n")
    choice = input("  Dil / Language (tr/en): ").strip().lower()
    return "tr" if choice == "tr" else "en"
