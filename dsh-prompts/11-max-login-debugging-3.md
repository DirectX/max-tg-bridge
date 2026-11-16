Пока полностью workflow не проходится, зависает на моменте ввода номера.

Также после появлении сообщения:

```
3/3. Telegram ID Владельца — кому прилетают сообщения и кто может отвечать в MAX?
  Целое число (свой /id). «минус» нельзя — только личный user id.
  Вы — владелец? Отправьте свой id или 'me'.
  Отправьте «отмена» или /cancel.
```
и ввода `me` в логе сыплются ошибки

ОБщий лог

```
max-tg-bridge  | 2026-11-16 19:32:11,659 | INFO    | store        | Создана свежая v3-база
max-tg-bridge  | 2026-11-16 19:32:14,262 | INFO    | tg.core      | Telegram: авторизовался @maxtgbridgebot через https://tgapi.geoctx.com/Eak0ibu6eoj8The6IMeimeoch5nuo7ae (admin=107153384)
max-tg-bridge  | 2026-11-16 19:32:14,263 | INFO    | main         | Мост v2 работает. Аккаунтов в работе: []
max-tg-bridge  | 2026-11-16 19:32:14,268 | INFO    | backup       | Бэкап-планировщик: ежедневно 04:00, хранение 7, dir=/data/backups
max-tg-bridge  | 2026-11-16 19:32:14,804 | INFO    | aiogram.dispatcher | Start polling
max-tg-bridge  | 2026-11-16 19:32:15,324 | INFO    | aiogram.dispatcher | Run polling for bot @maxtgbridgebot id=9031741939 - 'maxtgbridgebot'
max-tg-bridge  | 2026-11-16 19:32:48,160 | INFO    | aiogram.event | Update id=876419824 is handled. Duration 3147 ms by bot id=9031741939
max-tg-bridge  | 2026-11-16 19:33:20,660 | INFO    | aiogram.event | Update id=876419827 is handled. Duration 2114 ms by bot id=9031741939
max-tg-bridge  | 2026-11-16 19:33:33,547 | INFO    | aiogram.event | Update id=876419828 is handled. Duration 600 ms by bot id=9031741939
max-tg-bridge  | 2026-11-16 19:33:48,202 | INFO    | aiogram.event | Update id=876419829 is handled. Duration 619 ms by bot id=9031741939
max-tg-bridge  | 2026-11-16 19:33:52,026 | INFO    | aiogram.event | Update id=876419825 is not handled. Duration 60175 ms by bot id=9031741939
max-tg-bridge  | 2026-11-16 19:33:52,028 | ERROR   | aiogram.event | Cause exception while process update id=876419825 by bot id=9031741939
max-tg-bridge  | TelegramNetworkError: HTTP Client says - Request timeout error
max-tg-bridge  | Traceback (most recent call last):
max-tg-bridge  |   File "/usr/local/lib/python3.12/dist-packages/aiohttp/client_reqrep.py", line 745, in start
max-tg-bridge  |     message, payload = await protocol.read()  # type: ignore[union-attr]
max-tg-bridge  |                        ^^^^^^^^^^^^^^^^^^^^^
max-tg-bridge  |   File "/usr/local/lib/python3.12/dist-packages/aiohttp/streams.py", line 716, in read
max-tg-bridge  |     await self._waiter
max-tg-bridge  | asyncio.exceptions.CancelledError
max-tg-bridge  | 
max-tg-bridge  | The above exception was the direct cause of the following exception:
max-tg-bridge  | 
max-tg-bridge  | Traceback (most recent call last):
max-tg-bridge  |   File "/usr/local/lib/python3.12/dist-packages/aiogram/client/session/aiohttp.py", line 173, in make_request
max-tg-bridge  |     async with session.post(
max-tg-bridge  |   File "/usr/local/lib/python3.12/dist-packages/aiohttp/client.py", line 1756, in __aenter__
max-tg-bridge  |     self._resp: _RetType_co = await self._coro
max-tg-bridge  |                               ^^^^^^^^^^^^^^^^
max-tg-bridge  |   File "/usr/local/lib/python3.12/dist-packages/aiohttp/client.py", line 900, in _request
max-tg-bridge  |     resp = await handler(req)
max-tg-bridge  |            ^^^^^^^^^^^^^^^^^^
max-tg-bridge  |   File "/usr/local/lib/python3.12/dist-packages/aiohttp/client.py", line 878, in _connect_and_send_request
max-tg-bridge  |     await resp.start(conn)
max-tg-bridge  |   File "/usr/local/lib/python3.12/dist-packages/aiohttp/client_reqrep.py", line 740, in start
max-tg-bridge  |     with self._timer:
max-tg-bridge  |   File "/usr/local/lib/python3.12/dist-packages/aiohttp/helpers.py", line 759, in __exit__
max-tg-bridge  |     raise asyncio.TimeoutError from exc_val
max-tg-bridge  | TimeoutError
max-tg-bridge  | 
max-tg-bridge  | The above exception was the direct cause of the following exception:
max-tg-bridge  | 
max-tg-bridge  | Traceback (most recent call last):
max-tg-bridge  |   File "/usr/local/lib/python3.12/dist-packages/aiogram/dispatcher/dispatcher.py", line 325, in _process_update
max-tg-bridge  |     response = await self.feed_update(bot, update, **kwargs)
max-tg-bridge  |                ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
max-tg-bridge  |   File "/usr/local/lib/python3.12/dist-packages/aiogram/dispatcher/dispatcher.py", line 168, in feed_update
max-tg-bridge  |     response = await self.update.wrap_outer_middleware(
max-tg-bridge  |                ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
max-tg-bridge  |   File "/usr/local/lib/python3.12/dist-packages/aiogram/dispatcher/middlewares/error.py", line 27, in __call__
max-tg-bridge  |     return await handler(event, data)
max-tg-bridge  |            ^^^^^^^^^^^^^^^^^^^^^^^^^^
max-tg-bridge  |   File "/usr/local/lib/python3.12/dist-packages/aiogram/dispatcher/middlewares/user_context.py", line 60, in __call__
max-tg-bridge  |     return await handler(event, data)
max-tg-bridge  |            ^^^^^^^^^^^^^^^^^^^^^^^^^^
max-tg-bridge  |   File "/usr/local/lib/python3.12/dist-packages/aiogram/fsm/middleware.py", line 55, in __call__
max-tg-bridge  |     return await handler(event, data)
max-tg-bridge  |            ^^^^^^^^^^^^^^^^^^^^^^^^^^
max-tg-bridge  |   File "/usr/local/lib/python3.12/dist-packages/aiogram/dispatcher/event/telegram.py", line 126, in trigger
max-tg-bridge  |     return await wrapped_inner(event, kwargs)
max-tg-bridge  |            ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
max-tg-bridge  |   File "/usr/local/lib/python3.12/dist-packages/aiogram/dispatcher/event/handler.py", line 76, in call
max-tg-bridge  |     return await wrapped()
max-tg-bridge  |            ^^^^^^^^^^^^^^^
max-tg-bridge  |   File "/usr/local/lib/python3.12/dist-packages/aiogram/dispatcher/dispatcher.py", line 288, in _listen_update
max-tg-bridge  |     return await self.propagate_event(update_type=update_type, event=event, **kwargs)
max-tg-bridge  |            ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
max-tg-bridge  |   File "/usr/local/lib/python3.12/dist-packages/aiogram/dispatcher/router.py", line 179, in propagate_event
max-tg-bridge  |     return await observer.wrap_outer_middleware(_wrapped, event=event, data=kwargs)
max-tg-bridge  |            ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
max-tg-bridge  |   File "/usr/local/lib/python3.12/dist-packages/aiogram/dispatcher/router.py", line 171, in _wrapped
max-tg-bridge  |     return await self._propagate_event(
max-tg-bridge  |            ^^^^^^^^^^^^^^^^^^^^^^^^^^^^
max-tg-bridge  |   File "/usr/local/lib/python3.12/dist-packages/aiogram/dispatcher/router.py", line 207, in _propagate_event
max-tg-bridge  |     response = await router.propagate_event(update_type=update_type, event=event, **kwargs)
max-tg-bridge  |                ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
max-tg-bridge  |   File "/usr/local/lib/python3.12/dist-packages/aiogram/dispatcher/router.py", line 179, in propagate_event
max-tg-bridge  |     return await observer.wrap_outer_middleware(_wrapped, event=event, data=kwargs)
max-tg-bridge  |            ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
max-tg-bridge  |   File "/usr/local/lib/python3.12/dist-packages/aiogram/dispatcher/router.py", line 171, in _wrapped
max-tg-bridge  |     return await self._propagate_event(
max-tg-bridge  |            ^^^^^^^^^^^^^^^^^^^^^^^^^^^^
max-tg-bridge  |   File "/usr/local/lib/python3.12/dist-packages/aiogram/dispatcher/router.py", line 199, in _propagate_event
max-tg-bridge  |     response = await observer.trigger(event, **kwargs)
max-tg-bridge  |                ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
max-tg-bridge  |   File "/usr/local/lib/python3.12/dist-packages/aiogram/dispatcher/event/telegram.py", line 126, in trigger
max-tg-bridge  |     return await wrapped_inner(event, kwargs)
max-tg-bridge  |            ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
max-tg-bridge  |   File "/usr/local/lib/python3.12/dist-packages/aiogram/dispatcher/event/handler.py", line 76, in call
max-tg-bridge  |     return await wrapped()
max-tg-bridge  |            ^^^^^^^^^^^^^^^
max-tg-bridge  |   File "/app/app/tg/admin_handlers.py", line 104, in cmd_admin
max-tg-bridge  |     await message.answer(
max-tg-bridge  |   File "/usr/local/lib/python3.12/dist-packages/aiogram/methods/base.py", line 65, in emit
max-tg-bridge  |     return await bot(self)
max-tg-bridge  |            ^^^^^^^^^^^^^^^
max-tg-bridge  |   File "/usr/local/lib/python3.12/dist-packages/aiogram/client/bot.py", line 579, in __call__
max-tg-bridge  |     return await self.session(self, method, timeout=request_timeout)
max-tg-bridge  |            ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
max-tg-bridge  |   File "/usr/local/lib/python3.12/dist-packages/aiogram/client/session/base.py", line 268, in __call__
max-tg-bridge  |     return cast(TelegramType, await middleware(bot, method))
max-tg-bridge  |                               ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
max-tg-bridge  |   File "/usr/local/lib/python3.12/dist-packages/aiogram/client/session/aiohttp.py", line 180, in make_request
max-tg-bridge  |     raise TelegramNetworkError(method=method, message="Request timeout error") from e
max-tg-bridge  | aiogram.exceptions.TelegramNetworkError: HTTP Client says - Request timeout error
max-tg-bridge  | 2026-11-16 19:33:56,028 | INFO    | aiogram.event | Update id=[redactred] is not handled. Duration 61622 ms by bot id=[redactred]
max-tg-bridge  | 2026-11-16 19:33:56,030 | ERROR   | aiogram.event | Cause exception while process update id=[redactred] by bot id=[redactred]
max-tg-bridge  | TelegramNetworkError: HTTP Client says - Request timeout error
max-tg-bridge  | Traceback (most recent call last):
max-tg-bridge  |   File "/usr/local/lib/python3.12/dist-packages/aiohttp/client_reqrep.py", line 745, in start
max-tg-bridge  |     message, payload = await protocol.read()  # type: ignore[union-attr]
max-tg-bridge  |                        ^^^^^^^^^^^^^^^^^^^^^
max-tg-bridge  |   File "/usr/local/lib/python3.12/dist-packages/aiohttp/streams.py", line 716, in read
max-tg-bridge  |     await self._waiter
max-tg-bridge  | asyncio.exceptions.CancelledError
max-tg-bridge  | 
max-tg-bridge  | The above exception was the direct cause of the following exception:
max-tg-bridge  | 
max-tg-bridge  | Traceback (most recent call last):
max-tg-bridge  |   File "/usr/local/lib/python3.12/dist-packages/aiogram/client/session/aiohttp.py", line 173, in make_request
max-tg-bridge  |     async with session.post(
max-tg-bridge  |   File "/usr/local/lib/python3.12/dist-packages/aiohttp/client.py", line 1756, in __aenter__
max-tg-bridge  |     self._resp: _RetType_co = await self._coro
max-tg-bridge  |                               ^^^^^^^^^^^^^^^^
max-tg-bridge  |   File "/usr/local/lib/python3.12/dist-packages/aiohttp/client.py", line 900, in _request
max-tg-bridge  |     resp = await handler(req)
max-tg-bridge  |            ^^^^^^^^^^^^^^^^^^
max-tg-bridge  |   File "/usr/local/lib/python3.12/dist-packages/aiohttp/client.py", line 878, in _connect_and_send_request
max-tg-bridge  |     await resp.start(conn)
max-tg-bridge  |   File "/usr/local/lib/python3.12/dist-packages/aiohttp/client_reqrep.py", line 740, in start
max-tg-bridge  |     with self._timer:
max-tg-bridge  |   File "/usr/local/lib/python3.12/dist-packages/aiohttp/helpers.py", line 759, in __exit__
max-tg-bridge  |     raise asyncio.TimeoutError from exc_val
max-tg-bridge  | TimeoutError
max-tg-bridge  | 
max-tg-bridge  | The above exception was the direct cause of the following exception:
max-tg-bridge  | 
max-tg-bridge  | Traceback (most recent call last):
max-tg-bridge  |   File "/usr/local/lib/python3.12/dist-packages/aiogram/dispatcher/dispatcher.py", line 325, in _process_update
max-tg-bridge  |     response = await self.feed_update(bot, update, **kwargs)
max-tg-bridge  |                ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
max-tg-bridge  |   File "/usr/local/lib/python3.12/dist-packages/aiogram/dispatcher/dispatcher.py", line 168, in feed_update
max-tg-bridge  |     response = await self.update.wrap_outer_middleware(
max-tg-bridge  |                ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
max-tg-bridge  |   File "/usr/local/lib/python3.12/dist-packages/aiogram/dispatcher/middlewares/error.py", line 27, in __call__
max-tg-bridge  |     return await handler(event, data)
max-tg-bridge  |            ^^^^^^^^^^^^^^^^^^^^^^^^^^
max-tg-bridge  |   File "/usr/local/lib/python3.12/dist-packages/aiogram/dispatcher/middlewares/user_context.py", line 60, in __call__
max-tg-bridge  |     return await handler(event, data)
max-tg-bridge  |            ^^^^^^^^^^^^^^^^^^^^^^^^^^
max-tg-bridge  |   File "/usr/local/lib/python3.12/dist-packages/aiogram/fsm/middleware.py", line 55, in __call__
max-tg-bridge  |     return await handler(event, data)
max-tg-bridge  |            ^^^^^^^^^^^^^^^^^^^^^^^^^^
max-tg-bridge  |   File "/usr/local/lib/python3.12/dist-packages/aiogram/dispatcher/event/telegram.py", line 126, in trigger
max-tg-bridge  |     return await wrapped_inner(event, kwargs)
max-tg-bridge  |            ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
max-tg-bridge  |   File "/usr/local/lib/python3.12/dist-packages/aiogram/dispatcher/event/handler.py", line 76, in call
max-tg-bridge  |     return await wrapped()
max-tg-bridge  |            ^^^^^^^^^^^^^^^
max-tg-bridge  |   File "/usr/local/lib/python3.12/dist-packages/aiogram/dispatcher/dispatcher.py", line 288, in _listen_update
max-tg-bridge  |     return await self.propagate_event(update_type=update_type, event=event, **kwargs)
max-tg-bridge  |            ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
max-tg-bridge  |   File "/usr/local/lib/python3.12/dist-packages/aiogram/dispatcher/router.py", line 179, in propagate_event
max-tg-bridge  |     return await observer.wrap_outer_middleware(_wrapped, event=event, data=kwargs)
max-tg-bridge  |            ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
max-tg-bridge  |   File "/usr/local/lib/python3.12/dist-packages/aiogram/dispatcher/router.py", line 171, in _wrapped
max-tg-bridge  |     return await self._propagate_event(
max-tg-bridge  |            ^^^^^^^^^^^^^^^^^^^^^^^^^^^^
max-tg-bridge  |   File "/usr/local/lib/python3.12/dist-packages/aiogram/dispatcher/router.py", line 207, in _propagate_event
max-tg-bridge  |     response = await router.propagate_event(update_type=update_type, event=event, **kwargs)
max-tg-bridge  |                ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
max-tg-bridge  |   File "/usr/local/lib/python3.12/dist-packages/aiogram/dispatcher/router.py", line 179, in propagate_event
max-tg-bridge  |     return await observer.wrap_outer_middleware(_wrapped, event=event, data=kwargs)
max-tg-bridge  |            ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
max-tg-bridge  |   File "/usr/local/lib/python3.12/dist-packages/aiogram/dispatcher/router.py", line 171, in _wrapped
max-tg-bridge  |     return await self._propagate_event(
max-tg-bridge  |            ^^^^^^^^^^^^^^^^^^^^^^^^^^^^
max-tg-bridge  |   File "/usr/local/lib/python3.12/dist-packages/aiogram/dispatcher/router.py", line 199, in _propagate_event
max-tg-bridge  |     response = await observer.trigger(event, **kwargs)
max-tg-bridge  |                ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
max-tg-bridge  |   File "/usr/local/lib/python3.12/dist-packages/aiogram/dispatcher/event/telegram.py", line 126, in trigger
max-tg-bridge  |     return await wrapped_inner(event, kwargs)
max-tg-bridge  |            ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
max-tg-bridge  |   File "/usr/local/lib/python3.12/dist-packages/aiogram/dispatcher/event/handler.py", line 76, in call
max-tg-bridge  |     return await wrapped()
max-tg-bridge  |            ^^^^^^^^^^^^^^^
max-tg-bridge  |   File "/app/app/tg/admin_handlers.py", line 128, in cb_add
max-tg-bridge  |     await auth.start_add_account(callback.from_user.id, state)
max-tg-bridge  |   File "/app/app/tg/auth_fsm.py", line 83, in start_add_account
max-tg-bridge  |     await tg.bot.send_message(
max-tg-bridge  |   File "/usr/local/lib/python3.12/dist-packages/aiogram/client/bot.py", line 3077, in send_message
max-tg-bridge  |     return await self(call, request_timeout=request_timeout)
max-tg-bridge  |            ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
max-tg-bridge  |   File "/usr/local/lib/python3.12/dist-packages/aiogram/client/bot.py", line 579, in __call__
max-tg-bridge  |     return await self.session(self, method, timeout=request_timeout)
max-tg-bridge  |            ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
max-tg-bridge  |   File "/usr/local/lib/python3.12/dist-packages/aiogram/client/session/base.py", line 268, in __call__
max-tg-bridge  |     return cast(TelegramType, await middleware(bot, method))
max-tg-bridge  |                               ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
max-tg-bridge  |   File "/usr/local/lib/python3.12/dist-packages/aiogram/client/session/aiohttp.py", line 180, in make_request
max-tg-bridge  |     raise TelegramNetworkError(method=method, message="Request timeout error") from e
max-tg-bridge  | aiogram.exceptions.TelegramNetworkError: HTTP Client says - Request timeout error
max-tg-bridge  | 2026-11-16 19:33:59,456 | INFO    | browser.a1   | Chromium запущен (profile=/data/profiles/account_1)
max-tg-bridge  | 2026-11-16 19:34:40,247 | INFO    | max.dom.a1   | Открыл форму ввода телефона (переключатель с QR-экрана)
max-tg-bridge  | 2026-11-16 19:34:41,352 | INFO    | aiogram.event | Update id=[redactred] is handled. Duration 42717 ms by bot id=[redactred]
max-tg-bridge  | 2026-11-16 19:35:09,026 | ERROR   | aiogram.dispatcher | Failed to fetch updates - TelegramNetworkError: HTTP Client says - Request timeout error
max-tg-bridge  | 2026-11-16 19:35:09,026 | WARNING | aiogram.dispatcher | Sleep for 1.000000 seconds and try again... (tryings = 0, bot id = [redactred])
max-tg-bridge  | 2026-11-16 19:35:11,436 | INFO    | aiogram.dispatcher | Connection established (tryings = 1, bot id = [redactred])
max-tg-bridge  | 2026-11-16 19:36:20,024 | WARNING | tg.core      | Telegram send упал (HTTP Client says - Request timeout error), попытка 1/4, спим 2 c
max-tg-bridge  | 2026-11-16 19:37:23,031 | WARNING | tg.core      | Telegram send упал (HTTP Client says - Request timeout error), попытка 2/4, спим 4 c
max-tg-bridge  | 2026-11-16 19:37:46,023 | ERROR   | aiogram.dispatcher | Failed to fetch updates - TelegramNetworkError: HTTP Client says - Request timeout error
max-tg-bridge  | 2026-11-16 19:37:46,023 | WARNING | aiogram.dispatcher | Sleep for 1.000000 seconds and try again... (tryings = 0, bot id = [redactred])
max-tg-bridge  | 2026-11-16 19:37:57,999 | INFO    | aiogram.dispatcher | Connection established (tryings = 1, bot id = [redactred])
max-tg-bridge  | 2026-11-16 19:38:28,025 | WARNING | tg.core      | Telegram send упал (HTTP Client says - Request timeout error), попытка 3/4, спим 8 c
```