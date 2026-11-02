#!/usr/bin/env bash
set -euo pipefail

mkdir -p /data/cache/articles /data/cache/images /data/login-nonces

DESKTOP_PIDS=()

if [[ -n "${BROWSER_CDP_URL:-}" ]]; then
  echo "Reusing external Chromium and noVNC at ${BROWSER_CDP_URL}"
else
  Xvfb :99 \
    -screen 0 1280x800x24 \
    -ac \
    +extension GLX \
    +render \
    -noreset \
    >/tmp/xvfb.log 2>&1 &
  XVFB_PID=$!
  DESKTOP_PIDS+=("$XVFB_PID")

  for _ in $(seq 1 40); do
    if xdpyinfo -display :99 >/dev/null 2>&1; then
      break
    fi
    sleep 0.25
  done

  x11vnc \
    -display :99 \
    -localhost \
    -rfbport 5900 \
    -nopw \
    -shared \
    -forever \
    -repeat \
    >/tmp/x11vnc.log 2>&1 &
  X11VNC_PID=$!
  DESKTOP_PIDS+=("$X11VNC_PID")

  websockify \
    --web=/usr/share/novnc \
    127.0.0.1:6081 \
    127.0.0.1:5900 \
    >/tmp/websockify.log 2>&1 &
  WEBSOCKIFY_PID=$!
  DESKTOP_PIDS+=("$WEBSOCKIFY_PID")
fi

cleanup() {
  if [[ ${#DESKTOP_PIDS[@]} -gt 0 ]]; then
    kill "${DESKTOP_PIDS[@]}" 2>/dev/null || true
  fi
}
trap cleanup EXIT INT TERM

BIND_HOST="${READER_BIND_HOST:-127.0.0.1}"
FORWARDED_ALLOW_IPS="${READER_FORWARDED_ALLOW_IPS:-127.0.0.1}"

exec uvicorn app.main:app \
  --host "$BIND_HOST" \
  --port 8010 \
  --proxy-headers \
  --forwarded-allow-ips "$FORWARDED_ALLOW_IPS" \
  --no-access-log
