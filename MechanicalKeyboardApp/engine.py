"""
engine.py — Audio Engine Katmanı v2.1 (RAM Optimized)
=======================================================
RAM optimizasyonları özeti:
  1. 8 ayrı pool listesi → self._pools[8] dizisi (getattr/setattr yok)
  2. update_volume() artık geçici birleşik liste yaratmıyor
  3. Voice steal → hard stop (fadeout yerine) + channel temizliği
  4. queue.Queue(maxsize=128) → bellek birikimi engellendi
  5. drain_end_events() → pygame.event.clear() (get() liste yaratmaz)
  6. stop() → pool.clear() + explicit del → Sound ref'leri serbest
  7. _last_idx dizisi → getattr/setattr string lookup kaldırıldı
  8. gc.freeze() desteği için _pools tek merkezde tutuluyor
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

# Pool slot sabitleri — string attribute lookup yerine int index
# OPT: getattr/setattr yerine _pools[idx] ve _last_idx[idx] → hızlı, temiz
_PIDX_NORMAL   = 0
_PIDX_NORMAL_F = 1
_PIDX_NORMAL_R = 2
_PIDX_HEAVY    = 3
_PIDX_HEAVY_F  = 4
_PIDX_HEAVY_R  = 5
_PIDX_MOUSE    = 6
_PIDX_MOUSE3   = 7
_N_POOLS       = 8

# Queue üst sınırı — büyüyen bellek birikimini engeller
_QUEUE_MAXSIZE = 128


# ─────────────────────────────────────────────────────────────
#  PLAY COMMAND
# ─────────────────────────────────────────────────────────────
@dataclass(frozen=True, slots=True)
class PlayCommand:
    """InputHandler → AudioEngine arası immutable mesaj."""
    key_id    : str
    is_mouse  : bool = False
    is_release: bool = False


# ─────────────────────────────────────────────────────────────
#  WPM TRACKER
# ─────────────────────────────────────────────────────────────
class WpmTracker:
    """
    Son N tuş basışının timestamp'larından rolling WPM hesaplar.
    OPT: deque(maxlen=N) → sabit boyut, asla büyümez.
    """
    __slots__ = ("_times", "_lock", "_window")

    def __init__(self, window: int = 15) -> None:
        self._window = window
        self._times  : Deque[float] = deque(maxlen=window)  # sabit boyut
        self._lock   = threading.Lock()

    def record(self) -> None:
        with self._lock:
            self._times.append(time.monotonic())

    def wpm(self) -> float:
        with self._lock:
            if len(self._times) < 4:
                return 0.0
            elapsed = self._times[-1] - self._times[0]
            if elapsed < 0.01:
                return 0.0
            return (len(self._times) - 1) / elapsed * 12.0


# ─────────────────────────────────────────────────────────────
#  CHANNEL RING
# ─────────────────────────────────────────────────────────────
class ChannelRing:
    """
    Pre-allocated kanal havuzu.
    OPT: drain_end_events → pygame.event.clear() (get() gibi liste yaratmaz)
    OPT: steal → hard stop (fadeout yerine); eski ses derhal bırakılır
    """
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
            # OPT: hard stop → anında belleği serbest bırakır, SDL buffer birikimi yok
            victim = self._channels[self._steal_pos]
            victim.stop()                                      # ← fadeout yerine stop
            self._steal_pos = (self._steal_pos + 1) % self._n
            return victim, self._n

    def active_count(self) -> int:
        return sum(1 for ch in self._channels if ch.get_busy())

    def drain_end_events(self) -> None:
        # OPT: pygame.event.clear() — list() allocation yok, O(1) per event type
        for ev in self._end_events:
            pygame.event.clear(ev)

    def stop_all(self) -> None:
        for ch in self._channels:
            ch.stop()


# ─────────────────────────────────────────────────────────────
#  AUDIO ENGINE
# ─────────────────────────────────────────────────────────────
class AudioEngine:
    """
    Tek audio thread üzerinde çalışan ses motoru.

    RAM optimizasyonları:
      • self._pools[8]  — tek dizi, 8 ayrı attribute yok
      • self._last_idx  — int dizisi, string attr lookup yok
      • queue.Queue(maxsize=128) — sınırlı kuyruk
      • stop() pool.clear() — Sound ref'leri erken bırakılır
    """

    def __init__(self, cfg: dict, presets: dict, key_bindings: dict) -> None:
        self._cfg          = cfg
        self._presets      = presets
        self._key_bindings = key_bindings

        # OPT: 8 ayrı list attribute yerine tek dizi — Memory layout temiz,
        #      update_volume'da geçici birleşik liste yaratılmıyor
        self._pools    : List[List[pygame.mixer.Sound]] = [[] for _ in range(_N_POOLS)]
        self._last_idx : List[int]                      = [-1] * _N_POOLS  # round-robin

        self._custom_sounds: Dict[str, pygame.mixer.Sound] = {}
        self._pool_lock = threading.Lock()

        # OPT: queue.Queue(maxsize) → dolduğunda put_nowait atar, büyümez
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

        self._init_mixer()
        self._ring = ChannelRing(
            polyphony = cfg["engine"]["polyphony"],
            fade_ms   = cfg["engine"]["steal_fade_ms"],
        )

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
            self._thread = None  # thread referansını serbest bırak

        pygame.mixer.fadeout(150)
        time.sleep(0.16)
        self._ring.stop_all()
        pygame.mixer.quit()

        # OPT: Sound nesnelerini açıkça serbest bırak → SDL ses belleği geri döner
        with self._pool_lock:
            for pool in self._pools:
                pool.clear()
            self._custom_sounds.clear()

        log.info("AudioEngine stopped.")

    def enqueue_play(self, key_id: str, is_mouse: bool = False,
                     is_release: bool = False) -> None:
        # OPT: queue doluysa yeni event DROP edilir — bellek birikimi imkânsız
        try:
            self._queue.put_nowait(PlayCommand(
                key_id=key_id, is_mouse=is_mouse, is_release=is_release
            ))
            self._wakeup.set()
        except queue.Full:
            pass  # yavaşlama anında drop — ses kaçar ama RAM güvende

    def update_volume(self, new_vol: float) -> None:
        self._volume = max(0.0, min(1.0, new_vol))
        # OPT: eski kod `pool_a + pool_b + ...` ile 96-elemanlı geçici liste yaratıyordu
        #      Şimdi self._pools üzerinde doğrudan iterate — sıfır geçici allocation
        with self._pool_lock:
            for pool in self._pools:
                for snd in pool:
                    snd.set_volume(new_vol)
            for snd in self._custom_sounds.values():
                snd.set_volume(new_vol)

    def reload_sounds(self) -> None:
        """Tüm havuzları sıfırdan oluştur. Sadece main thread'den çağrılmalı."""
        cfg         = self._cfg
        sr          = cfg["mixer"]["sample_rate"]
        pool_size   = cfg["engine"]["pool_size"]
        norm_target = cfg["engine"]["normalize_target"]
        sound_dir   = Path(cfg["sound"]["dir"])
        p           = self._presets
        wpm_mod     = self._wpm_mod

        # Yeni havuzlar — önceki Sound'lar sonraki with bloğuna kadar yaşıyor
        new_pools: List[List[pygame.mixer.Sound]] = [[] for _ in range(_N_POOLS)]

        print("\n  ▶ Ses havuzları oluşturuluyor...")

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

            del audio; gc.collect()  # WAV float32 array hemen serbest
        else:
            log.warning("Key sound not found: %s", key_path)
            print(f"   [!] Ses dosyası bulunamadı: {key_path}")

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
            print(f"   [!] Ses dosyası bulunamadı: {mouse_path}")

        # ── ÖZEL ATAMALAR ─────────────────────────────────────
        new_custom: Dict[str, pygame.mixer.Sound] = {}
        for key_name, file_path in self._key_bindings.items():
            if os.path.isfile(file_path):
                try:
                    snd = pygame.mixer.Sound(file_path)
                    snd.set_volume(self._volume)
                    new_custom[key_name] = snd
                except Exception as exc:
                    log.warning("Custom bind error (%s): %s", key_name, exc)

        # ── Atomik pool değişimi ──────────────────────────────
        with self._pool_lock:
            # Eski Sound nesneleri burada serbest bırakılır (refcount=0 → SDL belleği geri döner)
            for i, pool in enumerate(new_pools):
                self._pools[i] = pool
            self._custom_sounds = new_custom
            self._last_idx[:] = [-1] * _N_POOLS  # yeni liste yaratmadan sıfırla

        del new_pools  # referans serbest
        gc.collect()
        print("  ▶ Hazır!\n")

    @property
    def active_voices(self) -> int:
        return self._active_voices

    @property
    def volume(self) -> float:
        return self._volume

    # ── PRIVATE: MIXER ─────────────────────────────────────────

    def _init_mixer(self) -> None:
        if sys.platform == "win32" and self._cfg["mixer"].get("use_directsound", True):
            os.environ.setdefault("SDL_AUDIODRIVER", "directsound")
        mcfg = self._cfg["mixer"]
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
            log.info("Mixer initialized: %s", pygame.mixer.get_init())
        except Exception as exc:
            log.critical("Mixer init failed: %s", exc)
            print(f"[FATAL] Mixer başlatılamadı: {exc}")
            sys.exit(1)

    def _load_raw(self, path: Path) -> Tuple[np.ndarray, int]:
        snd    = pygame.mixer.Sound(str(path))
        raw    = snd.get_raw()
        freq, fmt, _ = pygame.mixer.get_init()
        bps    = abs(fmt) // 8
        frames = max(1, int(snd.get_length() * freq))
        n_ch   = max(1, min(2, len(raw) // (frames * bps)))
        audio  = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32767.0
        del snd, raw  # pygame Sound ve bytes hemen serbest
        return audio, n_ch

    def _to_sounds(self, raw_pool: List[bytes]) -> List[pygame.mixer.Sound]:
        """bytes listesini Sound listesine çevir; bytes'ları hemen serbest bırak."""
        result: List[pygame.mixer.Sound] = []
        for data in raw_pool:
            snd = pygame.mixer.Sound(buffer=data)
            snd.set_volume(self._volume)
            result.append(snd)
        # OPT: raw_pool referansı burada düşer; bytes nesneleri GC'ye hazır
        return result

    # ── PRIVATE: POOL SEÇİMİ ───────────────────────────────────

    def _pick(self, pidx: int) -> Optional[pygame.mixer.Sound]:
        """
        Pool'dan son çalınandan farklı rastgele ses seç.
        OPT: getattr/setattr string lookup yerine _last_idx[pidx] int indexing.
        """
        with self._pool_lock:
            pool = self._pools[pidx]
            n    = len(pool)
            if n == 0:
                return None
            if n == 1:
                return pool[0]
            last = self._last_idx[pidx]
            idx  = last
            for _ in range(10):
                idx = random.randrange(n)
                if idx != last:
                    break
            self._last_idx[pidx] = idx
            return pool[idx]

    # ── PRIVATE: AUDIO LOOP ────────────────────────────────────

    def _audio_loop(self) -> None:
        loop_ms   = self._cfg["engine"]["audio_loop_ms"] / 1000.0
        batch_max = self._cfg["engine"]["queue_batch_max"]

        while self._running:
            processed = 0
            while processed < batch_max:
                try:
                    cmd = self._queue.get_nowait()
                except queue.Empty:
                    break
                self._do_play(cmd)
                processed += 1

            self._ring.drain_end_events()
            self._active_voices = self._ring.active_count()
            self._wakeup.wait(timeout=loop_ms)
            self._wakeup.clear()

        # OPT: thread kapanırken kuyruk referanslarını temizle
        while not self._queue.empty():
            try:
                self._queue.get_nowait()
            except queue.Empty:
                break

    def _do_play(self, cmd: PlayCommand) -> None:
        """
        Ses seç → kanal al → equal-power vol → ch.play()

        Yönlendirme: _PIDX_* sabitlerle doğrudan array indexing.
        """
        sound: Optional[pygame.mixer.Sound] = None
        if not cmd.is_release:
            sound = self._custom_sounds.get(cmd.key_id)

        if not cmd.is_release and not cmd.is_mouse:
            self._wpm.record()

        if sound is None:
            if cmd.is_release:
                pidx = _PIDX_HEAVY_R if cmd.key_id in HEAVY_KEYS else _PIDX_NORMAL_R
                sound = self._pick(pidx)

            elif cmd.is_mouse:
                pidx = _PIDX_MOUSE3 if cmd.key_id == MOUSE3_KEY else _PIDX_MOUSE
                sound = self._pick(pidx)

            else:
                fast = self._wpm.wpm() >= self._wpm_thresh
                if cmd.key_id in HEAVY_KEYS:
                    pidx = _PIDX_HEAVY_F if fast else _PIDX_HEAVY
                else:
                    pidx = _PIDX_NORMAL_F if fast else _PIDX_NORMAL
                sound = self._pick(pidx)

        if sound is None:
            return

        ch, n_active = self._ring.acquire()
        release_scale = 0.80 if cmd.is_release else 1.0
        eq_scale = max(0.42, 1.0 / (n_active + 1) ** 0.5) * release_scale
        ch.set_volume(self._volume * eq_scale)
        ch.play(sound)
