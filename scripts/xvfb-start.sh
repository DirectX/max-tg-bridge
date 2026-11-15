#!/bin/sh
# ============================================================================
#  xvfb-start.sh — «виртуальный дисплей» для Chromium (MAX_HEADLESS=false)
#  Используется, когда нужен видимый браузер: меньше fingerprints headless-режима
#  и можно подключиться глазами (x11vnc / noVNC) при капче или QR-коде.
#
#  Запуск в compose:   entrypoint: ["/usr/local/bin/xvfb-start.sh"]
#  VNC-глазки:         environment: { VNC_ENABLE: "1", VNC_PASSWORD: "secret" }
#                      + проброс портов 5900:5900 (или отдельный контейнер noVNC)
#  НЕ забудьте закрыть порт 5900 фаерволом наружу!
# ============================================================================
set -e

export DISPLAY="${DISPLAY:-:99}"

# Разрешение экрана = желаемый «размер окна» браузера.
Xvfb "${DISPLAY}" -screen 0 "${XVFB_RESOLUTION:-1440x900x24}" -ac +extension GLX +render -noreset &
XVFB_PID=$!

# Ждём готовности X-сервера (файл-сокет /tmp/.X11-unix).
SOCK="/tmp/.X11-unix/X${DISPLAY#:}"
i=0
until [ -e "${SOCK}" ]; do
    i=$((i+1))
    [ "$i" -gt 40 ] && echo "[xvfb-start] Xvfb не поднялся" >&2 && exit 1
    sleep 0.5
done
echo "[xvfb-start] Xvfb ${DISPLAY} готов"

if [ "${VNC_ENABLE:-0}" = "1" ] && command -v x11vnc >/dev/null 2>&1; then
    echo "[xvfb-start] поднимаю x11vnc на :${VNC_PORT:-5900}"
    if [ -n "${VNC_PASSWORD:-}" ]; then
        x11vnc -display "${DISPLAY}" -forever -shared -rfbport "${VNC_PORT:-5900}" \
               -passwd "${VNC_PASSWORD}" -bg -o /data/logs/x11vnc.log
    else
        x11vnc -display "${DISPLAY}" -forever -shared -rfbport "${VNC_PORT:-5900}" \
               -bg -o /data/logs/x11vnc.log
    fi
fi

exec "$@"
