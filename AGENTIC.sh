#!/bin/bash
# Orion Launch Script
# Starts Ollama, Kokoro TTS, Whisper STT, and serves the UI

DIR="/home/user/apps/BackTrace"
LOG_DIR="$DIR/logs"
mkdir -p "$LOG_DIR"

echo "🚀 Starting BackTrace..."

# 1. Ollama
if ! pgrep -x "ollama" > /dev/null; then
  echo "▶ Starting Ollama..."
  ollama serve > "$LOG_DIR/ollama.log" 2>&1 &
  sleep 2
else
  echo "✓ Ollama already running"
fi

# 2. Kokoro TTS
if ! lsof -i:5050 > /dev/null 2>&1; then
  echo "▶ Starting Kokoro TTS server..."
  python3 "$DIR/kokoro_server.py" > "$LOG_DIR/kokoro.log" 2>&1 &
  sleep 3
else
  echo "✓ Kokoro already running on :5050"
fi

# 3. Whisper STT
if ! lsof -i:5051 > /dev/null 2>&1; then
  echo "▶ Starting Whisper STT server..."
  python3 "$DIR/whisper_server.py" > "$LOG_DIR/whisper.log" 2>&1 &
  sleep 3
else
  echo "✓ Whisper already running on :5051"
fi

# 4. Web server
if ! lsof -i:8080 > /dev/null 2>&1; then
  echo "▶ Starting web server..."
  cd "$DIR" && python3 -m http.server 8080 > "$LOG_DIR/web.log" 2>&1 &
else
  echo "✓ Web server already running on :8080"
fi

echo ""
echo "✅ BackTrace is running!"
echo "   Open: http://localhost:8080/BackTrace.html"
echo ""
echo "Logs: $LOG_DIR"
echo "To stop all: bash BT_stop.sh"
