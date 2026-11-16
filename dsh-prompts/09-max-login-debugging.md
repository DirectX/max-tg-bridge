Теперь заработало на OкangePi в rootful режиме.

Два вопроса:

1. При вводе /admin почему-то не срабатывает форматирование, возможно вместо <b> нужно **

```
🤖 <b>Панель управления</b> (аккаунтов: 0, в работе: 0)
```

2. Не получается залогиниться:

```
⚠️ Ввод телефона не удался: Поле ввода телефона не найдено (селекторы PHONE_INPUT) — скриншот: auth_a1_no_phone_input_134424.png
Отправьте «отмена» или /cancel.
```
На скриншоте написано: 
```
QR-код устарел
Обновите его, чтобы войти в профиль или скачать приложение

Авторизационная сессия не найдена.

Войти по номеру телефона
```

При первом входе текст на этой странице такой:

```
Войдите в MAX по QR-коду
Наведите камеру на QR-код, чтобы войти в профиль или скачать приложение

Войти по номеру телефона
```

Возможно нужно скорректировать workflow и проследовать по ссылке `Войти по номеру телефона`. Код этой кнопки на странце такой:

```
<span class="content svelte-13fyxqk"><!----> Войти по номеру телефона<!----> <!----></span>
```

Также в логах такие ошибки:

```
[requestfailed] POST https://sdk-api.apptracer.ru/api/crash/trackSession?crashToken=[REDACTED]&sdkVersion=3.0.1 -> net::ERR_CONNECTION_REFUSED
[console:error] Failed to load resource: net::ERR_CONNECTION_REFUSED
[requestfailed] POST https://sdk-api.apptracer.ru/api/perf/upload?crashToken=[REDACTED]&sdkVersion=3.0.1 -> net::ERR_CONNECTION_REFUSED
[console:error] Failed to load resource: net::ERR_CONNECTION_REFUSED
```