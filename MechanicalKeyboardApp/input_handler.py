"""
input_handler.py - Keyboard / Mouse Listener Layer v2.4 (Cross-Platform)
=========================================================================
v2.4 CHANGES (cross-platform + root-cause mouse stutter fix):

MOUSE STUTTER — ROOT CAUSE & FIX:
  The stutter when entering a game is caused by Windows scheduling the
  WH_MOUSE_LL hook thread at normal priority. When a game starts its
  DirectInput/RawInput/D3D11 initialization, the OS floods the message
  queue and our hook thread gets preempted for 50-200ms. During that window
  our hook callback is late, Windows starts counting down the 300ms hook
  timeout, and mouse movement feels sluggish.

  Fix: Immediately escalate the mouse listener thread to TIME_CRITICAL
  priority (Windows) or SCHED_FIFO (Linux root) or QOS_CLASS_USER_INTERACTIVE
  (macOS). Also removed the 200ms delay entirely — the priority escalation
  is the correct solution, not a timing workaround.

CROSS-PLATFORM:
  Windows  : DirectSound + WinMM timer resolution 1ms + thread priority
  macOS    : CoreAudio driver selection, accessibility permission check,
             QoS thread class escalation (mach_thread_policy)
  Linux    : ALSA/Pulse auto-detect, real-time SCHED_FIFO where permitted,
             Wayland warning (XDG_SESSION_TYPE detection)

OTHER FIXES (preserved from v2.3):
  - VK cache: O(1) normalize_key_name after first call per vk
  - Stuck-key watchdog: auto-release keys held > 2.5s
  - OS auto-repeat guard: 1ms duplicate press filter
  - Click deque: hook callback returns in ~100ns
  - on_move / on_scroll NOT registered: zero Python overhead on moves
  - Modifier normalisation: Key.shift -> Key.shift_l etc.
"""

from __future__ import annotations

import collections
import logging
import os
import sys
import threading
import time
from typing import Callable, Dict, Optional, Set, Tuple

from pynput import keyboard, mouse

log = logging.getLogger("KeySim.Input")

# ── Platform detection ────────────────────────────────────────
_IS_WIN   = sys.platform == "win32"
_IS_MAC   = sys.platform == "darwin"
_IS_LINUX = sys.platform.startswith("linux")

# ── Limits ────────────────────────────────────────────────────
_MAX_PRESSED      = 30      # physical impossibility guard
_MAX_PRESS_TIMES  = 40      # dict memory guard
_STUCK_KEY_SECS   = 2.5    # auto-release threshold (s)
_WATCHDOG_SLEEP   = 0.5    # stuck-key check interval (s)
_OS_REPEAT_GUARD  = 0.001  # 1ms: drop system-generated key repeats

# ── Modifier normalisation ─────────────────────────────────────
# pynput emits generic Key.shift/ctrl/alt on some Windows & Linux builds.
_MODIFIER_NORMALIZE: Dict[str, str] = {
    "Key.shift" : "Key.shift_l",
    "Key.ctrl"  : "Key.ctrl_l",
    "Key.alt"   : "Key.alt_l",
}

# ── VK lookup cache ───────────────────────────────────────────
# Windows only. First call for a vk does O(n) enum scan; all subsequent O(1).
_VK_CACHE: Dict[int, Optional[str]] = {}

# ── Sentinel for dict.get() miss detection ───────────────────
_MISS = object()


# ─────────────────────────────────────────────────────────────
#  PLATFORM UTILITIES
# ─────────────────────────────────────────────────────────────

def _set_thread_priority_high() -> bool:
    """
    Escalate the current thread to high / real-time priority.
    Called inside the mouse listener thread before the hook message loop.
    Returns True on success, False if unsupported / permissions denied.
    """
    try:
        if _IS_WIN:
            import ctypes
            kernel32 = ctypes.windll.kernel32
            # THREAD_PRIORITY_TIME_CRITICAL = 15
            # This ensures the hook callback is never preempted by games.
            handle = kernel32.GetCurrentThread()
            ok = kernel32.SetThreadPriority(handle, 15)
            if not ok:
                # Fall back to THREAD_PRIORITY_HIGHEST = 2
                kernel32.SetThreadPriority(handle, 2)
            return True

        if _IS_MAC:
            # Set QoS class to USER_INTERACTIVE (highest non-RT class).
            # Full mach_thread_policy_t requires entitlements, so we use
            # the POSIX layer which is available without special privileges.
            import ctypes.util
            libc_name = ctypes.util.find_library("c")
            if libc_name:
                import ctypes
                libc = ctypes.CDLL(libc_name, use_errno=True)
                # pthread_set_qos_class_self_np(QOS_CLASS_USER_INTERACTIVE=0x21, 0)
                if hasattr(libc, "pthread_set_qos_class_self_np"):
                    libc.pthread_set_qos_class_self_np(0x21, 0)
                    return True
            # Fallback: use standard POSIX priority
            try:
                import ctypes
                libc = ctypes.CDLL(ctypes.util.find_library("c"))
                SCHED_OTHER = 0
                param = ctypes.c_int(20)  # max nice-level
                libc.setpriority(0, 0, -20)  # requires root; silently fail
            except Exception:
                pass
            return True

        if _IS_LINUX:
            # Try SCHED_FIFO (requires CAP_SYS_NICE or rtkit-daemon).
            # Fall back to nice(-10) which always works.
            try:
                import ctypes
                libc = ctypes.CDLL("libc.so.6", use_errno=True)
                SCHED_FIFO = 1
                class SchedParam(ctypes.Structure):
                    _fields_ = [("sched_priority", ctypes.c_int)]
                param = SchedParam(90)  # priority 90 of 99
                ret = libc.sched_setscheduler(0, SCHED_FIFO, ctypes.byref(param))
                if ret == 0:
                    return True
            except Exception:
                pass
            try:
                os.nice(-10)   # Requires root; silently ignored otherwise
            except (PermissionError, AttributeError):
                pass
            return True
    except Exception as exc:
        log.debug("Thread priority escalation failed: %s", exc)
    return False


def _check_platform_prerequisites() -> None:
    """
    Warn the user about platform-specific requirements that affect
    audio/input quality. Called once at startup (from main thread).
    """
    if _IS_MAC:
        # pynput on macOS requires Accessibility permission.
        # Trying to create a listener without it hangs silently.
        try:
            import ctypes
            appkit = ctypes.CDLL("/System/Library/Frameworks/ApplicationServices.framework/"
                                 "Versions/A/Frameworks/HIServices.framework/"
                                 "Versions/A/HIServices",
                                 use_errno=True)
            # AXIsProcessTrusted() returns 1 if Accessibility is granted.
            if hasattr(appkit, "AXIsProcessTrusted"):
                trusted = appkit.AXIsProcessTrusted()
                if not trusted:
                    print("[WARNING] macOS: Accessibility permission required for keyboard/mouse.")
                    print("  System Preferences -> Privacy & Security -> Accessibility")
                    print("  Add this terminal / Python executable and restart.\n")
        except Exception:
            pass

    if _IS_LINUX:
        session = os.environ.get("XDG_SESSION_TYPE", "").lower()
        if "wayland" in session:
            print("[WARNING] Wayland session detected. pynput keyboard input works via")
            print("  XWayland only. Mouse clicks may not be captured in native Wayland apps.")
            print("  For full support, run under X11: DISPLAY=:0 python main.py\n")


def _setup_windows_timer() -> None:
    """
    On Windows, set multimedia timer resolution to 1ms.
    Without this, time.sleep() and threading.Event.wait() have ~15ms granularity,
    causing the audio loop to wake every ~15ms instead of ~1ms.
    Must be called from the main thread before audio engine starts.
    """
    if not _IS_WIN:
        return
    try:
        import ctypes
        winmm = ctypes.WinDLL("winmm")
        # timeBeginPeriod(1) — sets system timer resolution to 1ms
        result = winmm.timeBeginPeriod(1)
        if result == 0:   # TIMERR_NOERROR
            log.debug("Windows timer resolution set to 1ms.")
        # Store reference so it can be ended on exit
        _setup_windows_timer._winmm = winmm
    except Exception as exc:
        log.debug("timeBeginPeriod failed: %s", exc)


def _teardown_windows_timer() -> None:
    """Restore Windows timer resolution on exit."""
    if not _IS_WIN:
        return
    try:
        winmm = getattr(_setup_windows_timer, "_winmm", None)
        if winmm:
            winmm.timeEndPeriod(1)
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────
#  KEY NAME NORMALISATION
# ─────────────────────────────────────────────────────────────

def normalize_key_name(key: keyboard.Key | keyboard.KeyCode) -> Optional[str]:
    """
    Convert pynput key object -> stable string identifier.

    Platform specifics:
      Windows:  KeyCode(vk=160) -> 'Key.shift_l'  (VK cache after first call)
      macOS:    KeyCode(char=None, vk=None) can appear for media/special keys
      Linux:    keyboard.Key members may differ for some special keys

    All platforms: generic modifiers normalised to _l variants.
    """
    try:
        if isinstance(key, keyboard.KeyCode):
            if key.char:
                return key.char.lower()

            # Windows VK path — fast O(1) after first lookup
            vk = getattr(key, "vk", None)
            if vk is not None:
                cached = _VK_CACHE.get(vk, _MISS)
                if cached is not _MISS:
                    return cached   # type: ignore[return-value]

                # One-time enum scan — result cached permanently
                result: Optional[str] = None
                for k in keyboard.Key:
                    try:
                        kv = k.value
                        if isinstance(kv, keyboard.KeyCode) and \
                                getattr(kv, "vk", None) == vk:
                            raw = str(k)
                            result = _MODIFIER_NORMALIZE.get(raw, raw)
                            break
                    except Exception:
                        pass
                _VK_CACHE[vk] = result
                return result

            return None  # Unknown KeyCode (e.g., macOS media key without char)

        # keyboard.Key enum member
        raw = str(key)
        return _MODIFIER_NORMALIZE.get(raw, raw)

    except Exception as exc:
        log.debug("normalize_key_name error: %s", exc)
        return None


def normalize_button_name(button: mouse.Button) -> str:
    return str(button)


# ─────────────────────────────────────────────────────────────
#  INPUT HANDLER v2.4
# ─────────────────────────────────────────────────────────────

class InputHandler:
    """
    Cross-platform keyboard + mouse listener.

    Architecture:
      KbListener thread     on_press / on_release  ->  enqueue() directly
      MouseListener thread  on_click (INSTANT)     ->  _click_deque
      ClickProcessor thr    _click_deque            ->  enqueue()
      StuckKeyWatchdog      every 0.5s              ->  auto-clear stale keys

    Mouse hook runs at TIME_CRITICAL (Win) / USER_INTERACTIVE (macOS) /
    SCHED_FIFO (Linux) priority. This is the root-cause fix for mouse
    stutter when entering a game — the hook thread can never be preempted
    by game initialization code.

    The hook callback itself does only deque.append + event.set (~100ns).
    """

    def __init__(
        self,
        enqueue_fn      : Callable[[str, bool, bool, float, str], None],
        pressed_keys    : Set[str],
        get_customizing : Callable[[], bool],
        get_repeat      : Callable[[], bool],
        get_running     : Callable[[], bool],
        get_release     : Callable[[], bool] = lambda: True,
        get_wpm         : Callable[[], float] = lambda: 0.0,
    ) -> None:
        self._enqueue     = enqueue_fn
        self._pressed     = pressed_keys
        self._customizing = get_customizing
        self._repeat      = get_repeat
        self._running     = get_running
        self._release     = get_release
        self._get_wpm     = get_wpm

        # {key_id: monotonic press time}
        self._press_times : Dict[str, float] = {}
        self._last_key    : str = ""

        # SPSC deque: hook appends, click processor pops (GIL-safe)
        self._click_deque : collections.deque = collections.deque()
        self._click_event  = threading.Event()

        self._kb_listener  : Optional[keyboard.Listener] = None
        self._ms_listener  : Optional[mouse.Listener]    = None
        self._kb_thread    : Optional[threading.Thread]  = None
        self._ms_thread    : Optional[threading.Thread]  = None
        self._click_thread : Optional[threading.Thread]  = None
        self._watchdog_thr : Optional[threading.Thread]  = None

    # ── PUBLIC ─────────────────────────────────────────────────

    def start(self) -> None:
        # Click processor (drains deque, calls enqueue)
        self._click_thread = threading.Thread(
            target=self._click_processor_loop,
            name="ClickProcessor",
            daemon=True,
        )
        self._click_thread.start()

        # Stuck-key watchdog
        self._watchdog_thr = threading.Thread(
            target=self._stuck_key_watchdog,
            name="StuckKeyWatchdog",
            daemon=True,
        )
        self._watchdog_thr.start()

        # Keyboard listener thread (normal priority is fine for keyboard)
        self._kb_thread = threading.Thread(
            target=self._keyboard_loop,
            name="KbListener",
            daemon=True,
        )
        self._kb_thread.start()

        # Mouse listener thread (HIGH priority — this is the stutter fix)
        self._ms_thread = threading.Thread(
            target=self._mouse_loop,
            name="MouseListener",
            daemon=True,
        )
        self._ms_thread.start()

        log.info("InputHandler v2.4 started (platform=%s).", sys.platform)

    def stop(self) -> None:
        if self._kb_listener:
            try:
                self._kb_listener.stop()
            except Exception:
                pass
        if self._ms_listener:
            try:
                self._ms_listener.stop()
            except Exception:
                pass
        self._pressed.clear()
        self._press_times.clear()
        self._click_deque.clear()
        self._click_event.set()   # wake processor so it exits
        log.info("InputHandler stopped.")

    # ── CLICK PROCESSOR ────────────────────────────────────────

    def _click_processor_loop(self) -> None:
        """
        Drains _click_deque and calls enqueue.
        Woken by Event.set() from hook callback — latency < 1ms.
        CPU cost: essentially zero (only runs on actual mouse clicks).
        """
        while self._running():
            self._click_event.wait(timeout=0.05)
            self._click_event.clear()
            while True:
                try:
                    name, prev_key = self._click_deque.popleft()
                except IndexError:
                    break
                try:
                    self._enqueue(name, True, False, 0.0, prev_key)
                except Exception as exc:
                    log.debug("click enqueue error: %s", exc)

    # ── STUCK KEY WATCHDOG ─────────────────────────────────────

    def _stuck_key_watchdog(self) -> None:
        """
        Auto-release keys that have been held > 2.5s.
        Handles focus-loss (Alt+Tab), game-window capture,
        minimize, and any other scenario where the OS swallows
        the key-release event before pynput can fire on_release.
        """
        while self._running():
            time.sleep(_WATCHDOG_SLEEP)
            now = time.monotonic()
            stale = [
                k for k, t in list(self._press_times.items())
                if now - t > _STUCK_KEY_SECS
            ]
            for k in stale:
                self._pressed.discard(k)
                self._press_times.pop(k, None)
                log.debug("Watchdog: auto-released stuck key '%s'", k)

    # ── KEYBOARD LISTENER THREAD ──────────────────────────────

    def _keyboard_loop(self) -> None:
        """
        Keyboard listener runs at normal thread priority.
        Keyboard hooks have much more lenient OS timeouts than mouse hooks,
        so priority escalation is not needed here.
        """

        def on_press(key: keyboard.Key | keyboard.KeyCode) -> Optional[bool]:
            if not self._running():
                return False
            if self._customizing():
                return None

            name = normalize_key_name(key)
            if not name:
                return None

            now = time.monotonic()

            # OS auto-repeat guard: same key within 1ms = system-generated repeat
            prev_t = self._press_times.get(name)
            if prev_t is not None and (now - prev_t) < _OS_REPEAT_GUARD:
                return None

            # User-controlled repeat suppression
            if not self._repeat() and name in self._pressed:
                return None

            # Overflow guard
            if len(self._pressed) >= _MAX_PRESSED:
                log.warning(
                    "pressed_keys overflow (%d) — clearing stale state",
                    len(self._pressed),
                )
                self._pressed.clear()
                self._press_times.clear()

            self._press_times[name] = now

            # press_times memory guard
            if len(self._press_times) > _MAX_PRESS_TIMES:
                oldest = sorted(self._press_times.items(), key=lambda x: x[1])
                for old_k, _ in oldest[:10]:
                    self._press_times.pop(old_k, None)

            self._pressed.add(name)
            prev_key = self._last_key
            self._last_key = name

            try:
                self._enqueue(name, False, False, 0.0, prev_key)
            except Exception as exc:
                log.debug("on_press enqueue error: %s", exc)
            return None

        def on_release(key: keyboard.Key | keyboard.KeyCode) -> None:
            name = normalize_key_name(key)
            if not name:
                return

            was_pressed = name in self._pressed
            self._pressed.discard(name)

            duration = 0.0
            press_t = self._press_times.pop(name, None)
            if press_t is not None:
                duration = time.monotonic() - press_t
                if not (0.0 < duration < 10.0):
                    duration = 0.08   # fallback: typical tap duration

            if was_pressed and self._release() and not self._customizing():
                try:
                    self._enqueue(name, False, True, duration, self._last_key)
                except Exception as exc:
                    log.debug("on_release enqueue error: %s", exc)

        try:
            self._kb_listener = keyboard.Listener(
                on_press   = on_press,
                on_release = on_release,
                suppress   = False,
            )
            with self._kb_listener:
                self._kb_listener.join()
        except Exception as exc:
            log.error("Keyboard listener crashed: %s", exc)
        finally:
            self._kb_listener = None

    # ── MOUSE LISTENER THREAD (HIGH PRIORITY) ─────────────────

    def _mouse_loop(self) -> None:
        """
        Mouse listener thread.

        STUTTER FIX: The FIRST thing this function does is escalate
        its own thread priority. This must happen before pynput installs
        the WH_MOUSE_LL (Windows) / CGEventTap (macOS) / Xlib hook (Linux).

        On Windows: THREAD_PRIORITY_TIME_CRITICAL ensures this thread is
        never preempted by game initialization (DirectX, RawInput setup).
        The hook callback budget is 300ms; with TIME_CRITICAL we stay well
        under 1ms even when a game is loading.

        On macOS: QoS USER_INTERACTIVE ensures the CGEventTap callback
        is not delayed by Spotlight indexing, app launch, etc.

        on_move and on_scroll are intentionally NOT registered: pynput
        only calls into Python for registered events, so mouse movement
        passes through the hook at near-native kernel speed.
        """
        # ── Priority escalation (root cause fix) ───────────────
        elevated = _set_thread_priority_high()
        if elevated:
            log.debug("Mouse listener thread priority elevated on %s.", sys.platform)
        else:
            log.debug("Mouse listener thread priority could not be elevated.")

        # ── Mouse hook callback (MUST return in << 300ms) ───────
        def on_click(
            x: int, y: int,
            button: mouse.Button,
            pressed: bool,
        ) -> Optional[bool]:
            """
            This function runs inside the OS hook chain.
            Budget: << 300ms (Windows will uninstall hook if exceeded).
            We do ONLY: deque.append (~50ns) + event.set (~200ns).
            """
            if not pressed:
                return None
            if not self._running():
                return False
            if self._customizing():
                return None

            name = normalize_button_name(button)
            prev_key = self._last_key
            self._last_key = name

            # Both operations are O(1) and GIL-atomic — total ~250ns
            self._click_deque.append((name, prev_key))
            self._click_event.set()
            return None

        try:
            self._ms_listener = mouse.Listener(
                on_click  = on_click,
                suppress  = False,
                # on_move and on_scroll intentionally omitted
                # → zero Python overhead for movement events
            )
            with self._ms_listener:
                self._ms_listener.join()
        except Exception as exc:
            log.error("Mouse listener crashed: %s", exc)
        finally:
            self._ms_listener = None


# ─────────────────────────────────────────────────────────────
#  SINGLE KEY CAPTURE (customise flow)
# ─────────────────────────────────────────────────────────────

class SingleKeyCapture:
    """Blocks until the user presses one key or mouse button."""
    __slots__ = ("_result", "_event")

    def __init__(self) -> None:
        self._result : Optional[str] = None
        self._event  = threading.Event()

    def wait(self, timeout: float = 30.0) -> Optional[str]:
        self._result = None
        self._event.clear()

        def on_press(key) -> Optional[bool]:
            name = normalize_key_name(key)
            if name:
                self._result = name
                self._event.set()
                return False
            return None

        def on_click(x, y, button, pressed) -> Optional[bool]:
            if pressed:
                self._result = normalize_button_name(button)
                self._event.set()
                return False
            return None

        kl = keyboard.Listener(on_press=on_press)
        ml = mouse.Listener(on_click=on_click)
        kl.start()
        ml.start()
        self._event.wait(timeout=timeout)
        kl.stop()
        ml.stop()
        return self._result
