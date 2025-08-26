import os
import time
import yaml
import wave
import struct
import shutil
import requests
import pvporcupine
import pyaudio
import threading
import re
import random
import sys
from typing import Optional
from datetime import datetime, UTC
from flask import Flask, request, jsonify
import pyttsx3
import logging
try:
    import pythoncom  # For COM initialization in each TTS thread on Windows
except ImportError:
    pythoncom = None
try:
    import msvcrt  # Windows console key capture
except ImportError:
    msvcrt = None
try:
    import winsound  # Windows-specific (user is on Windows)
except ImportError:  # fallback noop
    winsound = None
try:
    import keyboard  # For global hotkeys
except ImportError:
    keyboard = None

# Rich UI components
try:
    from rich.console import Console
    from rich.layout import Layout
    from rich.panel import Panel
    from rich.live import Live
    from rich import box
    RICH_AVAILABLE = True
except ImportError:
    RICH_AVAILABLE = False

console = Console() if 'Console' in globals() else None
log_lock = threading.Lock()
log_buffer = []  # store log lines
LOG_LIMIT = 800
ui_stop_event = threading.Event()

# Command input (non-blocking) state
command_mode = None  # None | 'send_text' | 'speak_only'
command_buffer = []
command_lock = threading.Lock()

# Status tracking
status_lock = threading.Lock()
status = {
    'recording': False,
    'recording_reason': '',
    'last_wake': None,
    'last_audio_webhook': None,  # dict: {'time': ts, 'success': bool, 'code': code}
    'last_text_webhook': None,   # same structure
    'failed_uploads': 0,
    'manual_start_count': 0,
    'device_errors': 0,
    'device_recoveries': 0,
    'last_device_error': None,
    'input_device': None,
}

import builtins as _b
_orig_print = _b.print
def _safe_print(*args, **kwargs):
    msg = ' '.join(str(a) for a in args)
    log(msg)
    if not RICH_AVAILABLE:
        _orig_print(*args, **kwargs)
_b.print = _safe_print  # Monkey-patch global print early

def log(message: str):
    """Append a message to in-memory log (and print fallback if Rich not active)."""
    ts = time.strftime('%H:%M:%S')
    line = f"[{ts}] {message}"
    with log_lock:
        log_buffer.append(line)
        if len(log_buffer) > LOG_LIMIT:
            del log_buffer[0:len(log_buffer)-LOG_LIMIT]
    if not RICH_AVAILABLE:
        # fallback plain print using original print to avoid recursion
        _orig_print(line)

def _build_layout(cfg):
    layout = Layout(name='root')
    layout.split_column(
        Layout(name='logs', ratio=7),
        Layout(name='bottom', ratio=3)
    )
    layout['bottom'].split_row(
        Layout(name='left', ratio=3),
        Layout(name='right', ratio=2)
    )
    layout['left'].split_column(
        Layout(name='shell', ratio=2),
        Layout(name='shortcuts', ratio=1)
    )
    # Logs
    with log_lock:
        logs_text = "\n".join(log_buffer[-400:]) if log_buffer else "(no logs yet)"
    layout['logs'].update(Panel(logs_text, title='Logs', border_style='cyan', box=box.ROUNDED))
    # Shell / current input
    with command_lock:
        mode = command_mode
        buf = ''.join(command_buffer)
    if mode == 'send_text':
        shell_line = f"> (send+tts) {buf}"
    elif mode == 'speak_only':
        shell_line = f"> (tts-only) {buf}"
    else:
        shell_line = "> press q or v to start typing (Enter=commit Esc=cancel)"
    layout['shell'].update(Panel(shell_line, title='Shell', border_style='magenta', box=box.ROUNDED))
    # Shortcuts panel
    sc_cfg = cfg.get('shortcuts', {}) or {}
    entries = []
    # General commands
    entries.append("<q> send text")
    entries.append("<v> playback tts")
    entries.append("<r> retry failed")
    entries.append("<x> exit")
    # Recording shortcuts
    if sc_cfg.get('start_recording'): entries.append(f"<{sc_cfg.get('start_recording')}> start")
    if sc_cfg.get('abort_recording'): entries.append(f"<{sc_cfg.get('abort_recording')}> abort")
    if sc_cfg.get('finalize_recording'): entries.append(f"<{sc_cfg.get('finalize_recording')}> send")
    entries.append(f"[global] {'on' if sc_cfg.get('use_global') else 'off'}")
    # Compress into two lines
    if len(entries) <= 4:
        line1 = '  '.join(entries)
        line2 = ''
    else:
        half = (len(entries) + 1) // 2
        line1 = '  '.join(entries[:half])
        line2 = '  '.join(entries[half:])
    shortcuts_text = (line1 + ('\n' + line2 if line2 else ''))
    layout['shortcuts'].update(Panel(shortcuts_text, title='Shortcuts', border_style='green', box=box.ROUNDED))
    # Right (status panel)
    with status_lock:
        f_uploads = len(pending_failed_uploads)
        last_wake = status['last_wake'] or '-'
        rec_state = 'Recording' if status['recording'] else 'Idle'
        rec_reason = status['recording_reason']
        aw = status['last_audio_webhook']
        tw = status['last_text_webhook']
    def _fmt(res):
        if not res:
            return '-'
        return f"{res.get('time','?')} {'OK' if res.get('success') else 'FAIL'} {res.get('code','')}"
    status_lines = [
        f"State: {rec_state}",
        f"Last wake: {last_wake}",
        f"Failed uploads: {f_uploads}",
        f"Last audio WH: {_fmt(aw)}",
        f"Last text WH: {_fmt(tw)}",
        f"Dev errs: {status.get('device_errors',0)} | Recov: {status.get('device_recoveries',0)}",
        f"Last dev err: {status.get('last_device_error','-') or '-'}",
        f"Input dev: {status.get('input_device','?')}",
        (f"Reason: {rec_reason}" if rec_reason else ''),
    ]
    status_lines = [l for l in status_lines if l]
    layout['right'].update(Panel('\n'.join(status_lines), title='Status', border_style='blue', box=box.ROUNDED))
    return layout

def ui_loop(cfg):
    if not RICH_AVAILABLE:
        return
    try:
        with Live(console=console, refresh_per_second=12, screen=True) as live:
            while not ui_stop_event.is_set():
                layout = _build_layout(cfg)
                live.update(layout)
                time.sleep(0.15)
    except Exception as e:
        _orig_print(f"UI loop error: {e}")


CONFIG_PATH = "config.yaml"

tts_voice_id = None   # Resolved voice id (string) selected at startup
tts_rate = None       # Configured speech rate (int)
recording_active = False  # Global flag to pause generic keyboard handling during active recording
shortcut_abort_requested = False   # Set by global hotkey (if enabled)
shortcut_finalize_requested = False
global_hotkeys_ready = False
manual_record_request = False  # Trigger manual recording (bypass wake word)

# ---------------- Failed upload tracking (for manual retry) ------------- #
pending_failed_uploads = []
pending_failed_uploads_lock = threading.Lock()

def _record_failed_upload(path):
    with pending_failed_uploads_lock:
        if path not in pending_failed_uploads:
            pending_failed_uploads.append(path)
            log(f"📌 Queued for retry: {path}")

def retry_failed_uploads(cfg):
    with pending_failed_uploads_lock:
        targets = list(pending_failed_uploads)
    if not targets:
        log("✅ No failed uploads to retry.")
        return
    log(f"🔁 Retrying {len(targets)} failed upload(s)...")
    for path in targets:
        if not os.path.isfile(path):
            log(f"⚠️  Missing file (skipping): {path}")
            with pending_failed_uploads_lock:
                if path in pending_failed_uploads:
                    pending_failed_uploads.remove(path)
            continue
        ok = send_to_any_webhook(path, cfg)
        if ok:
            play_sound("webhook_success", cfg)
            try:
                os.remove(path)
                log(f"🧹 Deleted local file {path}")
            except Exception as e:
                log(f"⚠️ Could not delete file after retry: {e}")
            with pending_failed_uploads_lock:
                if path in pending_failed_uploads:
                    pending_failed_uploads.remove(path)
        else:
            play_sound("webhook_failure", cfg)
            log(f"⛔ Still failing: {path}")
    with pending_failed_uploads_lock:
        remaining = len(pending_failed_uploads)
    if remaining:
        log(f"❗ {remaining} file(s) remain failed. Press 'r' again to retry later.")
    else:
        log("🎉 All previously failed uploads succeeded or were cleared.")

def init_tts(cfg):
    """Resolve desired voice/rate from config. Each speak spawns a fresh engine thread.

    This avoids long-lived engine state which previously caused one-shot behaviour.
    Restart the program to change voice settings.
    """
    global tts_voice_id, tts_rate
    tts_cfg = cfg.get('tts', {}) or {}
    desired_rate = tts_cfg.get('rate')
    voice_index = tts_cfg.get('voice_index')
    voice_name = tts_cfg.get('voice_name')
    try:
        temp_engine = pyttsx3.init()
        voices = temp_engine.getProperty('voices') or []
        chosen = None
        if voice_name:
            vn = str(voice_name).lower()
            exact = [v for v in voices if getattr(v, 'name', '').lower() == vn]
            subset = [v for v in voices if vn in getattr(v, 'name', '').lower()]
            chosen = (exact or subset or [None])[0]
        if not chosen and voice_index is not None and 0 <= voice_index < len(voices):
            chosen = voices[voice_index]
        if chosen:
            tts_voice_id = chosen.id
            log(f"TTS voice selected: {getattr(chosen,'name','?')}")
        else:
            log("TTS using default voice (no match).")
        if desired_rate is not None:
            try:
                tts_rate = int(desired_rate)
            except Exception:
                log("Invalid rate in config; ignoring.")
    except Exception as e:
        log(f"TTS init (voice discovery) failed: {e}")
    finally:
        try:
            temp_engine.stop()
        except Exception:
            pass
        del temp_engine

def speak_text(text):
    if not text:
        return

    def _speak_blocking(msg):
        if pythoncom:
            try:
                pythoncom.CoInitialize()
            except Exception:
                pass
        try:
            engine = pyttsx3.init()
            if tts_rate is not None:
                try: engine.setProperty('rate', tts_rate)
                except Exception: pass
            if tts_voice_id is not None:
                try: engine.setProperty('voice', tts_voice_id)
                except Exception: pass
            engine.say(msg)
            engine.runAndWait()
        except Exception as e:
            log(f"TTS error: {e}")
        finally:
            try:
                engine.stop()
            except Exception:
                pass
            if pythoncom:
                try: pythoncom.CoUninitialize()
                except Exception: pass

    threading.Thread(target=_speak_blocking, args=(text,), daemon=True).start()


def cleanup_text(original: str) -> str:
        """Prepare text for TTS: remove URLs & noisy symbol clusters while preserving meaning.

        Steps:
            1. Strip markdown links [label](url) -> label
            2. Remove raw http(s)/www URLs
            3. Replace clusters of certain symbols (@ # $ % ^ & * _ + = ~ ` | < > { } [ ]) with a space
            4. Collapse repeated punctuation like '!!!' -> '!'
            5. Collapse whitespace
        Sentence punctuation (.,!?;:) is preserved.
        """
        if not original:
                return ''
        text = original
        # Markdown links
        text = re.sub(r'\[(.*?)\]\((https?://[^)]+)\)', r'\1', text)
        # Raw URLs
        text = re.sub(r'https?://\S+', ' ', text)
        text = re.sub(r'www\.\S+', ' ', text)
        # Symbol clusters (keep normal punctuation)
        text = re.sub(r'[\@\#\$%\^&\*_+=~`|<>\\\{\}\[\]]+', ' ', text)
        # Reduce repeated punctuation (e.g., !!!! -> !, ??? -> ?)
        text = re.sub(r'([!?.])\1{1,}', r'\1', text)
        # Collapse whitespace
        text = re.sub(r'\s+', ' ', text).strip()
        return text


def load_config():
    if not os.path.exists(CONFIG_PATH):
        log("CONFIG ERROR: config.yaml not found.")
        raise SystemExit(1)
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return data


def play_sound(event, cfg):
    audio_cfg = cfg.get("audio_feedback", {})
    if not audio_cfg.get("enabled", True):
        return
    events = audio_cfg.get("events", {})
    path = events.get(event)
    if path and os.path.isfile(path):
        # Play specified wav file asynchronously if possible
        if winsound:
            try:
                winsound.PlaySound(path, winsound.SND_FILENAME | winsound.SND_ASYNC)
                return
            except Exception:
                pass
    # Fallback simple beeps with different frequencies
    if winsound:
        freq_map = {
            "wake_detected": 900,
            "recording_stopped": 600,
            "webhook_success": 1200,
            "webhook_failure": 300,
        }
        freq = freq_map.get(event, 750)
        try:
            winsound.Beep(freq, 180)
        except Exception:
            pass


def write_wave(filename, sample_rate, raw_bytes):
    os.makedirs(os.path.dirname(filename) or ".", exist_ok=True)
    with wave.open(filename, 'wb') as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)  # 16-bit
        wf.setframerate(sample_rate)
        wf.writeframes(raw_bytes)


def amplitude_is_silence(pcm_bytes, threshold):
    # Interpret bytes as sequence of int16 and compute peak amplitude
    frame = struct.unpack("<" + "h" * (len(pcm_bytes) // 2), pcm_bytes)
    peak = max(abs(s) for s in frame) if frame else 0
    return peak < threshold


def send_to_webhook_single(file_path, webhook_cfg, default_field_name="file"):
    url = webhook_cfg.get("url")
    if not url:
        return False, "No URL"
    timeout = webhook_cfg.get("timeout_seconds", 30)
    file_field = webhook_cfg.get("file_field_name", default_field_name)
    extra_fields = webhook_cfg.get("extra_fields", {}) or {}
    debug = bool(webhook_cfg.get("debug", False))
    try:
        with open(file_path, "rb") as f:
            files = {file_field: (os.path.basename(file_path), f, "audio/wav")}
            data = {k: str(v) for k, v in extra_fields.items() if isinstance(v, (str, int, float))}
            r = requests.post(url, files=files, data=data, timeout=timeout)
        ok = (r.status_code == 200)
        with status_lock:
            status['last_audio_webhook'] = {
                'time': time.strftime('%H:%M:%S'),
                'success': ok,
                'code': r.status_code,
            }
        log(f"➡️  Webhook {url} responded {r.status_code}{' (success)' if ok else ''}")
        if debug:
            body = r.text[:400].replace('\n', ' ')
            log(f"🔍 Body: {body}")
        return ok, r.status_code
    except Exception as e:
        with status_lock:
            status['last_audio_webhook'] = {
                'time': time.strftime('%H:%M:%S'),
                'success': False,
                'code': 'ERR'
            }
        log(f"❌ Webhook error for {url}: {e}")
        return False, str(e)


def _retry_params(cfg):
    retry_cfg = cfg.get("webhook_retry", {}) or {}
    max_attempts = int(retry_cfg.get("max_attempts", 1))
    if max_attempts < 1:
        max_attempts = 1
    base_delay = float(retry_cfg.get("base_delay_seconds", 1.0))
    backoff = float(retry_cfg.get("backoff_factor", 2.0))
    max_delay = float(retry_cfg.get("max_delay_seconds", 30.0))
    jitter = bool(retry_cfg.get("jitter", True))
    return {
        "max_attempts": max_attempts,
        "base_delay": base_delay,
        "backoff": backoff,
        "max_delay": max_delay,
        "jitter": jitter,
    }


def send_to_webhook_with_retry(file_path, webhook_cfg, cfg, default_field_name="file"):
    params = _retry_params(cfg)
    attempts = params["max_attempts"]
    for attempt in range(1, attempts + 1):
        ok, info = send_to_webhook_single(file_path, webhook_cfg, default_field_name=default_field_name)
        if ok:
            if attempt > 1:
                log(f"✅ Succeeded after {attempt} attempt(s).")
            return True
        if attempt == attempts:
            log(f"❌ Exhausted {attempts} attempt(s) for webhook {webhook_cfg.get('url')}")
            return False
        # compute delay
        delay = params["base_delay"] * (params["backoff"] ** (attempt - 1))
        delay = min(delay, params["max_delay"])
        if params["jitter"]:
            # add +/- 25% jitter
            jitter_span = delay * 0.25
            delay = delay + random.uniform(-jitter_span, jitter_span)
            delay = max(0.05, delay)
        log(f"⏳ Retry {attempt + 1}/{attempts} for {webhook_cfg.get('url')} in {delay:.2f}s (last error/status: {info})")
        time.sleep(delay)
    return False


def send_to_any_webhook(file_path, cfg):
    # Audio uploads use 'audio_webhooks'
    webhooks_list = cfg.get("audio_webhooks") or []
    if not webhooks_list:
        log("⚠️  No audio webhooks configured (expecting 'audio_webhooks:' list in config.yaml).")
        return False
    log(f"📡 Attempting up to {len(webhooks_list)} audio webhook(s) sequentially...")
    for idx, wh in enumerate(webhooks_list, 1):
        success = send_to_webhook_with_retry(file_path, wh, cfg)
        if success:
            log(f"✅ Audio webhook #{idx} succeeded; stopping attempts.")
            return True
    log(f"↪️  Audio webhook #{idx} failed after retries; trying next...")
    log("❌ All configured audio webhooks failed.")
    return False

def send_text_to_webhooks(text, cfg):
    """Send text JSON to 'text_webhooks'. Success if any returns 200."""
    webhooks_list = cfg.get("text_webhooks") or []
    if not webhooks_list:
        log("⚠️  No text webhooks configured (expecting 'text_webhooks:' list); text not sent.")
        return False
    log(f"📨 Sending text to {len(webhooks_list)} text webhook(s)...")
    any_success = False
    for idx, wh in enumerate(webhooks_list, 1):
        url = wh.get("url")
        if not url:
            log(f"#{idx} missing url; skipping")
            continue
        # Build a lightweight wrapper to reuse retry logic without file
        def single_text_attempt():
            try:
                r = requests.post(url, json={"text": text}, timeout=wh.get("timeout_seconds", 10))
                ok_local = (r.status_code == 200)
                with status_lock:
                    status['last_text_webhook'] = {
                        'time': time.strftime('%H:%M:%S'),
                        'success': ok_local,
                        'code': r.status_code,
                    }
                log(f"➡️  Text webhook #{idx} {url} -> {r.status_code}{' (success)' if ok_local else ''}")
                return ok_local, r.status_code
            except Exception as e:
                with status_lock:
                    status['last_text_webhook'] = {
                        'time': time.strftime('%H:%M:%S'),
                        'success': False,
                        'code': 'ERR'
                    }
                log(f"❌ Text webhook #{idx} error: {e}")
                return False, str(e)

        # Adapt retry loop
        params = _retry_params(cfg)
        attempts = params["max_attempts"]
        for attempt in range(1, attempts + 1):
            ok, info = single_text_attempt()
            if ok:
                any_success = True
                if attempt > 1:
                    log(f"✅ Text webhook succeeded after {attempt} attempt(s).")
                break
            if attempt == attempts:
                log(f"❌ Text webhook #{idx} exhausted {attempts} attempt(s).")
                break
            delay = params["base_delay"] * (params["backoff"] ** (attempt - 1))
            delay = min(delay, params["max_delay"])
            if params["jitter"]:
                jitter_span = delay * 0.25
                delay = delay + random.uniform(-jitter_span, jitter_span)
                delay = max(0.05, delay)
            log(f"⏳ Retry {attempt + 1}/{attempts} for text webhook #{idx} in {delay:.2f}s (last: {info})")
            time.sleep(delay)
        if any_success:
            break
    if not any_success:
        log("❌ No webhook accepted the text (all failed or non-200).")
    return any_success


# ---------------- Global shortcut registration (optional) ------------- #
def _set_flag(kind):
    global shortcut_abort_requested, shortcut_finalize_requested, manual_record_request, recording_active
    if kind == 'start':
        # Only start if not currently recording
        if not recording_active:
            manual_record_request = True
            log("🎙 Manual recording start shortcut pressed.")
        return
    # The remaining flags only matter during an active recording
    if not recording_active:
        return
    if kind == 'abort':
        shortcut_abort_requested = True
        log("🧹 Global abort shortcut pressed.")
    elif kind == 'finalize':
        shortcut_finalize_requested = True
        log("✋ Global finalize shortcut pressed.")


def register_global_shortcuts(cfg):
    sc_cfg = cfg.get("shortcuts", {}) or {}
    if not sc_cfg.get("use_global"):
        return
    if keyboard is None:
        log("⚠️ Global shortcuts requested but 'keyboard' module not available. Install with: pip install keyboard (may require admin).")
        return

    def _normalize(spec):
        if not spec:
            return None
        s = str(spec).strip().lower()
        # ctrl+<letter>
        if s.startswith('ctrl+') and len(s) == 6 and 'a' <= s[-1] <= 'z':
            return s.replace('+', '+')  # keep format
        # single char
        if len(s) == 1:
            return s
        # sequences (multi-char) not supported globally
        log(f"ℹ️ Global shortcut '{spec}' ignored (multi-character sequences not supported globally).")
        return None

    start_spec = _normalize(sc_cfg.get('start_recording'))
    abort_spec = _normalize(sc_cfg.get('abort_recording'))
    finalize_spec = _normalize(sc_cfg.get('finalize_recording'))
    if not any([start_spec, abort_spec, finalize_spec]):
        log("ℹ️ No valid global shortcuts to register.")
        return
    try:
        parts = []
        if start_spec:
            keyboard.add_hotkey(start_spec, lambda: _set_flag('start'))
            parts.append(f"start={start_spec}")
        if abort_spec:
            keyboard.add_hotkey(abort_spec, lambda: _set_flag('abort'))
            parts.append(f"abort={abort_spec}")
        if finalize_spec:
            keyboard.add_hotkey(finalize_spec, lambda: _set_flag('finalize'))
            parts.append(f"finalize={finalize_spec}")
        log("🔗 Registered global shortcuts: " + ", ".join(parts))
        global global_hotkeys_ready
        global_hotkeys_ready = True
    except Exception as e:
        log(f"⚠️ Failed to register global shortcuts: {e}")
        return


def record_audio_after_wake(porcupine, audio_stream, cfg):
    rec_cfg = cfg.get("recording", {})
    silence_threshold = rec_cfg.get("silence_threshold", 500)
    silence_duration = rec_cfg.get("silence_duration_seconds", 10)
    max_record = rec_cfg.get("max_record_seconds", 120)
    output_dir = rec_cfg.get("output_dir", "recordings")

    shortcuts_cfg = cfg.get("shortcuts", {}) or {}
    raw_abort = str(shortcuts_cfg.get("abort_recording", '')).strip()
    raw_finalize = str(shortcuts_cfg.get("finalize_recording", '')).strip()

    use_global = bool(shortcuts_cfg.get("use_global"))

    def _parse_shortcut(spec):
        if not spec:
            return None
        spec_l = spec.lower()
        # Ctrl+<letter>
        if spec_l.startswith('ctrl+') and len(spec_l) == 6:
            letter = spec_l[-1]
            if 'a' <= letter <= 'z':
                # Control char: Ctrl+A => 0x01 ... Ctrl+Z => 0x1A
                ctrl_char = chr(ord(letter) - 96)
                return {"type": "char", "value": ctrl_char, "label": f"Ctrl+{letter.upper()}"}
        # Single character
        if len(spec_l) == 1:
            return {"type": "char", "value": spec_l, "label": spec}
        # Sequence of characters (case-insensitive, no modifiers)
        return {"type": "sequence", "value": spec_l, "label": spec}

    abort_sc = _parse_shortcut(raw_abort)
    finalize_sc = _parse_shortcut(raw_finalize)

    if abort_sc and finalize_sc and abort_sc == finalize_sc:
        log("⚠️  abort_recording and finalize_recording shortcuts are identical; abort will take precedence.")

    start_time = time.time()
    last_sound_time = start_time
    frames = []

    frame_length = porcupine.frame_length
    sample_rate = porcupine.sample_rate
    frame_duration = frame_length / float(sample_rate)

    global recording_active
    recording_active = True
    with status_lock:
        status['recording'] = True
        status['recording_reason'] = 'active'
    keys_info = []
    if abort_sc:
        keys_info.append(f"[{abort_sc['label']}] abort")
    if finalize_sc:
        keys_info.append(f"[{finalize_sc['label']}] finalize/send")
    extra = (" | ".join(keys_info)) if keys_info else ""
    log(f"🎙 Recording (max {max_record}s, stop after {silence_duration}s silence){(' -- ' + extra) if extra else ''}...")

    aborted = False
    # Sequence buffer: list of (char, timestamp)
    seq_buffer = []
    seq_window = 1.0  # seconds to keep recent keystrokes for sequence matching

    # Reset global shortcut flags at start
    global shortcut_abort_requested, shortcut_finalize_requested
    shortcut_abort_requested = False
    shortcut_finalize_requested = False

    while True:
        pcm = audio_stream.read(frame_length, exception_on_overflow=False)
        frames.append(pcm)
        if not amplitude_is_silence(pcm, silence_threshold):
            last_sound_time = time.time()

        # Global shortcut checks (if enabled)
        if use_global:
            if abort_sc and shortcut_abort_requested:
                aborted = True
                reason = "🧹 Aborted by global shortcut"
                shortcut_abort_requested = False
                break
            if finalize_sc and shortcut_finalize_requested:
                reason = "✋ Manual finalize (global)"
                shortcut_finalize_requested = False
                break

        # Check shortcut keys (Windows console)
        if msvcrt and (abort_sc or finalize_sc) and msvcrt.kbhit():
            # Drain all available characters this tick
            while msvcrt.kbhit():
                try:
                    ch_raw = msvcrt.getwch()
                except Exception:
                    break
                if not ch_raw:
                    continue
                ch = ch_raw
                # Normalize: for printable we lower; leave control chars
                if ord(ch) >= 32:
                    ch = ch.lower()
                now = time.time()
                seq_buffer.append((ch, now))
                # Trim old
                seq_buffer = [(c, t) for (c, t) in seq_buffer if now - t <= seq_window]

                def _match(sc):
                    if not sc:
                        return False
                    if sc['type'] == 'char':
                        return ch == sc['value']
                    if sc['type'] == 'sequence':
                        s = ''.join(c for c, _ in seq_buffer)
                        return s.endswith(sc['value'])
                    return False

                if abort_sc and _match(abort_sc):
                    aborted = True
                    reason = "🧹 Aborted by user"
                    seq_buffer.clear()
                    break
                if not aborted and finalize_sc and _match(finalize_sc):
                    reason = "✋ Manual finalize"
                    seq_buffer.clear()
                    break
            if aborted or reason.startswith("✋"):
                break

        elapsed = time.time() - start_time
        silence_elapsed = time.time() - last_sound_time

        if elapsed >= max_record:
            reason = f"⏱ Max length {max_record}s reached"
            break
        if silence_elapsed >= silence_duration and elapsed > 0.5:  # ensure we captured something
            reason = f"🤫 Silence {silence_duration}s"
            break

    log(f"🛑 Recording stopped: {reason}")
    play_sound("recording_stopped", cfg)
    recording_active = False
    with status_lock:
        status['recording'] = False
        status['recording_reason'] = reason

    if aborted:
        log("🚫 Recording discarded (no file saved / no upload).")
        return None, True

    # If we stopped because of silence, trim the trailing silence_duration seconds
    if "Silence" in reason:
        frame_length = porcupine.frame_length
        frame_duration = frame_length / float(sample_rate)
        frames_to_trim = int(silence_duration / frame_duration)
        if frames_to_trim > 0 and len(frames) > frames_to_trim + 5:  # keep at least a few frames
            original_count = len(frames)
            frames = frames[:-frames_to_trim]
            trimmed_seconds = frames_to_trim * frame_duration
            log(f"✂️  Trimmed trailing ~{trimmed_seconds:.2f}s silence (removed {frames_to_trim} frames of {original_count}).")
        else:
            log("✂️  Skipped trimming (recording too short to safely trim).")

    # Build file name with timestamp
    ts = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    filename = os.path.join(output_dir, f"recording_{ts}.wav")
    raw_bytes = b"".join(frames)
    write_wave(filename, sample_rate, raw_bytes)
    size_kb = len(raw_bytes) / 1024
    log(f"💾 Saved {filename} ({size_kb:.1f} KB)")
    return filename, False


def listen_loop(cfg):
    access_key = cfg.get("access_key")
    wake_path = cfg.get("wakeword_path")
    model_path = cfg.get("model_path")
    if not all([access_key, wake_path, model_path]):
        log("CONFIG ERROR: access_key, wakeword_path, model_path must be set in config.yaml")
        return
    # Friendly validation for common misconfigurations
    if isinstance(access_key, str) and access_key.startswith("YOUR_"):
        log("CONFIG ERROR: Replace placeholder access_key in config.yaml with your real Picovoice Access Key from console.picovoice.ai")
        return
    if not os.path.isfile(wake_path):
        log(f"CONFIG ERROR: wakeword_path file not found: {wake_path}")
        return
    if not os.path.isfile(model_path):
        log(f"CONFIG ERROR: model_path file not found: {model_path}")
        return

    porcupine = pvporcupine.create(access_key=access_key, keyword_paths=[wake_path], model_path=model_path)
    pa = pyaudio.PyAudio()
    # Try to capture selected input device index/name for status
    try:
        default_index = pa.get_default_input_device_info().get('index')
        dev_info = pa.get_device_info_by_index(default_index)
        with status_lock:
            status['input_device'] = dev_info.get('name')
    except Exception:
        with status_lock:
            status['input_device'] = 'Unknown'
    audio_stream = pa.open(
        rate=porcupine.sample_rate,
        channels=1,
        format=pyaudio.paInt16,
        input=True,
        frames_per_buffer=porcupine.frame_length,
    )
    keyword_name = os.path.splitext(os.path.basename(wake_path))[0]
    log(f"🎤 Listening for wake word '{keyword_name}' ... Press Ctrl+C to exit.")

    global manual_record_request

    try:
        while True:
            if manual_record_request:
                manual_record_request = False
                # Provide the same audible cue as a wake detection
                play_sound("wake_detected", cfg)
                with status_lock:
                    status['last_wake'] = time.strftime('%H:%M:%S')
                    status['manual_start_count'] += 1
                audio_file, aborted = record_audio_after_wake(porcupine, audio_stream, cfg)
                if not aborted and audio_file:
                    def uploader(path):
                        ok = send_to_any_webhook(path, cfg)
                        if ok:
                            play_sound("webhook_success", cfg)
                            try:
                                os.remove(path)
                                log(f"🧹 Deleted local file {path}")
                            except Exception as e:
                                log(f"⚠️ Could not delete file: {e}")
                        else:
                            play_sound("webhook_failure", cfg)
                            _record_failed_upload(path)
                    threading.Thread(target=uploader, args=(audio_file,), daemon=True).start()
                continue

            try:
                pcm = audio_stream.read(porcupine.frame_length, exception_on_overflow=False)
            except Exception as e:
                # Mark device error and attempt simple reopen loop
                with status_lock:
                    status['device_errors'] += 1
                    status['last_device_error'] = time.strftime('%H:%M:%S')
                log(f"🎧 Device read error: {e}. Attempting reopen...")
                recovered = False
                for _ in range(5):
                    try:
                        time.sleep(0.5)
                        audio_stream.close()
                    except Exception:
                        pass
                    try:
                        audio_stream = pa.open(
                            rate=porcupine.sample_rate,
                            channels=1,
                            format=pyaudio.paInt16,
                            input=True,
                            frames_per_buffer=porcupine.frame_length,
                        )
                        with status_lock:
                            status['device_recoveries'] += 1
                        log("🎧 Device stream recovered.")
                        recovered = True
                        break
                    except Exception:
                        continue
                if not recovered:
                    log("❌ Unable to recover audio device; will retry on next loop.")
                continue
            pcm_unpacked = struct.unpack_from("h" * porcupine.frame_length, pcm)
            result = porcupine.process(pcm_unpacked)
            if result >= 0:
                log(f"🔑 Wake word '{keyword_name}' detected!")
                play_sound("wake_detected", cfg)
                with status_lock:
                    status['last_wake'] = time.strftime('%H:%M:%S')
                audio_file, aborted = record_audio_after_wake(porcupine, audio_stream, cfg)
                if aborted or not audio_file:
                    continue

                def uploader(path):
                    ok = send_to_any_webhook(path, cfg)
                    if ok:
                        play_sound("webhook_success", cfg)
                        try:
                            os.remove(path)
                            log(f"🧹 Deleted local file {path}")
                        except Exception as e:
                            log(f"⚠️ Could not delete file: {e}")
                    else:
                        play_sound("webhook_failure", cfg)
                        _record_failed_upload(path)

                threading.Thread(target=uploader, args=(audio_file,), daemon=True).start()
    except KeyboardInterrupt:
        log("👋 Exiting.")
    finally:
        audio_stream.close()
        pa.terminate()
        porcupine.delete()



def keyboard_loop(cfg):
    if msvcrt is None:
        log("Keyboard interaction not available on this platform.")
        return
    log("⌨️  Keyboard controls active (non-blocking mode). 'q'=compose send+tts, 'v'=compose tts-only, Enter=commit, Esc=cancel, r=retry uploads, x=exit notice")
    global command_mode, command_buffer
    while True:
        if msvcrt.kbhit():
            ch = msvcrt.getwch()
            if ch == '\r':  # Enter
                with command_lock:
                    mode = command_mode
                    text = ''.join(command_buffer).strip()
                    command_buffer.clear()
                    command_mode = None
                if mode and text:
                    if mode == 'send_text':
                        log(f"🔤 Speaking & sending: {text}")
                        speak_text(text)
                        send_text_to_webhooks(text, cfg)
                    elif mode == 'speak_only':
                        log(f"🔊 Speaking (local only): {text}")
                        speak_text(text)
            elif ch in ('\x1b',):  # ESC
                with command_lock:
                    command_buffer.clear()
                    command_mode = None
                log("↩️  Input canceled")
            elif ch and len(ch) == 1:
                lower = ch.lower()
                with command_lock:
                    if command_mode:
                        if ch in ('\b', '\x08'):
                            if command_buffer:
                                command_buffer.pop()
                        elif 32 <= ord(ch) < 127:
                            command_buffer.append(ch)
                    else:
                        if lower == 'q':
                            command_mode = 'send_text'
                            command_buffer = []
                        elif lower == 'v':
                            command_mode = 'speak_only'
                            command_buffer = []
                        elif lower == 'r':
                            threading.Thread(target=retry_failed_uploads, args=(cfg,), daemon=True).start()
                        elif lower == 'x':
                            log("Exiting requested by user (x key). Press Ctrl+C to stop main loop.")
            # else ignore other scan codes (function keys etc.)
        time.sleep(0.05)


# ---------------- Flask webhook (incoming text) ------------- #
app = Flask(__name__)


def start_webhook_listener(cfg):
    listener_cfg = cfg.get("webhook_listener", {})
    host = listener_cfg.get("host", "0.0.0.0")
    port = listener_cfg.get("port", 5000)
    endpoint = listener_cfg.get("endpoint", "/response")

    @app.route(endpoint, methods=["POST"])
    def handle_response():
        data = request.json
        if not data or "text" not in data:
            return jsonify({"error": "Invalid payload, 'text' field is required."}), 400
        text = data.get("text", "")
        # Normalize to string and strip
        if not isinstance(text, str):
            try:
                text = str(text)
            except Exception:
                text = ""
        if not text.strip():
            return jsonify({"status": "ignored", "reason": "blank text"}), 204
        original_text = text
        cleaned = cleanup_text(original_text)
        log(f"📥 Received text: {original_text}")
        if cleaned:
            speak_text(cleaned)
        return jsonify({"status": "success", "message": "Spoken"}), 200

    # Suppress Flask default banner/log noise when using Rich full-screen UI
    logging.getLogger('werkzeug').setLevel(logging.ERROR)
    os.environ['WERKZEUG_RUN_MAIN'] = 'true'
    threading.Thread(target=lambda: app.run(host=host, port=port, debug=False, use_reloader=False), daemon=True).start()


def main():
    cfg = load_config()
    init_tts(cfg)
    register_global_shortcuts(cfg)
    start_webhook_listener(cfg)
    if RICH_AVAILABLE:
        threading.Thread(target=ui_loop, args=(cfg,), daemon=True).start()
    if msvcrt:
        threading.Thread(target=keyboard_loop, args=(cfg,), daemon=True).start()
    listen_loop(cfg)
    # Per-call TTS threads will exit naturally; nothing to clean.


if __name__ == "__main__":
    main()
