"""
app/browser.py  (v2)
====================
Изолированные браузер-юниты для аккаунтов MAX.

Модель изоляции (требование 2.0):
    * ОДИН аккаунт = ОДИН экземпляр playwright-драйвера (свой node-процесс)
      + ОДИН launch_persistent_context со своим user_data_dir.
      -> cookies/localStorage/IndexedDB/service worker не пересекаются.
      -> падение драйвера/контекста сносит ТОЛЬКО этот аккаунт;
         supervisor (account_manager) поднимет именно его.
    * Профиль: <profiles_root>/account_<id>/ (на диске = переживает рестарты).
    * Маскировка автоматизации — прежний проверенный stealth-набор.

Память на 32 ГБ: 2–5 Chromium по ~500–700 МБ с запасом; лимиты контейнера
подняты в docker-compose (shm_size 4gb).
"""

from __future__ import annotations

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
# Маскировка автоматизации. Инжектируется в КАЖДОЙ странице до её скриптов.
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

  // 3. Языки под RU-пользователя
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

  // 6. WebGL renderer: подменяем строки SwiftShader на «обычный» GPU
  const patch = (proto) => {
    if (!proto) return;
    const orig = proto.getParameter;
    proto.getParameter = function (parameter) {
      if (parameter === 37445) return 'Google Inc. (Intel)';
      if (parameter === 37446) return 'ANGLE (Intel, Mesa Intel UHD 630, OpenGL 4.5)';
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
})();
"""


def chromium_args(cfg: Config, *, headless: bool) -> list[str]:
    """Аргументы Chromium под 24/7 на Orange Pi 5 Plus (RK3588)."""
    args = [
        # --- антифрод --------------------------------------------------
        "--disable-blink-features=AutomationControlled",
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
        # --- стабильность/память: N браузеров в одном контейнере ---------
        "--no-sandbox",
        "--disable-dev-shm-usage",
        "--disable-gpu",
        "--disable-extensions",
        "--renderer-process-limit=1",
        "--process-per-tab",
        "--disable-background-timer-throttling",
        "--js-flags=--max-old-space-size=512",
        "--autoplay-policy=user-gesture-required",
        "--no-zygote",
    ]
    if not headless:
        args += ["--start-maximized"]
    return args


class AccountBrowser:
    """Браузер-юнит одного аккаунта: playwright -> persistent context -> page."""

    def __init__(self, cfg: Config, *, account_id: int, account_name: str) -> None:
        self.cfg = cfg
        self.account_id = account_id
        self.account_name = account_name
        self.profile_dir = cfg.account_profile_dir(account_id)
        self._log = get_logger(f"browser.a{account_id}")
        self._pw: Playwright | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None
        self._log_fh = None

    # ------------------------------------------------------------------
    @property
    def context(self) -> BrowserContext:
        assert self._context is not None, f"AccountBrowser a{self.account_id} не запущен"
        return self._context

    @property
    def page(self) -> Page:
        assert self._page is not None, f"AccountBrowser a{self.account_id} не запущен"
        return self._page

    # ------------------------------------------------------------------
    async def start(self) -> None:
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        log_path = self.cfg.browser_log.with_name(
            f"browser_a{self.account_id}.log"
        )
        self._log_fh = open(log_path, "a", buffering=1, encoding="utf-8")
        self._pw = await async_playwright().start()
        await self._open_context()
        self._log.info("Chromium запущен (profile=%s)", self.profile_dir)

    async def _open_context(self) -> None:
        assert self._pw is not None
        cfg = self.cfg
        headless = cfg.headless

        self._context = await self._pw.chromium.launch_persistent_context(
            user_data_dir=str(self.profile_dir),
            headless=headless,
            args=chromium_args(cfg, headless=headless),
            ignore_default_args=["--enable-automation", "--hide-scrollbars"],
            user_agent=cfg.user_agent,
            viewport={"width": cfg.viewport_width, "height": cfg.viewport_height}
            if headless else None,
            no_viewport=not headless,
            locale=cfg.locale,
            timezone_id=cfg.timezone,
            color_scheme="light",
            reduced_motion="no-preference",
            java_script_enabled=True,
            bypass_csp=False,
        )
        await self._context.add_init_script(STEALTH_JS)
        self._context.on(
            "close",
            lambda _c: self._log.warning("Контекст аккаунта закрыт (внешний сигнал?)"),
        )

        pages = self._context.pages
        self._page = pages[0] if pages else await self._context.new_page()
        self._wire_page_logging(self._page)

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
            lambda req: w(f"[requestfailed] {req.method} {req.url[:160]} -> {req.failure}"),
        )
        page.on("crash", lambda _p: self._log.error("Страница MAX crashed"))

    # ------------------------------------------------------------------
    async def soft_restart(self) -> None:
        """Мягкий рестарт: контекст закрывается (профиль на диске!), открывается новый."""
        self._log.info("Мягкий рестарт Chromium аккаунта…")
        try:
            if self._context:
                await self._context.close()
        except PlaywrightError as exc:
            self._log.debug("Контекст уже мёртв: %s", exc)
        finally:
            self._context = None
            self._page = None
        await self._open_context()

    async def stop(self) -> None:
        for closer in (self._close_context, self._stop_pw):
            try:
                await closer()
            except Exception as exc:  # noqa: BLE001
                self._log.debug("остановка: %s", exc)
        if self._log_fh:
            try:
                self._log_fh.close()
            except OSError:
                pass
        self._log.info("Chromium аккаунта остановлен")

    async def _close_context(self) -> None:
        if self._context:
            await self._context.close()
        self._context = None
        self._page = None

    async def _stop_pw(self) -> None:
        if self._pw:
            await self._pw.stop()
        self._pw = None

    async def __aenter__(self) -> "AccountBrowser":
        await self.start()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.stop()
