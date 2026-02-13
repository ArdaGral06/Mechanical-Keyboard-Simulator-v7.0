"""
dsp.py — Ses İşleme (DSP) Katmanı
==================================
Runtime sırasında ÇALIŞMAZ.
Yalnızca başlangıçta ses havuzları oluşturulurken çağrılır.

Fonksiyon hiyerarşisi:
  build_pool()
    └─ build_variation()
         ├─ pitch_shift()
         ├─ highpass()
         ├─ bass_boost()
         ├─ presence_boost()
         ├─ reverb_tail()
         └─ normalize()
"""

from __future__ import annotations
import gc
import logging
from fractions import Fraction
from typing import Any, Dict

import numpy as np
from scipy.signal import resample_poly, butter, sosfilt

log = logging.getLogger("KeySim.DSP")

# ── Tip takma adları ──────────────────────────────────────────
AudioF32  = np.ndarray   # float32, interleaved stereo veya mono
Preset    = Dict[str, Any]


# ─────────────────────────────────────────────────────────────
#  TEMİZ FİLTRE YARDIMCILARI
# ─────────────────────────────────────────────────────────────
def _apply_sos(audio: AudioF32, n_ch: int, sos: np.ndarray) -> AudioF32:
    """Stereo veya mono ses verisine SOS filtresi uygula."""
    if n_ch == 2:
        out       = np.empty_like(audio)
        out[0::2] = sosfilt(sos, audio[0::2].astype(np.float64)).astype(np.float32)
        out[1::2] = sosfilt(sos, audio[1::2].astype(np.float64)).astype(np.float32)
        return out
    return sosfilt(sos, audio.astype(np.float64)).astype(np.float32)


def _shelf_boost(audio: AudioF32, n_ch: int, sos: np.ndarray,
                 linear_gain: float) -> AudioF32:
    """Shelving boost: orijinal + (filtrelenmiş × gain)."""
    if n_ch == 2:
        out       = np.empty_like(audio)
        for sl in (slice(None, None, 2), slice(1, None, 2)):
            x64      = audio[sl].astype(np.float64)
            out[sl]  = (x64 + sosfilt(sos, x64) * linear_gain).astype(np.float32)
        return out
    x64 = audio.astype(np.float64)
    return (x64 + sosfilt(sos, x64) * linear_gain).astype(np.float32)


# ─────────────────────────────────────────────────────────────
#  DSP FONKSİYONLARI
# ─────────────────────────────────────────────────────────────
def pitch_shift(audio: AudioF32, factor: float, n_ch: int) -> AudioF32:
    """
    resample_poly tabanlı pitch shift — STFT'den temiz.

    factor < 1.0  →  Daha kalın/derin ses (heavy keys: 0.70–0.82)
    factor = 1.0  →  Değişiklik yok
    factor > 1.0  →  Daha tiz ses

    Anti-alias filtresi dahil → artifact yok.
    """
    if abs(factor - 1.0) < 0.002:
        return audio.copy()

    frac       = Fraction(1.0 / factor).limit_denominator(150)
    up, down   = frac.numerator, frac.denominator

    if n_ch == 2:
        L  = resample_poly(audio[0::2].astype(np.float64), up, down)
        R  = resample_poly(audio[1::2].astype(np.float64), up, down)
        n  = min(len(L), len(R))
        out       = np.empty(n * 2, dtype=np.float32)
        out[0::2] = L[:n].astype(np.float32)
        out[1::2] = R[:n].astype(np.float32)
        return out

    return resample_poly(audio.astype(np.float64), up, down).astype(np.float32)


def highpass(audio: AudioF32, n_ch: int, sr: int, fc_hz: float) -> AudioF32:
    """
    DC offset ve <fc_hz gürültüyü keser.
    Bass boost öncesi uygulanmazsa distorsiyon oluşur.
    """
    nyq = sr / 2.0
    sos = butter(2, fc_hz / nyq, btype="high", output="sos")
    return _apply_sos(audio, n_ch, sos)


def bass_boost(audio: AudioF32, n_ch: int, sr: int,
               gain_db: float, fc_hz: float) -> AudioF32:
    """
    Low-shelf boost — heavy key'lerin 'THUNK' karakteri.
    gain_db: istenen kazanç (6–10 dB tipik)
    fc_hz:   kesim frekansı (260–400 Hz tipik)
    """
    if gain_db <= 0.0:
        return audio
    nyq        = sr / 2.0
    sos        = butter(2, fc_hz / nyq, btype="low", output="sos")
    lin_gain   = 10.0 ** (gain_db / 20.0) - 1.0
    return _shelf_boost(audio, n_ch, sos, lin_gain)


def presence_boost(audio: AudioF32, n_ch: int, sr: int,
                   gain_db: float, fc_hz: float) -> AudioF32:
    """
    High-shelf presence boost — normal key TIK netliği (2–5 kHz).
    gain_db: istenen kazanç (1.5–3 dB tipik), 0.5× zayıflatılır.
    """
    if gain_db <= 0.0:
        return audio
    nyq      = sr / 2.0
    sos      = butter(2, fc_hz / nyq, btype="high", output="sos")
    lin_gain = (10.0 ** (gain_db / 20.0) - 1.0) * 0.5
    return _shelf_boost(audio, n_ch, sos, lin_gain)


def reverb_tail(audio: AudioF32, n_ch: int, sr: int,
                decay: float, delay_s: float) -> AudioF32:
    """
    Gecikme tabanlı tek-yansıma reverb.
    fftconvolve'a göre avantajlar: clipping yok, hızlı, kararlı.
    """
    delay_n = int(delay_s * sr)
    if n_ch == 2:
        delay_n *= 2
    if delay_n >= len(audio):
        return audio.copy()
    out = audio.copy()
    out[delay_n:] += audio[: len(audio) - delay_n] * decay
    return out


def normalize(audio: AudioF32, target: float) -> AudioF32:
    """
    Peak normalize — hedef lineer genlik (örn. 0.25 = -12 dBFS).

    Neden -12 dBFS (0.25)?
      N eş zamanlı ses × 0.25 / √N = 0.25√N.
      N=16 → 0.25×4 = 1.0 → int16 asla taşmaz.
    """
    peak = float(np.max(np.abs(audio)))
    if peak > 1e-7:
        return (audio * (target / peak)).astype(np.float32)
    return audio.astype(np.float32)


# ─────────────────────────────────────────────────────────────
#  PRESET TABLANSINDAN VARİYASYON OLUŞTUR
# ─────────────────────────────────────────────────────────────
def build_variation(
    audio         : AudioF32,
    n_ch          : int,
    sr            : int,
    preset_name   : str,
    presets       : dict,
    rng           : np.random.RandomState,
    norm_target   : float,
) -> bytes:
    """
    Bir preset + random seed'den tek ses varyasyonu üretir.
    Dönen değer: int16 PCM bytes (pygame.mixer.Sound(buffer=...) için).

    Numpy array döndürülmez → bellek hemen serbest bırakılır.
    """
    p    = presets[preset_name]
    hp   = presets["highpass_fc_hz"]
    seed = float(rng.uniform(0.0, 1.0))

    # ── Pitch ────────────────────────────────────────────────
    pitch  = p["pitch"]["min"] + seed * p["pitch"]["range"]

    # ── Bass ─────────────────────────────────────────────────
    if "bass" in p:
        b       = p["bass"]
        bass_db = b["db_min"] + seed * b["db_range"]
        bass_fc = b["fc_min"] + seed * b.get("fc_range", 0.0)
    else:
        bass_db = float(p.get("bass_db", 0.0))
        bass_fc = float(p.get("bass_fc", 350.0))

    # ── Presence ─────────────────────────────────────────────
    pres   = p.get("presence", {})
    pres_db = pres.get("db_min", 0.0) + seed * pres.get("db_range", 0.0)
    pres_fc = pres.get("fc", 2200.0)

    # ── Reverb ───────────────────────────────────────────────
    rv      = p["reverb"]
    rev_dec = rv["decay_min"] + seed * rv["decay_range"]
    rev_del = rv["delay_min"] + seed * rv["delay_range"]

    # ── İşlem zinciri ─────────────────────────────────────────
    proc = pitch_shift(audio, pitch, n_ch)
    proc = highpass(proc, n_ch, sr, hp)
    proc = bass_boost(proc, n_ch, sr, bass_db, bass_fc)
    proc = presence_boost(proc, n_ch, sr, pres_db, pres_fc)
    proc = reverb_tail(proc, n_ch, sr, rev_dec, rev_del)
    proc = normalize(proc, norm_target)

    # float32 → int16 PCM bytes
    pcm  = np.clip(proc * 32767.0, -32768, 32767).astype(np.int16)
    data = pcm.tobytes()

    # Numpy array'i hemen serbest bırak → RAM tasarrufu
    del proc, pcm
    return data


def build_pool(
    audio       : AudioF32,
    n_ch        : int,
    sr          : int,
    preset_name : str,
    presets     : dict,
    pool_size   : int,
    norm_target : float,
    seed_base   : int,
    label       : str,
) -> list[bytes]:
    """
    pool_size kadar varyasyon üretir.
    Her varyasyon bytes olarak döner (pygame.Sound sonra oluşturulur).
    """
    rng   = np.random.RandomState(seed_base)
    pool  : list[bytes] = []

    for i in range(pool_size):
        data = build_variation(audio, n_ch, sr, preset_name,
                               presets, rng, norm_target)
        pool.append(data)
        print(f"   [{label:>7}] {i + 1:>2}/{pool_size}", end="\r", flush=True)

    print()
    gc.collect()   # Pitch-shift'in numpy geçici tamponu temizle
    return pool
