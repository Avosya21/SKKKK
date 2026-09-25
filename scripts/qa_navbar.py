"""Проверка шапки: на разных экранах шапка не шире экрана, не выше 2 строк на телефоне, проект обрезается многоточием, ничего не наезжает.
Запуск: venv\Scripts\python.exe scripts\qa_navbar.py [--shots папка]"""
from __future__ import annotations

import argparse
import secrets
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
import qa_pages as qp  # noqa: E402

JS = """() => {
  const q = s => document.querySelector(s);
  const nav = q('.navbar-skkkk'), proj = q('.navbar-project'), brand = q('.navbar-brand[href="/"]');
  const r = e => e ? e.getBoundingClientRect() : null;
  const rows = new Set([...nav.querySelectorAll('.navbar-brand[href="/"], .navbar-project, .navbar-back, #nav-session')]
    .filter(e => e.offsetParent).map(e => Math.round(r(e).top / 8)));
  const items = [...nav.querySelectorAll('.navbar-brand[href="/"], .navbar-project, .navbar-back, #nav-session, #nav-user')].filter(e => e.offsetParent);
  const overlaps = [];
  for (let i = 0; i < items.length; i++) for (let j = i + 1; j < items.length; j++) {
    const a = r(items[i]), b = r(items[j]);
    if (items[i].contains(items[j]) || items[j].contains(items[i])) continue;
    if (a.left < b.right - 1 && b.left < a.right - 1 && a.top < b.bottom - 1 && b.top < a.bottom - 1) overlaps.push(items[i].className + '|' + items[j].className);
  }
  return {navH: Math.round(r(nav).height), docW: document.documentElement.scrollWidth, vw: innerWidth, overlaps,
    projClipped: proj ? proj.scrollWidth > proj.clientWidth + 1 : false,
    projEllipsis: proj ? getComputedStyle(proj).textOverflow : null, projDisplay: proj ? getComputedStyle(proj).display : null,
    brandClipped: brand.scrollWidth > brand.clientWidth + 1, rightEdge: Math.round(Math.max(...items.map(e => r(e).right)))};
}"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--shots", default="")
    args = ap.parse_args()
    from playwright.sync_api import sync_playwright

    pw = {r: secrets.token_urlsafe(12) for r in ("admin", "manager", "user")}
    proc, tmp = qp.start_instance(pw)
    base = f"http://127.0.0.1:{qp.PORT}"
    bad = 0
    try:
        with sync_playwright() as p:
            b = p.chromium.launch(headless=True)
            for w, h in [(320, 640), (360, 740), (390, 844), (430, 900), (600, 900), (768, 1024), (992, 800), (1280, 800), (1920, 1080)]:
                ctx = b.new_context(viewport={"width": w, "height": h})
                page = ctx.new_page()
                qp.ui_login(page, base, "audit-admin", pw["admin"])
                for path in ("/", "/projects/18"):
                    page.goto(base + path)
                    page.wait_for_selector("#nav-session:not(.d-none)", timeout=15000)
                    page.wait_for_timeout(600)
                    m = page.evaluate(JS)
                    problems = []
                    if m["docW"] > m["vw"]: problems.append("H-скролл")
                    if m["overlaps"]: problems.append(f"наезд {m['overlaps']}")
                    if m["rightEdge"] > m["vw"]: problems.append("за краем")
                    if m["brandClipped"]: problems.append("название обрезано")
                    if m["projClipped"] and (m["projEllipsis"] != "ellipsis" or m["projDisplay"] != "block"): problems.append("проект обрезан без многоточия")
                    if w < 768 and m["navH"] > 112: problems.append(f"высота {m['navH']}")
                    if w >= 768 and m["navH"] > 64: problems.append(f"высота {m['navH']}")
                    bad += bool(problems)
                    print(f"{w:>5} {path:<14} h={m['navH']:<3} {'; '.join(problems) or 'ok'}", flush=True)
                    if args.shots and path != "/":
                        Path(args.shots).mkdir(parents=True, exist_ok=True)
                        page.screenshot(path=f"{args.shots}/nav_{w}.png", clip={"x": 0, "y": 0, "width": w, "height": 130})
                ctx.close()
            b.close()
    finally:
        proc.terminate()
        shutil.rmtree(tmp, ignore_errors=True)
    print("ИТОГ:", "ВСЁ ПРОШЛО" if not bad else f"ПРОБЛЕМ: {bad}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
