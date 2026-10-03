"""Аудит вёрстки страниц портала на разных экранах и движках: переполнение по ширине, наезд элементов, размеры кнопок и полей, мелкий шрифт,
обрезанный текст, «зависшие» заглушки «Загрузка…», сдвиги вёрстки при загрузке (CLS), ошибки консоли и сети, скриншоты.

Безопасность: поднимает ОТДЕЛЬНЫЙ экземпляр приложения на порту 18777 (uvicorn, без запуска Caddy/LiveKit/рекордеров) с КОПИЕЙ базы и
временными паролями, которые живут только в памяти этого запуска. Боевой сервер и его данные не затрагиваются.

Запуск:  venv\\Scripts\\python.exe scripts\\qa_pages.py [--engine chromium|webkit|android] [--shots папка] [--json файл] [--channel chrome]
`--channel chrome|msedge` берёт установленный в системе браузер, если браузеры Playwright не скачаны.
Код выхода 0 всегда (это отчёт); ключевые проблемы печатаются в конце."""
from __future__ import annotations

import argparse
import json
import os
import secrets
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
PORT = 18777

PAGES = [  # (путь, нужен вход)
    ("/login", False), ("/", True), ("/projects/18", True), ("/projects/29", True),
    ("/projects/18/tz", True), ("/projects/18/chat", True), ("/projects/18/graph", True), ("/admin/conflicts", True),
    ("/admin/map/graph", True),
]
VIEWPORTS = [("desktop-1920", 1920, 1080), ("laptop-1366", 1366, 768), ("tablet-768", 768, 1024),
             ("phone-390", 390, 844), ("phone-320", 320, 568), ("phone-landscape", 844, 390)]

INIT_JS = """
window.__cls = 0; window.__shifts = [];
try {
  new PerformanceObserver((list) => {
    for (const e of list.getEntries()) {
      if (e.hadRecentInput) continue;
      window.__cls += e.value;
      const src = (e.sources || []).map(s => s.node && (s.node.id ? '#' + s.node.id : (s.node.className && String(s.node.className).split(' ')[0] ? '.' + String(s.node.className).split(' ')[0] : s.node.nodeName))).filter(Boolean).slice(0, 2);
      window.__shifts.push([Math.round(e.value * 1000) / 1000, src.join(',')]);
    }
  }).observe({ type: 'layout-shift', buffered: true });
} catch (_) {}
"""

AUDIT_JS = r"""
() => {
  const W = innerWidth, H = innerHeight, mobile = W <= 820;
  const vis = (e) => { const r = e.getBoundingClientRect(); const s = getComputedStyle(e);
    // clip вида `rect(0px, 0px, 0px, 0px)` — стандартный способ спрятать input под подписью (Bootstrap .btn-check):
    // сам элемент не цель, цель — label, поэтому такие input в подсчёт мелких целей не берём.
    const clipped = s.clip && s.clip !== 'auto' && /rect\(\s*0(px)?[,\s]/.test(s.clip);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none' && Number(s.opacity) > 0.01 && !clipped; };
  const sel = (e) => e.tagName.toLowerCase() + (e.id ? '#' + e.id : '') + (e.className && typeof e.className === 'string' ? '.' + e.className.trim().split(/\s+/).slice(0, 2).join('.') : '');
  const label = (e) => (e.getAttribute('aria-label') || e.title || e.textContent || e.value || e.placeholder || '').trim().replace(/\s+/g, ' ').slice(0, 28);
  const out = {};
  const doc = document.documentElement;
  out.viewport = [W, H];
  out.scrollWidth = doc.scrollWidth; out.scrollHeight = doc.scrollHeight;
  out.hscroll = doc.scrollWidth > W + 1;
  // элементы, вылезающие вправо/влево за экран (кроме внутри прокручиваемых контейнеров)
  const inScroller = (e) => { for (let p = e.parentElement; p && p !== document.body; p = p.parentElement) {
    const s = getComputedStyle(p); if (/(auto|scroll|hidden|clip)/.test(s.overflowX) && p.scrollWidth > p.clientWidth) return true; } return false; };
  const over = [];
  for (const e of document.querySelectorAll('body *')) {
    if (!vis(e)) continue; const r = e.getBoundingClientRect();
    if ((r.right > W + 2 || r.left < -2) && !inScroller(e) && getComputedStyle(e).position !== 'fixed') over.push([sel(e), Math.round(r.left), Math.round(r.right)]);
  }
  out.overflowing = over.slice(0, 6); out.overflowingCount = over.length;
  // интерактивные элементы: размеры
  const targets = [];
  for (const e of document.querySelectorAll('a[href], button, input:not([type=hidden]), select, textarea, summary, [role=button], [role=tab]')) {
    if (!vis(e)) continue; const r = e.getBoundingClientRect();
    if (e.tagName === 'A' && e.closest('p, li, td, .small, .text-muted, label') && getComputedStyle(e).display === 'inline') continue; // текстовая ссылка в строке (исключение WCAG)
    if (e.type === 'checkbox' || e.type === 'radio') { if (Math.min(r.width, r.height) >= 16) continue; }
    targets.push([sel(e), label(e), Math.round(r.width), Math.round(r.height)]);
  }
  out.targets = targets.length;
  const small = targets.filter(t => Math.min(t[2], t[3]) < 24);
  const warn = targets.filter(t => Math.min(t[2], t[3]) >= 24 && Math.min(t[2], t[3]) < (mobile ? 36 : 28));
  out.smallFail = small.length; out.smallFailSamples = small.slice(0, 6);
  out.smallWarn = warn.length; out.smallWarnSamples = warn.slice(0, 6);
  // наезд интерактивных элементов друг на друга
  const boxes = [];
  for (const e of document.querySelectorAll('a[href], button, input:not([type=hidden]), select, textarea, summary')) {
    if (!vis(e)) continue; const r = e.getBoundingClientRect(); boxes.push([e, r]);
  }
  const overlaps = [];
  for (let i = 0; i < boxes.length && overlaps.length < 40; i++) for (let j = i + 1; j < boxes.length; j++) {
    const [a, ra] = boxes[i], [b, rb] = boxes[j];
    if (a.contains(b) || b.contains(a)) continue;
    const w = Math.min(ra.right, rb.right) - Math.max(ra.left, rb.left), h = Math.min(ra.bottom, rb.bottom) - Math.max(ra.top, rb.top);
    if (w > 2 && h > 2) { const inter = w * h, small_ = Math.min(ra.width * ra.height, rb.width * rb.height); if (inter / small_ > 0.25) overlaps.push([sel(a), label(a), sel(b), label(b)]); }
  }
  out.overlaps = overlaps.length; out.overlapSamples = overlaps.slice(0, 5);
  // мелкий шрифт и обрезанный текст
  let tiny = 0; const tinySamples = []; let clipped = 0; const clippedSamples = [];
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  while (walker.nextNode()) {
    const n = walker.currentNode; if (!n.textContent.trim()) continue; const p = n.parentElement; if (!p || !vis(p)) continue;
    const fs = parseFloat(getComputedStyle(p).fontSize); if (fs < 11) { tiny++; if (tinySamples.length < 4) tinySamples.push([sel(p), fs, n.textContent.trim().slice(0, 20)]); }
  }
  for (const e of document.querySelectorAll('body *')) {
    if (!vis(e) || e.children.length > 3) continue; const s = getComputedStyle(e);
    if ((s.overflow === 'hidden' || s.textOverflow === 'ellipsis') && e.scrollWidth > e.clientWidth + 2 && e.textContent.trim()) { clipped++; if (clippedSamples.length < 4) clippedSamples.push([sel(e), e.textContent.trim().slice(0, 30), e.scrollWidth, e.clientWidth]); }
  }
  out.tiny = tiny; out.tinySamples = tinySamples; out.clipped = clipped; out.clippedSamples = clippedSamples;
  // зависшие заглушки
  const stuck = Array.from(document.querySelectorAll('body *')).filter(e => vis(e) && e.children.length === 0 && /^(Загрузка|Проверяю|Загружаю)[^а-я]{0,3}$/i.test((e.textContent || '').trim())).map(sel);
  out.stuck = stuck.slice(0, 5);
  // пустые видимые карточки
  out.emptyCards = Array.from(document.querySelectorAll('.card, .card-body')).filter(e => vis(e) && e.getBoundingClientRect().height < 24 && !e.textContent.trim()).length;
  out.cls = Math.round((window.__cls || 0) * 1000) / 1000; out.shifts = (window.__shifts || []).sort((a, b) => b[0] - a[0]).slice(0, 3);
  out.title = document.title;
  return out;
}
"""

GEOMETRY_JS = "() => Array.from(document.querySelectorAll('.card, main > *, .container > *')).slice(0, 24).map(e => { const r = e.getBoundingClientRect(); return [e.id || e.className || e.tagName, Math.round(r.top + scrollY), Math.round(r.height)]; })"


def start_instance(passwords: dict[str, str]):
    from app.app.services.auth import hash_password

    tmp = Path(tempfile.mkdtemp(prefix="skkkk_audit_"))
    data = tmp / "data"
    data.mkdir()
    src = sqlite3.connect(str(ROOT / "data" / "app.db"), timeout=30)
    dst = sqlite3.connect(str(data / "app.db"))
    src.backup(dst)
    src.close()
    dst.close()
    for pid in (18, 29):
        srcp = ROOT / "data" / "projects" / str(pid)
        if srcp.is_dir():
            shutil.copytree(srcp, data / "projects" / str(pid), ignore=shutil.ignore_patterns("artifacts", "tmp"), dirs_exist_ok=True)
    allow = tmp / "Login_up.txt"
    allow.write_text("audit-admin\naudit-manager\naudit-user\n", encoding="utf-8")
    env = {
        **os.environ,
        "SECRET_KEY": secrets.token_urlsafe(32),
        "ADMIN_PASSWORD_HASH": hash_password(passwords["admin"]), "MANAGER_PASSWORD_HASH": hash_password(passwords["manager"]), "USER_PASSWORD_HASH": hash_password(passwords["user"]),
        "DATA_DIR": str(data), "DATABASE_URL": f"sqlite:///{data / 'app.db'}", "LOGIN_ALLOWLIST_PATH": str(allow),
        "SKKKK_DISABLE_EDGE": "1", "WEB_HOST": "127.0.0.1", "WEB_PORT": str(PORT), "PYTHONIOENCODING": "utf-8",
        # Вёрстка и сценарии портала, не вход: личный пароль и карточку не проходим. База паролей — своя в DATA_DIR.
        "PERSONAL_PASSWORD": "off", "CREDENTIALS_DB_PATH": str(data / "credentials.db"),
    }
    log = open(tmp / "server.log", "w", encoding="utf-8")
    proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.app.main:app", "--host", "127.0.0.1", "--port", str(PORT), "--log-level", "warning"],
                            cwd=str(ROOT), env=env, stdout=log, stderr=subprocess.STDOUT)
    for _ in range(90):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{PORT}/health", timeout=2).read()
            return proc, tmp
        except Exception:  # noqa: BLE001
            time.sleep(1)
    proc.kill()
    raise RuntimeError("тестовый экземпляр не поднялся: " + (tmp / "server.log").read_text(encoding="utf-8", errors="ignore")[-400:])


def fill_login(page, name: str, password: str) -> None:
    """Поля входа по id. Поле Telegram на странице входа теперь неактивно (hide_telegram) — заполняем, только если оно есть."""
    page.fill("#login-name", name)
    tg = page.locator("#login-telegram")
    if tg.count() and tg.is_enabled():
        tg.fill("audit-" + name[-5:])
    page.fill("#login-password", password)


def ui_login(page, base: str, name: str, password: str) -> None:
    page.goto(base + "/login")
    fill_login(page, name, password)
    page.locator("#login-form button[type=submit]").click()
    page.wait_for_url(lambda u: "/login" not in u, timeout=20000)


def settle(page) -> None:
    """Дождаться загрузки данных: заглушки исчезли или прошло 9 секунд."""
    end = time.time() + 9
    while time.time() < end:
        n = page.evaluate("Array.from(document.querySelectorAll('body *')).filter(e => e.children.length === 0 && /^(Загрузка|Проверяю|Загружаю)[^а-я]{0,3}$/i.test((e.textContent||'').trim())).length")
        if n == 0:
            break
        time.sleep(0.5)
    time.sleep(1.2)


def audit_page(page, base: str, path: str, shots: Path | None, tag: str, chromium: bool) -> dict:
    errors: list[str] = []
    bad_resp: list[str] = []
    h1 = lambda m: errors.append(m.text[:140]) if m.type == "error" else None  # noqa: E731
    h2 = lambda e: errors.append("pageerror: " + str(e)[:140])  # noqa: E731
    h3 = lambda r: bad_resp.append(f"{r.status} {r.url.replace(base, '')[:70]}") if r.status >= 400 and r.status != 401 else None  # noqa: E731
    page.on("console", h1); page.on("pageerror", h2); page.on("response", h3)
    t0 = time.time()
    resp = page.goto(base + path, wait_until="load", timeout=45000)
    settle(page)
    geo1 = page.evaluate(GEOMETRY_JS)
    time.sleep(2.5)
    geo2 = page.evaluate(GEOMETRY_JS)
    res = page.evaluate(AUDIT_JS)
    res["status"] = resp.status if resp else 0
    res["loadSec"] = round(time.time() - t0, 1)
    res["errors"] = errors[:4]
    res["badResponses"] = bad_resp[:4]
    # «плывёт»: положение и высота блоков через 2,5 с после первой отрисовки данных
    moved = [(a[0], a[1], b[1], a[2], b[2]) for a, b in zip(geo1, geo2) if abs(a[1] - b[1]) > 3 or abs(a[2] - b[2]) > 3]
    res["drift"] = [f"{str(m[0])[:26]} top {m[1]}→{m[2]} h {m[3]}→{m[4]}" for m in moved[:4]]
    if shots:
        try:
            page.screenshot(path=str(shots / f"{tag}.png"), full_page=True)
        except Exception:  # noqa: BLE001
            pass
    page.remove_listener("console", h1); page.remove_listener("pageerror", h2); page.remove_listener("response", h3)
    return res


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--engine", default="chromium", choices=["chromium", "webkit", "android"])
    ap.add_argument("--shots", default="")
    ap.add_argument("--json", default="")
    ap.add_argument("--only", default="", help="подстрока в пути страницы, чтобы проверить только её")
    ap.add_argument("--channel", default="", help="канал браузера (chrome, msedge) вместо скачанного Playwright")
    args = ap.parse_args()
    from playwright.sync_api import sync_playwright

    passwords = {r: secrets.token_urlsafe(12) for r in ("admin", "manager", "user")}
    proc, tmp = start_instance(passwords)
    shots = Path(args.shots) if args.shots else None
    if shots:
        shots.mkdir(parents=True, exist_ok=True)
    results: list[dict] = []
    try:
        with sync_playwright() as p:
            if args.engine == "android":
                base = f"http://10.0.2.2:{PORT}"
                b = p.chromium.connect_over_cdp("http://127.0.0.1:9222")
                combos = [("android-chrome", None, None, None)]
            else:
                base = f"http://127.0.0.1:{PORT}"
                b = getattr(p, args.engine).launch(headless=True, **({"channel": args.channel} if args.channel else {}))
                if args.engine == "webkit":
                    combos = [(f"webkit-{n.replace(' ', '')}", None, None, n) for n in ("iPhone 15", "iPhone 15 landscape", "iPad Pro 11")]
                else:
                    combos = [(n, w, h, None) for n, w, h in VIEWPORTS]
            for name, w, h, profile in combos:
                for role in ("admin", "user") if args.engine == "chromium" and name in ("desktop-1920", "phone-390") else ("admin",):
                    if args.engine == "android":
                        ctx = b.contexts[0]
                        page = ctx.new_page()
                    else:
                        opts = p.devices[profile] if profile else {"viewport": {"width": w, "height": h}}
                        ctx = b.new_context(**opts)
                        if args.engine == "chromium":
                            ctx.add_init_script(INIT_JS)
                        page = ctx.new_page()
                    try:
                        ui_login(page, base, f"audit-{role}", passwords[role])
                    except Exception as exc:  # noqa: BLE001
                        print(f"! вход не удался ({name}/{role}): {str(exc)[:100]}", flush=True)
                        continue
                    for path, needs_login in PAGES:
                        if args.only and args.only not in path:
                            continue
                        if role == "user" and path not in ("/", "/projects/18"):
                            continue
                        if path.startswith("/admin") and role != "admin":
                            continue
                        tag = f"{name}_{role}_{path.strip('/').replace('/', '_') or 'home'}"
                        try:
                            r = audit_page(page, base, path, shots, tag, args.engine == "chromium")
                        except Exception as exc:  # noqa: BLE001
                            r = {"error": str(exc)[:160]}
                        r.update({"page": path, "screen": name, "role": role})
                        results.append(r)
                        flag = []
                        if r.get("hscroll"): flag.append("H-СКРОЛЛ")
                        if r.get("smallFail"): flag.append(f"мелких<24: {r['smallFail']}")
                        if r.get("overlaps"): flag.append(f"наезд: {r['overlaps']}")
                        if r.get("stuck"): flag.append("заглушка")
                        if r.get("cls", 0) > 0.1: flag.append(f"CLS {r['cls']}")
                        if r.get("drift"): flag.append(f"плывёт: {len(r['drift'])}")
                        if r.get("errors") or r.get("badResponses"): flag.append("ошибки")
                        print(f"[{name:>15}/{role:<5}] {path:<22} {'; '.join(flag) or 'ok'}", flush=True)
                    if args.engine == "android":
                        page.close()
                    else:
                        ctx.close()
            if args.engine != "android":
                b.close()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:  # noqa: BLE001
            proc.kill()
        shutil.rmtree(tmp, ignore_errors=True)
    if args.json:
        Path(args.json).write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\nстраниц проверено: {len(results)}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
