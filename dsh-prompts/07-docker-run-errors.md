docker compose logs -f bridge
max-tg-bridge  | 2026-11-16 08:00:22,622 | INFO    | store        | Создана свежая v3-база
max-tg-bridge  | 2026-11-16 08:00:22,676 | ERROR   | main         | Фатальная ошибка
max-tg-bridge  | Traceback (most recent call last):
max-tg-bridge  |   File "/app/app/main.py", line 133, in amain
max-tg-bridge  |     await runtime.run()
max-tg-bridge  |   File "/app/app/main.py", line 58, in run
max-tg-bridge  |     auth = auth_fsm.register(auth_router, self.cfg, self.store, self.tg, self.mgr)
max-tg-bridge  |   File "/app/app/tg/auth_fsm.py", line 362, in register
max-tg-bridge  |     @router.message(F.text.lower().in_("отмена", "cancel") &
max-tg-bridge  | TypeError: MagicFilter.in_() takes 2 positional arguments but 3 were given
max-tg-bridge  | 2026-11-16 08:00:22,676 | INFO    | main         | Останавливаюсь…
max-tg-bridge  | 2026-11-16 08:00:24,327 | INFO    | main         | Все ресурсы освобождены
max-tg-bridge  | 2026-11-16 08:00:24,409 | ERROR   | asyncio      | Fatal error on SSL transport
max-tg-bridge  | protocol: <asyncio.sslproto.SSLProtocol object at 0x7ba117dd3c70>
max-tg-bridge  | transport: <_SelectorSocketTransport closing fd=9>
max-tg-bridge  | Traceback (most recent call last):
max-tg-bridge  |   File "/usr/lib/python3.10/asyncio/selector_events.py", line 924, in write
max-tg-bridge  |     n = self._sock.send(data)
max-tg-bridge  | OSError: [Errno 9] Bad file descriptor
max-tg-bridge  | 
max-tg-bridge  | During handling of the above exception, another exception occurred:
max-tg-bridge  | 
max-tg-bridge  | Traceback (most recent call last):
max-tg-bridge  |   File "/usr/lib/python3.10/asyncio/sslproto.py", line 690, in _process_write_backlog
max-tg-bridge  |     self._transport.write(chunk)
max-tg-bridge  |   File "/usr/lib/python3.10/asyncio/selector_events.py", line 930, in write
max-tg-bridge  |     self._fatal_error(exc, 'Fatal write error on socket transport')
max-tg-bridge  |   File "/usr/lib/python3.10/asyncio/selector_events.py", line 725, in _fatal_error
max-tg-bridge  |     self._force_close(exc)
max-tg-bridge  |   File "/usr/lib/python3.10/asyncio/selector_events.py", line 737, in _force_close
max-tg-bridge  |     self._loop.call_soon(self._call_connection_lost, exc)
max-tg-bridge  |   File "/usr/lib/python3.10/asyncio/base_events.py", line 753, in call_soon
max-tg-bridge  |     self._check_closed()
max-tg-bridge  |   File "/usr/lib/python3.10/asyncio/base_events.py", line 515, in _check_closed
max-tg-bridge  |     raise RuntimeError('Event loop is closed')
max-tg-bridge  | RuntimeError: Event loop is closed
max-tg-bridge  | 2026-11-16 08:00:24,411 | ERROR   | asyncio      | Unclosed client session
max-tg-bridge  | client_session: <aiohttp.client.ClientSession object at 0x7ba117dd1d80>
max-tg-bridge  | 2026-11-16 08:00:24,411 | ERROR   | asyncio      | Unclosed connector
max-tg-bridge  | connections: ['deque([(<aiohttp.client_proto.ResponseHandler object at 0x7ba117e824c0>, 22843.830587562)])']
max-tg-bridge  | connector: <aiohttp.connector.TCPConnector object at 0x7ba117dd1de0>
max-tg-bridge  | 2026-11-16 08:00:28,268 | ERROR   | main         | Фатальная ошибка
max-tg-bridge  | Traceback (most recent call last):
max-tg-bridge  |   File "/app/app/main.py", line 133, in amain
max-tg-bridge  |     await runtime.run()
max-tg-bridge  |   File "/app/app/main.py", line 58, in run
max-tg-bridge  |     auth = auth_fsm.register(auth_router, self.cfg, self.store, self.tg, self.mgr)
max-tg-bridge  |   File "/app/app/tg/auth_fsm.py", line 362, in register
max-tg-bridge  |     @router.message(F.text.lower().in_("отмена", "cancel") &
max-tg-bridge  | TypeError: MagicFilter.in_() takes 2 positional arguments but 3 were given
max-tg-bridge  | 2026-11-16 08:00:28,268 | INFO    | main         | Останавливаюсь…
max-tg-bridge  | 2026-11-16 08:00:29,845 | INFO    | main         | Все ресурсы освобождены
max-tg-bridge  | 2026-11-16 08:00:29,930 | ERROR   | asyncio      | Fatal error on SSL transport
max-tg-bridge  | protocol: <asyncio.sslproto.SSLProtocol object at 0x7da325ec3ca0>
max-tg-bridge  | transport: <_SelectorSocketTransport closing fd=9>
max-tg-bridge  | Traceback (most recent call last):
max-tg-bridge  |   File "/usr/lib/python3.10/asyncio/selector_events.py", line 924, in write
max-tg-bridge  |     n = self._sock.send(data)
max-tg-bridge  | OSError: [Errno 9] Bad file descriptor
