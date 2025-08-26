# Wake Word Recorder & Uploader

Continuously listens for a custom Porcupine wake word (e.g. "Alfredo"). After
the wake word is detected it records microphone audio until either:

1. Continuous silence lasts a configured number of seconds, OR
2. A configured maximum recording length is reached.

The captured audio is written to a WAV file and uploaded (multipart/form-data)
to the first successful endpoint in the `audio_webhooks` list. If an upload
returns HTTP 200 the local file is deleted; otherwise it is retained and can be
retried (key `r`). Distinct audio cues (beeps or user-provided WAV files)
indicate wake detection, recording stop, upload success, and upload failure.

You can also manually enter text (`q`) which is spoken locally and POSTed as
JSON `{"text": "..."}` to the first responding `text_webhooks` endpoint.
Inbound processed text (e.g. from an external NLP pipeline) can be sent back to
the embedded Flask listener and will be spoken if it is non‑blank.

## Features

- Offline Porcupine wake word detection (low latency)
- Silence-based endpointing with configurable threshold & window
- Max duration cap as safety limit
- Sequential audio webhook failover (`audio_webhooks`) – stops on first 200
- Separate text webhook list (`text_webhooks`) for manual commands
- Background audio upload & deletion on success
- Retry failed audio uploads (`r` key)
- Inbound Flask endpoint to speak returned text (ignores blank payloads)
- Simple per-utterance TTS threads (pyttsx3) (best-effort concurrency)
- Custom or fallback beep event sounds

## Requirements

Python 3.8+ (tested on Windows). Install dependencies:

```powershell
pip install -r requirements.txt
```

You also need:

- A Picovoice (Porcupine) Access Key from https://console.picovoice.ai/
- Your trained keyword file (`*.ppn`) and matching model parameters file (e.g. `porcupine_params_pt.pv`)

## Configuration (`config.yaml`)

Current structure (abridged example):

```yaml
access_key: "YOUR_PICOVOICE_ACCESS_KEY"
wakeword_path: "Alfredo_pt_windows_v3_0_0.ppn"
model_path: "porcupine_params_pt.pv"

recording:
  silence_threshold: 500          # int16 peak amplitude below => silence
  silence_duration_seconds: 5     # continuous silence to stop
  max_record_seconds: 120         # hard cap
  output_dir: "recordings"        # temp storage before upload deletion

# Audio upload targets (tried in order until one returns 200)
audio_webhooks:
  - url: "https://primary.example.com/webhook/audio"
    timeout_seconds: 10
    file_field_name: "audio_file"
  - url: "https://fallback.example.com/webhook/audio"
    timeout_seconds: 30
    file_field_name: "audio_file"

# Text webhook targets (manual 'q' JSON posts); first 200 wins
text_webhooks:
  - url: "https://primary.example.com/webhook/text"
    timeout_seconds: 10
  - url: "https://fallback.example.com/webhook/text"
    timeout_seconds: 30

audio_feedback:
  enabled: true
  events:
    wake_detected: null
    recording_stopped: null
    webhook_success: null
    webhook_failure: null

tts:
  rate: 250
  voice_name: "Microsoft Maria Desktop - Portuguese(Brazil)"
  voice_index: 1
  debug: true

webhook_listener:
  host: 0.0.0.0
  port: 5000
  endpoint: /response  # POST {"text": "..."}
```

### Key Parameters

- `silence_threshold`: Tune based on your microphone noise floor. Typical values 300–1200.
- `silence_duration_seconds`: Increase if you speak with long pauses.
- `max_record_seconds`: Safety upper bound (e.g., 120s).
- `output_dir`: Temporary storage before (possible) deletion.
- `audio_feedback.events.*`: Provide WAV file paths for custom sounds; leave `null` for built-in beeps.

## Running

1. Edit `config.yaml` with your access key and file paths.
2. Run the listener:

```powershell
python main.py
```

3. Speak the wake word. After the beep, talk. Stop speaking; when silence limit is hit or max time reached, another beep plays. Upload occurs in background.

4. On a successful webhook (HTTP 200) you hear the success beep and the local file is deleted. On failure the file remains for inspection/retry.

### Keyboard Commands (Windows console)

When the script is running:

- `q` : Prompt for text, speak it AND send JSON `{text: ...}` to first successful `text_webhooks` endpoint
- `v` : Prompt for text, speak locally only (no outbound send)
- `r` : Retry any previously failed audio uploads
- `x` : Print exit notice (Ctrl+C actually stops program)

### Inbound Text → Speech

The embedded Flask server listens on `/response` (configurable). POST JSON:

```json
{"text": "Olá mundo"}
```

It is queued for speech immediately without blocking the wake loop.

### Webhook Failover

`audio_webhooks`: Tried sequentially until one returns HTTP 200 (otherwise file kept for retry).

`text_webhooks`: Tried sequentially for manual text; stops on first HTTP 200. (Blank or whitespace text is ignored before speaking.)

### TTS Notes

Each utterance spawns a short-lived pyttsx3 engine thread. Rapid bursts of
messages from the inbound webhook may overlap and occasionally produce
`TTS error: run loop already started`; they are logged and skipped. If you need
guaranteed ordering without overlap, implement a single-thread queue.

## Webhook Contracts

Audio: `multipart/form-data` with field name from `file_field_name` (default
`audio_file`) containing WAV bytes. HTTP 200 => success (delete local file).

Text (manual): JSON `{"text": "..."}`; first 200 halts further attempts.

Inbound (listener): POST JSON `{"text": "..."}` to `/response`; blank or missing
`text` is ignored (204). Non-blank is spoken.

## Custom Sounds

Provide short `.wav` files (mono or stereo) for any event. Update the corresponding path in `config.yaml`. If a file is missing or cannot be played a fallback Beep is used (Windows `winsound`).

## Notes / Troubleshooting

- If you get `OSError: [Errno -9996] Invalid input device`, specify the correct input device index in the code (can be added if needed) using PyAudio's device enumeration.
- Adjust `silence_threshold` if recordings end too early or too late.
- The previous `soundfile` int64 dtype issue is avoided by writing raw 16-bit PCM via the `wave` module.
- If you need to change the wake keyword/model at runtime you must restart the script (Porcupine context is created once).

## Possible Enhancements

- Single-threaded TTS queue for strict ordering
- Structured logging / JSON logs
- Automatic exponential backoff for upload failures
- Optional VAD library integration

## License

Internal / Personal Project (add your desired license terms here).
