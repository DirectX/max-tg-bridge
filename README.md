# MAX ⇄ Telegram Bridge

Двусторонний production-мост между веб-клиентом мессенджера **MAX**
(`https://web.max.ru`) и **Telegram** для школьного чата. Расчёт на
одноплатник **Orange Pi 5 Plus** (RK3588, `linux/arm64`, 32 ГБ, headless)
и работу 24/7 в Docker.

```
                        домашний резидентный IP (без прокси!)
                                    │
 Orange Pi 5 Plus (arm64, Docker)   │
 ┌───────────────────────────────────┴─────────────────────────┐
 │  max-tg-bridge (python asyncio)                             │
 │  ┌──────────────┐   MutationObserver + drain   ┌─────────┐  │
 │  │ aiogram v3   │◄────────────────────────────►│Chromium │──┼──► web.max.ru (напрямую)
 │  │ long-polling │      JS-мост страницы        │(Playwr.)│  │
 │  └──────┬───────┘                              └─────────┘  │
 └─────────┼───────────────────────────────────────────────────┘
           │ https://tg-proxy.example.com   (свой Nginx-реверс)
           ▼
   api.telegram.org (заблокирован напрямую — только через прокси)
```

* **MAX → Telegram**: `MutationObserver` внутри страницы ловит новые
  сообщения; текст + отправитель → `send_message`; картинки скачиваются
  `fetch()` **внутри вкладки** (работает с `blob:`-URL и приватными CDN-ccылками,
  куки подставляет браузер) → base64 → `BytesIO` → `send_photo` с подписью.
* **Telegram → MAX**: reply на пересланное сообщение или префикс `!макс` —
  текст печатается посимвольно с «человеческими» задержками и уходит Enter'ом;
  фото скачивается через прокси во временную директорию и загружается через
  `input[type=file]`.
* **Антифрод/стабильность**: `--disable-blink-features=AutomationControlled`,
  чистка `navigator.webdriver`, десктопный UA, случайные задержки,
  `shm_size: 2gb`, суточная мягкая перезагрузка вкладки в 03:00, watchdog
  с авто-переоткрытием чата, `restart: unless-stopped`.

---

## 1. Структура проекта

```
max-tg-bridge/
├── app/
│   ├── __init__.py
│   ├── main.py            # точка входа: сборка runtime, сигналы, супервизия задач
│   ├── auth.py            # первичная авторизация MAX в терминале (--manual режим)
│   ├── bridge.py          # маршрутизация MAX<->TG, дедуп, анти-эхо-петля, watchdog
│   ├── browser.py         # Playwright persistent context, stealth-инъекции, аргументы Chromium
│   ├── max_dom.py         # DOM-адаптер MAX: JS-мост чтения, скачив картинок, композер
│   ├── telegram_bot.py    # aiogram v3 через TelegramAPIServer.from_base (Nginx-прокси)
│   ├── config.py          # вся конфигурация из ENV (+.env локально)
│   ├── selectors.py       # ВСЕ СЕЛЕКТОРЫ MAX — единственный файл под верстку
│   ├── store.py           # sqlite: переданные сообщения, TG->MAX маппинг
│   └── utils.py           # ретраи, human-delay, нарезка текста, лог
├── nginx/
│   └── telegram-api.conf.example   # реверс api.telegram.org на вашем TLS-домене
├── scripts/
│   └── xvfb-start.sh      # Xvfb + x11vnc для режима MAX_HEADLESS=false / капчи
├── tests/
│   ├── run_js_bridge_test.sh
│   └── test_max_dom_bridge.js   # юнит-тест JS-моста чтения на мини-DOM (node)
├── data/                  # создаётся автоматически (в .gitignore)
│   ├── chrome_profile/    # persistent-профиль Chromium (сессия MAX)
│   ├── state/             # bridge.db (дедупликация переживает рестарты)
│   ├── tmp/               # картинки TG -> MAX
│   └── logs/              # browser.log, auth_*.png, x11vnc.log
├── Dockerfile             # mcr.microsoft.com/playwright/python (multi-arch, arm64)
├── docker-compose.yml     # platform linux/arm64, shm_size 2gb, лимиты
├── requirements.txt
├── .env.example
└── README.md
```

## 2. Что понадобится

| Что | Зачем |
|---|---|
| Orange Pi 5 Plus + Docker + compose plugin v2 | хост |
| Telegram-бот (@BotFather) | приём/отправка |
| ID чата (`TG_CHAT_ID`) | пункт назначения в TG |
| HTTPS-домен с TLS-сертификатом и Nginx | реверс Telegram API (`TG_API_BASE`) |
| Аккаунт MAX (номер телефона) | чтение/ответы в чате |
| ~1 ГБ трафика на `docker build` | образ Playwright |

> ⚠️ **Сетевая идеология**: на MAX — **всегда напрямую** с домашнего IP
> (резидентный IP = «обычный пользователь», любой прокси-датацентровый IP —
> флаг антифроду). Telegram — **только** через ваш `TG_API_BASE`. В коде это
> разведено жёстко: у браузерного контекста нет никаких proxy-настроек, а
> весь aiogram ходит исключительно в `TG_API_BASE`.

## 3. Деплой

### 3.1 Nginx-прокси Telegram (если ещё не готов)

Готовый конфиг: [`nginx/telegram-api.conf.example`](nginx/telegram-api.conf.example).
Ключевое — проксируются **два** префикса и подменяется Host:

```nginx
location /bot  { proxy_pass https://api.telegram.org; proxy_set_header Host api.telegram.org; proxy_ssl_server_name on; }
location /file { proxy_pass https://api.telegram.org; proxy_set_header Host api.telegram.org; proxy_ssl_server_name on; }
```

Проверка с любой машины:

```bash
curl -s https://tg-proxy.example.com/bot<TOKEN>/getMe
# ожидаем: {"ok":true,...}
```

### 3.2 Код на плате

```bash
git clone <репозиторий> && cd max-tg-bridge     # или rsync/scp каталога
cp .env.example .env
nano .env                                        # токены, TG_API_BASE, MAX_CHAT_TITLE
chmod 600 .env                                   # секреты не должны светить всем uid
mkdir -p data/{chrome_profile,state,tmp,logs}

docker compose up -d --build
# Собирается на самой плате ~5-10 минут (pip-слой), браузер уже в базовом образе.
# Альтернатива — собрать на x86-ноутбуке и загнать в registry:
#   docker buildx build --platform linux/arm64 -t max-tg-bridge:latest --push .
```

### 3.3 Первичная авторизация MAX (главный этап)

Без GUI, из терминала (SSH достаточно — скрипт интерактивный):

```bash
docker compose run --rm bridge python -m app.auth
```

```text
┌─ Первичная авторизация MAX ─────────────────────────────
│  Профиль браузера сохраняется в /data/chrome_profile
└──────────────────────────────────────────────────────────

Номер телефона MAX (например +79991234567): +79991234567
Скриншот сохранён: /data/logs/auth_phone_entered_153012.png
Код из СМС: 482913
Скриншот сохранён: /data/logs/auth_success_153104.png

✅ Авторизация завершена, профиль сохранён.
```

Cookies/localStorage/IndexedDB остаются в `./data/chrome_profile` — мост
забирает их при `docker compose up -d`. Повторный вход нужен только если
MAX сам разорвёт сессию (мост сообщит об этом в Telegram).

> Если контейнер ещё не собран — тем же образом поднимется только что
> собранный; `run` использует тот же image и тома.

### 3.4 Альтернатива: если MAX показал капчу / QR-код

Автоскрипт умеет вводить телефон и код, но не «разгадывать картинки».
Три способа посмотреть и довести вход руками:

**Вариант А — временный VNC inside контейнер (рекомендую).**
В `docker-compose.yml` у сервиса `bridge`:

```yaml
    entrypoint: ["/usr/local/bin/xvfb-start.sh"]
    command: ["python", "-m", "app.auth", "--manual"]
    environment:
      DISPLAY: ":99"
      VNC_ENABLE: "1"
      VNC_PASSWORD: "придумайте-пароль"
    ports:
      - "127.0.0.1:5900:5900"     # не наружу!
```

Подключитесь VNC-клиентом через туннель: `ssh -L 5900:127.0.0.1:5900 pi@опи`
→ клиентом к `localhost:5900`. Вы увидите реальный Chromium, пройдёте капчу
мышкой, затем `docker compose logs -f` подскажет кнопки Enter для шагов.
Для браузера это выглядит как настоящий десктоп (Xvfb-«экран», не headless).
По завершении **верните исходный compose** без entrypoint/ports.

Для «картинки прямо в браузере» можно поверх поднять noVNC:

```bash
docker run -d --rm --name novnc \
  --network max-tg-bridge-net \
  -e VNC_SERVER=bridge:5900 \
  -p 127.0.0.1:6080:8080 \
  m1k1o/noVNC:latest
# на ноутбуке: ssh -L 6080:localhost:6080 pi@опи
#               открыть http://localhost:6080/vnc.html  (пароль из VNC_PASSWORD)
```

**Вариант Б — X11-forwarding.** На ноутбуке (Linux/macOS с XQuartz):

```bash
ssh -X -o ForwardX11Timeout=0 -o ForwardX11Trusted=yes pi@опи
cd max-tg-bridge
docker compose run --rm \
  -e MAX_HEADLESS=false -e DISPLAY=$DISPLAY \
  -v /tmp/.X11-unix:/tmp/.X11-unix:ro \
  --entrypoint sh bridge -c "python -m app.auth --manual"
# да, с пробросом сокета X11; на macOS путь XQuartz тот же.
# Окно Chromium появится на вашем ноутбуке, процесс крутится на плате.
```

**Вариант В — «авторизоваться дома» и перенести профиль.** Войдите в
web.max.ru в обычном Chrome на ноутбуке, затем скопируйте каталог профиля
Playwright (или просто дождитесь, пока MAX не станет просить код только раз
в полгода — профиль переживает рестарты). Менее гигиенично, но работает:
профиль — это и есть вся сессия.

### 3.5 Пуск боевого режима

```bash
docker compose up -d
docker compose logs -f bridge
# в Telegram: /start, /status
```

## 4. Как пользоваться мостом

| Действие | Результат |
|---|---|
| Сообщение в чате MAX | в TG: `👤 Мария Ивановна · 12.11 15:03` + текст; фото — подписанным `send_photo` (несколько фото — последовательно) |
| **Reply** в TG на сообщение из MAX | в MAX уходит «ответ» на то же сообщение (best-effort через контекст-меню), при неудаче — цитата «…» + ответ |
| `!макс текст` (или `!m`, `/max`) | обычное сообщение в MAX |
| Фото reply-ом или с префиксом | скачается через прокси, сохранится в `data/tmp/`, загрузится в MAX через файловый инпут, подпись — текст сообщения |
| `/status` | аптайм, счётчики, размер очереди |
| `TG_ALLOWED_USER_IDS` | ограничить, кто в TG вправе писать в MAX |

Ваши собственные сообщения **не пересылаются** в TG: фильтр по
`MAX_SELF_NAME`, CSS-маркерам исходящих и «эхо-окну» на 90 секунд.

## 5. Селекторы: настройка под актуальную вёрстку MAX

`web.max.ru` — React-приложение с минифицированными классами, вёрстка
меняется релиз за релизом. **Все** точки касания DOM — списки кандидатов в
[`app/selectors.py`](app/selectors.py); код перебирает их по порядку.
Признак того, что нужен заход в DevTools: мост пишет
`Не нашёл поле ввода телефона`, `Чат ... не открылся` или молчит при новых
сообщениях.

Быстрый чек-лист (полная инструкция — в докстринге `selectors.py`):

1. Откройте `web.max.ru` в обычном Chrome, F12 → pick element.
2. Ищите устойчивые признаки: `[data-message-id]`, `[role=...]`,
   `aria-label`, `placeholder`, подстроки классов `div[class*="message"]`.
   **Не** используйте `div:nth-child(...)`-цепочки.
3. Проверяйте гипотезу в консоли: `document.querySelectorAll('div[class*="message"]').length`.
4. Особое внимание `CHAT_SCOPE` — область ленты; без него превью из
   левого списка чатов тоже матчатся «сообщениям» и приходят дубли.
5. `docker compose restart bridge` — новые селекторы подхвачены.

Полезно при разведке: `data/logs/browser.log` содержит console-ошибки
страницы и упавшие запросы MAX.

## 6. Отладка

| Симптом | Куда смотреть / что делать |
|---|---|
| Мост не стартует: «TG_API_BASE должен быть HTTPS» | заполните `.env`, прокси обязателен с валидным TLS |
| `401 Unauthorized` от Telegram | токен/прокси: `curl .../bot<token>/getMe` |
| `MAX не показывает авторизованный интерфейс` | сессия протухла → `docker compose run --rm bridge python -m app.auth` |
| Чат не найден | точное название из шапки чата в `MAX_CHAT_TITLE`; или возьмите URL чата в `MAX_CHAT_URL` |
| Картинки не долетают, есть `fetch картинки не удался` | посмотрите `auth_*/browser.log`; включён резервный путь — скриншот элемента; если и он пуст → правьте `MESSAGE_IMAGES` |
| Дубли/нет сообщений | отладка `data/logs/`: `seed=` при старте должно быть ≈ числу видимых сообщений; проверьте `CHAT_SCOPE`, `MESSAGE_TEXT` |
| Chromium падает (`SIGBUS`, `Out of memory`) | не уменьшайте `shm_size: '2gb'`; при тесноте — снизьте лимит `memory` и `MAX_VIEWPORT_HEIGHT` |
| Сессия live, но MAX кидает на вход ночью | у аккаунта мог смениться телефон/сессия — перелогин через app.auth; профиль перезапишется |
| Нужно посмотреть «глазами» | раздел 3.4 вариант А (Xvfb+VNC), плюс `docker compose exec bridge ls /data/logs` + скачайте любой `auth_*.png` |
| Логи | `docker compose logs -f --tail 200 bridge`; `tail -f data/logs/browser.log` |

**Диагностический стенд без железа** — запуск на любой x86-машине с тем же
образом (`platform: linux/amd64` в копии compose): удобно подбирать селекторы
через `app.auth --manual` + VNC, а затем просто скопировать `data/chrome_profile`
и `.env` на Orange Pi.

## 7. Тесты и проверка конфигурации

```bash
bash tests/run_js_bridge_test.sh   # JS-мост чтения: мини-DOM + node, без браузера
python3 -m compileall -q app/      # синтаксис всех модулей
docker compose config --quiet      # валидность compose + наличие .env
```

JS-тест покрывает самое хрупкое: извлечение сообщений, ключи дедупликации
(`data-message-id` / хэш / фото-без-текста по src), mine- и системные фильтры,
инкрементальный `drain`. При правке `_JS_BRIDGE` в `max_dom.py` — гоняйте его.

## 8. Безопасность

* `.env` (токены, номер телефона) — права `600`, в `.gitignore`, в контейнер
  попадает только через `env_file`. В образ и в логи секреты не пишутся
  (токены в логах Telegram-клиента не печатаются).
* Профиль Chromium содержит **живую сессию MAX** — каталог `data/`
  не отдаём наружу (права, бэкапы шифруем).
* ВNC-порт — только через SSH-туннель (`127.0.0.1:5900`), с паролем, и только
  на время ручной авторизации.
* Ограничьте право отвечать в MAX через `TG_ALLOWED_USER_IDS` — в школьном
  родительском чате TG обычно больше людей, чем нужно для отправки.

## 9. Известные ограничения (дизайн-решения)

* Альбом из N фото пересылается N последовательными `send_photo`
  (подпись — на первом), и наоборот; «склейка» в media-group не делалась
  намеренно — reply-маппинг на альбомы усложняет отладку.
* Редактирование/удаление сообщений в MAX не транслируется (новое
  «отредактированное» сообщение уйдёт как новое — ключ дедупликации по
  содержимому).
* Голосовые, видеосообщения и файлы-документы из MAX не пересылаются
  (патчится в `MESSAGE_IMAGES`/новых обработчиках по той же схеме).
* Reply-через-меню в MAX — best-effort: при изменении контекстного меню
  мост деградирует до обычного сообщения с цитатой.
* Это **не** официальный API (у MAX публичного Bot API для веб-чатов нет) —
  используйте свой аккаунт и будьте готовы, что вход через код понадобится
  снова; профиль с диска делает это редкостью.
