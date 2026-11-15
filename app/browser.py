"""
app/browser.py
==============
Управление Playwright/Chromium: запуск persistent context, маскировка
автоматизации (антифрод MAX), сбор лога браузера на диск.

Persistent context (launch_persistent_context) сохраняет в ./data/chrome_profile
cookies, localStorage, IndexedDB и service worker MAX — то есть сессия
переживает перезапуск контейнера, повторный вход не нужен.
"""

from __future__ import annotations

import os

from playwright.async_api import (
    BrowserContext,
    Error as PlaywrightError,
    Page,
    Playwright,
    async_playwright,
)

from .config import Config
from .utils import get_logger

log = get_logger("browser")

# ---------------------------------------------------------------------------
# Маскировка автоматизации.
# Инжектируется В КАЖДОЙ странице до её скриптов (add_init_script),
# поэтому обходит типичные проверки navigator.webdriver / window.chrome.
# ---------------------------------------------------------------------------
STEALTH_JS = r"""
(() => {
  // 1. navigator.webdriver === undefined (главный маркер Headless/Playwright)
  Object.defineProperty(Navigator.prototype, 'webdriver', {
    get: () => undefined,
    configurable: true,
  });
  for (const proto of [Object.getPrototypeOf(window.navigator), Navigator.prototype]) {
    if (Object.prototype.hasOwnProperty.call(proto, 'webdriver')) {
      Object.defineProperty(proto, 'webdriver', { get: () => undefined });
    }
  }

  // 2. window.chrome — как у настоящего десктопного Chrome
  if (!window.chrome) {
    window.chrome = {
      runtime: {
        OnInstalledReason: { CHROME_UPDATE: 'chrome_update', INSTALL: 'install',
          SHARED_MODULE_UPDATE: 'shared_module_update', UPDATE: 'update' },
        connect: () => {}, sendMessage: () => {},
      },
      loadTimes: () => ({}),
      csi: () => ({}),
      app: { isInstalled: false,
        InstallState: { DISABLED: 'disabled', INSTALLED: 'installed',
                        NOT_INSTALLED: 'not_installed' },
        RunningState: { CANNOT_RUN: 'cannot_run', READY_TO_RUN: 'ready_to_run',
                        RUNNING: 'running' } },
    };
  }

  // 3. Языки/часовые пояса под RU-пользователя
  try {
    Object.defineProperty(navigator, 'languages', { get: () => ['ru-RU', 'ru', 'en-US'] });
  } catch (e) {}

  // 4. Plugins — у headless по умолчанию пустой массив, это палевно
  try {
    Object.defineProperty(navigator, 'plugins', {
      get: () => [
        { name: 'Chrome PDF Plugin', filename: 'internal-pdf-viewer' },
        { name: 'Chrome PDF Viewer', filename: 'mhjfbmdgcfjbbpaeojofohoefgiehjai' },
        { name: 'Native Client',    filename: 'internal-nacl-plugin' },
      ],
    });
    Object.defineProperty(navigator, 'mimeTypes', { get: () => [] });
  } catch (e) {}

  // 5. Permissions API не должна выдавать headless-несоответствия
  const origQuery = window.PermissionStatus
    ? navigator.permissions.query.bind(navigator.permissions) : null;
  if (origQuery) {
    navigator.permissions.query = (parameters) =>
      (parameters && parameters.name === 'notifications')
        ? Promise.resolve({ state: Notification.permission })
        : origQuery(parameters);
  }

  // 6. WebGL renderer: софт-рендер SwiftShader — частый маркер сервера.
  //    Подменяем строки на «обычный» GPU. Сам рендер остаётся программным,
  //    для мессенджера это безразлично.
  const patch = (proto) => {
    if (!proto) return;
    const orig = proto.getParameter;
    proto.getParameter = function (parameter) {
      if (parameter === 37445) return 'Google Inc. (Intel)';        // UNMASKED_VENDOR_WEBGL
      if (parameter === 37446) return 'ANGLE (Intel, Mesa Intel UHD 630, OpenGL 4.5)'; // UNMASKED_RENDERER_WEBGL
      return orig ? orig.apply(this, [parameter]) : null;
    };
  };
  patch(window.WebGLRenderingContext && WebGLRenderingContext.prototype);
  patch(window.WebGL2RenderingContext && WebGL2RenderingContext.prototype);

  // 7. devicePixelRatio/outerWin — в headless бывают несостыковки
  try {
    Object.defineProperty(window, 'devicePixelRatio', { get: () => 1 });
    Object.defineProperty(window, 'outerWidth',  { get: () => window.innerWidth });
    Object.defineProperty(window, 'outerHeight', { get: () => window.innerHeight + 88 });
  } catch (e) {}

  // 8. Пятно «Headless/Chrome» в userAgent данных клиентскихHints тоже чинится
  //    на уровне аргументов запуска (user_agent), здесь только appVersion-эхо.
})();
"""


def chromium_args(cfg: Config) -> list[str]:
    """Аргументы Chromium под 24/7 на Orange Pi 5 Plus (RK3588, headless-контейнер)."""
    args = [
        # --- антифрод --------------------------------------------------
        "--disable-blink-features=AutomationControlled",  # ключевой флаг
        "--disable-features=Translate,MediaRouter,OptimizationHints,"
        "InterestFeedContentSuggestions,CalculateNativeWinOcclusion",
        # --- отпечаток «обычного» десктопа ------------------------------
        f"--window-size={cfg.viewport_width},{cfg.viewport_height}",
        "--window-position=0,0",
        f"--lang={cfg.locale}",
        "--accept-lang=ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
        "--no-first-run",
        "--no-default-browser-check",
        "--no-service-autorun",
        "--disable-sync",
        "--disable-component-update",
        "--metrics-recording-only",
        "--password-store=basic",
        "--use-mock-keychain",
        # --- стабильность/память на ARM в контейнере ---------------------
        "--no-sandbox",            # в docker-контейнере под root без user namespaces
        "--disable-dev-shm-usage", # /dev/shm всё равно поднят в 2GB, но перестраховка
        "--disable-gpu",           # на RK3588 в headless GPU только мешает
        "--disable-extensions",
        "--renderer-process-limit=2",
        "--process-per-tab",
        "--disable-background-timer-throttling",
        "--js-flags=--max-old-space-size=512",
        "--autoplay-policy=user-gesture-required",
        "--no-zygote",             # меньше осиротевых процессов-зомби при рестартах
    ]
    if not cfg.headless:
        args += ["--start-maximized"]
    return args


class BrowserManager:
    """Обёртка над playwright/persistent-context c перевыпуском при падении."""

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self._pw: Playwright | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None
        self._log_fh = None

    # ------------------------------------------------------------------
    @property
    def context(self) -> BrowserContext:
        assert self._context is not None, "BrowserManager не запущен"
        return self._context

    @property
    def page(self) -> Page:
        assert self._page is not None, "BrowserManager не запущен"
        return self._page

    # ------------------------------------------------------------------
    async def __aenter__(self) -> "BrowserManager":
        await self.start()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.stop()

    # ------------------------------------------------------------------
    async def start(self) -> None:
        cfg = self.cfg
        cfg.profile_dir.mkdir(parents=True, exist_ok=True)

        if not cfg.headless and not os.getenv("DISPLAY"):
            log.warning(
                "MAX_HEADLESS=false, но DISPLAY не задан. Запустите Xvfb "
                "(в образе есть скрипт /usr/local/bin/xvfb-start.sh) "
                "или используйте MAX_HEADLESS=true. Временный переход в headless."
            )
            cfg.headless = True

        self._log_fh = open(cfg.browser_log, "a", buffering=1, encoding="utf-8")
        self._pw = await async_playwright().start()
        await self._open_context()

    async def _open_context(self) -> None:
        assert self._pw is not None
        cfg = self.cfg

        self._context = await self._pw.chromium.launch_persistent_context(
            user_data_dir=str(cfg.profile_dir),
            headless=cfg.headless,
            args=chromium_args(cfg),
            ignore_default_args=["--enable-automation", "--hide-scrollbars"],
            user_agent=cfg.user_agent,
            viewport={"width": cfg.viewport_width, "height": cfg.viewport_height}
            if cfg.headless else None,
            no_viewport=not cfg.headless,
            locale=cfg.locale,
            timezone_id=cfg.timezone,
            color_scheme="light",
            reduced_motion="no-preference",
            java_script_enabled=True,
            bypass_csp=False,
        )
        await self._context.add_init_script(STEALTH_JS)
        self._context.on("close", lambda _ctx: log.warning("Браузерное контекст закрыт"))

        # Единственная постоянная вкладка моста.
        pages = self._context.pages
        self._page = pages[0] if pages else await self._context.new_page()
        self._wire_page_logging(self._page)
        log.info(
            "Chromium запущен (headless=%s, UA=%s)",
            cfg.headless,
            cfg.user_agent[:48],
        )

    def _wire_page_logging(self, page: Page) -> None:
        def w(line: str) -> None:
            if self._log_fh and not self._log_fh.closed:
                self._log_fh.write(line + "\n")

        page.on(
            "console",
            lambda msg: w(f"[console:{msg.type}] {msg.text}")
            if msg.type in ("error", "warning") else None,
        )
        page.on("pageerror", lambda err: w(f"[pageerror] {err}"))
        page.on(
            "requestfailed",
            lambda req: w(
                f"[requestfailed] {req.method} {req.url[:160]} -> {req.failure}"
            ),
        )
        page.on(
            "crash",
            lambda _p: log.error("Страница MAX crashed — потребуется перезапуск"),
        )

    # ------------------------------------------------------------------
    async def soft_restart(self) -> None:
        """Мягкий рестарт: закрыть контекст (профиль на диске!), открыть заново."""
        log.info("Мягкий рестарт Chromium…")
        try:
            if self._context:
                await self._context.close()
        except PlaywrightError as exc:
            log.debug("Контекст уже мёртв при soft_restart: %s", exc)
        finally:
            self._context = None
            self._page = None
        await self._open_context()

    async def stop(self) -> None:
        try:
            if self._context:
                await self._context.close()
        except Exception as exc:  # noqa: BLE001
            log.debug("Ошибка при закрытии контекста: %s", exc)
        try:
            if self._pw:
                await self._pw.stop()
        except Exception as exc:  # noqa: BLE001
            log.debug("Ошибка при остановке playwright: %s", exc)
        if self._log_fh:
            self._log_fh.close()
        log.info("Chromium остановлен")
