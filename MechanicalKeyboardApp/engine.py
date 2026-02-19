"""
engine.py — Audio Engine Katmanı v2.2 (Enhanced Realism)
=========================================================
v2.2 YENİLİKLERİ:
  1. Release duration-based volume — basılı kalma süresine göre release ses seviyesi
  2. WPM burst detection — typing burst'lerini algılar, doğal hız değişimi
  3. Key-transition aware variation — aynı/farklı tuş için varyasyon farkı
  4. Enhanced micro timing — improved human-like inconsistency

RAM optimizasyonları (v2.1'den devam):
  1. 8 ayrı pool listesi → self._pools[8] dizisi
  2. update_volume() geçici liste yaratmaz
  3. Voice steal → hard stop + channel temizliği
  4. queue.Queue(maxsize=128) → bellek birikimi engellendi
  5. drain_end_events() → pygame.event.clear()
  6. stop() → pool.clear() + explicit del
  7. _last_idx dizisi → getattr/setattr kaldırıldı
  8. gc.freeze() desteği
"""

from __future__ import annotations

import gc
import logging
import os
import queue
import random
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Deque, Dict, List, Optional, Tuple

import pygame
import numpy as np

from dsp import build_pool, build_release_pool
from sound_pack_loader import KeyPackLoader, PACK_FOLDER_KEY

log = logging.getLogger("KeySim.Engine")

# ─────────────────────────────────────────────────────────────
#  AĞIR TUŞLAR
# ─────────────────────────────────────────────────────────────
HEAVY_KEYS: frozenset = frozenset({
    "Key.space", "Key.enter", "Key.backspace", "Key.delete",
    "Key.shift", "Key.shift_r", "Key.shift_l",
    "Key.ctrl",  "Key.ctrl_l",  "Key.ctrl_r",
    "Key.alt",   "Key.alt_l",   "Key.alt_r",  "Key.alt_gr",
    "Key.tab",   "Key.caps_lock", "Key.escape",
    "Key.insert","Key.home",    "Key.end",
    "Key.page_up", "Key.page_down",
    "Key.f1",  "Key.f2",  "Key.f3",  "Key.f4",
    "Key.f5",  "Key.f6",  "Key.f7",  "Key.f8",
    "Key.f9",  "Key.f10", "Key.f11", "Key.f12",
    "Key.up",  "Key.down","Key.left","Key.right",
    "Key.num_lock","Key.scroll_lock","Key.pause","Key.print_screen",
    "Key.menu","Key.cmd", "Key.cmd_r",
    "Key.media_play_pause","Key.media_volume_up","Key.media_volume_down",
})

MOUSE3_KEY = "Button.middle"

# Pool slot sabitleri
_PIDX_NORMAL   = 0
_PIDX_NORMAL_F = 1
_PIDX_NORMAL_R = 2
_PIDX_HEAVY    = 3
_PIDX_HEAVY_F  = 4
_PIDX_HEAVY_R  = 5
_PIDX_MOUSE    = 6
_PIDX_MOUSE3   = 7
_N_POOLS       = 8

_QUEUE_MAXSIZE = 128

# ─────────────────────────────────────────────────────────────
#  MICRO VARIATION SABİTLERİ v2.2
# ─────────────────────────────────────────────────────────────
_MV_RECENT_N  = 4       # anti-repetition penceresi
_MV_VOL_LOW   = 0.962   # volume jitter alt sınır
_MV_VOL_HIGH  = 1.038   # volume jitter üst sınır

# CHANGE 1: Release volume boost — duration-based ek boost için baseline
_MV_REL_BOOST_BASE = 1.16   # baseline release boost

# CHANGE 2: Duration-based release scaling sabitleri
# Kısa basış (<50ms)  → hafif release  (%18–20)
# Orta basış (50-200) → normal release (%20–22)
# Uzun basış (>200ms) → tok release    (%22–25)
_REL_DUR_SHORT  = 0.050   # 50ms altı = kısa
_REL_DUR_LONG   = 0.200   # 200ms üstü = uzun
_REL_SCALE_MIN  = 0.86    # kısa basış: boost × 0.86 = ~%18–20
_REL_SCALE_MID  = 1.00    # normal: boost × 1.00 = baseline
_REL_SCALE_MAX  = 1.16    # uzun basış: boost × 1.16 = ~%22–25

_MV_FADE_MIN  = 1
_MV_FADE_MAX  = 4

# CHANGE 3: Key transition variation — aynı tuş vs farklı tuş
# Aynı tuş ardışık basılırsa pitch range daralır (tutarlı ses)
# Farklı tuşa geçişte pitch range genişler (çeşitlilik)
_MV_SAME_KEY_PITCH_SCALE  = 0.65   # aynı tuş: pitch range × 0.65
_MV_DIFF_KEY_PITCH_SCALE  = 1.20   # farklı tuş: pitch range × 1.20


# ─────────────────────────────────────────────────────────────
#  PLAY COMMAND v2.2
# ─────────────────────────────────────────────────────────────
@dataclass(frozen=True, slots=True)
class PlayCommand:
    """
    InputHandler → AudioEngine arası immutable mesaj.

    v2.2 YENİLİK:
      duration: basılı kalma süresi (release için, press=0.0)
      last_key: son basılan tuş ID'si (key transition tracking)
    """
    key_id    : str
    is_mouse  : bool  = False
    is_release: bool  = False
    not_before: float = 0.0   # time.monotonic() zaman damgası
    duration  : float = 0.0   # CHANGE: basılı kalma süresi (release için)
    last_key  : str   = ""    # CHANGE: son basılan tuş (transition tracking)


# ─────────────────────────────────────────────────────────────
#  WPM TRACKER v2.2 — BURST DETECTION
# ─────────────────────────────────────────────────────────────
class WpmTracker:
    """
    Rolling WPM + burst detection.
    
    v2.2 GELİŞME:
      • Burst detection — son 5 tuş vs önceki 10 tuş hızını karşılaştırır
      • Mikro duraksama algılama — typing momentum tracking
    """
    __slots__ = ("_times", "_lock", "_window", "_last_wpm", "_burst_factor")

    def __init__(self, window: int = 15) -> None:
        self._window = window
        self._times  : Deque[float] = deque(maxlen=window)
        self._lock   = threading.Lock()
        self._last_wpm : float = 0.0        # CHANGE: son hesaplanan WPM
        self._burst_factor : float = 1.0    # CHANGE: burst çarpanı (1.0–1.5)

    def record(self) -> None:
        with self._lock:
            self._times.append(time.monotonic())

    def wpm(self) -> float:
        """Rolling window WPM hesabı."""
        with self._lock:
            if len(self._times) < 4:
                return 0.0
            elapsed = self._times[-1] - self._times[0]
            if elapsed < 0.01:
                return 0.0
            return (len(self._times) - 1) / elapsed * 12.0

    def burst_wpm(self) -> Tuple[float, float]:
        """
        CHANGE: Burst-aware WPM hesabı.
        
        Returns: (base_wpm, burst_factor)
          base_wpm: normal rolling WPM
          burst_factor: 0.7–1.3 arası çarpan
            >1.0 = burst (hızlanma)
            <1.0 = slowdown (yavaşlama)
        """
        with self._lock:
            n = len(self._times)
            if n < 8:
                return (0.0, 1.0)
            
            # Base WPM: tüm pencere
            elapsed_total = self._times[-1] - self._times[0]
            if elapsed_total < 0.01:
                return (0.0, 1.0)
            base_wpm = (n - 1) / elapsed_total * 12.0
            
            # Burst detection: son 5 vs önceki segment karşılaştırması
            if n >= 10:
                # Son 5 tuşun hızı
                recent_elapsed = self._times[-1] - self._times[-5]
                if recent_elapsed > 0.01:
                    recent_wpm = 4 / recent_elapsed * 12.0
                else:
                    recent_wpm = base_wpm
                
                # Burst factor: recent_wpm / base_wpm
                # Hızlanma: >1.0, yavaşlama: <1.0
                burst = recent_wpm / max(base_wpm, 1.0)
                # Clamp: 0.7–1.3 arası (aşırı uçları önle)
                burst = max(0.7, min(1.3, burst))
                
                # Smooth: ani sıçramaları yumuşat
                self._burst_factor = 0.6 * self._burst_factor + 0.4 * burst
            else:
                self._burst_factor = 1.0
            
            self._last_wpm = base_wpm
            return (base_wpm, self._burst_factor)


# ─────────────────────────────────────────────────────────────
#  MICRO VARIATOR v2.2
# ─────────────────────────────────────────────────────────────
_MV_PITCH_BASE  = 0.025
_MV_PITCH_MIN   = 0.015
_MV_PITCH_WPM_N = 100.0
_MV_PITCH_BKTS  = 8


class MicroVariator:
    """
    v2.2 GELİŞMELER:
      • Key transition tracking — aynı/farklı tuş için varyasyon
      • Burst-aware pitch adjustment — burst sırasında pitch daralmaz
      • Duration-based release volume — basılı kalma süresine göre
    """
    __slots__ = ("_rng", "_recent", "_pitch_recent", "_last_key_id")

    def __init__(self) -> None:
        self._rng          : random.Random = random.Random()
        self._recent       : deque         = deque(maxlen=_MV_RECENT_N)
        self._pitch_recent : deque         = deque(maxlen=_MV_RECENT_N - 1)
        self._last_key_id  : str           = ""  # CHANGE: son basılan tuş ID

    # ── Pitch ──────────────────────────────────────────────────
    def pitch_offset(self, ema_wpm: float, is_same_key: bool = False,
                     burst_factor: float = 1.0) -> float:
        """
        v2.2 CHANGE: Key transition + burst awareness
        
        is_same_key: True ise pitch range daralır (tutarlı ses)
        burst_factor: >1.0 ise pitch range genişler (hızlanma çeşitliliği)
        """
        # Base range: WPM'e göre daralma
        t  = max(0.0, min(1.0, ema_wpm / _MV_PITCH_WPM_N))
        pr = _MV_PITCH_BASE - t * (_MV_PITCH_BASE - _MV_PITCH_MIN)
        
        # CHANGE: Key transition scaling
        if is_same_key:
            pr *= _MV_SAME_KEY_PITCH_SCALE  # aynı tuş: dar range
        else:
            pr *= _MV_DIFF_KEY_PITCH_SCALE  # farklı tuş: geniş range
        
        # CHANGE: Burst expansion — burst sırasında varyasyon artır
        if burst_factor > 1.05:
            pr *= min(1.15, 0.95 + burst_factor * 0.1)
        
        # Bucket anti-repetition
        for _ in range(10):
            raw = self._rng.uniform(-pr, pr)
            bkt = int((raw + _MV_PITCH_BASE) / (2 * _MV_PITCH_BASE) * _MV_PITCH_BKTS) % _MV_PITCH_BKTS
            if bkt not in self._pitch_recent:
                break
        self._pitch_recent.append(bkt)
        return raw

    def pitch_pool_bias(self, ema_wpm: float, pool_size: int,
                        is_same_key: bool = False,
                        burst_factor: float = 1.0) -> Optional[int]:
        """v2.2 CHANGE: Key transition + burst parameters"""
        if pool_size < 4:
            return None
        raw = self.pitch_offset(ema_wpm, is_same_key, burst_factor)
        normalized = (raw + _MV_PITCH_BASE) / (2 * _MV_PITCH_BASE)
        return int(normalized * pool_size) % pool_size

    # ── Volume ─────────────────────────────────────────────────
    def vol_scale(self, is_release: bool, duration: float = 0.0,
                  wpm: float = 0.0) -> float:
        """
        v2.2 CHANGE: Duration-based release volume + WPM scaling
        
        Press/Mouse: ±3.8% jitter
        Release: duration'a göre değişken boost
          • <50ms  → hafif (%18–20) — quick tap
          • 50-200 → normal (%20–22) — regular press
          • >200ms → tok (%22–25) — sustained press
        
        WPM>60 ise release hafifçe azalır (hızlı yazımda release baskın değil)
        """
        base = self._rng.uniform(_MV_VOL_LOW, _MV_VOL_HIGH)
        
        if not is_release:
            return base
        
        # CHANGE: Duration-based release boost
        if duration < _REL_DUR_SHORT:
            # Kısa basış: hafif release
            dur_scale = _REL_SCALE_MIN
        elif duration > _REL_DUR_LONG:
            # Uzun basış: tok release
            dur_scale = _REL_SCALE_MAX
        else:
            # Orta basış: linear interpolation
            t = (duration - _REL_DUR_SHORT) / (_REL_DUR_LONG - _REL_DUR_SHORT)
            dur_scale = _REL_SCALE_MIN + t * (_REL_SCALE_MID - _REL_SCALE_MIN)
        
        release_boost = _MV_REL_BOOST_BASE * dur_scale
        
        # CHANGE: WPM-based release attenuation
        # WPM>60 ise release hafifçe azalt (hızlı yazımda release baskın değil)
        if wpm > 60.0:
            wpm_atten = max(0.88, 1.0 - (wpm - 60.0) * 0.002)
            release_boost *= wpm_atten
        
        return base * release_boost

    # ── Attack Fade ────────────────────────────────────────────
    def fade_ms(self, is_release: bool, is_mouse: bool) -> int:
        """
        ch.play(fade_ms=N): 0'dan vol'e N ms'de yükselir.
        Press:   1-4ms random
        Release: 1ms crisp
        Mouse:   1ms crisp
        """
        if is_release or is_mouse:
            return 1
        return self._rng.randint(_MV_FADE_MIN, _MV_FADE_MAX)

    # ── Anti-Repetition + Key Tracking ─────────────────────────
    def was_recent(self, pidx: int, sound_idx: int) -> bool:
        """Son _MV_RECENT_N kombinasyon içinde mi?"""
        return (pidx, sound_idx) in self._recent

    def record(self, pidx: int, sound_idx: int) -> None:
        """Seçilen kombinasyonu kaydet."""
        self._recent.append((pidx, sound_idx))

    def update_key(self, key_id: str) -> bool:
        """
        CHANGE v2.2: Son basılan tuşu güncelle.
        Returns: True = aynı tuş, False = farklı tuş
        """
        is_same = (key_id == self._last_key_id)
        self._last_key_id = key_id
        return is_same


# ─────────────────────────────────────────────────────────────
#  CHANNEL RING
# ─────────────────────────────────────────────────────────────
class ChannelRing:
    """Pre-allocated kanal havuzu."""
    __slots__ = ("_n", "_channels", "_end_events", "_steal_pos", "_lock", "_fade_ms")

    def __init__(self, polyphony: int, fade_ms: int) -> None:
        self._n         = polyphony
        self._fade_ms   = fade_ms
        self._steal_pos = 0
        self._lock      = threading.Lock()
        self._channels  : List[pygame.mixer.Channel] = [
            pygame.mixer.Channel(i) for i in range(polyphony)
        ]
        self._end_events: List[int] = []
        self._assign_end_events()

    def _assign_end_events(self) -> None:
        try:
            events = [pygame.event.custom_type() for _ in range(self._n)]
        except AttributeError:
            base   = getattr(pygame, "USEREVENT", 24)
            events = [base + 1 + i for i in range(self._n)]
        for ch, ev in zip(self._channels, events):
            ch.set_endevent(ev)
        self._end_events = events

    def acquire(self) -> Tuple[pygame.mixer.Channel, int]:
        with self._lock:
            active  = 0
            free_ch : Optional[pygame.mixer.Channel] = None
            for ch in self._channels:
                if ch.get_busy():
                    active += 1
                elif free_ch is None:
                    free_ch = ch
            if free_ch is not None:
                return free_ch, active
            victim = self._channels[self._steal_pos]
            victim.stop()
            self._steal_pos = (self._steal_pos + 1) % self._n
            return victim, self._n

    def active_count(self) -> int:
        return sum(1 for ch in self._channels if ch.get_busy())

    def drain_end_events(self) -> None:
        for ev in self._end_events:
            pygame.event.clear(ev)

    def stop_all(self) -> None:
        for ch in self._channels:
            ch.stop()


# ─────────────────────────────────────────────────────────────
#  AUDIO ENGINE v2.2
# ─────────────────────────────────────────────────────────────
class AudioEngine:
    """
    Tek audio thread üzerinde çalışan ses motoru.
    v2.2: Enhanced realism — duration-based release, burst detection, key transitions
    """

    def __init__(self, cfg: dict, presets: dict, key_bindings: dict) -> None:
        self._cfg          = cfg
        self._presets      = presets
        self._key_bindings = key_bindings

        self._pools    : List[List[pygame.mixer.Sound]] = [[] for _ in range(_N_POOLS)]
        self._last_idx : List[int]                      = [-1] * _N_POOLS

        self._custom_sounds: Dict[str, pygame.mixer.Sound] = {}
        self._sound_file_cache: Dict[str, pygame.mixer.Sound] = {}
        self._pool_lock = threading.Lock()

        # JSON Soundpack loader — None: klasik mod, KeyPackLoader: pack modu
        self._pack_loader: Optional[KeyPackLoader] = None

        self._queue  : queue.Queue[PlayCommand] = queue.Queue(maxsize=_QUEUE_MAXSIZE)
        self._wakeup = threading.Event()
        self._running = False
        self._thread  : Optional[threading.Thread] = None

        self._volume : float = cfg["app"]["initial_volume"]
        self._active_voices : int = 0

        wpm_cfg          = presets.get("wpm", {})
        self._wpm        = WpmTracker(window=wpm_cfg.get("measurement_window", 15))
        self._wpm_thresh : float = float(wpm_cfg.get("fast_threshold_wpm", 55))
        self._wpm_mod    : dict  = {
            "pitch_add"          : float(wpm_cfg.get("fast_pitch_add", 0.022)),
            "reverb_decay_scale" : float(wpm_cfg.get("fast_reverb_decay_scale", 0.72)),
        }

        self._ema_wpm  : float = 0.0
        self._ema_alpha: float = float(wpm_cfg.get("ema_alpha", 0.25))
        self._band_lo  : float = self._wpm_thresh - 10.0
        self._band_rng : float = 40.0

        self._init_mixer()
        self._ring = ChannelRing(
            polyphony = cfg["engine"]["polyphony"],
            fade_ms   = cfg["engine"]["steal_fade_ms"],
        )
        self._mv = MicroVariator()

    # ── PUBLIC API ─────────────────────────────────────────────

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._thread  = threading.Thread(
            target=self._audio_loop, name="AudioEngine", daemon=True
        )
        self._thread.start()
        log.info("AudioEngine started.")

    def stop(self) -> None:
        if not self._running:
            return
        self._running = False
        self._wakeup.set()
        if self._thread:
            self._thread.join(timeout=2.0)
            self._thread = None

        pygame.mixer.fadeout(150)
        time.sleep(0.16)
        self._ring.stop_all()
        pygame.mixer.quit()

        with self._pool_lock:
            for pool in self._pools:
                pool.clear()
            self._custom_sounds.clear()
            self._sound_file_cache.clear()

        if self._pack_loader is not None:
            self._pack_loader.unload()
            self._pack_loader = None

        log.info("AudioEngine stopped.")

    def enqueue_play(self, key_id: str, is_mouse: bool = False,
                     is_release: bool = False, duration: float = 0.0,
                     last_key: str = "") -> None:
        """
        v2.2 CHANGE: duration ve last_key parametreleri eklendi.
        """
        # Timing jitter: press → 0.5-1.5ms, release/mouse → 0ms
        if not is_release and not is_mouse:
            jitter = random.uniform(0.0005, 0.0015)
        else:
            jitter = 0.0
        try:
            self._queue.put_nowait(PlayCommand(
                key_id     = key_id,
                is_mouse   = is_mouse,
                is_release = is_release,
                not_before = time.monotonic() + jitter,
                duration   = duration,
                last_key   = last_key,
            ))
            self._wakeup.set()
        except queue.Full:
            pass

    def update_volume(self, new_vol: float) -> None:
        self._volume = max(0.0, min(1.0, new_vol))
        with self._pool_lock:
            for pool in self._pools:
                for snd in pool:
                    snd.set_volume(new_vol)
            for snd in self._custom_sounds.values():
                snd.set_volume(new_vol)
        if self._pack_loader is not None:
            self._pack_loader.set_volume(new_vol)

    def reload_sounds(self) -> None:
        """Rebuild all sound pools from scratch."""
        cfg         = self._cfg
        sr          = cfg["mixer"]["sample_rate"]
        pool_size   = cfg["engine"]["pool_size"]
        norm_target = cfg["engine"]["normalize_target"]
        sound_dir   = Path(cfg["sound"]["dir"])
        p           = self._presets
        wpm_mod     = self._wpm_mod

        new_pools: List[List[pygame.mixer.Sound]] = [[] for _ in range(_N_POOLS)]

        lang = self._cfg["app"].get("language", "en")
        _loading_msg = "Building sound pools..." if lang == "en" else "Ses havuzlari olusturuluyor..."
        _ready_msg   = "Ready!" if lang == "en" else "Hazir!"
        print(f"\n  \u25b6 {_loading_msg}")

        # ── KLAVYE ────────────────────────────────────────────
        key_path = sound_dir / cfg["sound"]["key_file"]
        if key_path.exists():
            audio, n_ch = self._load_raw(key_path)

            print("   Normal keys (slow)...")
            new_pools[_PIDX_NORMAL] = self._to_sounds(
                build_pool(audio, n_ch, sr, "normal_key", p, pool_size, norm_target, 42, "normal·slow"))

            print("   Normal keys (fast WPM)...")
            new_pools[_PIDX_NORMAL_F] = self._to_sounds(
                build_pool(audio, n_ch, sr, "normal_key", p, pool_size, norm_target, 142, "normal·fast",
                           fast_modifier=wpm_mod))

            print("   Normal keys (release)...")
            new_pools[_PIDX_NORMAL_R] = self._to_sounds(
                build_release_pool(audio, n_ch, sr, p["normal_key"]["release"],
                                   pool_size, norm_target, 242, "normal·rel"))

            print("   Heavy keys (slow)...")
            new_pools[_PIDX_HEAVY] = self._to_sounds(
                build_pool(audio, n_ch, sr, "heavy_key", p, pool_size, norm_target, 99, "heavy·slow"))

            print("   Heavy keys (fast WPM)...")
            new_pools[_PIDX_HEAVY_F] = self._to_sounds(
                build_pool(audio, n_ch, sr, "heavy_key", p, pool_size, norm_target, 199, "heavy·fast",
                           fast_modifier=wpm_mod))

            print("   Heavy keys (release)...")
            new_pools[_PIDX_HEAVY_R] = self._to_sounds(
                build_release_pool(audio, n_ch, sr, p["heavy_key"]["release"],
                                   pool_size, norm_target, 299, "heavy·rel"))

            del audio; gc.collect()
        else:
            log.warning("Key sound not found: %s", key_path)
            print(f"   [!] Sound file not found: {key_path}")

        # ── FARE ──────────────────────────────────────────────
        mouse_path = sound_dir / cfg["sound"]["mouse_file"]
        if mouse_path.exists():
            audio, n_ch = self._load_raw(mouse_path)

            print("   Mouse clicks...")
            new_pools[_PIDX_MOUSE] = self._to_sounds(
                build_pool(audio, n_ch, sr, "mouse_click", p, pool_size, norm_target, 77, "mouse·click"))

            print("   Mouse button 3 (middle)...")
            new_pools[_PIDX_MOUSE3] = self._to_sounds(
                build_pool(audio, n_ch, sr, "mouse_button3", p, pool_size, norm_target, 177, "mouse·btn3"))

            del audio; gc.collect()
        else:
            log.warning("Mouse sound not found: %s", mouse_path)
            print(f"   [!] Sound file not found: {mouse_path}")

        # ── MOD SEÇİMİ: JSON PACK vs KLASİK ÖZEL ATAMALAR ────────
        new_custom: Dict[str, pygame.mixer.Sound] = {}

        if PACK_FOLDER_KEY in self._key_bindings:
            # ── JSON PACK MODU ────────────────────────────────────
            if self._pack_loader is not None:
                self._pack_loader.unload()
            else:
                self._pack_loader = KeyPackLoader()

            pack_folder = Path(self._key_bindings[PACK_FOLDER_KEY])
            if pack_folder.is_dir():
                try:
                    self._pack_loader.load_folder(pack_folder, self._volume)
                    # Pack resolve() tüm ses yönlendirmesini üstlenir.
                    # _custom_sounds boş kalır — pack'te olmayan tuşlar DSP pool'a fallback yapar.
                except Exception as exc:
                    log.error("Pack loading error: %s", exc)
                    print(f"   [!] Pack loading error: {exc}")
                    self._pack_loader.unload()
                    self._pack_loader = None
            else:
                log.warning("Pack folder not found: %s", pack_folder)
                print(f"   [!] Pack folder not found: {pack_folder}")
                self._pack_loader = None

        else:
            # ── KLASİK ÖZEL ATAMALAR ─────────────────────────────
            if self._pack_loader is not None:
                self._pack_loader.unload()
                self._pack_loader = None

            for key_name, file_path in self._key_bindings.items():
                if not os.path.isfile(file_path):
                    continue
                try:
                    snd = self._sound_file_cache.get(file_path)
                    if snd is None:
                        snd = pygame.mixer.Sound(file_path)
                        self._sound_file_cache[file_path] = snd
                    snd.set_volume(self._volume)
                    new_custom[key_name] = snd
                except Exception as exc:
                    log.warning("Custom bind error (%s): %s", key_name, exc)

        with self._pool_lock:
            for i, pool in enumerate(new_pools):
                self._pools[i] = pool
            self._custom_sounds = new_custom
            self._last_idx[:] = [-1] * _N_POOLS

        del new_pools
        gc.collect()
        print(f"  \u25b6 {_ready_msg}\n")

    @property
    def active_voices(self) -> int:
        return self._active_voices

    @property
    def volume(self) -> float:
        return self._volume

    # ── PRIVATE ────────────────────────────────────────────────

    def _init_mixer(self) -> None:
        """
        Cross-platform SDL/pygame mixer initialisation.

        Windows  : SDL_AUDIODRIVER=directsound (lowest latency, no WASAPI overhead)
        macOS    : SDL_AUDIODRIVER=coreaudio    (native low-latency path)
        Linux    : Try pipewire -> pulse -> alsa in priority order.
                   SDL auto-selects if env not set, but explicit ordering
                   avoids PipeWire's extra processing layer when possible.

        Buffer size 512 (~11ms @44100) is ideal for Windows DirectSound.
        On macOS and Linux the same value works; CoreAudio and ALSA both
        handle 512-sample buffers without issues.
        """
        mcfg = self._cfg["mixer"]

        # ── Audio driver selection per platform ─────────────────
        if sys.platform == "win32":
            if mcfg.get("use_directsound", True):
                os.environ.setdefault("SDL_AUDIODRIVER", "directsound")
        elif sys.platform == "darwin":
            os.environ.setdefault("SDL_AUDIODRIVER", "coreaudio")
        # Linux: let SDL auto-select (pipewire/pulse/alsa based on environment)
        # Override only if explicitly set in config
        elif sys.platform.startswith("linux"):
            linux_driver = mcfg.get("linux_audio_driver", "")
            if linux_driver:
                os.environ.setdefault("SDL_AUDIODRIVER", linux_driver)

        # ── SDL_HINT: disable compositing / audio effects ───────
        # Prevents WASAPI session from applying enhancements that add latency.
        os.environ.setdefault("SDL_AUDIO_ALLOW_FREQUENCY_CHANGE", "0")
        os.environ.setdefault("SDL_AUDIO_ALLOW_FORMAT_CHANGE", "0")
        os.environ.setdefault("SDL_AUDIO_ALLOW_CHANNELS_CHANGE", "0")

        try:
            pygame.mixer.pre_init(
                frequency = mcfg["sample_rate"],
                size      = mcfg["bit_depth"],
                channels  = mcfg["channels"],
                buffer    = mcfg["buffer_size"],
            )
            pygame.init()
            pygame.mixer.quit()
            pygame.mixer.init(
                frequency = mcfg["sample_rate"],
                size      = mcfg["bit_depth"],
                channels  = mcfg["channels"],
                buffer    = mcfg["buffer_size"],
            )
            pygame.mixer.set_num_channels(mcfg["max_sdl_ch"])
            freq, fmt, ch = pygame.mixer.get_init()
            log.info("Mixer initialized: %dHz fmt=%d ch=%d buf=%d",
                     freq, fmt, ch, mcfg["buffer_size"])
        except Exception as exc:
            log.critical("Mixer init failed: %s", exc)
            print(f"[FATAL] Mixer init failed: {exc}")
            sys.exit(1)

    def _load_raw(self, path: Path) -> Tuple[np.ndarray, int]:
        snd    = pygame.mixer.Sound(str(path))
        raw    = snd.get_raw()
        freq, fmt, _ = pygame.mixer.get_init()
        bps    = abs(fmt) // 8
        frames = max(1, int(snd.get_length() * freq))
        n_ch   = max(1, min(2, len(raw) // (frames * bps)))
        audio  = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32767.0
        del snd, raw
        return audio, n_ch

    def _to_sounds(self, raw_pool: List[bytes]) -> List[pygame.mixer.Sound]:
        result: List[pygame.mixer.Sound] = []
        for data in raw_pool:
            snd = pygame.mixer.Sound(buffer=data)
            snd.set_volume(self._volume)
            result.append(snd)
        return result

    def _pick(self, pidx: int,
              pitch_bias: Optional[int] = None) -> Tuple[Optional[pygame.mixer.Sound], int]:
        """Pool'dan ses seç — pitch bias + anti-repetition."""
        with self._pool_lock:
            pool = self._pools[pidx]
            n    = len(pool)
            if n == 0:
                return None, -1
            if n == 1:
                return pool[0], 0
            last = self._last_idx[pidx]

            idx = last
            if (pitch_bias is not None
                    and 0 <= pitch_bias < n
                    and pitch_bias != last
                    and not self._mv.was_recent(pidx, pitch_bias)):
                idx = pitch_bias
            else:
                for _ in range(12):
                    candidate = random.randrange(n)
                    if candidate != last and not self._mv.was_recent(pidx, candidate):
                        idx = candidate
                        break
                else:
                    for candidate in range(n):
                        if candidate != last:
                            idx = candidate
                            break

            self._last_idx[pidx] = idx
            return pool[idx], idx

    def _audio_loop(self) -> None:
        loop_ms    = self._cfg["engine"]["audio_loop_ms"] / 1000.0
        batch_max  = self._cfg["engine"]["queue_batch_max"]
        _delay_buf : Deque[PlayCommand] = deque()

        # CPU optimisation: drain pygame events and count voices only
        # every _UI_POLL loops instead of every iteration.
        # At 1ms loop: every 40 iterations = every 40ms. Still fine for UI.
        # Eliminates ~12000 pygame calls/s -> ~300 pygame calls/s.
        _UI_POLL   = 40
        _loop_ctr  = 0
        # Pre-cache bound methods to avoid attribute lookup on every iter
        _q_get     = self._queue.get_nowait
        _ring_play = self._ring
        _do        = self._do_play

        # Platform-specific sleep precision note:
        # Windows: timeBeginPeriod(1) is called in main.py via
        #   input_handler._setup_windows_timer() -> 1ms resolution.
        # macOS/Linux: threading.Event.wait() already has ~1ms resolution
        #   via select()/kqueue()/epoll() under the hood.
        # No extra action needed here — the Event-based wake is precise
        # on all platforms once the Windows timer is fixed in main.

        while self._running:
            now       = time.monotonic()
            _loop_ctr += 1

            # ── Drain incoming commands into delay buffer ───────
            drained = 0
            while drained < batch_max:
                try:
                    _delay_buf.append(_q_get())
                    drained += 1
                except queue.Empty:
                    break

            # ── Process commands whose not_before has passed ────
            # Re-queue any not yet ready (jitter buffer)
            _pending: Deque[PlayCommand] = deque()
            while _delay_buf:
                cmd = _delay_buf.popleft()
                if now >= cmd.not_before:
                    _do(cmd)
                else:
                    _pending.append(cmd)
            if _pending:
                _delay_buf.extendleft(reversed(_pending))

            # ── Periodic pygame housekeeping (CPU saving) ───────
            if _loop_ctr >= _UI_POLL:
                _loop_ctr = 0
                _ring_play.drain_end_events()
                self._active_voices = _ring_play.active_count()

            self._wakeup.wait(timeout=loop_ms)
            self._wakeup.clear()

        _delay_buf.clear()
        while not self._queue.empty():
            try:
                self._queue.get_nowait()
            except queue.Empty:
                break

    def _do_play(self, cmd: PlayCommand) -> None:
        """
        v2.2 CHANGE: Duration-based release volume + key transition tracking
        Pack loader entegrasyonu: loader.resolve() → None ise normal akış devam eder.
        """
        # ── WPM TRACKING (her zaman, ses kaynağından bağımsız) ──
        if not cmd.is_release and not cmd.is_mouse:
            self._wpm.record()

        # ── JSON PACK LOADER — önce dene ──────────────────────────
        # JSON mode: keyup → None (yok say). keydown → slice sesi.
        # Fallback: dosya adı eşleşmesi. None → normal DSP pool akışı.
        if self._pack_loader is not None:
            pack_snd = self._pack_loader.resolve(cmd.key_id, cmd.is_release)
            if pack_snd is not None:
                ch, n_active = self._ring.acquire()
                eq_scale = max(0.42, 1.0 / (n_active + 1) ** 0.5)
                ch.set_volume(min(1.0, self._volume * eq_scale))
                ch.play(pack_snd)
                return   # ← pack sesi çalındı, DSP akışına gerek yok

        # ── NORMAL DSP POOL AKIŞI (orijinal mantık) ───────────────
        sound : Optional[pygame.mixer.Sound] = None
        pidx  : int = -1
        s_idx : int = -1

        if not cmd.is_release:
            sound = self._custom_sounds.get(cmd.key_id)
        else:
            # FIX: custom bound bir tusun release'i DSP pool'dan calmasini engelle
            # (default + custom ses cakismasi bugfix)
            if cmd.key_id in self._custom_sounds:
                return

        # Key transition tracking - ayni tus mu?
        is_same_key = self._mv.update_key(cmd.key_id if not cmd.is_release else cmd.last_key)

        # CHANGE: Burst-aware WPM
        base_wpm, burst_factor = self._wpm.burst_wpm()

        if sound is None:
            if cmd.is_release:
                pidx = _PIDX_HEAVY_R if cmd.key_id in HEAVY_KEYS else _PIDX_NORMAL_R
                sound, s_idx = self._pick(pidx)

            elif cmd.is_mouse:
                pidx = _PIDX_MOUSE3 if cmd.key_id == MOUSE3_KEY else _PIDX_MOUSE
                sound, s_idx = self._pick(pidx)

            else:
                # WPM EMA smoothing
                self._ema_wpm = (self._ema_alpha * base_wpm
                                 + (1.0 - self._ema_alpha) * self._ema_wpm)
                ratio    = max(0.0, min(1.0,
                               (self._ema_wpm - self._band_lo) / self._band_rng))
                use_fast = random.random() < ratio
                if cmd.key_id in HEAVY_KEYS:
                    pidx = _PIDX_HEAVY_F if use_fast else _PIDX_HEAVY
                else:
                    pidx = _PIDX_NORMAL_F if use_fast else _PIDX_NORMAL

                # CHANGE: Pitch bias with key transition + burst
                psize = len(self._pools[pidx])
                bias  = self._mv.pitch_pool_bias(
                    self._ema_wpm, psize, is_same_key, burst_factor
                ) if psize > 3 else None
                sound, s_idx = self._pick(pidx, bias)

        if sound is None:
            return

        if pidx >= 0 and s_idx >= 0:
            self._mv.record(pidx, s_idx)

        # ── Kanal + Micro Variation ───────────────────────────
        ch, n_active = self._ring.acquire()

        eq_scale = max(0.42, 1.0 / (n_active + 1) ** 0.5)
        
        # CHANGE: Duration-based release volume
        mv_scale = self._mv.vol_scale(cmd.is_release, cmd.duration, self._ema_wpm)
        vol      = min(1.0, self._volume * eq_scale * mv_scale)

        fms = self._mv.fade_ms(cmd.is_release, cmd.is_mouse)

        ch.set_volume(vol)
        ch.play(sound, fade_ms=fms)
