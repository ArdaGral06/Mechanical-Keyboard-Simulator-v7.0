"""
dsp.py — Ses İşleme (DSP) Katmanı v2.1 (RAM Optimized)
========================================================
RAM optimizasyonları:
  1. _get_sos() cache — butter() scipy hesabı tekrar kullanılır (CPU+RAM)
  2. _apply_sos()    — float32 conversion temp array kaldırıldı
  3. _shelf_boost()  — aynı, tmp64 reuse ile bir temp array azaldı
  4. pitch_shift()   — L/R explicit del, peak RAM düştü
  5. build_variation() — proc zinciri arasında implicit CPython refcounting
"""

from __future__ import annotations
import gc
import logging
from fractions import Fraction
from typing import Dict, List, Optional, Tuple

import numpy as np
from scipy.signal import resample_poly, butter, sosfilt

log = logging.getLogger("KeySim.DSP")

AudioF32 = np.ndarray

# ─────────────────────────────────────────────────────────────
#  FILTER SOS CACHE
# ─────────────────────────────────────────────────────────────
# OPT: butter() scipy çağrısı aynı parametrelerle tekrar tekrar yapılıyor
# (özellikle sabit fc'li highpass). Cache ile hesap bir kez yapılır.
# fc_norm 7 decimal'e yuvarlanır → farklı ama yakın değerler de hit eder.
# maxsize=256 → ~1KB RAM (SOS matrix küçük, float64 array birkaç satır)
_SOS_CACHE: Dict[Tuple, np.ndarray] = {}
_SOS_CACHE_MAX = 256


def _get_sos(order: int, fc_norm: float, btype: str) -> np.ndarray:
    """Bounded cache'li butter() SOS hesabı."""
    key = (order, round(fc_norm, 7), btype)
    sos = _SOS_CACHE.get(key)
    if sos is None:
        sos = butter(order, fc_norm, btype=btype, output="sos")
        if len(_SOS_CACHE) < _SOS_CACHE_MAX:
            _SOS_CACHE[key] = sos
    return sos


def clear_filter_cache() -> None:
    """reload_sounds() sonrası cache'i temizle (opsiyonel, preset değişirse)."""
    _SOS_CACHE.clear()


# ─────────────────────────────────────────────────────────────
#  FİLTRE YARDIMCILARI
# ─────────────────────────────────────────────────────────────
def _apply_sos(audio: AudioF32, n_ch: int, sos: np.ndarray) -> AudioF32:
    """
    OPT: Eski kod: sosfilt(...).astype(np.float32) → 3 geçici array (float64 slice,
    sosfilt result float64, float32 dönüşümü). Şimdi: tmp64 reuse + implicit
    float64→float32 cast during assignment → 2 geçici array. N/2 × 4 byte tasarruf.
    """
    if n_ch == 2:
        out = np.empty_like(audio)
        tmp = audio[0::2].astype(np.float64)
        tmp = sosfilt(sos, tmp)            # eski tmp freed, yeni float64 result
        out[0::2] = tmp                    # implicit float64→float32, no extra alloc
        del tmp
        tmp = audio[1::2].astype(np.float64)
        tmp = sosfilt(sos, tmp)
        out[1::2] = tmp
        del tmp
        return out
    tmp = audio.astype(np.float64)
    result = sosfilt(sos, tmp).astype(np.float32)
    del tmp
    return result


def _shelf_boost(audio: AudioF32, n_ch: int, sos: np.ndarray,
                 linear_gain: float) -> AudioF32:
    """OPT: Aynı tmp64 reuse yaklaşımı — float32 conversion temp array yok."""
    if n_ch == 2:
        out = np.empty_like(audio)
        for sl in (slice(None, None, 2), slice(1, None, 2)):
            x64     = audio[sl].astype(np.float64)
            filt64  = sosfilt(sos, x64)
            out[sl] = x64 + filt64 * linear_gain   # implicit float64→float32
            del x64, filt64
        return out
    x64    = audio.astype(np.float64)
    filt64 = sosfilt(sos, x64)
    result = (x64 + filt64 * linear_gain).astype(np.float32)
    del x64, filt64
    return result


# ─────────────────────────────────────────────────────────────
#  DSP PRİMİTİFLERİ
# ─────────────────────────────────────────────────────────────
def pitch_shift(audio: AudioF32, factor: float, n_ch: int) -> AudioF32:
    """
    OPT: L, R dizileri out'a kopyalandıktan hemen sonra del → peak RAM düşer.
    Eski kod: L ve R, out atandıktan sonra da yaşıyordu (scope sonuna kadar).
    """
    if abs(factor - 1.0) < 0.002:
        return audio.copy()
    frac     = Fraction(1.0 / factor).limit_denominator(150)
    up, down = frac.numerator, frac.denominator
    if n_ch == 2:
        L  = resample_poly(audio[0::2].astype(np.float64), up, down)
        R  = resample_poly(audio[1::2].astype(np.float64), up, down)
        n  = min(len(L), len(R))
        out       = np.empty(n * 2, dtype=np.float32)
        out[0::2] = L[:n]; del L   # OPT: L kullanılır, hemen freed
        out[1::2] = R[:n]; del R   # OPT: R kullanılır, hemen freed
        return out
    return resample_poly(audio.astype(np.float64), up, down).astype(np.float32)


def highpass(audio: AudioF32, n_ch: int, sr: int, fc_hz: float) -> AudioF32:
    """OPT: _get_sos() ile butter() cache'lendi."""
    sos = _get_sos(2, fc_hz / (sr / 2.0), "high")
    return _apply_sos(audio, n_ch, sos)


def bass_boost(audio: AudioF32, n_ch: int, sr: int,
               gain_db: float, fc_hz: float) -> AudioF32:
    """OPT: _get_sos() cache."""
    if gain_db <= 0.0:
        return audio
    sos      = _get_sos(2, fc_hz / (sr / 2.0), "low")
    lin_gain = 10.0 ** (gain_db / 20.0) - 1.0
    return _shelf_boost(audio, n_ch, sos, lin_gain)


def presence_boost(audio: AudioF32, n_ch: int, sr: int,
                   gain_db: float, fc_hz: float) -> AudioF32:
    """OPT: _get_sos() cache."""
    if gain_db <= 0.0:
        return audio
    sos      = _get_sos(2, fc_hz / (sr / 2.0), "high")
    lin_gain = (10.0 ** (gain_db / 20.0) - 1.0) * 0.5
    return _shelf_boost(audio, n_ch, sos, lin_gain)


def reverb_tail(audio: AudioF32, n_ch: int, sr: int,
                decay: float, delay_s: float) -> AudioF32:
    delay_n = int(delay_s * sr)
    if n_ch == 2:
        delay_n *= 2
    if delay_n >= len(audio):
        return audio.copy()
    out = audio.copy()
    out[delay_n:] += audio[: len(audio) - delay_n] * decay
    return out


def normalize(audio: AudioF32, target: float) -> AudioF32:
    peak = float(np.max(np.abs(audio)))
    if peak > 1e-7:
        return (audio * (target / peak)).astype(np.float32)
    return audio.astype(np.float32)


# ─────────────────────────────────────────────────────────────
#  NORMAL HAVUZ — VARİYASYON OLUŞTURİCİ
# ─────────────────────────────────────────────────────────────
def build_variation(
    audio        : AudioF32,
    n_ch         : int,
    sr           : int,
    preset_name  : str,
    presets      : dict,
    rng          : np.random.RandomState,
    norm_target  : float,
    fast_modifier: Optional[dict] = None,
) -> bytes:
    """
    Preset + seed → int16 PCM bytes.
    OPT: proc zincirinde CPython refcount anında freed → peak RAM düşük.
    """
    p    = presets[preset_name]
    hp   = p.get("highpass_override_fc", presets["highpass_fc_hz"])
    seed = float(rng.uniform(0.0, 1.0))

    pitch     = p["pitch"]["min"] + seed * p["pitch"]["range"]
    rand_p    = p.get("random_pitch_range", 0.0)
    if rand_p > 0:
        pitch += (float(rng.uniform()) - 0.5) * 2.0 * rand_p
    if fast_modifier:
        pitch += fast_modifier.get("pitch_add", 0.0)
    pitch = max(0.40, pitch)

    if "bass" in p:
        b       = p["bass"]
        bass_db = b["db_min"] + seed * b["db_range"]
        bass_fc = b["fc_min"] + seed * b.get("fc_range", 0.0)
    else:
        bass_db = float(p.get("bass_db", 0.0))
        bass_fc = float(p.get("bass_fc", 350.0))

    pres    = p.get("presence", {})
    pres_db = pres.get("db_min", 0.0) + seed * pres.get("db_range", 0.0)
    pres_fc = pres.get("fc", 2200.0)

    rv      = p["reverb"]
    rev_dec = rv["decay_min"] + seed * rv["decay_range"]
    rev_del = rv["delay_min"] + seed * rv["delay_range"]
    if fast_modifier:
        rev_dec *= fast_modifier.get("reverb_decay_scale", 1.0)

    rand_v     = p.get("random_volume_range", 0.0)
    vol_jitter = 1.0 + (float(rng.uniform()) - 0.5) * 2.0 * rand_v

    # İşlem zinciri — her adımda eski proc CPython refcount=0 → anında freed
    proc = pitch_shift(audio, pitch, n_ch)
    proc = highpass(proc, n_ch, sr, hp)
    proc = bass_boost(proc, n_ch, sr, bass_db, bass_fc)
    proc = presence_boost(proc, n_ch, sr, pres_db, pres_fc)
    proc = reverb_tail(proc, n_ch, sr, rev_dec, rev_del)
    proc = normalize(proc, norm_target)

    if abs(vol_jitter - 1.0) > 0.002:
        proc = np.clip(proc * vol_jitter, -1.0, 1.0).astype(np.float32)

    pcm  = np.clip(proc * 32767.0, -32768, 32767).astype(np.int16)
    data = pcm.tobytes()
    del proc, pcm  # numpy array'leri bytes'a dönüştükten sonra serbest
    return data


def build_pool(
    audio        : AudioF32,
    n_ch         : int,
    sr           : int,
    preset_name  : str,
    presets      : dict,
    pool_size    : int,
    norm_target  : float,
    seed_base    : int,
    label        : str,
    fast_modifier: Optional[dict] = None,
) -> List[bytes]:
    rng  = np.random.RandomState(seed_base)
    pool : List[bytes] = []
    for i in range(pool_size):
        data = build_variation(audio, n_ch, sr, preset_name,
                               presets, rng, norm_target, fast_modifier)
        pool.append(data)
        print(f"   [{label:>13}] {i + 1:>2}/{pool_size}", end="\r", flush=True)
    print()
    gc.collect()
    return pool


# ─────────────────────────────────────────────────────────────
#  RELEASE HAVUZU — PSEUDO-RELEASE (SPRING BOUNCE)
# ─────────────────────────────────────────────────────────────
def build_release_variation(
    audio       : AudioF32,
    n_ch        : int,
    sr          : int,
    release_cfg : dict,
    rng         : np.random.RandomState,
    norm_target : float,
) -> bytes:
    semitones    = release_cfg.get("pitch_semitones", 4.0)
    pitch_factor = 2.0 ** (semitones / 12.0)

    rand_p = release_cfg.get("random_pitch_range", 0.012)
    seed   = float(rng.uniform())
    pitch_factor *= 1.0 + (seed - 0.5) * 2.0 * rand_p

    hp_fc   = release_cfg.get("highpass_fc_hz", 300.0)
    vol     = release_cfg.get("volume_scale", 0.25)
    rev_dec = release_cfg.get("reverb_decay", 0.020)
    rev_del = release_cfg.get("reverb_delay_s", 0.003)

    proc = pitch_shift(audio, pitch_factor, n_ch)
    proc = highpass(proc, n_ch, sr, hp_fc)
    proc = reverb_tail(proc, n_ch, sr, rev_dec, rev_del)
    proc = normalize(proc, norm_target)
    proc = (proc * vol).astype(np.float32)

    pcm  = np.clip(proc * 32767.0, -32768, 32767).astype(np.int16)
    data = pcm.tobytes()
    del proc, pcm
    return data


def build_release_pool(
    audio       : AudioF32,
    n_ch        : int,
    sr          : int,
    release_cfg : dict,
    pool_size   : int,
    norm_target : float,
    seed_base   : int,
    label       : str,
) -> List[bytes]:
    if not release_cfg.get("enabled", False):
        return []
    rng  = np.random.RandomState(seed_base)
    pool : List[bytes] = []
    for i in range(pool_size):
        data = build_release_variation(audio, n_ch, sr, release_cfg, rng, norm_target)
        pool.append(data)
        print(f"   [{label:>13}] {i + 1:>2}/{pool_size}", end="\r", flush=True)
    print()
    gc.collect()
    return pool
