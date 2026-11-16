"""
app/selectors.py
================
ЕДИНСТВЕННАЯ точка правки проекта под актуальную верстку web.max.ru.

Веб-клиент MAX — SPA на React с минифицированными классами вида
`_1a2b _c3d4 message-xK9f2`, которые меняются от релиза к релизу.
Поэтому каждый селектор ниже — это СПИСОК вариантов, которые мост
перебирает по порядку и использует первый сработавший.

============================================================================
КАК ЗА 10 МИНУТ ПОДНИМАТЬ СЕЛЕКТОРЫ ЧЕРЕЗ DEVTOOLS
============================================================================
1. Откройте web.max.ru в обычном десктопном Chrome (на ноутбуке, не на Orange Pi)
   и войдите в аккаунт / откройте нужный чат.
2. F12 (DevTools) -> вкладка Elements. Наведите на элемент -> Ctrl+Shift+C
   откроет инспектор прямо на нём (или нажмите иконку курсора в левом верхнем
   углу DevTools и кликните по элементу).
3. Правой кнопкой по узлу в дереве -> Copy -> Copy selector  (даст CSS-путь),
   но ПУТИ-ПОТОМКИ (.app > div > div:nth-child(3) > span) НЕ ИСПОЛЬЗУЙТЕ —
   они ломаются от любого релиза. Ищите устойчивые признаки:
   - data-атрибуты:  [data-message-id], [data-testid="..."], [data-role="..."]
   - семантические:  [role="listitem"], [contenteditable="true"]
   - aria-атрибуты:  [aria-label="Написать сообщение"], input[placeholder*="Сообщение"]
   - стабильные куски класса (подстрока!):  div[class*="message"], div[class*="message-text"]
     В консоли DevTools проверяются так:  document.querySelectorAll('div[class*="message"]')
   - текстовые якоря:   div:has-text("Отправить")   (это Playwright-синтаксис)
4. Проверяйте каждую кандидатику в консоли DevTools:
       document.querySelectorAll('div[class*="message"] ').length
   Если возвращается вменяемое число сообщений чата — селектор подходит.
   Чат-контейнер обычно «виртуализированный»: в DOM живут только видимые
   сообщения — поэтому мост и использует MutationObserver (см. app/max_dom.py).
5. Если у элементов есть текстовые/плейсхолдер-якоря — используйте их
   (поле телефона имеет placeholder с «телефон»/«номер», поле кода — «код»).
6. После правки файла достаточно `docker compose restart bridge` —
   код трогать не нужно.

============================================================================
ВАЖНЫЕ ЗАМЕТКИ ПО SPECIFIKE MAX WEB
============================================================================
* Вход: сначала экран «Введите номер телефона» (не путать с уже авторизованным
  интерфейсом). Поле телефона принимает формат +7…, кнопка «Далее».
* Далее экран «Введите код из SMS» — часто это НЕ одно input-поле, а
  «невидимое» поле (styles: opacity:0 / width:1px) рядом с нарисованными
  квадратиками цифр. Поэтому для SMS в приоритете ввод «в активный элемент»
  (page.keyboard.type), а селектор — лишь способ дождаться появления экрана.
* Картинки в чате — это <img src="blob:https://web.max.ru/...."> либо
  <div style="background-image:url(https://...)">; скачивание реализовано
  через fetch() ВНУТРИ страницы, поэтому куки и авторизация подставляются
  браузером автоматически (см. app/max_dom.py: download_image_base64).
* Прикрепление файла: почти всегда существует скрытый
  input[type="file"] — Playwright умеет ставить файлы и в скрытые инпуты.
============================================================================
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# 1. ЭКРАН ВХОДА (только для app/auth.py)
# ---------------------------------------------------------------------------

# ПЕРВЫМ делом web.max.ru показывает экран «Войдите в MAX по QR-коду»
# (срок жизни QR ~2 минуты, потом «QR-код устарел»). Поля телефона там НЕТ —
# он появляется только после клика по переключателю «Войти по номеру телефона».
# Верстка — Svelte: <span class="content svelte-13fyxqk"> Войти по номеру телефона</span>.
# ВАЖНО: не привязываемся к хэшу svelte-класса (он меняется с каждой сборкой),
# ищем по тексту/атрибутам.
PHONE_SWITCH: list[str] = [
    'button:has-text("Войти по номеру телефона")',
    'a:has-text("Войти по номеру телефона")',
    '[role="button"]:has-text("номеру телефона")',
    'span[class*="content"]:has-text("Войти по номеру телефона")',
    ':text("Войти по номеру телефона")',
    'button:has-text("по номеру")',
    'button:has-text("Телефон")',
]

# ---------------------------------------------------------------------------
# ФОРМА ТЕЛЕФОНА — по реальному DOM web.max.ru (снят 2026-11-16, Svelte):
#
#   <form class="auth auth--phone auth--regular svelte-vywflk">
#     <div class="field">
#       <div class="input svelte-1cug6p"><div class="input svelte-1vjsp2y">
#         <div class="before">
#           <button class="country" aria-haspopup="dialog">  <-- страна, НЕ инпут!
#             ... +7 <span class="placeholder">123 456 78 90</span>
#           </button>
#         </div>
#         <input type="text" class="field svelte-1vjsp2y"
#                autofocus inputmode="decimal" autocomplete="tel">   <-- ВОТ ЭТОТ
#       </div>
#     </div>
#     <button class="button button--large button--primary button--disabled"
#             disabled type="submit"><span class="content">Продолжить</span></button>
#     <div class="footer">
#       <button class="button button--small button--ghost" type="button">
#         <span class="content">Войти по QR-коду</span></button>   <-- обратный ход
#     </div>
#   </form>
#
# ОТКУДА БРАЛИСЬ ПРЕДЫДУЩИЕ ПРОВАЛЫ (важно для понимания):
#   * input[type="tel"] НЕ матчит: у поля type="text" + inputmode="decimal";
#   * input[placeholder*="…"] НЕ матчит: плейсхолдер — это <span>, не атрибут;
#   * name="phone" — атрибута нет вообще.
# Якоря ниже — по реальным атрибутам (autocomplete/inputmode/autofocus) и по
# форме (form.auth--phone), а не по хэшу svelte-класса (он меняется на сборке).

# Якорь самой формы телефона: самый надёжный признак «мы на шаге номера».
PHONE_FORM: list[str] = [
    'form.auth--phone',
    'form.auth:has(input[autocomplete="tel"])',
    'form.auth:has(input[inputmode="decimal"])',
]

# Любой экран аутентификации (QR или телефон) — для _looks_like_login/watchdog.
AUTH_SCREEN: list[str] = [
    'form.auth',
    '[class*="auth--qr"]',
    '[class*="authLogo"]',
]

# Поле ввода номера телефона (внимательно: type="text", не "tel"!).
PHONE_INPUT: list[str] = [
    'form.auth--phone input[autocomplete="tel"]',
    'input[autocomplete="tel"]',
    'form.auth--phone input[inputmode="decimal"]',
    'input[inputmode="decimal"]',
    'form.auth--phone input[autofocus]',
    'input[autofocus]',
    'div.field > div.input input[type="text"]',
    'form.auth input[type="text"]',
    # legacy-догадки v2.0 — на случай возврата классической верстки
    'input[type="tel"]',
    'input[name="phone"]',
    'input[placeholder*="телефон" i]',
    'input[placeholder*="номер" i]',
]

# Кнопка «Продолжить» на экране телефона. Внимательно: в исходном состоянии она
# имеет класс button--disabled и атрибут disabled — активируется только когда
# поле приняло достаточное число цифров. Кликать надо с ожиданием enabled
# (это делает submit_phone), иначе повиснем на 30-секундном авто-вейте.
PHONE_SUBMIT_BUTTON: list[str] = [
    'form.auth--phone button[type="submit"]',
    'form.auth button[type="submit"]',
    'button.button--primary:has-text("Продолжить")',
    'button:has-text("Продолжить")',
    'button:has-text("Далее")',
    'button:has-text("Получить код")',
    'button[type="submit"]',
]

# Кнопка страны/кода (+7) внутри поля. Трогать НЕ нужно для РФ: префикс уже
# выбран, а в само поле печатаются ТОЛЬКО 10 цифр абонента (см. submit_phone).
# Сюда идут только если номер не из списка стран — тогда мост честно ругается.
COUNTRY_BUTTON: list[str] = [
    'form.auth--phone button.country',
    'button.country[aria-haspopup="dialog"]',
    'div.before button[aria-haspopup="dialog"]',
]

# QR-экран («Войдите в MAX по QR-коду»): поле телефона появляется только после
# клика по этому переключателю. Верстка — <span class="content svelte-13fyxqk">
# Войти по номеру телефона</span>; хэш класса меняться будет, якорь по тексту.
PHONE_SWITCH: list[str] = [
    'button:has-text("Войти по номеру телефона")',
    'a:has-text("Войти по номеру телефона")',
    '[role="button"]:has-text("номеру телефона")',
    'span[class*="content"]:has-text("Войти по номеру телефона")',
    ':text("Войти по номеру телефона")',
    'button:has-text("по номеру")',
    'button:has-text("Телефон")',
]

# Обратный переключатель (с телефонной формы на QR) — «Войти по QR-коду».
# Нужен, чтобы НЕ путать его с PHONE_SWITCH и понимать, что мы уже на шаге номера.
QR_SWITCH: list[str] = [
    'button:has-text("Войти по QR-коду")',
    'span[class*="content"]:has-text("по QR-коду")',
]

# Поле / экран ввода кода из SMS. Используется чтобы ДОЖДАТЬСЯ второго шага.
# NB: inputmode="decimal" у поля ТЕЛЕФОНА, поэтому numeric/one-time-code —
# безопасные различители шага кода.
SMS_INPUT: list[str] = [
    'input[autocomplete="one-time-code"]',
    'input[name="code"]',
    'input[inputmode="numeric"]',
    'input[placeholder*="код" i]',
    'input[type="password"]',
]

# Кнопка подтверждения кода (если Enter по какой-то причине не сработал).
SMS_SUBMIT_BUTTON: list[str] = [
    'button:has-text("Подтвердить")',
    'button:has-text("Войти")',
    'button:has-text("Проверить")',
    'button[type="submit"]',
]

# Маркеры «мы в авторизованном интерфейсе» (появляются после успешного входа):
# поле ввода сообщения — лучший индикатор, что сессия активна.
LOGGED_IN_MARKERS: list[str] = [
    '[contenteditable="true"]',
    'textarea[placeholder*="общение"]',
    'textarea[placeholder*="сообщение" i]',
]

# Маркеры графической капчи / QR (для auth.py — подсказать человеку про VNC).
CAPTCHA_MARKERS: list[str] = [
    'iframe[src*="captcha"]',
    'iframe[src*="kaptcha"]',
    '[class*="captcha"]',
    '[class*="qr-code"]',
    '[class*="qrcode"]',
    'canvas[aria-label*="QR" i]',
]

# ---------------------------------------------------------------------------
# 2. НАВИГАЦИЯ: ПОИСК И ОТКРЫТИЕ НУЖНОГО ЧАТА
# ---------------------------------------------------------------------------

# Кнопка/иконка поиска по чатам в левой панели.
SEARCH_TRIGGER: list[str] = [
    '[aria-label*="оиск" ]',
    '[aria-label*="Найти" ]',
    'input[placeholder*="оиск" i]',
    '[class*="search" i]',
]

# Поле ввода строки поиска (появляется после клика по SEARCH_TRIGGER или сразу).
SEARCH_INPUT: list[str] = [
    'input[placeholder*="оиск" i]',
    'input[placeholder*="Найти" i]',
    'input[type="search"]',
    '[role="searchbox"]',
    '[contenteditable="true"][role="textbox"]',
]

# Строка результата поиска / элемент списка чатов с нужным названием.
# Текст названия подставляется в :has-text("<MAX_CHAT_TITLE>").
CHAT_ITEM: list[str] = [
    '[role="listitem"]:has-text("{title}")',
    'div[class*="chat" i]:has-text("{title}")',
    'div[class*="dialog" i]:has-text("{title}")',
    'a:has-text("{title}")',
    'li:has-text("{title}")',
]

# Контейнер открытого чата (для проверки, что чат действительно открыт).
# Ждём появления внутри него поля ввода сообщения.
CHAT_OPENED_MARKERS: list[str] = [
    '[contenteditable="true"]',
    'textarea[placeholder*="общение"]',
    '[class*="composer"]',
]

# !!!!! ОЧЕНЬ ВАЖНЫЙ СЕЛЕКТОР !!!!!
# Область ЛЕНТЫ ОТКРЫТОГО чата, внутри которой ищутся сообщения.
# Нужен, чтобы мост не читал превью сообщений из левого списка диалогов
# (они матчатся тем же `div[class*="message"]`). В DevTools найдите
# вертикальный скролл-контейнер сообщений (обыко class*="list", "scroller",
# "chat-body", "messages") и проверьте:
#     document.querySelector('div[class*="chat-body"]').querySelectorAll('…текст…').length
# Если ни один кандидат не найдётся, JS-мост сканирует весь document —
# в этом случае возможны дубли из превью, поэтому подберите селектор.
CHAT_SCOPE: list[str] = [
    '[data-testid="conversation-view"]',
    'div[class*="chat-body"]',
    'div[class*="message-list"]',
    'div[class*="messages-list"]',
    'div[class*="conversation"]',
    'div[class*="chat-content"]',
]


# ---------------------------------------------------------------------------
# 3. ЛЕНТА СООБЩЕНИЙ (MAX -> Telegram)
# ---------------------------------------------------------------------------

# Корпус одного сообщения. ОБЯЗАТЕЛЬНО: у каждого кандидата должен быть
# «вложенный» признак текста (см. MESSAGE_TEXT) — у MAX вложенные узлы тоже
# матчатся по `class*="message"`, лишние отсекает внутренний фильтр
# «элемент содержит элемент-кандидата из MESSAGE_TEXT» в app/max_dom.py.
MESSAGE_NODE: list[str] = [
    '[data-message-id]',
    '[data-message-uid]',
    '[data-testid="message"]',
    '[data-msg-id]',
    'div[class*="message-text"]',
    'div[class*="message"] >> nth=0',   # уточняется внутренним фильтром
    'div[class*="bubble"]',
    'div[class*="chat-item"]',
]

# Блок с текстом сообщения внутри MESSAGE_NODE.
MESSAGE_TEXT: list[str] = [
    '[data-testid="message-text"]',
    'div[class*="message-content"]',
    'span[class*="message"]',
    'div[class*="text-container"]',
    'div[class*="textContainer"]',
    'div[class*="message-text"]',
    'div[class*="messageText"]',
]

# Имя отправителя внутри MESSAGE_NODE (для групповых чатов).
MESSAGE_AUTHOR: list[str] = [
    '[data-testid="message-author"]',
    'div[class*="author" i]',
    'div[class*="sender" i]',
    'div[class*="peer-name" i]',
    'span[class*="author" i]',
    'span[class*="sender" i]',
    'div[class*="dialog-title" i]',
]

# Картинки (вложенные в MESSAGE_NODE). Ловит и <img src=blob:...>,
# и <img src=https://...>, и превью-ссылки, и div с background-image.
MESSAGE_IMAGES: list[str] = [
    'img[src^="blob:"]',
    'img[src^="https://"]',
    'img[src^="data:image"]',
    'a[href^="https://"] img',
    'div[class*="image" i]',
    'div[class*="photo" i]',
    'div[class*="thumb" i]',
]

# «Дата/время» сообщения — используется только как часть ключа дедупликации,
# если у сообщения нет data-message-id.
MESSAGE_META: list[str] = [
    'time',
    '[datetime]',
    '[class*="time" i]',
    '[class*="date" i]',
]

# Индикатор «печатает…»: пока он виден, сообщение в ленте может быть
# неполным — мост даёт ему ещё пару секунд.
TYPING_INDICATOR: list[str] = [
    '[class*="typing" i]',
    '[class*="typing-progress" i]',
    'div:has-text("печатает")',
]

# Подсказки «исходящее (моё) сообщение»: подстроки class/id сообщения,
# по которым JS-мост помечает message.mine = true (защита от эхо-петли).
# В DevTools кликните по СВОЕМУ отправленному сообщению и посмотрите, чем
# его класс/атрибуты отличаются от входящего (часто "-out", "outgoing",
# "self", "sent", "mine") — допишите подстроку сюда.
MINE_MARKERS: list[str] = [
    "message-out",
    "outgoing",
    "-out-",
    "self",
    "sent",
    "mine",
]

# ---------------------------------------------------------------------------
# 4. КОМПОЗЕР: ОТПРАВКА (Telegram -> MAX)
# ---------------------------------------------------------------------------

# Поле ввода нового сообщения. Приоритет у contenteditable (React-десктоп),
# textarea — если MAX вернётся к классическому инпуту.
COMPOSER: list[str] = [
    '[contenteditable="true"][role="textbox"]',
    '[contenteditable="true"]:not([data-placeholder=""])',
    '[contenteditable="true"]',
    'textarea[placeholder*="общение" i]',
    'textarea[placeholder*="Сообщение"]',
    'div[role="textbox"]',
    'input[placeholder*="Сообщение"]',
]

# Кнопка «Прикрепить» (нужна, только если set_input_files по какому-то
# пути не сработал и вкладку файлов надо открывать руками).
ATTACH_BUTTON: list[str] = [
    '[aria-label*="репить" ]',
    '[aria-label*="акрепить" ]',
    '[aria-label*="фото" i]',
    '[aria-label*="файл" i]',
    'button[class*="attach" i]',
    '[class*="attach" i]',
]

# Файловой инпут. Playwright умеет setInputFiles и в display:none инпуты.
FILE_INPUT: list[str] = [
    'input[type="file"]',
    'form input[type="file"]',
    'input[accept*="image"]',
]

# Кнопка «Отправить». По умолчанию мост отправляет Enter'ом (как человек);
# кнопка — fallback, если по ENDL keydown не перехватился приложением.
SEND_BUTTON: list[str] = [
    '[aria-label*="тправить" ]',
    '[data-testid="send-message"]',
    'button[class*="send" i]',
    'button[class*="send-message" i]',
    'div[class*="send" i]',
]

# Кнопка «Ответить» (стрелка), появляющаяся при наведении на сообщение.
# Используется в режиме reply:TG->MAX best-effort; если не найдена — мост
# отправит обычное сообщение с цитатой «Ответ на: …» текстом.
REPLY_CONTROL: list[str] = [
    '[aria-label*="ветить"]',
    '[data-testid="reply"]',
    'button[class*="reply" i]',
    'div[class*="reply" i]',
    '[class*="context-menu"] :has-text("Ответить")',
]

# Индикатор «файл прикреплён / превью загрузки» рядом с композером.
# По нему мост понимает, что картинка из Telegram загрузилась в MAX,
# и можно отправлять сообщение.
ATTACH_PREVIEW: list[str] = [
    '[class*="attach"] img',
    '[class*="attachment"] img',
    '[class*="preview"] img',
    'div[class*="composer"] img[src^="blob:"]',
    '[class*="draft"] img[src^="blob:"]',
    'img[src^="blob:"]',
]

# Атрибуты сообщения, по которым мост строит УСТОЙЧИВЫЙ id (дедупликация
# при перезапусках). Первый непустой атрибут у MESSAGE_NODE или его родителя.
MESSAGE_ID_ATTRS: list[str] = [
    "data-message-id",
    "data-message-uid",
    "data-testid",
    "data-msg-id",
    "id",
]

# Ограничитель: сообщения, у которых текст нулевой длины и нет картинок,
# игнорируются (системные «Х вступил в чат» и т.п.)
IGNORE_SYSTEM_MARKERS: list[str] = [
    "вступил",
    "покинул",
    "закрепил",
    "изменил название",
    "изменил аватар",
    "добавил",
    "удалил",
]
