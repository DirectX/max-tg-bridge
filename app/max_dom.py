"""
app/max_dom.py
==============
Адаптер к DOM веб-клиента MAX (web.max.ru). Всё, что знает про верстку MAX,
живёт здесь + в app/selectors.py.

Архитектура чтения сообщений:
    В страницу инжектируется JS-мост `window.__MaxBridge`:
      * MutationObserver по документу ленты + страховочный интервал-скан;
      * каждый найденный «якорь» (текст/картинка сообщения) поднимается до
        границы одного сообщения (пока родитель не начнёт содержать >1 якорь);
      * по устойчивому ключу (data-message-id либо хэш отправитель+текст+время)
        дедуплицирует внутри страницы;
      * новые сообщения складывает в память страницы (queue), Python забирает
        их коротким `drain()` раз в MAX_POLL_INTERVAL секунд (чтение памяти —
        это НЕ сетевой polling, нагрузка на MAX нулевая).

Скачивание картинок: fetch() выполняется ВНУТРИ страницы, поэтому браузер
сам подставляет куки/авторизацию MAX, а blob:-URLы ресурсодоступны только
изнутри вкладки. Результат — base64 -> io.BytesIO -> bot.send_photo.

ВСЕ селекторы берутся из app/selectors.py — правки верстки не должны
требовать изменений здесь.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass, field

from playwright.async_api import Error as PWError
from playwright.async_api import Locator, Page, TimeoutError as PWTimeout

from . import selectors as S
from .config import Config
from .utils import get_logger, human_pause, jitter, normalize_ws

log = get_logger("max.dom")


class MaxAuthError(RuntimeError):
    """Сессия MAX протуха: требуется повторный `python -m app.auth`."""


class MaxChatNotFound(RuntimeError):
    """Не найден/не открылся целевой чат — проверьте MAX_CHAT_TITLE/URL."""


@dataclass(slots=True)
class MaxImage:
    src: str
    width: int = 0
    height: int = 0


@dataclass(slots=True)
class MaxMessage:
    key: str                       # устойчивый id для дедупликации
    sender: str
    text: str
    images: list[MaxImage] = field(default_factory=list)
    mine: bool = False             # исходящее (моё) — для защиты от эхо-петли
    ts: int = 0                    # мс по часам страницы


# ---------------------------------------------------------------------------
# JS-мост, инжектируемый в страницу
# ---------------------------------------------------------------------------
_JS_BRIDGE = r"""
(() => {
  'use strict';
  if (window.__MaxBridge) { window.__MaxBridge.reinstall(); return; }

  const B = {
    cfg: null, queue: [], seen: new Set(), mo: null, timer: null,
    scheduled: false, lastCollect: 0,
  };

  const qa = (sel, root) => {
    try { return Array.prototype.slice.call((root || document).querySelectorAll(sel)); }
    catch (e) { return []; }
  };
  const qf = (sels, root) => {
    for (const s of (sels || [])) { const e = (root || document).querySelector(s); if (e) return e; }
    return null;
  };
  const hash = (str) => {
    let h = 5381;
    for (let i = 0; i < str.length; i++) { h = ((h << 5) + h) ^ str.charCodeAt(i); }
    return (h >>> 0).toString(36);
  };

  function scope() {
    return qf(B.cfg.scope) || document;
  }

  // Элемент-«коробка» одного сообщения: поднимаемся от якоря (текста/картинки)
  // вверх, пока родитель содержит ровно один якорь.
  function anchorSel() { return B.cfg.text.join(',') + ',' + B.cfg.images.join(','); }

  function messageBox(anchor) {
    const sel = anchorSel();
    let box = anchor;
    for (let i = 0; i < 12 && box.parentElement; i++) {
      const p = box.parentElement;
      let n = 0;
      try { n = p.querySelectorAll(sel).length; } catch (e) { break; }
      if (n > 1) break;
      box = p;
    }
    return box;
  }

  function dataId(box) {
    let node = box;
    for (let depth = 0; node && depth < 4; depth++, node = node.parentElement) {
      for (const attr of (B.cfg.idAttrs || [])) {
        if (!node.getAttribute) continue;
        const v = node.getAttribute(attr);
        if (v && v.length <= 128 && (attr !== 'id' || /\d/.test(v))) {
          return attr + '=' + v;
        }
      }
    }
    return '';
  }

  function isMine(box, sender) {
    if (B.cfg.selfName && sender && sender.toLowerCase() === B.cfg.selfName.toLowerCase()) {
      return true;
    }
    let node = box;
    for (let depth = 0; node && depth < 3; depth++, node = node.parentElement) {
      const cls = String(node.className || '') + ' ' + (node.getAttribute && node.getAttribute('dir') || '');
      for (const m of (B.cfg.mineMarkers || [])) {
        if (cls.toLowerCase().includes(m.toLowerCase())) return true;
      }
    }
    return false;
  }

  function collectImages(box) {
    const out = [];
    const seen = new Set();
    for (const sel of B.cfg.images) {
      for (const img of qa(sel, box)) {
        let src = img.src || img.currentSrc || '';
        if (!src && img.style && img.style.backgroundImage) {
          const m = img.style.backgroundImage.match(/url\(["']?(.*?)["']?\)/);
          if (m) src = m[1];
        }
        if (!src || seen.has(src)) continue;
        if (src.startsWith('data:image/svg')) continue;               // иконки
        const w = img.naturalWidth || img.width || 0;
        const h = img.naturalHeight || img.height || 0;
        if (w && w < 56 && h < 56) continue;                          // аватарки/эмодзи
        let isAvatar = false;
        let n = img;
        for (let d = 0; n && d < 4; d++, n = n.parentElement) {
          if (String(n.className || '').toLowerCase().includes('avatar')) { isAvatar = true; break; }
        }
        if (isAvatar) continue;
        seen.add(src);
        out.push({ src, width: w, height: h });
      }
    }
    return out;
  }

  function scan(markOnly) {
    if (!B.cfg) return 0;
    const root = scope();
    // Пока в чате кто-то «печатает», новые узлы могут быть неполными —
    // приостанавливаем сбор максимум на 4 секунды.
    const typing = qf(B.cfg.typing, root);
    if (typing && (Date.now() - B.lastCollect) < 4000) return 0;

    const sel = anchorSel();
    let anchors = [];
    for (const s of (B.cfg.text.concat(B.cfg.images))) {
      for (const el of qa(s, root)) {
        if (!el.querySelector(sel)) anchors.push(el);   // только листья
      }
    }
    let added = 0;
    const done = new Set();
    for (const anchor of anchors) {
      const box = messageBox(anchor);
      if (done.has(box)) continue;
      done.add(box);

      const textEl = box.querySelector(B.cfg.text.join(','));
      const text = textEl ? (textEl.innerText || '').replace(/\u00a0/g, ' ').trim() : '';
      const imgs = collectImages(box);
      if (!text && imgs.length === 0) continue;

      const authorEl = box.querySelector(B.cfg.author.join(','));
      const sender = authorEl ? (authorEl.innerText || '').trim() : '';
      const metaEl = box.querySelector((B.cfg.meta || []).join(','));
      const meta = metaEl ? (metaEl.getAttribute && metaEl.getAttribute('datetime')) ||
                             (metaEl.innerText || '').trim() : '';

      const id = dataId(box);
      let key;
      if (id) {
        key = id;
      } else if (!text && imgs.length) {
        // фото-без-текста: хэшируем src — иначе все «пустые» сообщения
        // в сессии схлопнулись бы в один ключ
        key = 'h' + hash([sender, meta, imgs.map((i) => i.src).join('|')].join('\u0001'));
      } else {
        key = 'h' + hash([sender, text.split('\n')[0].slice(0, 80), meta].join('\u0001'));
      }
      if (B.seen.has(key)) continue;
      B.seen.add(key);
      if (B.seen.size > 20000) { B.seen = new Set(Array.from(B.seen).slice(-15000)); }

      if (markOnly) { added++; continue; }

      const low = text.toLowerCase();
      if (!imgs.length && (B.cfg.systemMarkers || []).some((m) => low.includes(m))) continue;

      B.queue.push({
        key, sender, text, imgs,
        mine: isMine(box, sender),
        ts: Date.now(),
      });
      if (B.queue.length > 1000) B.queue.splice(0, B.queue.length - 1000);
      added++;
    }
    if (added && !markOnly) B.lastCollect = Date.now();
    return added;
  }

  B.reinstall = function () { B.schedule(); };

  B.install = function (cfg) {
    B.cfg = cfg;
    if (!B.mo) {
      B.mo = new MutationObserver(() => B.schedule());
      B.mo.observe(document.documentElement, { childList: true, subtree: true, characterData: true });
      B.timer = setInterval(() => B.schedule(), 2500);
    }
    B.schedule();
    return true;
  };

  B.schedule = function () {
    if (B.scheduled) return;
    B.scheduled = true;
    setTimeout(() => { B.scheduled = false; try { scan(false); } catch (e) {} }, 120);
  };

  B.seed = function () { scan(true); return B.seen.size; };
  B.drain = function () { return B.queue.splice(0, 60); };
  B.stats = function () { return { seen: B.seen.size, queued: B.queue.length, installed: !!B.cfg }; };

  window.__MaxBridge = B;
})();
"""

# fetch() внутри страницы -> base64 (куки/авторизация MAX подставляются сами)
_JS_FETCH_IMAGE = r"""
async ([src, maxBytes]) => {
  try {
    const resp = await fetch(src, { cache: 'force-cache', credentials: 'include' });
    if (!resp.ok) return { error: 'http_' + resp.status };
    const blob = await resp.blob();
    if (blob.size > maxBytes) return { error: 'too_large', size: blob.size };
    const mime = (blob.type || '').toLowerCase();
    if (mime && !mime.startsWith('image/')) return { error: 'mime:' + mime };
    const buf = await blob.arrayBuffer();
    const bytes = new Uint8Array(buf);
    let bin = '';
    const chunk = 0x8000;
    for (let i = 0; i < bytes.length; i += chunk) {
      bin += String.fromCharCode.apply(null, Array.from(bytes.subarray(i, i + chunk)));
    }
    return { ok: true, mime: mime || 'image/jpeg', size: blob.size, b64: btoa(bin) };
  } catch (e) { return { error: String(e).slice(0, 140) }; }
}
"""

# Находит бокс <img> по src, скроллит в центр и отдаёт координаты
# (fallback: скриншот кусочка страницы, если fetch не пропустили CSP)
_JS_IMG_BOX = r"""
async (src) => {
  const imgs = Array.prototype.slice.call(document.images);
  const img = imgs.find((i) => i.src === src || i.currentSrc === src);
  if (!img) return null;
  try { img.scrollIntoView({ block: 'center', inline: 'center' }); } catch (e) {}
  await new Promise((r) => setTimeout(r, 350));
  const r = img.getBoundingClientRect();
  if (r.width < 16 || r.height < 16) return null;
  return { x: r.x, y: r.y, width: r.width, height: r.height };
}
"""


def _css_only(selectors: list[str]) -> list[str]:
    """Оставляем только чистый CSS (без Playwright-расширений :has-text / >>)."""
    return [s for s in selectors if ":has-text" not in s and ">>" not in s]


# ---------------------------------------------------------------------------


class MaxWeb:
    """Одна вкладка web.max.ru: вход в чат, чтение, отправка."""

    def __init__(self, page: Page, cfg: Config) -> None:
        self.page = page
        self.cfg = cfg
        self._bridge_installed = False

    # ---------------------------------------------------------------- utils
    def _js_config(self) -> dict:
        return {
            "scope": _css_only(S.CHAT_SCOPE),
            "text": _css_only(S.MESSAGE_TEXT),
            "author": _css_only(S.MESSAGE_AUTHOR),
            "images": _css_only(S.MESSAGE_IMAGES),
            "meta": _css_only(S.MESSAGE_META),
            "typing": _css_only(S.TYPING_INDICATOR),
            "idAttrs": S.MESSAGE_ID_ATTRS,
            "mineMarkers": S.MINE_MARKERS,
            "systemMarkers": S.IGNORE_SYSTEM_MARKERS,
            "selfName": self.cfg.max_self_name,
        }

    async def _first(self, candidates: list[str], root: Page | Locator | None = None,
                     timeout_ms: int = 5_000, state: str = "visible") -> Locator | None:
        """Возвращает первый видимый Locator из списка селекторов."""
        for sel in candidates:
            loc = (root or self.page).locator(sel).first
            try:
                await loc.wait_for(state=state, timeout=timeout_ms)
                return loc
            except (PWTimeout, PWError):
                continue
        return None

    async def current_url(self) -> str:
        try:
            return str(await self.page.evaluate("() => location.href"))
        except PWError:
            return self.page.url

    # ------------------------------------------------------------ lifecycle
    async def open_max(self) -> None:
        log.info("Открываю %s", self.cfg.max_url)
        await self.page.goto(self.cfg.max_url, wait_until="domcontentloaded", timeout=90_000)
        # SPA-приложению нужно время на boot + авторизацию по cookies
        marker = await self._first(S.CHAT_OPENED_MARKERS, timeout_ms=25_000, state="attached")
        if marker is None:
            url = await self.current_url()
            raise MaxAuthError(
                "MAX не показывает авторизованный интерфейс "
                f"(url={url}). Выполните первичный вход: docker compose run --rm "
                "bridge python -m app.auth"
            )

    async def open_target_chat(self) -> None:
        """Открывает чат по MAX_CHAT_URL либо поиск => MAX_CHAT_TITLE."""
        if await self._looks_like_login():
            raise MaxAuthError("Редирект на страницу входа — сессия MAX истекла.")

        if self.cfg.max_chat_url:
            log.info("Открываю чат по URL: %s", self.cfg.max_chat_url)
            await self.page.goto(self.cfg.max_chat_url, wait_until="domcontentloaded")
        else:
            await self._open_chat_by_search(self.cfg.max_chat_title)

        composer = await self._first(S.CHAT_OPENED_MARKERS, timeout_ms=20_000)
        if composer is None:
            raise MaxChatNotFound(
                f"Чат {self.cfg.max_chat_title!r} не открылся: проверьте "
                "MAX_CHAT_TITLE / MAX_CHAT_URL и актуальность CHAT_OPENED_MARKERS."
            )
        await self.page.evaluate(_JS_BRIDGE)
        await self.page.evaluate("(cfg) => window.__MaxBridge.install(cfg)", self._js_config())
        seeded = await self.page.evaluate("() => window.__MaxBridge.seed()")
        self._bridge_installed = True
        log.info("Целевой чат открыт, история из %d сообщений помечена как переданная", seeded)

    async def _open_chat_by_search(self, title: str) -> None:
        if not title:
            raise MaxChatNotFound("Не задан ни MAX_CHAT_URL, ни MAX_CHAT_TITLE")
        log.info("Ищу чат %r через поиск", title)

        # Поле поиска может быть сразу на странице — иначе кликаем триггер.
        box = await self._first(S.SEARCH_INPUT, timeout_ms=2_500)
        if box is None:
            trigger = await self._first(S.SEARCH_TRIGGER, timeout_ms=5_000)
            if trigger is None:
                raise MaxChatNotFound("Не нашёл поле/иконку поиска (SEARCH_TRIGGER/INPUT).")
            await trigger.click()
            await jitter(0.4, 0.8)
            box = await self._first(S.SEARCH_INPUT, timeout_ms=8_000)
        if box is None:
            raise MaxChatNotFound("Поиск открылся, но поле ввода не найдено.")

        await box.click()
        await box.fill("")
        await box.press_sequentially(title, delay=90)
        await jitter(1.2, 2.2)  # даём отрисоваться результатам

        item_sel = [t.format(title=title) for t in S.CHAT_ITEM]
        item = await self._first(item_sel, timeout_ms=10_000)
        if item is None:
            raise MaxChatNotFound(
                f"В поиске нет строки с чатом {title!r}. Уточните MAX_CHAT_TITLE "
                "или возьмите точное название из шапки чата."
            )
        await item.click()
        await jitter(0.8, 1.6)

    async def _looks_like_login(self) -> bool:
        url = await self.current_url()
        if any(part in url for part in ("/login", "/auth", "sign-in")):
            return True
        phone = await self._first(S.PHONE_INPUT, timeout_ms=800, state="attached")
        return phone is not None

    # -------------------------------------------------------------- reading
    async def poll(self) -> list[MaxMessage]:
        """Забирает накопленные JS-мостом новые сообщения."""
        if not self._bridge_installed:
            return []
        try:
            raw = await self.page.evaluate("() => window.__MaxBridge.drain()")
        except PWError:
            # страница перезагружается (daily reload) — переинсталируем мост
            self._bridge_installed = False
            raise
        out: list[MaxMessage] = []
        for item in raw or []:
            out.append(
                MaxMessage(
                    key=str(item.get("key", "")),
                    sender=str(item.get("sender") or "").strip(),
                    text=normalize_ws(str(item.get("text") or "")),
                    images=[
                        MaxImage(src=str(i.get("src", "")),
                                 width=int(i.get("width") or 0),
                                 height=int(i.get("height") or 0))
                        for i in item.get("imgs", [])
                    ],
                    mine=bool(item.get("mine")),
                    ts=int(item.get("ts") or 0),
                )
            )
        return out

    async def rearm_bridge(self) -> None:
        """После page.reload(): заново инжектирует мост и сидит историю."""
        await self._first(S.CHAT_OPENED_MARKERS, timeout_ms=30_000)
        await self.page.evaluate(_JS_BRIDGE)
        await self.page.evaluate("(cfg) => window.__MaxBridge.install(cfg)", self._js_config())
        seeded = await self.page.evaluate("() => window.__MaxBridge.seed()")
        self._bridge_installed = True
        log.info("JS-мост переустановлен после перезагрузки (seed=%d)", seeded)

    async def ensure_listening(self) -> bool:
        """Лёгкая самопроверка: жив ли JS-мост в странице. Если SPA перемонтировал
        документ/страница перезагрузилась — переинсталирует. Возвращает True,
        если слушатель активен."""
        if self._bridge_installed:
            try:
                ok = await self.page.evaluate(
                    "() => !!(window.__MaxBridge && window.__MaxBridge.stats().installed)"
                )
                if ok:
                    return True
                log.warning("__MaxBridge исчез из страницы — переустанавливаю")
            except PWError:
                self._bridge_installed = False
        try:
            await self.rearm_bridge()
            return True
        except Exception as exc:  # noqa: BLE001
            log.warning("Переустановить JS-мост не удалось (%s) — watchdog подстрахует", exc)
            return False

    async def health(self) -> bool:
        """Жива ли вкладка и авторизация."""
        try:
            url = await self.current_url()
            if "max.ru" not in url:
                return False
            if await self._looks_like_login():
                return False
            await self.page.evaluate("() => !!document.documentElement")
            return True
        except Exception:  # noqa: BLE001
            return False

    # ------------------------------------------------------------- images
    async def download_image(self, src: str) -> bytes | None:
        """Возвращает байты картинки; None — не скачалось (упадёт тихо)."""
        cfg = self.cfg
        if src.startswith("data:"):
            try:
                header, b64 = src.split(",", 1)
                if len(b64) * 3 / 4 > cfg.max_image_bytes:
                    log.warning("data:-картинка слишком большая")
                    return None
                return base64.b64decode(b64)
            except Exception as exc:  # noqa: BLE001
                log.warning("Не распарсил data-URL: %s", exc)
                return None

        res = await self.page.evaluate(_JS_FETCH_IMAGE, [src, cfg.max_image_bytes])
        if isinstance(res, dict) and res.get("ok"):
            try:
                return base64.b64decode(res["b64"])
            except Exception as exc:  # noqa: BLE001
                log.warning("base64-декод картинки упал: %s", exc)

        log.info("fetch картинки не удался (%s) — пробую скриншот <img>",
                 (res or {}).get("error", "?") if isinstance(res, dict) else res)
        return await self._screenshot_image(src)

    async def _screenshot_image(self, src: str) -> bytes | None:
        try:
            box = await self.page.evaluate(_JS_IMG_BOX, src)
            if not box:
                return None
            png = await self.page.screenshot(clip=box, type="png", timeout=15_000)
            if len(png) > self.cfg.max_image_bytes:
                log.warning("Скриншот картинки больше лимита — пропускаю")
                return None
            return png
        except (PWError, PWTimeout) as exc:
            log.warning("Скриншот картинки не вышел: %s", exc)
            return None

    async def _submit(self) -> None:
        """Enter + подстраховка кнопкой «Отправить», если SPA не перехватила Enter."""
        await self.page.keyboard.press("Enter")
        await jitter(0.4, 0.9)
        try:
            leftover = await self.page.evaluate(
                """(sels) => {
                     for (const s of sels) {
                       const el = document.querySelector(s);
                       if (el && (el.innerText || el.value || '').trim()) return true;
                     }
                     return false;
                   }""",
                _css_only(S.COMPOSER),
            )
            if leftover:
                send_btn = await self._first(S.SEND_BUTTON, timeout_ms=2_000)
                if send_btn is not None:
                    log.info("Enter не сработал — жму кнопку отправки")
                    await send_btn.click()
        except (PWError, PWTimeout):
            pass

    # ------------------------------------------------------------- writing
    async def send_text(self, text: str) -> None:
        """Печатает текст посимвольно (антифрод) и отправляет Enter'ом.

        Переводы строк внутри текста отправляются как Shift+Enter,
        чтобы не «спалить» отправление раньше времени.
        """
        await human_pause()
        composer = await self._first(S.COMPOSER, timeout_ms=10_000)
        if composer is None:
            raise RuntimeError("Поле ввода сообщения не найдено (COMPOSER).")

        await composer.click()
        await jitter(0.25, 0.6)

        lines = text.split("\n")
        for idx, line in enumerate(lines):
            if line:
                await composer.press_sequentially(line, delay=55)
            if idx < len(lines) - 1:
                await self.page.keyboard.press("Shift+Enter")
                await jitter(0.1, 0.3)

        await human_pause(0.6, 1.6)
        await self._submit()

    async def send_photo(self, data: bytes, caption: str = "",
                         filename: str = "tg_photo.jpg") -> None:
        """Сохраняет байты во временную директорию (tmp_dir, удобно для
        отладки) и загружает файл в чат MAX через input[type=file]."""
        from uuid import uuid4

        safe = "".join(c for c in filename if c.isalnum() or c in "._-")[:64] or "photo.png"
        path = self.cfg.tmp_dir / f"{uuid4().hex[:8]}_{safe}"
        path.write_bytes(data)
        try:
            await self._upload_and_send(path, caption)
        finally:
            try:
                path.unlink(missing_ok=True)  # не копить мусор в tmp
            except OSError:
                pass

    async def _upload_and_send(self, path, caption: str = "") -> None:
        """Загрузка файла в чат MAX через input[type=file] + подпись + Enter."""
        await human_pause()
        file_input: Locator | None = None
        for sel in S.FILE_INPUT:
            loc = self.page.locator(sel).first
            try:
                await loc.wait_for(state="attached", timeout=3_000)
                file_input = loc
                break
            except (PWTimeout, PWError):
                continue

        if file_input is not None:
            try:
                await file_input.set_input_files(str(path))
            except PWError as exc:
                log.warning("set_input_files не сработал (%s), пробую через кнопку «Прикрепить»", exc)
                file_input = None

        if file_input is None:
            attach = await self._first(S.ATTACH_BUTTON, timeout_ms=5_000)
            if attach is None:
                raise RuntimeError("Не найден ни input[type=file], ни кнопка «Прикрепить».")
            async with self.page.expect_file_chooser(timeout=10_000) as fc_info:
                await attach.click()
            chooser = await fc_info.value
            await chooser.set_files(str(path))

        preview = await self._first(S.ATTACH_PREVIEW, timeout_ms=20_000, state="attached")
        if preview is None:
            log.warning("Превью вложения не дождался — всё равно пробую отправить")
        await human_pause(1.0, 2.0)

        if caption:
            await self._type_caption(caption)

        await human_pause(0.6, 1.4)
        await self._submit()

    async def _type_caption(self, caption: str) -> None:
        composer = await self._first(S.COMPOSER, timeout_ms=8_000)
        if composer is None:
            return
        await composer.click()
        await composer.press_sequentially(caption[:900], delay=60)

    async def reply_text(self, quoted_preview: str, text: str) -> bool:
        """Best-effort ответ на конкретное сообщение; False -> вызывающий
        отправит обычное сообщение с цитатой текстом.

        Ищем текст-якорь, поднимаемся к «пузырьку» сообщения, наводим мышь
        (у MAX контролы сообщения появляются на hover) и жмём «Ответить».
        """
        try:
            snippet = (quoted_preview or "")[:60].strip()
            if len(snippet) < 3:
                return False
            text_sel = json.dumps(snippet)
            anchor = self.page.locator(f"text={text_sel}").last
            await anchor.scroll_into_view_if_needed(timeout=5_000)

            # Пузырьк сообщения: ближайший предок, внутри которого есть
            # контрол ответа. Поднимаемся максимум на 6 уровней.
            node = anchor
            control = None
            for _ in range(6):
                node = node.locator("xpath=..")
                try:
                    await node.hover(timeout=2_500)
                except (PWError, PWTimeout):
                    continue
                control = await self._first(S.REPLY_CONTROL, root=node, timeout_ms=1_200)
                if control is not None:
                    break
            if control is None:
                return False
            await control.click()
            await jitter(0.5, 1.0)
            await self.send_text(text)
            return True
        except (PWError, PWTimeout) as exc:
            log.info("Reply через UI не вышел (%s) — отправлю отдельным сообщением", exc)
            return False

    async def reload_page(self) -> None:
        log.info("Суточная мягкая перезагрузка страницы MAX…")
        await self.page.reload(wait_until="domcontentloaded", timeout=120_000)
        await self.open_target_chat_silently()

    async def open_target_chat_silently(self) -> None:
        """Переокрытие чата после reload без выбрасывания ошибок наружу."""
        try:
            marker = await self._first(S.CHAT_OPENED_MARKERS, timeout_ms=20_000)
            if marker is None and self.cfg.max_chat_url:
                await self.page.goto(self.cfg.max_chat_url, wait_until="domcontentloaded")
            elif marker is None:
                await self._open_chat_by_search(self.cfg.max_chat_title)
            await self._first(S.CHAT_OPENED_MARKERS, timeout_ms=20_000)
            await self.rearm_bridge()
        except Exception as exc:  # noqa: BLE001
            log.error("После reload не удалось переоткрыть чат: %s", exc)
            self._bridge_installed = False

    async def keepalive(self) -> None:
        """Имитирует «живую» вкладку: MAX по visibility/фокусу может закрывать
        websocket, периодически «будим» страницу невидимыми событиями."""
        try:
            await self.page.evaluate(
                """() => {
                     window.dispatchEvent(new Event('focus'));
                     document.dispatchEvent(new Event('visibilitychange'));
                   }"""
            )
        except PWError:
            pass
