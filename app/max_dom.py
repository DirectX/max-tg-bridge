"""
app/max_dom.py  (v2)
====================
Адаптер к DOM веб-клиента MAX (web.max.ru) для ОДНОЙ вкладки одного аккаунта.

v2 отличия от v1:
  * MaxWeb привязан к конкретному аккаунту: параметры чата/самоимени/лимита
    картинок приходят в AccountTarget (не из глобального ENV);
  * добавлены «шаги логина» (goto_home/submit_phone/submit_code/wait_logged_in/
    detect_captcha/screenshot) — переиспользуются и CLI app.auth, и
    интерактивной FSM авторизации через Telegram;
  * JS-мост чтения (_JS_BRIDGE) НЕ изменялся — покрыт tests/test_max_dom_bridge.js.

Архитектура чтения (как в v1): в страницу инжектируется window.__MaxBridge
(MutationObserver + interval-скан + дедуп по ключу), Python забирает drain().
Картинки скачиваются fetch() ВНУТРИ страницы (blob:-URL и авторизационные
куки живут только там); фолбэк — скриншот элемента.
ВСЕ селекторы — из app/selectors.py.
"""

from __future__ import annotations

import asyncio
import base64
import json
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from playwright.async_api import Error as PWError
from playwright.async_api import Locator, Page, TimeoutError as PWTimeout

from . import selectors as S
from .config import Config
from .utils import get_logger, human_pause, jitter, normalize_ws

log = get_logger("max.dom")

# Сколько секунд ждём активации кнопки «Продолжить» после ввода номера
# (в реальном DOM она стартует с disabled). Наружу вынесено ради тестов.
SUBMIT_ENABLE_WAIT_S = 12.0


class MaxAuthError(RuntimeError):
    """Сессия MAX протухла: требуется переавторизация аккаунта."""


class MaxChatNotFound(RuntimeError):
    """Не найден/не открылся целевой чат — проверьте настройки аккаунта."""


@dataclass(slots=True)
class AccountTarget:
    """Всё, что MaxWeb должен знать про конкретный аккаунт."""
    account_id: int
    chat_title: str = ""
    chat_url: str = ""
    self_name: str = ""
    tmp_dir: Path = Path("/data/tmp")
    max_image_bytes: int = 9 * 1024 * 1024

    @property
    def label(self) -> str:
        return f"a{self.account_id}"


@dataclass(slots=True)
class MaxImage:
    src: str
    width: int = 0
    height: int = 0


@dataclass(slots=True)
class MaxMessage:
    key: str
    sender: str
    text: str
    images: list[MaxImage] = field(default_factory=list)
    mine: bool = False
    ts: int = 0


# ===========================================================================
# JS-мост, инжектируемый в страницу (ИЗМЕНЯТЬСЯ ТОЛЬКО ВМЕСТЕ С ТЕСТОМ:
# tests/run_js_bridge_test.sh)
# ===========================================================================
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


# ===========================================================================


class MaxWeb:
    """Одна вкладка web.max.ru конкретного аккаунта MAX."""

    def __init__(self, page: Page, cfg: Config, acct: AccountTarget) -> None:
        self.page = page
        self.cfg = cfg
        self.acct = acct
        self.label = acct.label
        self._log = get_logger(f"max.dom.{self.label}")
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
            "selfName": self.acct.self_name,
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

    async def screenshot(self, kind: str) -> Path:
        """Скриншот в cfg.screenshots_dir; путь возвращаем для отправки в TG.

        v2.2.1: по умолчанию JPEG (quality=SHOT_QUALITY, 1440x900 -> 150-250 КБ
        вместо 1.5-2 МБ PNG). Это не косметика: скриншоты отправляются админу
        в Telegram через ретранслятор, и 2 МБ на 30 КБ/с = 60 с таймаут.
        """
        cfg = self.cfg
        self.cfg.screenshots_dir.mkdir(parents=True, exist_ok=True)
        ext = "jpg" if cfg.shot_jpeg else "png"
        path = self.cfg.screenshots_dir / (
            f"auth_{self.label}_{kind}_{datetime.now():%H%M%S}.{ext}"
        )
        try:
            if cfg.shot_jpeg:
                await self.page.screenshot(path=str(path), type="jpeg",
                                           quality=cfg.shot_quality, full_page=False)
            else:
                await self.page.screenshot(path=str(path), full_page=False)
            try:
                size_kb = path.stat().st_size // 1024
            except OSError:
                size_kb = -1
            self._log.debug("Скриншот %s (%d КБ)", path.name, size_kb)
        except PWError as exc:
            self._log.warning("Скриншот не сделан: %s", exc)
            raise
        return path

    async def detect_captcha(self) -> bool:
        for sel in S.CAPTCHA_MARKERS:
            try:
                if await self.page.locator(sel).first.count() > 0:
                    return True
            except PWError:
                continue
        return False

    async def _looks_like_login(self) -> bool:
        """Любой экран аутентификации (QR или форма телефона) = сессии нет.
        Якоря form.auth / auth--phone / authLogo надёжнее поиска по полю:
        ловим и «мы уже на шаге номера», и «откатились на QR»."""
        url = await self.current_url()
        if any(part in url for part in ("/login", "/auth", "sign-in")):
            return True
        for group in (S.AUTH_SCREEN, S.PHONE_FORM, S.PHONE_SWITCH):
            if await self._first(group, timeout_ms=800) is not None:
                return True
        return False

    # ------------------------------------------------------- шаги логина
    # Используются app/auth_fsm.py (через Telegram) и app/auth.py (CLI).
    async def _open_phone_form(self) -> bool:
        """MAX первым делом показывает экран QR-кода («Войдите в MAX по QR-коду»);
        поле телефона появляется ТОЛЬКО после клика по переключателю
        «Войти по номеру телефона». Кликаем и ждём форму.

        QR живёт ~2 минуты и протухает («QR-код устарел») — для headless-бота
        телефонный путь единственный, так что переключаемся всегда, когда
        поля телефона нет, но переключатель виден.
        """
        switch = await self._first(S.PHONE_SWITCH, timeout_ms=6_000)
        if switch is None:
            return False
        try:
            await switch.click()
        except (PWError, PWTimeout) as exc:
            self._log.warning("Клик «Войти по номеру телефона» не вышел: %s", exc)
            return False
        await jitter(0.8, 1.6)
        form = await self._first(S.PHONE_FORM, timeout_ms=6_000)
        phone = await self._first(S.PHONE_INPUT, timeout_ms=8_000, state="attached")
        if form is None and phone is None:
            self._log.warning("Переключатель нажал, но формы телефона нет — "
                              "проверьте PHONE_FORM/PHONE_INPUT/PHONE_SWITCH по скриншоту")
            return False
        self._log.info("Открыл форму ввода телефона (переключатель с QR-экрана)")
        return True

    async def goto_home(self) -> str:
        """Открывает MAX и определяет состояние: 'logged_in' | 'login_form' | 'unknown'.

        'login_form' возвращается и когда форма была сразу, и когда мы сами
        переключились с QR-экрана (submit_phone тогда найдёт поле).
        """
        await self.page.goto(self.cfg.max_url, wait_until="domcontentloaded", timeout=90_000)
        await jitter(2.0, 3.5)
        # уже на форме телефона?
        if await self._first(S.PHONE_FORM, timeout_ms=2_500) is not None \
                or await self._first(S.PHONE_INPUT, timeout_ms=1_500, state="attached") is not None:
            return "login_form"
        # уже авторизованы (композер/лента)?
        if await self._first(S.LOGGED_IN_MARKERS, timeout_ms=3_000, state="attached") is not None:
            return "logged_in"
        # QR-экран -> переключаемся на телефон
        if await self._open_phone_form():
            return "login_form"
        # финальная проверка: вдруг просто долго грузился
        if await self._first(S.LOGGED_IN_MARKERS, timeout_ms=10_000, state="attached") is not None:
            return "logged_in"
        return "unknown"

    async def _country_prefix(self) -> str:
        """Код страны, показанный кнопкой «+7» (префикс живёт НЕ в поле, а в
        отдельной кнопке страны). Пустая строка — не прочиталось."""
        for sel in _css_only(S.COUNTRY_BUTTON):
            try:
                el = self.page.locator(sel).first
                if await el.count() == 0:
                    continue
                txt = await el.inner_text()
                m = re.search(r"\+\s?(\d{1,3})", txt or "")
                if m:
                    return m.group(1)
            except (PWError, PWTimeout):
                continue
        return ""

    async def submit_phone(self, phone: str) -> tuple[Path, bool]:
        """Вводит номер и жмёт «Продолжить». Возвращает (скриншот, captcha?).

        Реальный DOM (form.auth--phone): код страны (+7) — отдельная кнопка
        `button.country`, а в поле `input[inputmode=decimal]` печатаются
        ТОЛЬКО 10 цифр абонента. Кнопка «Продолжить» в исходном состоянии
        имеет атрибут disabled и класс button--disabled — становится активной,
        когда поле приняло достаточно цифр. Поэтому:
          1) разбираем номер в национальную часть;
          2) сверяем код страны на кнопке с кодом номера (иначе честно
             ругаемся: для другого кода нужно выбрать страну по флагу);
          3) печатаем посимвольно только цифры;
          4) ждём enabled у кнопки (своим циклом, не 30-секундным авто-веем).
        """
        digits = re.sub(r"\D", "", phone or "")
        if len(digits) == 11 and digits[0] in "78":
            national = digits[1:]
            cc = "7"
        elif len(digits) == 10:
            national, cc = digits, "7"
        elif len(digits) >= 11:
            national, cc = digits[-10:], digits[: len(digits) - 10]
        else:
            shot = await self.screenshot("bad_phone")
            raise RuntimeError(
                f"Не понимаю номер {phone!r} (цифр: {len(digits)}) — "
                f"нужно 10–11 цифр. Скриншот: {shot.name}"
            )

        phone_input = await self._first(S.PHONE_INPUT, timeout_ms=6_000)
        if phone_input is None:
            # страница могла откатиться на QR-экран (перезагрузка/протухание)
            await self._open_phone_form()
            phone_input = await self._first(S.PHONE_INPUT, timeout_ms=8_000)
        if phone_input is None:
            shot = await self.screenshot("no_phone_input")
            raise RuntimeError(
                "Поле ввода телефона не найдено (см. PHONE_INPUT/PHONE_SWITCH "
                f"в selectors.py) — скриншот: {shot.name}"
            )

        # код страны на форме должен совпадать с кодом номера (+7 по умолчанию)
        shown_cc = await self._country_prefix()
        if shown_cc and cc and shown_cc != cc.lstrip("0"):
            shot = await self.screenshot("country_mismatch")
            raise RuntimeError(
                f"На форме выбран код +{shown_cc}, а номер {phone} — с кодом "
                f"+{cc}. Выберите страну в браузере (кнопка с флагом) или "
                f"укажите номер +{shown_cc}…. Скриншот: {shot.name}"
            )

        await phone_input.click()
        await jitter(0.25, 0.6)
        # чистим поле «по-человечески» (fill() на Svelte-контролируемых инпутах
        # иногда не доносит событие изменения)
        await self.page.keyboard.press("Control+A")
        await jitter(0.08, 0.2)
        await self.page.keyboard.press("Delete")
        await jitter(0.15, 0.4)
        await phone_input.press_sequentially(national, delay=120)
        await jitter(0.6, 1.4)

        submit = await self._first(S.PHONE_SUBMIT_BUTTON, timeout_ms=5_000)
        if submit is not None:
            # кнопка стартует disabled; ждём активности своим циклом, чтобы не
            # висеть 30 с на авто-вее и дать внятную диагностику
            enabled = False
            deadline = time.monotonic() + SUBMIT_ENABLE_WAIT_S
            while time.monotonic() < deadline:
                try:
                    if await submit.is_enabled():
                        enabled = True
                        break
                except (PWError, PWTimeout):
                    break
                await asyncio.sleep(0.3)
            if not enabled:
                val = ""
                try:
                    val = await phone_input.input_value()
                except (PWError, PWTimeout):
                    pass
                shot = await self.screenshot("submit_disabled")
                raise RuntimeError(
                    "Кнопка «Продолжить» осталась неактивной — MAX не принял "
                    f"номер (в поле: {val!r}). Проверьте скриншот {shot.name}: "
                    "код страны, длина номера, не появилась ли подсказка."
                )
            await submit.click()
        else:
            await self.page.keyboard.press("Enter")
        await jitter(1.5, 2.5)
        shot = await self.screenshot("after_phone")
        return shot, await self.detect_captcha()

    async def submit_code(self, code: str) -> tuple[Path, bool]:
        """Вводит СМС-код в активное поле. Возвращает (скриншот, captcha?)."""
        code_input = await self._first(S.SMS_INPUT, timeout_ms=20_000)
        if code_input is not None:
            try:
                await code_input.click()
            except (PWError, PWTimeout):
                pass  # поле может быть «невидимым» — печатаем в фокус
        await jitter(0.3, 0.6)
        await self.page.keyboard.type(code, delay=140)
        await jitter(0.5, 1.0)

        submit = await self._first(S.SMS_SUBMIT_BUTTON, timeout_ms=3_000)
        if submit is not None:
            try:
                await submit.click()
            except (PWError, PWTimeout):
                await self.page.keyboard.press("Enter")
        else:
            await self.page.keyboard.press("Enter")
        await jitter(1.5, 2.5)
        shot = await self.screenshot("after_code")
        return shot, await self.detect_captcha()

    async def wait_logged_in(self, timeout_s: float = 90) -> bool:
        marker = await self._first(
            S.LOGGED_IN_MARKERS, timeout_ms=int(timeout_s * 1000), state="attached"
        )
        return marker is not None

    # ------------------------------------------------------------ рабочий цикл
    async def open_max(self) -> None:
        """Боевой вход: страница должна открыться УЖЕ авторизованной."""
        self._log.info("Открываю %s", self.cfg.max_url)
        await self.page.goto(self.cfg.max_url, wait_until="domcontentloaded",
                             timeout=90_000)
        marker = await self._first(S.CHAT_OPENED_MARKERS, timeout_ms=25_000, state="attached")
        if marker is None:
            url = await self.current_url()
            raise MaxAuthError(
                f"[{self.label}] MAX не показывает авторизованный интерфейс (url={url})"
            )

    async def open_target_chat(self) -> None:
        """Открывает чат аккаунта по chat_url либо через поиск по chat_title."""
        if await self._looks_like_login():
            raise MaxAuthError(f"[{self.label}] редирект на страницу входа — сессия истекла")

        if self.acct.chat_url:
            self._log.info("Открываю чат по URL: %s", self.acct.chat_url)
            await self.page.goto(self.acct.chat_url, wait_until="domcontentloaded")
        else:
            await self._open_chat_by_search(self.acct.chat_title)

        composer = await self._first(S.CHAT_OPENED_MARKERS, timeout_ms=20_000)
        if composer is None:
            raise MaxChatNotFound(
                f"[{self.label}] чат {self.acct.chat_title!r} не открылся: проверьте "
                "название/URL и селекторы CHAT_OPENED_MARKERS"
            )
        await self.page.evaluate(_JS_BRIDGE)
        await self.page.evaluate("(cfg) => window.__MaxBridge.install(cfg)", self._js_config())
        seeded = await self.page.evaluate("() => window.__MaxBridge.seed()")
        self._bridge_installed = True
        self._log.info("Целевой чат открыт, history seed=%d", seeded)

    async def _open_chat_by_search(self, title: str) -> None:
        if not title:
            raise MaxChatNotFound(f"[{self.label}] не заданы ни chat_url, ни chat_title")
        self._log.info("Ищу чат %r через поиск", title)

        box = await self._first(S.SEARCH_INPUT, timeout_ms=2_500)
        if box is None:
            trigger = await self._first(S.SEARCH_TRIGGER, timeout_ms=5_000)
            if trigger is None:
                raise MaxChatNotFound(f"[{self.label}] не нашёл поле/иконку поиска")
            await trigger.click()
            await jitter(0.4, 0.8)
            box = await self._first(S.SEARCH_INPUT, timeout_ms=8_000)
        if box is None:
            raise MaxChatNotFound(f"[{self.label}] поиск открылся, но поля ввода нет")

        await box.click()
        await box.fill("")
        await box.press_sequentially(title, delay=90)
        await jitter(1.2, 2.2)

        item_sel = [t.format(title=title) for t in S.CHAT_ITEM]
        item = await self._first(item_sel, timeout_ms=10_000)
        if item is None:
            raise MaxChatNotFound(
                f"[{self.label}] в поиске нет строки с чатом {title!r}"
            )
        await item.click()
        await jitter(0.8, 1.6)

    # -------------------------------------------------------------- reading
    async def poll(self) -> list[MaxMessage]:
        """Забирает накопленные JS-мостом новые сообщения."""
        if not self._bridge_installed:
            return []
        try:
            raw = await self.page.evaluate("() => window.__MaxBridge.drain()")
        except PWError:
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
        self._log.info("JS-мост переустановлен (seed=%d)", seeded)

    async def ensure_listening(self) -> bool:
        """Жив ли JS-мост в странице; при потере — переинсталирует."""
        if self._bridge_installed:
            try:
                ok = await self.page.evaluate(
                    "() => !!(window.__MaxBridge && window.__MaxBridge.stats().installed)"
                )
                if ok:
                    return True
                self._log.warning("__MaxBridge исчез — переустанавливаю")
            except PWError:
                self._bridge_installed = False
        try:
            await self.rearm_bridge()
            return True
        except Exception as exc:  # noqa: BLE001
            self._log.warning("Переустановка моста не удалась: %s — watchdog подстрахует", exc)
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
        cfg_lim = self.acct.max_image_bytes
        if src.startswith("data:"):
            try:
                _, b64 = src.split(",", 1)
                if len(b64) * 3 / 4 > cfg_lim:
                    self._log.warning("data:-картинка слишком большая")
                    return None
                return base64.b64decode(b64)
            except Exception as exc:  # noqa: BLE001
                self._log.warning("Не распарсил data-URL: %s", exc)
                return None

        res = await self.page.evaluate(_JS_FETCH_IMAGE, [src, cfg_lim])
        if isinstance(res, dict) and res.get("ok"):
            try:
                return base64.b64decode(res["b64"])
            except Exception as exc:  # noqa: BLE001
                self._log.warning("base64-декод упал: %s", exc)

        self._log.info("fetch картинки не удался (%s) — пробую скриншот <img>",
                       (res or {}).get("error", "?") if isinstance(res, dict) else res)
        return await self._screenshot_image(src)

    async def _screenshot_image(self, src: str) -> bytes | None:
        try:
            box = await self.page.evaluate(_JS_IMG_BOX, src)
            if not box:
                return None
            png = await self.page.screenshot(
                clip=box,
                type="jpeg" if self.cfg.shot_jpeg else "png",
                quality=self.cfg.shot_quality if self.cfg.shot_jpeg else None,
                timeout=15_000)
            if len(png) > self.acct.max_image_bytes:
                self._log.warning("Скриншот картинки больше лимита")
                return None
            return png
        except (PWError, PWTimeout) as exc:
            self._log.warning("Скриншот картинки не вышел: %s", exc)
            return None

    # ------------------------------------------------------------- writing
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
                    self._log.info("Enter не сработал — жму кнопку отправки")
                    await send_btn.click()
        except (PWError, PWTimeout):
            pass

    async def send_text(self, text: str) -> None:
        """Печатает посимвольно (антифрод); переводы строк — Shift+Enter."""
        await human_pause()
        composer = await self._first(S.COMPOSER, timeout_ms=10_000)
        if composer is None:
            raise RuntimeError(f"[{self.label}] поле ввода сообщения не найдено (COMPOSER)")

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
        """Байты -> временная папка аккаунта -> input[type=file] -> подпись -> Enter."""
        from uuid import uuid4

        safe = "".join(c for c in filename if c.isalnum() or c in "._-")[:64] or "photo.png"
        path = self.acct.tmp_dir / f"a{self.acct.account_id}_{uuid4().hex[:8]}_{safe}"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        try:
            await self._upload_and_send(path, caption)
        finally:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass

    async def _upload_and_send(self, path, caption: str = "") -> None:
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
                self._log.warning(
                    "set_input_files не сработал (%s), пробую кнопку «Прикрепить»", exc)
                file_input = None

        if file_input is None:
            attach = await self._first(S.ATTACH_BUTTON, timeout_ms=5_000)
            if attach is None:
                raise RuntimeError(
                    f"[{self.label}] нет ни input[type=file], ни кнопки «Прикрепить»")
            async with self.page.expect_file_chooser(timeout=10_000) as fc_info:
                await attach.click()
            chooser = await fc_info.value
            await chooser.set_files(str(path))

        preview = await self._first(S.ATTACH_PREVIEW, timeout_ms=20_000, state="attached")
        if preview is None:
            self._log.warning("Превью вложения не дождался — всё равно пробую отправить")
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
        """Best-effort «Ответить» на сообщение; False -> вызывающий шлёт цитатой."""
        try:
            snippet = (quoted_preview or "")[:60].strip()
            if len(snippet) < 3:
                return False
            text_sel = json.dumps(snippet)
            anchor = self.page.locator(f"text={text_sel}").last
            await anchor.scroll_into_view_if_needed(timeout=5_000)

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
            self._log.info("Reply через UI не вышел (%s) — уйдёт отдельным сообщением", exc)
            return False

    async def reload_page(self) -> None:
        self._log.info("Мягкая перезагрузка страницы MAX…")
        await self.page.reload(wait_until="domcontentloaded", timeout=120_000)
        await self.open_target_chat_silently()

    async def open_target_chat_silently(self) -> None:
        try:
            marker = await self._first(S.CHAT_OPENED_MARKERS, timeout_ms=20_000)
            if marker is None and self.acct.chat_url:
                await self.page.goto(self.acct.chat_url, wait_until="domcontentloaded")
            elif marker is None:
                await self._open_chat_by_search(self.acct.chat_title)
            await self._first(S.CHAT_OPENED_MARKERS, timeout_ms=20_000)
            await self.rearm_bridge()
        except Exception as exc:  # noqa: BLE001
            self._log.error("После reload чат не переоткрыт: %s", exc)
            self._bridge_installed = False

    async def keepalive(self) -> None:
        """«Будим» страницу: SPA мессенджера закрывает ws на фоновых вкладках."""
        try:
            await self.page.evaluate(
                """() => {
                     window.dispatchEvent(new Event('focus'));
                     document.dispatchEvent(new Event('visibilitychange'));
                   }"""
            )
        except PWError:
            pass
