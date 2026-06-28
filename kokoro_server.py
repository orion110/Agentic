#!/usr/bin/env python3
"""
Orion Kokoro TTS Server
Wraps Kokoro ONNX to serve audio over HTTP for Orion voice mode.

Usage:
    python3 kokoro_server.py

Requires:
    pip install flask kokoro-onnx sounddevice numpy --break-system-packages
    Download kokoro-v1.0.onnx and voices-v1.0.bin to ~/VEO-x/
"""

import os
import io
import threading
import numpy as np
from flask import Flask, request, jsonify, send_file
from flask_cors import CORS

SETTINGS_DIR = os.path.expanduser("~/VEO-x")
ONNX_MODEL   = os.path.join(SETTINGS_DIR, "kokoro-v1.0.onnx")
VOICES_FILE  = os.path.join(SETTINGS_DIR, "voices-v1.0.bin")

app = Flask(__name__)
CORS(app)  # allow requests from localhost:8080

_kokoro = None
_lock = threading.Lock()

def get_kokoro():
    global _kokoro
    with _lock:
        if _kokoro is None:
            from kokoro_onnx import Kokoro
            _kokoro = Kokoro(ONNX_MODEL, VOICES_FILE)
            print("[Orion TTS] Kokoro loaded.")
        return _kokoro

def samples_to_wav_bytes(samples, sample_rate):
    """Convert float32 numpy array to WAV bytes."""
    import wave, struct
    samples_int16 = np.clip(samples, -1.0, 1.0)
    samples_int16 = (samples_int16 * 32767).astype(np.int16)
    buf = io.BytesIO()
    with wave.open(buf, 'wb') as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(samples_int16.tobytes())
    buf.seek(0)
    return buf

@app.route('/speak', methods=['POST'])
def speak():
    data = request.get_json()
    if not data or 'text' not in data:
        return jsonify({'error': 'Missing text'}), 400

    text = data['text'].strip()
    if not text:
        return jsonify({'error': 'Empty text'}), 400

    voice = data.get('voice', 'af_heart')
    speed = float(data.get('speed', 0.9))

    # Truncate to 500 chars to avoid slow generation
    if len(text) > 500:
        text = text[:500].rsplit(' ', 1)[0] + '...'

    try:
        kokoro = get_kokoro()
        samples, sr = kokoro.create(text, voice=voice, speed=speed, lang="en-us")
        wav_buf = samples_to_wav_bytes(samples, sr)
        return send_file(wav_buf, mimetype='audio/wav', as_attachment=False)
    except Exception as e:
        print(f"[Orion TTS] Error: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/health', methods=['GET'])
def health():
    return jsonify({'status': 'ok', 'model': ONNX_MODEL})

@app.route('/voices', methods=['GET'])
def voices():
    return jsonify({'voices': [
        'af_heart', 'af_sky', 'af_bella', 'af_sarah',
        'am_adam', 'am_michael', 'bf_emma', 'bm_george'
    ]})

if __name__ == '__main__':
    # Pre-check model files
    if not os.path.exists(ONNX_MODEL):
        print(f"[ERROR] kokoro-v1.0.onnx not found at {ONNX_MODEL}")
        print(f"Download from: https://github.com/thewh1teagle/kokoro-onnx/releases")
        exit(1)
    if not os.path.exists(VOICES_FILE):
        print(f"[ERROR] voices-v1.0.bin not found at {VOICES_FILE}")
        exit(1)

    print("[Orion TTS] Starting Kokoro server on http://localhost:5050")
    print("[Orion TTS] Pre-loading model...")
    try:
        get_kokoro()
    except Exception as e:
        print(f"[Orion TTS] Failed to load Kokoro: {e}")
        exit(1)

    app.run(host='127.0.0.1', port=5050, debug=False)
