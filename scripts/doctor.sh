#!/bin/sh
# ============================================================================
# scripts/doctor.sh — диагностика среды Docker/хоста для max-tg-bridge
# ============================================================================
# Отвечает на вопрос «почему сборка падает НЕ из-за кода приложения»:
#   • «exec /bin/sh: exec format error»      -> неверная архитектура образа / нет QEMU
#   • «failed to mount ... operation not permitted» -> storage-драйвер/rootless/fs
#   • «no space left on device», «mirror unreachable», отсутствие openssl
#
# Запуск (без аргументов — только чтение, ничего не меняет):
#   sh scripts/doctor.sh
#   sh scripts/doctor.sh --smoke     # + пробный mini-build (тянет busybox или кэш)
# ============================================================================
set -u

SMOKE=0
[ "${1:-}" = "--smoke" ] && SMOKE=1

PROBLEMS=0
HINTS=""

say()  { printf '%s\n' "$*"; }
hdr()  { printf '\n\033[1m== %s\033[0m\n' "$*"; }
ok()   { printf '  \033[32mOK\033[0m   %s\n' "$*"; }
warn() { printf '  \033[33mWARN\033[0m %s\n' "$*"; }
bad()  { printf '  \033[31mFAIL\033[0m %s\n' "$*"; PROBLEMS=$((PROBLEMS + 1)); }
hint() { HINTS="$HINTS
  • $*"; }

# читает KEY=... из dotenv-файла: без кавычек, без CR, без строок-комментариев
_envval() {
  sed -n "s/^[[:space:]]*$1=//p" "$2" 2>/dev/null | tail -1 \
    | tr -d '\r' | sed -e 's/^"//' -e 's/"$//' -e "s/^'//" -e "s/'$//"
}

command -v docker >/dev/null 2>&1 || { bad "docker не найден в PATH"; say "$HINTS"; exit 1; }

# ------------------------------------------------------------------ базовое
hdr "Хост"
ARCH=$(uname -m)
KREL=$(uname -r)
say "  архитектура: $ARCH, ядро: $KREL"
case "$ARCH" in
  aarch64|arm64) WANT=linux/arm64 ;;
  x86_64|amd64)  WANT=linux/amd64 ;;
  *)             WANT="?"; warn "неизвестная архитектура $ARCH" ;;
esac

if [ -f /etc/os-release ]; then
  . /etc/os-release
  say "  дистрибутив: ${PRETTY_NAME:-?}"
fi

# ------------------------------------------------------- rootful vs rootless
hdr "Демон Docker"
DOCKER_ROOT=$(docker info --format '{{.DockerRootDir}}' 2>/dev/null)
say "  DockerRootDir: ${DOCKER_ROOT:-?}"
SEC=$(docker info --format '{{range .SecurityOptions}}{{.Name}} {{end}}' 2>/dev/null)
# rootless вычисляем по DockerRootDir (надёжнее строки SecurityOptions,
# которая в некоторых версиях docker не содержит слова «rootless»)
case "${DOCKER_ROOT:-}" in
  /home/*|/root/*) ROOTLESS=1 ;;
  *)               ROOTLESS=0 ;;
esac
case "$SEC" in
  *rootless*) ROOTLESS=1 ;;
esac
if [ "$ROOTLESS" = "1" ]; then
    warn "rootless-демон (root=${DOCKER_ROOT:-?}${SEC:+, $SEC})"
    hint "rootless + overlay — самый частый источник «operation not permitted» (особенно на ядрах Rockchip BSP 5.10). Надёжнее rootful: sudo systemctl enable --now docker; sudo usermod -aG docker \$USER (перелогин) — и собирать обычным docker compose."
else
    ok "rootful-демон"
fi

# ------------------------------------------- containerd image store (snapshotter)
hdr "Снепшоттер / storage driver"
DRIVER=$(docker info --format '{{.Driver}}' 2>/dev/null)
say "  storage driver: ${DRIVER:-?}"
if docker info 2>/dev/null | grep -qiE 'containerd.*snapshotter|io\.containerd\.snapshotter'; then
  SNAP=$(docker info 2>/dev/null | grep -iE 'snapshotter' | head -2 | tr -s ' ')
  say "  $SNAP"
fi
if [ -d "$DOCKER_ROOT/containerd" ] || docker info --format '{{.ServerVersion}}' >/dev/null 2>&1; then
  # containerd-snapshotter ломает rootless-фолбэк на fuse-overlayfs -> EPERM
  if [ "$ROOTLESS" = "1" ]; then
    CS=$(docker info 2>/dev/null | grep -ciE 'storage driver: overlayfs|io.containerd.snapshotter')
    if [ "${CS:-0}" -gt 0 ]; then
      bad "rootless + containerd image store (overlayfs внутри user namespace)"
      hint "Выключите containerd image store: /etc/docker/daemon.json (или ~/.config/docker/daemon.json для rootless) → {\"features\": {\"containerd-snapshotter\": false}}, затем systemctl restart docker. Docker сам выберет fuse-overlayfs."
    fi
  fi
fi

# ------------------------------------------------- filesystem под Docker root
hdr "Файловая система Docker root"
if [ -n "${DOCKER_ROOT:-}" ] && [ -d "$DOCKER_ROOT" ]; then
  FSTYPE=$(stat -f -c %T "$DOCKER_ROOT" 2>/dev/null)
  say "  тип ФС: ${FSTYPE:-?}  (путь: $DOCKER_ROOT)"
  case "$FSTYPE" in
    overlay|fuse*|nfs*|cifs|9p|tmpfs)
      bad "overlay поверх $FSTYPE обычно не работает (EPERM / EINVAL)"
      hint "Перенесите Docker root на обычную ФС: sudo edit /etc/docker/daemon.json → {\"data-root\": \"/var/lib/docker\"} и перезапустите демон."
      ;;
    *) ok "ФС выглядит пригодной ($FSTYPE)" ;;
  esac
  AVAIL=$(df -P "$DOCKER_ROOT" 2>/dev/null | awk 'NR==2 {print $4}')
  if [ -n "${AVAIL:-}" ]; then
    AVAIL_GB=$((AVAIL / 1024 / 1024))
    if [ "$AVAIL_GB" -lt 6 ]; then
      bad "места под Docker root: ${AVAIL_GB} ГБ (< 6 ГБ) — 720 МБ базового образа + pip-кэш не поместятся"
      hint "Освободите место: docker system prune -a --volumes (осторожно: удалит неиспользуемые образы/томов)."
    else
      ok "свободно ${AVAIL_GB} ГБ"
    fi
  fi
fi

# ------------------------------------------------------------- unprivileged userns
hdr "Unprivileged user namespaces"
for SYS in /proc/sys/kernel/unprivileged_userns_clone \
           /proc/sys/user/max_user_namespaces \
           /proc/sys/kernel/apparmor_restrict_unprivileged_userns; do
  if [ -r "$SYS" ]; then
    VAL=$(cat "$SYS" 2>/dev/null)
    say "  $SYS = $VAL"
    case "$SYS" in
      *apparmor_restrict_unprivileged_userns)
        [ "$VAL" = "1" ] && {
          bad "AppArmor ограничивает unprivileged userns (=1)"
          hint "sudo sysctl -w kernel.apparmor_restrict_unprivileged_userns=0 (и записать в /etc/sysctl.d/99-docker.conf для пережития ребута). Без этого rootless/overlay-маунты получают EPERM."
        } ;;
      *unprivileged_userns_clone)
        [ "$VAL" = "0" ] && {
          bad "unprivileged userns выключен (=0)"
          hint "sudo sysctl -w kernel.unprivileged_userns_clone=1"
        } ;;
      *max_user_namespaces)
        [ "${VAL:-1}" = "0" ] && {
          bad "max_user_namespaces = 0"
          hint "sudo sysctl -w user.max_user_namespaces=10000"
        } ;;
    esac
  fi
done

# ------------------------------------------- DOCKER_DEFAULT_PLATFORM (скрытый пин)
hdr "Переменные, форсирующие платформу"
if [ -n "${DOCKER_DEFAULT_PLATFORM:-}" ]; then
  bad "DOCKER_DEFAULT_PLATFORM=$DOCKER_DEFAULT_PLATFORM — форсирует платформу для всех pull/build"
  hint "unset DOCKER_DEFAULT_PLATFORM (уберите из ~/.bashrc / ~/.profile / systemd-юнита docker)"
else
  ok "DOCKER_DEFAULT_PLATFORM не задана"
fi

# ------------------------------------------------------------------- binfmt/QEMU
hdr "Мультиархитетурность (binfmt/QEMU)"
BINFM="/proc/sys/fs/binfmt_misc"
if [ -d "$BINFM" ] && [ -n "$(ls -A "$BINFM" 2>/dev/null)" ]; then
  ls "$BINFM" 2>/dev/null | sed 's/^/  /' | head -12
  if [ -r "$BINFM/qemu-aarch64/status" ]; then
    ST=$(cat "$BINFM/qemu-aarch64/status" 2>/dev/null)
    case "$ST" in *enabled*) ok "qemu-aarch64: $ST" ;; *) warn "qemu-aarch64: $ST" ;; esac
  else
    warn "qemu-aarch64 не зарегистрирован"
  fi
else
  warn "binfmt_misc пуст (QEMU не подключён)"
fi

NEED_QEMU=0
case "$ARCH" in
  x86_64|amd64)
    if [ "${BRIDGE_PLATFORM:-}" = "linux/arm64" ]; then
      NEED_QEMU=1
      say "  запрошен кросс-сбор: BRIDGE_PLATFORM=linux/arm64 на $ARCH"
    fi ;;
  aarch64|arm64)
    if [ "${BRIDGE_PLATFORM:-}" = "linux/amd64" ]; then
      NEED_QEMU=2
      say "  запрошен кросс-сбор: BRIDGE_PLATFORM=linux/amd64 на $ARCH"
    fi ;;
esac
if [ "$NEED_QEMU" = "1" ]; then
  if [ -r "$BINFM/qemu-aarch64/status" ] && grep -q enabled "$BINFM/qemu-aarch64/status" 2>/dev/null; then
    ok "QEMU для arm64 есть"
  else
    bad "Нужен QEMU для сборки arm64 на x86"
    hint "docker run --privileged --rm tonistiigi/binfmt --install arm64"
  fi
elif [ "$NEED_QEMU" = "2" ]; then
  if [ -r "$BINFM/qemu-x86_64/status" ] && grep -q enabled "$BINFM/qemu-x86_64/status" 2>/dev/null; then
    ok "QEMU для amd64 есть"
  else
    bad "Нужен QEMU для сборки amd64 на arm64"
    hint "docker run --privileged --rm tonistiigi/binfmt --install amd64  (или не кросс-собирайте: BRIDGE_PLATFORM=)"
  fi
else
  ok "кросс-сборка не запрошена — соберётся нативная $WANT"
fi

# --------------------------------------------------------- образ приложения
hdr "Базовый образ Playwright"
TAG=mcr.microsoft.com/playwright/python:v1.49.0-jammy
if docker image inspect "$TAG" >/dev/null 2>&1; then
  ARCHS=$(docker image inspect "$TAG" --format '{{.Os}}/{{.Architecture}}' 2>/dev/null)
  say "  локально: $TAG ($ARCHS)"
  case "$ARCHS" in
    *"$WANT"*) ok "локальный образ соответствует архитектуре хоста" ;;
    *) bad "локально скачан $ARCHS, а хост $WANT → buildx переиспользует чужой platform-вариант под тем же тегом (exec format error)"
       hint "docker image rm $TAG && BRIDGE_PLATFORM=$WANT docker compose up -d --build" ;;
  esac
else
  say "  базовый образ ещё не скачан (норма для первой сборки)"
fi

# ------------------------------------------------------------------- apt-сеть
hdr "Зеркала apt (нужны только для --build-arg INSTALL_VNC=1)"
if docker run --rm --network bridge debian:bookworm-slim \
     sh -c 'apt-get update -qq >/dev/null 2>&1 && echo reachable' 2>/dev/null | grep -q reachable; then
  ok "apt-зеркала доступны из контейнера"
else
  warn "apt недоступен/тяжело проверить (боевая сборка apt-слой НЕ требует)"
  hint "Если нужен VNC-слой и apt не тянется: соберите образ без INSTALL_VNC, а x11vnc ставьте позже в исполняемом контейнере."
fi

# ------------------------------------------------------------------ openssl в базе
hdr "openssl внутри базового образа (шифрование бэкапов)"
if docker image inspect "$TAG" >/dev/null 2>&1; then
  if docker run --rm --entrypoint sh "$TAG" -c 'command -v openssl' >/dev/null 2>&1; then
    ok "openssl есть — BACKUP_PASSWORD работает"
  else
    bad "openssl НЕТ в базовом образе — бэкапы не будут шифроваться"
    hint "Добавьте apt-слой (INSTALL_VNC не при чём) или уберите BACKUP_PASSWORD; app/backup.py в этом случае вернёт предупреждение вместо тихой потери."
  fi
else
  say "  пропущено: базовый образ не скачан"
fi

# ------------------------------------------------- Telegram-прокси: /bot vs /file
hdr "Telegram-прокси: эндпоинты /bot и /file"
# Мост пользуется ДВУМЯ путями:
#   /bot<token>/<method>     — все вызовы API (sendMessage/getUpdates/getFile)
#                              и ЗАГРУЗКА файлов (скриншоты, бэкапы)
#   /file/bot<token>/<path>  — СКАЧИВАНИЕ того, что прислали вам (фото Владельца)
# Классическая дыра: проксируют только /bot. Тогда текст ходит в обе стороны,
# скриншоты админа доезжают, а фото из Telegram теряются молча. v2.2.1 сам
# обнаруживает это при старте контейнера; здесь проверяем с хоста.
ENV_FILE="${ENV_FILE:-.env}"
if [ -r "$ENV_FILE" ] && command -v curl >/dev/null 2>&1; then
  BASE=$(_envval TG_API_BASE "$ENV_FILE")
  TOK=$(_envval TG_BOT_TOKEN "$ENV_FILE")
  case "$BASE" in
    http://*) bad "TG_API_BASE начинается с http:// — Bot API требует HTTPS"
              hint "Прокси обязан быть по https, иначе Telegram отклонит запросы." ;;
  esac
  if [ -n "$BASE" ] && [ -n "$TOK" ]; then
    say "  прокси: $BASE (token $(printf '%s' "$TOK" | cut -c1-6)…)"

    BOT_BODY=/tmp/doctor_bot.$$.json
    BOT_TIMING=$(curl -s -o "$BOT_BODY" -w '%{http_code}/%{time_total}s' \
                 --max-time 15 "$BASE/bot$TOK/getMe" 2>/dev/null)
    if [ -s "$BOT_BODY" ] && grep -q '"ok":true' "$BOT_BODY" 2>/dev/null; then
      ok "/bot отвечает ($BOT_TIMING) — API доступен"
    else
      bad "/bot не отвечает как Telegram API ($BOT_TIMING)"
      hint "Без /bot мост не работает вовсе. Проверьте с хоста: curl -v $BASE/bot<token>/getMe"
    fi
    rm -f "$BOT_BODY"

    FILE_BODY=/tmp/doctor_file.$$.txt
    FILE_META=$(curl -s -o "$FILE_BODY" -w '%{http_code}|%{content_type}' \
                 --max-time 15 "$BASE/file/bot$TOK/probe.test" 2>/dev/null)
    FILE_CODE=${FILE_META%%|*}
    FILE_CT=${FILE_META#*|}
    if [ "$FILE_CODE" = "200" ] || printf '%s' "$FILE_CT" | grep -q json; then
      ok "/file проксируется ($FILE_CODE, $FILE_CT) — фото из Telegram доступны"
    else
      warn "/file НЕ проксируется ($FILE_CODE, ct=$FILE_CT) — фото из TG скачиваться не будут"
      hint "Свой Nginx: проксируйте и путь /file (готовый конфиг в nginx/telegram-api.conf.example)."
      hint "Сторонний ретранслятор: если он не раздаёт /file, лечится только сменой прокси — текст при этом работает. Подробности: README, раздел 3.1."
    fi
    rm -f "$FILE_BODY"
  else
    say "  пропущено: TG_API_BASE / TG_BOT_TOKEN не прочитаны из $ENV_FILE"
  fi
else
  say "  пропущено: нет $ENV_FILE или curl — проверка есть в логе старта контейнера"
  say "  («Прокси /bot отвечает» / «Эндпоинт /file НЕ проксирует Telegram API»)"
fi

# ---------------------------------------------------------------------- smoke
if [ "$SMOKE" = "1" ]; then
  hdr "Пробная сборка (один RUN-слой)"
  TMP=$(mktemp -d)
  printf 'FROM busybox\nRUN true\n' > "$TMP/Dockerfile"
  if docker build -q -t doctor-smoke:latest "$TMP" >/dev/null 2>&1; then
    ok "docker build + overlay-маунт работают"
  else
    bad "docker build упал на простейшем RUN — проблема точно в хосте/демоне, не в нашем Dockerfile"
    docker build -t doctor-smoke:latest "$TMP" 2>&1 | tail -5 | sed 's/^/      /'
    hint "Смотрите разделы FAIL выше: почти всегда это rootless+containerd-snapshotter или ФС под Docker root."
  fi
  rm -rf "$TMP"
fi

# ---------------------------------------------------------------------- итог
hdr "ИТОГ"
if [ "$PROBLEMS" -eq 0 ]; then
  ok "критичных проблем не найдено; сборка: docker compose up -d --build"
  exit 0
fi
bad_count=$PROBLEMS
say "  найдено проблем: $bad_count"
say "  Рекомендации:$HINTS"
exit 1
