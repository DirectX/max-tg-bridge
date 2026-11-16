# ============================================================================
#  MAX ⇄ Telegram bridge — образ под linux/arm64 (Orange Pi 5 Plus, RK3588)
# ============================================================================
#  Официальный образ Playwright многослойный и МНОГОАРХИТЕКТУРНЫЙ:
#  поддерживает linux/amd64 и linux/arm64, содержит Chromium/Firefox/WebKit,
#  все системные зависимости и Xvfb. Это лучший выбор для ARM SBC: не нужно
#  самому тащить библиотеки, которые Chromium ест из системы.
#
#  ВНИМАНИЕ: тег образа ДОЛЖЕН совпадать с версией playwright в
#  requirements.txt (сейчас 1.49.0). Обновляйте синхронно в обоих местах.
#
#  Сборка на самой плате не нужна — соберите на x86-сервере/ноутбуке:
#     docker buildx build --platform linux/arm64 -t max-tg-bridge:latest --load .
#  ...или просто `docker compose up -d --build` на плате (compose укажет
#  platform: linux/arm64 и образ подтянется/соберётся сам).
# ============================================================================

FROM mcr.microsoft.com/playwright/python:v1.49.0-jammy

#--- Мета -------------------------------------------------------------------
LABEL org.opencontainers.image.title="max-telegram-bridge-v2" \
      org.opencontainers.image.description="24/7 Multi-Account мост web.max.ru <-> Telegram (Playwright + aiogram)" \
      org.opencontainers.image.architecture="arm64"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    # Playwright внутри образа уже установил браузеры в /ms-playwright —
    # не даём pip случайно скачать другой Chromium:
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /app

#--- Системные дополнения ----------------------------------------------------
# Официальный образ Playwright уже содержит Xvfb и все библиотеки Chromium.
# openssl  — шифрование архивов бэкапа (AES-256-CBC + PBKDF2), см. app/backup.py;
# x11vnc   — «глазки» для ручного прохождения капчи/QR (опция).
RUN apt-get update \
 && apt-get install -y --no-install-recommends openssl x11vnc \
 && rm -rf /var/lib/apt/lists/*

#--- Код приложения ----------------------------------------------------------
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY scripts ./scripts
COPY tests ./tests
COPY pytest.ini ./
COPY scripts/xvfb-start.sh /usr/local/bin/xvfb-start.sh
RUN chmod +x /usr/local/bin/xvfb-start.sh ./scripts/*.sh

#--- Каталог данных ----------------------------------------------------------
# /data монтируется томом из docker-compose:
#   /data/profiles        — изолированные профили: account_1/, account_2/, .trash/
#   /data/state           — sqlite v3 (роли, аккаунты, инвайты, дедуп, маппинги)
#   /data/tmp             — временные файлы (картинки TG, сборка архива бэкапа)
#   /data/logs            — browser_a<N>.log, скриншоты auth_a<N>_*.png
#   /data/backups         — суточные архивы bridge_YYYYMMDD_HHMMSS.tar.gz[.enc]
RUN mkdir -p /data/profiles /data/state /data/tmp /data/logs /data/backups

#--- Безопасность ------------------------------------------------------------
# Официальный образ playwright работает под root; нам это безразлично
# (--no-sandbox уже в аргументах Chromium), но можно завести своего юзера:
#   RUN useradd -m -u 1000 bridge && chown -R bridge:bridge /data /app
#   USER bridge
# Для первого запуска оставляем root — меньше проблем с правами на тома.

#--- Здравого смысла ---------------------------------------------------------
# Chromium в контейнере любит /dev/shm — в compose выделено 2GB,
# плюс --disable-dev-shm-usage в аргументах запуска.

# Проверка живости: процесс python должен жить, а профиль — не быть пустым.
HEALTHCHECK --interval=60s --timeout=10s --start-period=120s --retries=5 \
    CMD python -c "import pathlib,sys; sys.exit(0 if (pathlib.Path('/data/state').exists() and pathlib.Path('/data/profiles').exists()) else 1)" || exit 1

# Тестовые зависимости (pytest, pytest-asyncio) уже в requirements.txt —
# слой выше установлен вместе с ними, поэтому отдельного шага нет.

# Основной режим. Авторизация аккаунтов — из Telegram (/admin → Добавить).
# Аварийный консольный вход:
#   docker compose run --rm bridge python -m app.auth --list
# Тесты (зависимости уже в образе):
#   docker compose run --rm bridge pytest
CMD ["python", "-m", "app.main"]
