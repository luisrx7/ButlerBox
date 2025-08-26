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
from datetime import datetime, UTC
from flask import Flask, request, jsonify
import pyttsx3
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
            print(f"📌 Queued for retry: {path}")

def retry_failed_uploads(cfg):
    with pending_failed_uploads_lock:
        targets = list(pending_failed_uploads)
    if not targets:
        print("✅ No failed uploads to retry.")
        return
    print(f"🔁 Retrying {len(targets)} failed upload(s)...")
    for path in targets:
        if not os.path.isfile(path):
            print(f"⚠️  Missing file (skipping): {path}")
            with pending_failed_uploads_lock:
                if path in pending_failed_uploads:
                    pending_failed_uploads.remove(path)
            continue
        ok = send_to_any_webhook(path, cfg)
        if ok:
            play_sound("webhook_success", cfg)
            try:
                os.remove(path)
                print(f"🧹 Deleted local file {path}")
            except Exception as e:
                print(f"⚠️ Could not delete file after retry: {e}")
            with pending_failed_uploads_lock:
                if path in pending_failed_uploads:
                    pending_failed_uploads.remove(path)
        else:
            play_sound("webhook_failure", cfg)
            print(f"⛔ Still failing: {path}")
    with pending_failed_uploads_lock:
        remaining = len(pending_failed_uploads)
    if remaining:
        print(f"❗ {remaining} file(s) remain failed. Press 'r' again to retry later.")
    else:
        print("🎉 All previously failed uploads succeeded or were cleared.")

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
            print(f"TTS voice selected: {getattr(chosen,'name','?')}")
        else:
            print("TTS using default voice (no match).")
        if desired_rate is not None:
            try:
                tts_rate = int(desired_rate)
            except Exception:
                print("Invalid rate in config; ignoring.")
    except Exception as e:
        print(f"TTS init (voice discovery) failed: {e}")
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
            print(f"TTS error: {e}")
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
        print("CONFIG ERROR: config.yaml not found.")
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
        print(f"➡️  Webhook {url} responded {r.status_code}{' (success)' if ok else ''}")
        if debug:
            body = r.text[:400].replace('\n', ' ')
            print(f"🔍 Body: {body}")
        return ok, r.status_code
    except Exception as e:
        print(f"❌ Webhook error for {url}: {e}")
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
                print(f"✅ Succeeded after {attempt} attempt(s).")
            return True
        if attempt == attempts:
            print(f"❌ Exhausted {attempts} attempt(s) for webhook {webhook_cfg.get('url')}")
            return False
        # compute delay
        delay = params["base_delay"] * (params["backoff"] ** (attempt - 1))
        delay = min(delay, params["max_delay"])
        if params["jitter"]:
            # add +/- 25% jitter
            jitter_span = delay * 0.25
            delay = delay + random.uniform(-jitter_span, jitter_span)
            delay = max(0.05, delay)
        print(f"⏳ Retry {attempt + 1}/{attempts} for {webhook_cfg.get('url')} in {delay:.2f}s (last error/status: {info})")
        time.sleep(delay)
    return False


def send_to_any_webhook(file_path, cfg):
    # Audio uploads use 'audio_webhooks'
    webhooks_list = cfg.get("audio_webhooks") or []
    if not webhooks_list:
        print("⚠️  No audio webhooks configured (expecting 'audio_webhooks:' list in config.yaml).")
        return False
    print(f"📡 Attempting up to {len(webhooks_list)} audio webhook(s) sequentially...")
    for idx, wh in enumerate(webhooks_list, 1):
        success = send_to_webhook_with_retry(file_path, wh, cfg)
        if success:
            print(f"✅ Audio webhook #{idx} succeeded; stopping attempts.")
            return True
        print(f"↪️  Audio webhook #{idx} failed after retries; trying next...")
    print("❌ All configured audio webhooks failed.")
    return False

def send_text_to_webhooks(text, cfg):
    """Send text JSON to 'text_webhooks'. Success if any returns 200."""
    webhooks_list = cfg.get("text_webhooks") or []
    if not webhooks_list:
        print("⚠️  No text webhooks configured (expecting 'text_webhooks:' list); text not sent.")
        return False
    print(f"📨 Sending text to {len(webhooks_list)} text webhook(s)...")
    any_success = False
    for idx, wh in enumerate(webhooks_list, 1):
        url = wh.get("url")
        if not url:
            print(f"#{idx} missing url; skipping")
            continue
        # Build a lightweight wrapper to reuse retry logic without file
        def single_text_attempt():
            try:
                r = requests.post(url, json={"text": text}, timeout=wh.get("timeout_seconds", 10))
                ok_local = (r.status_code == 200)
                print(f"➡️  Text webhook #{idx} {url} -> {r.status_code}{' (success)' if ok_local else ''}")
                return ok_local, r.status_code
            except Exception as e:
                print(f"❌ Text webhook #{idx} error: {e}")
                return False, str(e)

        # Adapt retry loop
        params = _retry_params(cfg)
        attempts = params["max_attempts"]
        for attempt in range(1, attempts + 1):
            ok, info = single_text_attempt()
            if ok:
                any_success = True
                if attempt > 1:
                    print(f"✅ Text webhook succeeded after {attempt} attempt(s).")
                break
            if attempt == attempts:
                print(f"❌ Text webhook #{idx} exhausted {attempts} attempt(s).")
                break
            delay = params["base_delay"] * (params["backoff"] ** (attempt - 1))
            delay = min(delay, params["max_delay"])
            if params["jitter"]:
                jitter_span = delay * 0.25
                delay = delay + random.uniform(-jitter_span, jitter_span)
                delay = max(0.05, delay)
            print(f"⏳ Retry {attempt + 1}/{attempts} for text webhook #{idx} in {delay:.2f}s (last: {info})")
            time.sleep(delay)
        if any_success:
            break
    if not any_success:
        print("❌ No webhook accepted the text (all failed or non-200).")
    return any_success


# ---------------- Global shortcut registration (optional) ------------- #
def _set_flag(kind):
    global shortcut_abort_requested, shortcut_finalize_requested, manual_record_request, recording_active
    if kind == 'start':
        # Only start if not currently recording
        if not recording_active:
            manual_record_request = True
            print("🎙 Manual recording start shortcut pressed.")
        return
    # The remaining flags only matter during an active recording
    if not recording_active:
        return
    if kind == 'abort':
        shortcut_abort_requested = True
        print("🧹 Global abort shortcut pressed.")
    elif kind == 'finalize':
        shortcut_finalize_requested = True
        print("✋ Global finalize shortcut pressed.")


def register_global_shortcuts(cfg):
    sc_cfg = cfg.get("shortcuts", {}) or {}
    if not sc_cfg.get("use_global"):
        return
    if keyboard is None:
        print("⚠️ Global shortcuts requested but 'keyboard' module not available. Install with: pip install keyboard (may require admin).")
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
        print(f"ℹ️ Global shortcut '{spec}' ignored (multi-character sequences not supported globally).")
        return None

    start_spec = _normalize(sc_cfg.get('start_recording'))
    abort_spec = _normalize(sc_cfg.get('abort_recording'))
    finalize_spec = _normalize(sc_cfg.get('finalize_recording'))
    if not any([start_spec, abort_spec, finalize_spec]):
        print("ℹ️ No valid global shortcuts to register.")
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
        print("🔗 Registered global shortcuts: " + ", ".join(parts))
        global global_hotkeys_ready
        global_hotkeys_ready = True
    except Exception as e:
        print(f"⚠️ Failed to register global shortcuts: {e}")
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
        print("⚠️  abort_recording and finalize_recording shortcuts are identical; abort will take precedence.")

    start_time = time.time()
    last_sound_time = start_time
    frames = []

    frame_length = porcupine.frame_length
    sample_rate = porcupine.sample_rate
    frame_duration = frame_length / float(sample_rate)

    global recording_active
    recording_active = True
    keys_info = []
    if abort_sc:
        keys_info.append(f"[{abort_sc['label']}] abort")
    if finalize_sc:
        keys_info.append(f"[{finalize_sc['label']}] finalize/send")
    extra = (" | ".join(keys_info)) if keys_info else ""
    print(f"🎙 Recording (max {max_record}s, stop after {silence_duration}s silence){(' -- ' + extra) if extra else ''}...")

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

    print(f"🛑 Recording stopped: {reason}")
    play_sound("recording_stopped", cfg)
    recording_active = False

    if aborted:
        print("🚫 Recording discarded (no file saved / no upload).")
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
            print(f"✂️  Trimmed trailing ~{trimmed_seconds:.2f}s silence (removed {frames_to_trim} frames of {original_count}).")
        else:
            print("✂️  Skipped trimming (recording too short to safely trim).")

    # Build file name with timestamp
    ts = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    filename = os.path.join(output_dir, f"recording_{ts}.wav")
    raw_bytes = b"".join(frames)
    write_wave(filename, sample_rate, raw_bytes)
    size_kb = len(raw_bytes) / 1024
    print(f"💾 Saved {filename} ({size_kb:.1f} KB)")
    return filename, False


def listen_loop(cfg):
    access_key = cfg.get("access_key")
    wake_path = cfg.get("wakeword_path")
    model_path = cfg.get("model_path")
    if not all([access_key, wake_path, model_path]):
        print("CONFIG ERROR: access_key, wakeword_path, model_path must be set in config.yaml")
        return
    # Friendly validation for common misconfigurations
    if isinstance(access_key, str) and access_key.startswith("YOUR_"):
        print("CONFIG ERROR: Replace placeholder access_key in config.yaml with your real Picovoice Access Key from console.picovoice.ai")
        return
    if not os.path.isfile(wake_path):
        print(f"CONFIG ERROR: wakeword_path file not found: {wake_path}")
        return
    if not os.path.isfile(model_path):
        print(f"CONFIG ERROR: model_path file not found: {model_path}")
        return

    porcupine = pvporcupine.create(access_key=access_key, keyword_paths=[wake_path], model_path=model_path)
    pa = pyaudio.PyAudio()
    audio_stream = pa.open(
        rate=porcupine.sample_rate,
        channels=1,
        format=pyaudio.paInt16,
        input=True,
        frames_per_buffer=porcupine.frame_length,
    )
    keyword_name = os.path.splitext(os.path.basename(wake_path))[0]
    print(f"🎤 Listening for wake word '{keyword_name}' ... Press Ctrl+C to exit.")

    global manual_record_request

    try:
        while True:
            if manual_record_request:
                manual_record_request = False
                # Provide the same audible cue as a wake detection
                play_sound("wake_detected", cfg)
                audio_file, aborted = record_audio_after_wake(porcupine, audio_stream, cfg)
                if not aborted and audio_file:
                    def uploader(path):
                        ok = send_to_any_webhook(path, cfg)
                        if ok:
                            play_sound("webhook_success", cfg)
                            try:
                                os.remove(path)
                                print(f"🧹 Deleted local file {path}")
                            except Exception as e:
                                print(f"⚠️ Could not delete file: {e}")
                        else:
                            play_sound("webhook_failure", cfg)
                            _record_failed_upload(path)
                    threading.Thread(target=uploader, args=(audio_file,), daemon=True).start()
                continue

            pcm = audio_stream.read(porcupine.frame_length, exception_on_overflow=False)
            pcm_unpacked = struct.unpack_from("h" * porcupine.frame_length, pcm)
            result = porcupine.process(pcm_unpacked)
            if result >= 0:
                print(f"🔑 Wake word '{keyword_name}' detected!")
                play_sound("wake_detected", cfg)
                audio_file, aborted = record_audio_after_wake(porcupine, audio_stream, cfg)
                if aborted or not audio_file:
                    continue

                def uploader(path):
                    ok = send_to_any_webhook(path, cfg)
                    if ok:
                        play_sound("webhook_success", cfg)
                        try:
                            os.remove(path)
                            print(f"🧹 Deleted local file {path}")
                        except Exception as e:
                            print(f"⚠️ Could not delete file: {e}")
                    else:
                        play_sound("webhook_failure", cfg)
                        _record_failed_upload(path)

                threading.Thread(target=uploader, args=(audio_file,), daemon=True).start()
    except KeyboardInterrupt:
        print("👋 Exiting.")
    finally:
        audio_stream.close()
        pa.terminate()
        porcupine.delete()



def keyboard_loop(cfg):
    if msvcrt is None:
        print("Keyboard interaction not available on this platform.")
        return
    print("⌨️  Keyboard controls: [q]=speak & send text, [v]=speak text only, [r]=retry failed uploads, [x]=exit notice")
    while True:
        # Skip general key handling during active recording so record-time shortcuts work reliably
        if 'recording_active' in globals() and recording_active:
            time.sleep(0.05)
            continue
        if msvcrt.kbhit():
            ch = msvcrt.getwch()
            if not ch:
                continue
            ch = ch.lower()
            if ch == 'q':
                print("Enter text (blank to cancel): ", end='', flush=True)
                user_text = input().strip()
                if user_text:
                    print(f"🔤 Speaking & sending: {user_text}")
                    speak_text(user_text)
                    send_text_to_webhooks(user_text, cfg)
            elif ch == 'v':
                print("Enter text to speak only (blank to cancel): ", end='', flush=True)
                user_text = input().strip()
                if user_text:
                    speak_text(user_text)
                    print(f"🔊 Speaking (local only): {user_text}")
            elif ch == 'r':
                threading.Thread(target=retry_failed_uploads, args=(cfg,), daemon=True).start()
            elif ch == 'x':
                print("Exiting requested by user (x key). Press Ctrl+C to stop main loop.")
            # ignore others
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
        print(f"📥 Received text: {original_text}")
        if cleaned:
            speak_text(cleaned)
        return jsonify({"status": "success", "message": "Spoken"}), 200

    threading.Thread(target=lambda: app.run(host=host, port=port, debug=False, use_reloader=False), daemon=True).start()


def main():
    cfg = load_config()
    init_tts(cfg)
    register_global_shortcuts(cfg)
    start_webhook_listener(cfg)
    if msvcrt:
        threading.Thread(target=keyboard_loop, args=(cfg,), daemon=True).start()
    listen_loop(cfg)
    # Per-call TTS threads will exit naturally; nothing to clean.


if __name__ == "__main__":
    main()
