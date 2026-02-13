"""
engine.py — Audio Engine Katmanı
==================================
Tüm pygame.mixer çağrıları YALNIZCA bu modüldeki _audio_loop() thread'inde yapılır.
Dışarıdan erişim sadece enqueue_play() ve update_volume() üzerinden olur.

Mimari:
  InputHandler ──enqueue_play()──► SimpleQueue ──► _audio_loop() ──► pygame.mixer
                                                         │
                                            set_endevent + active_count()
                                                         │
                                               _active_voices (int, thread-safe)
"""

from __future__ import annotations

import gc
import logging
import os
import queue
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pygame
import numpy as np

from dsp import build_pool

log = logging.getLogger("KeySim.Engine")


# ─────────────────────────────────────────────────────────────
#  AĞIR TUŞLAR — Kalın ses havuzuna yönlendirilecekler
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


# ─────────────────────────────────────────────────────────────
#  PLAY COMMAND — Immutable veri yapısı
# ─────────────────────────────────────────────────────────────
@dataclass(frozen=True, slots=True)
class PlayCommand:
    """
    InputHandler → AudioEngine arası mesaj.
    Ses seçimi ve volume hesabı audio thread'de yapılır.
    """
    key_id  : str
    is_mouse: bool = False


# ─────────────────────────────────────────────────────────────
#  CHANNEL RING — Pre-allocated, tek kilit
# ─────────────────────────────────────────────────────────────
class ChannelRing:
    """
    Sabit boyutlu ses kanalı havuzu.

    • acquire() → (channel, n_active) atomik olarak döner.
    • Voice stealing: round-robin, en eski kanal alınır.
    • set_endevent: her kanala benzersiz event atanır.
      Audio loop bu eventleri temizler → pygame event queue dolmaz.
    """

    __slots__ = ("_n", "_channels", "_end_events", "_steal_pos",
                 "_lock", "_fade_ms")

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
        """
        pygame.event.custom_type() → her kanala benzersiz event tipi.
        Fallback: pygame.USEREVENT + offset (eski pygame sürümleri için).
        """
        try:
            events = [pygame.event.custom_type() for _ in range(self._n)]
        except AttributeError:
            base   = getattr(pygame, "USEREVENT", 24)
            events = [base + 1 + i for i in range(self._n)]

        for ch, ev in zip(self._channels, events):
            ch.set_endevent(ev)

        self._end_events = events
        log.debug("End events assigned: %s", events[:3])

    def acquire(self) -> Tuple[pygame.mixer.Channel, int]:
        """
        Boş kanal bul + aktif kanal sayısını döndür.
        Tüm kanallar doluysa → round-robin voice steal (fade_ms ile).
        """
        with self._lock:
            active   = 0
            free_ch  : Optional[pygame.mixer.Channel] = None

            for ch in self._channels:
                if ch.get_busy():
                    active += 1
                elif free_ch is None:
                    free_ch = ch

            if free_ch is not None:
                return free_ch, active

            # Voice stealing
            victim = self._channels[self._steal_pos]
            if self._fade_ms > 0:
                victim.fadeout(self._fade_ms)
            else:
                victim.stop()
            self._steal_pos = (self._steal_pos + 1) % self._n
            return victim, self._n

    def active_count(self) -> int:
        """Gerçek zamanlı aktif kanal sayısı — O(POLYPHONY), doğru değer."""
        return sum(1 for ch in self._channels if ch.get_busy())

    def drain_end_events(self) -> int:
        """
        Kanal bitiş eventlerini temizle.
        Dönen değer: biten kanal sayısı.
        Amaç: pygame event queue'nun dolmasını engelle.
        """
        total = 0
        for ev in self._end_events:
            total += len(pygame.event.get(ev))
        return total

    def stop_all(self) -> None:
        for ch in self._channels:
            ch.stop()


# ─────────────────────────────────────────────────────────────
#  AUDIO ENGINE
# ─────────────────────────────────────────────────────────────
class AudioEngine:
    """
    Tek audio thread üzerinde çalışan ses motoru.

    Genel Bakış:
      enqueue_play()  → PlayCommand kuyruğa eklenir, wakeup event set edilir.
      _audio_loop()   → kuyruk drene edilir, sesler çalınır, voice count güncellenir.

    Thread güvenliği:
      pygame.mixer yalnızca _audio_loop() içinden çağrılır.
      Dışarıdan erişim: enqueue_play(), update_volume(), active_voices property.
    """

    def __init__(self, cfg: dict, presets: dict, key_bindings: dict) -> None:
        self._cfg          = cfg
        self._presets      = presets
        self._key_bindings = key_bindings

        # Ses havuzları — pygame.mixer.Sound nesneleri
        self._normal_pool : List[pygame.mixer.Sound] = []
        self._heavy_pool  : List[pygame.mixer.Sound] = []
        self._mouse_pool  : List[pygame.mixer.Sound] = []
        self._custom_sounds: Dict[str, pygame.mixer.Sound] = {}

        self._last_normal  = -1
        self._last_heavy   = -1
        self._last_mouse   = -1
        self._pool_lock    = threading.Lock()  # reload sırasında havuz erişimi

        # Thread state
        self._queue  : queue.SimpleQueue[PlayCommand] = queue.SimpleQueue()
        self._wakeup = threading.Event()
        self._running = False
        self._thread  : Optional[threading.Thread] = None

        # Ses seviyesi — atomic float (GIL korumalı)
        self._volume : float = cfg["app"]["initial_volume"]

        # Aktif ses sayısı — yalnızca audio thread yazar, UI thread okur
        self._active_voices : int = 0

        # Mixer + Channel Ring
        self._init_mixer()
        self._ring = ChannelRing(
            polyphony = cfg["engine"]["polyphony"],
            fade_ms   = cfg["engine"]["steal_fade_ms"],
        )

    # ── PUBLIC API ─────────────────────────────────────────────

    def start(self) -> None:
        """Audio thread'i başlat."""
        if self._running:
            return
        self._running = True
        self._thread  = threading.Thread(
            target=self._audio_loop, name="AudioEngine", daemon=True
        )
        self._thread.start()
        log.info("AudioEngine started.")

    def stop(self) -> None:
        """Graceful shutdown: kuyruğu boşalt, sesleri fade-out, thread'i join et."""
        if not self._running:
            return
        self._running = False
        self._wakeup.set()

        if self._thread:
            self._thread.join(timeout=2.0)

        # Fade-out + stop
        pygame.mixer.fadeout(150)
        time.sleep(0.16)
        self._ring.stop_all()
        pygame.mixer.quit()
        log.info("AudioEngine stopped.")

    def enqueue_play(self, key_id: str, is_mouse: bool = False) -> None:
        """
        Ses çalma isteği kuyruğa ekle.
        Thread-safe: herhangi bir thread'den çağrılabilir.
        """
        self._queue.put(PlayCommand(key_id=key_id, is_mouse=is_mouse))
        self._wakeup.set()

    def update_volume(self, new_vol: float) -> None:
        """
        Ses seviyesini güncelle.
        Havuzdaki tüm Sound nesnelerine uygulanır.
        Thread-safe değildir — yalnızca main thread'den çağrılmalı.
        """
        self._volume = max(0.0, min(1.0, new_vol))
        with self._pool_lock:
            for snd in self._normal_pool + self._heavy_pool + self._mouse_pool:
                snd.set_volume(self._volume)
            for snd in self._custom_sounds.values():
                snd.set_volume(self._volume)

    def reload_sounds(self) -> None:
        """
        Ses havuzlarını yeniden oluştur.
        Yalnızca main thread'den, is_customizing=True iken çağrılmalı.
        """
        cfg = self._cfg
        sr          = cfg["mixer"]["sample_rate"]
        pool_size   = cfg["engine"]["pool_size"]
        norm_target = cfg["engine"]["normalize_target"]
        sound_dir   = Path(cfg["sound"]["dir"])

        print("\n  ▶ Ses havuzları oluşturuluyor...")

        new_normal  : List[pygame.mixer.Sound] = []
        new_heavy   : List[pygame.mixer.Sound] = []
        new_mouse   : List[pygame.mixer.Sound] = []
        new_custom  : Dict[str, pygame.mixer.Sound] = {}

        # ── Klavye sesleri ───────────────────────────────────
        key_path = sound_dir / cfg["sound"]["key_file"]
        if key_path.exists():
            audio, n_ch = self._load_raw(key_path)

            print("   Normal keys...")
            raw_pool = build_pool(audio, n_ch, sr, "normal_key",
                                  self._presets, pool_size, norm_target, 42, "normal")
            new_normal = [self._bytes_to_sound(b) for b in raw_pool]
            del raw_pool; gc.collect()

            print("   Heavy keys  (Space/Enter/Backspace...)...")
            raw_pool = build_pool(audio, n_ch, sr, "heavy_key",
                                  self._presets, pool_size, norm_target, 99, "heavy")
            new_heavy = [self._bytes_to_sound(b) for b in raw_pool]
            del raw_pool, audio; gc.collect()
        else:
            log.warning("Key sound not found: %s", key_path)
            print(f"   [!] Ses dosyası bulunamadı: {key_path}")

        # ── Fare sesleri ─────────────────────────────────────
        mouse_path = sound_dir / cfg["sound"]["mouse_file"]
        if mouse_path.exists():
            audio, n_ch = self._load_raw(mouse_path)
            print("   Mouse clicks...")
            raw_pool = build_pool(audio, n_ch, sr, "mouse_click",
                                  self._presets, pool_size, norm_target, 77, "mouse")
            new_mouse = [self._bytes_to_sound(b) for b in raw_pool]
            del raw_pool, audio; gc.collect()
        else:
            log.warning("Mouse sound not found: %s", mouse_path)
            print(f"   [!] Ses dosyası bulunamadı: {mouse_path}")

        # ── Özel atamalar ─────────────────────────────────────
        for key_name, file_path in self._key_bindings.items():
            if os.path.isfile(file_path):
                try:
                    snd = pygame.mixer.Sound(file_path)
                    snd.set_volume(self._volume)
                    new_custom[key_name] = snd
                except Exception as exc:
                    log.warning("Custom bind load error (%s): %s", key_name, exc)

        # ── Atomik pool değişimi ──────────────────────────────
        with self._pool_lock:
            self._normal_pool  = new_normal
            self._heavy_pool   = new_heavy
            self._mouse_pool   = new_mouse
            self._custom_sounds = new_custom
            self._last_normal  = self._last_heavy = self._last_mouse = -1

        gc.collect()
        print("  ▶ Hazır!\n")

    @property
    def active_voices(self) -> int:
        """UI tarafından okunabilir aktif ses sayısı."""
        return self._active_voices

    @property
    def volume(self) -> float:
        return self._volume

    # ── PRIVATE: MIXER ─────────────────────────────────────────

    def _init_mixer(self) -> None:
        """
        Mixer başlatma.
        'Re-init trick': bazı sistemlerde SDL kendi buffer boyutunu zorlarsa,
        quit()+init() ile gerçek 512-sample buffer elde edilir.
        """
        if sys.platform == "win32" and self._cfg["mixer"]["use_directsound"]:
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
        """
        WAV → float32 numpy interleaved, kanal sayısı.
        pygame.mixer WAV'ı mixer ayarlarına otomatik dönüştürür.
        """
        snd    = pygame.mixer.Sound(str(path))
        raw    = snd.get_raw()
        freq, fmt, _ = pygame.mixer.get_init()
        bps    = abs(fmt) // 8
        frames = max(1, int(snd.get_length() * freq))
        n_ch   = max(1, min(2, len(raw) // (frames * bps)))
        audio  = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32767.0
        del snd  # pygame Sound nesnesini serbest bırak
        return audio, n_ch

    def _bytes_to_sound(self, data: bytes) -> pygame.mixer.Sound:
        """int16 PCM bytes → pygame.mixer.Sound, volume set edilmiş."""
        snd = pygame.mixer.Sound(buffer=data)
        snd.set_volume(self._volume)
        return snd

    # ── PRIVATE: POOL SEÇİMİ ───────────────────────────────────

    def _pick(self, pool: List[pygame.mixer.Sound],
              attr: str) -> Optional[pygame.mixer.Sound]:
        """
        Pool'dan son çalınandan farklı rastgele varyasyon seç.
        GIL korumalı — kilit gerekmez (pool yalnızca reload'da değişir,
        o sırada is_customizing=True ve kuyruk durur).
        """
        with self._pool_lock:
            n = len(pool)
            if n == 0:
                return None
            if n == 1:
                return pool[0]
            last = getattr(self, attr)
            idx  = last
            for _ in range(10):
                import random
                idx = random.randrange(n)
                if idx != last:
                    break
            setattr(self, attr, idx)
            return pool[idx]

    # ── PRIVATE: AUDIO LOOP ────────────────────────────────────

    def _audio_loop(self) -> None:
        """
        Audio engine ana döngüsü — ayrı thread'de çalışır.

        Her iterasyonda:
          1. Yeni komutları kuyruğundan al (en fazla batch_max adet)
          2. Her komut için: ses seç → kanal al → equal-power vol → ch.play()
          3. Bitiş eventlerini temizle (pygame event queue dolmasın)
          4. active_voices'ı gerçek kanal sayısıyla güncelle
          5. Wakeup event'ini bekle (yeni komut veya timeout=loop_ms)
        """
        loop_ms   = self._cfg["engine"]["audio_loop_ms"] / 1000.0
        batch_max = self._cfg["engine"]["queue_batch_max"]

        while self._running:
            # ── 1. Kuyruk drain ───────────────────────────────
            processed = 0
            while processed < batch_max:
                try:
                    cmd = self._queue.get_nowait()
                except queue.Empty:
                    break
                self._do_play(cmd)
                processed += 1

            # ── 2. End eventleri temizle ──────────────────────
            self._ring.drain_end_events()

            # ── 3. Gerçek voice count ─────────────────────────
            self._active_voices = self._ring.active_count()

            # ── 4. Bir sonraki komuta kadar bekle ─────────────
            self._wakeup.wait(timeout=loop_ms)
            self._wakeup.clear()

    def _do_play(self, cmd: PlayCommand) -> None:
        """
        Ses çal — yalnızca _audio_loop() içinden çağrılır.

        Equal-power mixing:
          N aktif ses iken yeni ses = volume / √(N+1)
          Bu SDL_mixer'ın sample-sum karıştırmasında int16 taşmasını engeller.
          N=0→1.0× | N=3→0.5× | N=8→0.33× | minimum 0.42×
        """
        # Ses seç
        sound: Optional[pygame.mixer.Sound] = self._custom_sounds.get(cmd.key_id)

        if sound is None:
            if cmd.is_mouse:
                sound = self._pick(self._mouse_pool,  "_last_mouse")
            elif cmd.key_id in HEAVY_KEYS:
                sound = self._pick(self._heavy_pool,  "_last_heavy")
            else:
                sound = self._pick(self._normal_pool, "_last_normal")

        if sound is None:
            return

        # Kanal + aktif sayı (tek atomik işlem)
        ch, n_active = self._ring.acquire()

        # Equal-power volume
        eq_scale = max(0.42, 1.0 / (n_active + 1) ** 0.5)
        vol      = self._volume * eq_scale

        ch.set_volume(vol)
        ch.play(sound)
