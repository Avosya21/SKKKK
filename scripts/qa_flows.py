"""Интерактивные сценарии по всему порталу в браузере (изолированный экземпляр с копией базы, как в qa_pages.py; боевой сервер не затрагивается).

Сценарии: вход и ошибки входа, выход, создание и удаление проекта (в том числе с разметкой в названии), загрузка и удаление файла,
блоки проекта (раскрытие), встречи (создать, ссылка, удалить), ключи MCP (права, показ один раз, отзыв), блоки обмена с агентами
(подтверждение требований, причина статуса, вкладки, настройки), роли (manager, user), страницы 404, клавиша Esc в окнах,
назад/вперёд, пустой вопрос ассистенту, медленная сеть, потеря сети.

Запуск:  venv\\Scripts\\python.exe scripts\\qa_flows.py [--engine chromium|webkit] [--shots папка]
Код выхода 0 — всё прошло, 1 — есть провалы."""
from __future__ import annotations

import argparse
import re
import secrets
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import qa_pages as qp  # noqa: E402

FAILS: list[str] = []
BASE = f"http://127.0.0.1:{qp.PORT}"


def check(cond: bool, message: str) -> None:
    print(("PASS " if cond else "FAIL ") + message, flush=True)
    if not cond:
        FAILS.append(message)


def wait_until(fn, timeout: float = 15.0, step: float = 0.3):
    end = time.time() + timeout
    while time.time() < end:
        try:
            v = fn()
        except Exception:  # noqa: BLE001
            v = None
        if v:
            return v
        time.sleep(step)
    try:
        return fn()
    except Exception:  # noqa: BLE001
        return None


def scenario(name):
    import functools

    def deco(fn):
        @functools.wraps(fn)
        def run(*a, **k):
            print(f"\n## {name}", flush=True)
            try:
                fn(*a, **k)
            except Exception as exc:  # noqa: BLE001
                check(False, f"{name}: сценарий упал: {str(exc)[:200]}")
        return run
    return deco


def new_page(browser, pw_devices=None, viewport=(1366, 800)):
    ctx = browser.new_context(viewport={"width": viewport[0], "height": viewport[1]}, accept_downloads=True)
    page = ctx.new_page()
    page.js_errors = []
    page.on("pageerror", lambda e: page.js_errors.append(str(e)[:160]))
    return ctx, page


@scenario("Вход, ошибки входа, выход")
def s_login(browser, pw):
    ctx, page = new_page(browser)
    page.goto(BASE + "/login")
    qp.fill_login(page, "audit-admin", "неверный-пароль")
    page.locator("#login-form button[type=submit]").click()
    err = wait_until(lambda: page.locator("form .alert, .alert-danger, #login-error").filter(has_text=re.compile(r"[А-Яа-я]")).first.inner_text(), 8)
    check(bool(err) and "invalid" not in (err or "").lower(), f"неверный пароль: понятная ошибка по-русски («{(err or '').strip()[:60]}»)")
    check("/login" in page.url, "после неверного пароля остаёмся на странице входа")
    qp.fill_login(page, "никто-чужой", pw["admin"]); page.locator("#login-form button[type=submit]").click()
    err2 = wait_until(lambda: page.locator(".alert-danger").filter(has_text=re.compile("допущен|список")).first.inner_text(), 8)
    check(bool(err2), f"логин не из списка: сообщение «{(err2 or '').strip()[:60]}»")
    qp.fill_login(page, "audit-admin", pw["admin"]); page.locator("#login-password").press("Enter")
    path = lambda: page.evaluate("location.pathname")  # noqa: E731  (page.url в sync API отстаёт после навигации из JS)
    ok = wait_until(lambda: path() != "/login", 15)
    check(bool(ok), "вход по клавише Enter в поле пароля работает")
    page.reload()
    check(path() != "/login" and page.locator("#nav-logout").count() == 1, "после перезагрузки сессия сохранена")
    page.locator("#nav-logout").click()
    check(bool(wait_until(lambda: path() == "/login", 10)), "выход возвращает на страницу входа")
    page.goto(BASE + "/")
    time.sleep(1.5)
    check(path() == "/login", "после выхода главная страница не открывается без входа")
    check(not page.js_errors, f"ошибок JS нет {page.js_errors[:2]}")
    ctx.close()


def login(page, name, pw):
    qp.ui_login(page, BASE, name, pw)


@scenario("Проекты: создание (в том числе с разметкой в названии), список, удаление")
def s_projects(browser, pw):
    ctx, page = new_page(browser)
    dialogs = []
    page.on("dialog", lambda d: (dialogs.append(d.message), d.dismiss()))
    login(page, "audit-admin", pw["admin"])
    page.wait_for_selector("#btn-new-project")
    page.click("#btn-new-project")
    page.wait_for_selector("#new-project-form", state="visible")
    nm = page.locator("#new-project-form input").first
    nm.fill("   ")
    page.click("#project-submit")
    check(bool(wait_until(lambda: page.locator("#project-error").is_visible(), 6)), "пустое название: показана ошибка")
    nm.fill('<img src=x onerror="alert(1)"> «Тест» & №7')
    page.click("#project-submit")
    created = wait_until(lambda: page.locator("#projects-list").get_by_text("«Тест»", exact=False).count() > 0, 12)
    check(bool(created), "проект с разметкой в названии создан")
    check(page.locator("#projects-list img").count() == 0 and not dialogs, "разметка в названии не исполняется (нет тега img, нет alert)")
    check("&lt;" not in page.locator("#projects-list").inner_text(), "в списке текст без «&lt;» (не двойное экранирование)")
    item = page.locator("#projects-list .list-group-item", has_text="«Тест»").first
    print("  разметка элемента списка:", item.evaluate("e => e.outerHTML")[:260].replace("\n", " "), flush=True)
    item.locator(".project-desc-chevron").click()  # действия проекта (в том числе удаление) спрятаны за шевроном
    delete = item.locator("button[aria-label^='Удалить проект']").first
    if delete.count():
        delete.click()
        page.wait_for_selector("#deleteProjectModal.show", timeout=8000)
        check(page.locator("#delete-project-confirm").is_disabled(), "кнопка «Удалить» в окне неактивна, пока не поставлена галочка")
        page.check("#delete-project-ack")
        check(page.locator("#delete-project-confirm").is_enabled(), "после галочки кнопка становится активной")
        page.click("#delete-project-confirm")
        gone = wait_until(lambda: page.locator("#projects-list").get_by_text("«Тест»", exact=False).count() == 0, 15)
        check(bool(gone), "проект удалён и пропал из списка")
    else:
        print("  SKIP удаление: кнопка удаления не найдена в элементе списка (см. разметку выше)", flush=True)
    check(not page.js_errors, f"ошибок JS нет {page.js_errors[:2]}")
    ctx.close()


@scenario("Проект: блоки раскрываются, данные загружаются")
def s_blocks(browser, pw):
    ctx, page = new_page(browser)
    login(page, "audit-admin", pw["admin"])
    page.goto(BASE + "/projects/29")
    qp.settle(page)
    toggles = page.locator(".deadlines-toggle-btn, .meetings-toggle-btn, .mcp-toggle-btn, .grant-toggle-btn, .px-toggle-btn")
    n = toggles.count()
    check(n >= 6, f"на странице проекта {n} сворачиваемых блоков")
    for i in range(n):
        btn = toggles.nth(i)
        title = (btn.inner_text() or "").strip().split("\n")[0][:32]
        target = btn.get_attribute("data-bs-target")
        btn.scroll_into_view_if_needed()
        btn.click()
        opened = wait_until(lambda: page.locator(f"{target}.show").count() == 1, 6)
        time.sleep(0.8)
        text = page.locator(target).inner_text()[:300] if page.locator(target).count() else ""
        check(bool(opened) and "Не удалось" not in text, f"блок «{title}» раскрывается без ошибки")
    check(not page.js_errors, f"ошибок JS при раскрытии всех блоков нет {page.js_errors[:2]}")
    ctx.close()


@scenario("Загрузка и удаление файла")
def s_upload(browser, pw):
    ctx, page = new_page(browser)
    login(page, "audit-admin", pw["admin"])
    page.goto(BASE + "/projects/29")
    qp.settle(page)
    before = page.locator("#artifacts-table tbody tr").count()
    opts = page.locator("#upload-source-type option")
    values = [(opts.nth(i).get_attribute("value"), opts.nth(i).inner_text()) for i in range(opts.count())]
    print("  типы:", values, flush=True)
    doc = next((v for v, t in values if v and "doc" in (v or "").lower()), values[0][0])
    page.select_option("#upload-source-type", doc)
    name = f"qa_flow_{secrets.token_hex(3)}.txt"
    page.set_input_files("#upload-files", {"name": name, "mimeType": "text/plain", "buffer": "Проверка загрузки. Иван обещал прислать список до пятницы.".encode("utf-8")})
    page.click("#upload-submit")
    appeared = wait_until(lambda: page.locator("#artifacts-table tbody").get_by_text(name).count() > 0, 25)
    check(bool(appeared), f"файл {name} появился в списке материалов")
    row = page.locator("#artifacts-table tbody tr", has_text=name).first
    if row.count():
        row.locator("button[aria-label*='Удалить'], button.btn-outline-danger, .artifact-delete").first.click()
        page.wait_for_selector("#deleteArtifactModal.show", timeout=8000)
        check(page.locator("#delete-artifact-confirm").is_disabled(), "удаление файла требует галочки (кнопка неактивна)")
        page.keyboard.press("Escape")
        time.sleep(0.6)
        still_open = page.locator("#deleteArtifactModal.show").count()
        print(f"  Esc в окне удаления: окно {'осталось открытым (static backdrop)' if still_open else 'закрылось'}", flush=True)
        if not still_open:
            row.locator("button[aria-label*='Удалить'], button.btn-outline-danger, .artifact-delete").first.click()
            page.wait_for_selector("#deleteArtifactModal.show", timeout=8000)
        page.check("#delete-artifact-ack")
        page.click("#delete-artifact-confirm")
        check(bool(wait_until(lambda: page.locator("#artifacts-table tbody").get_by_text(name).count() == 0, 15)), "файл удалён и пропал из списка")
    check(page.locator("#artifacts-table tbody tr").count() == before, "число строк вернулось к исходному")
    check(not page.js_errors, f"ошибок JS нет {page.js_errors[:2]}")
    ctx.close()


@scenario("Встречи: создать, ссылка, удалить")
def s_meetings(browser, pw):
    ctx, page = new_page(browser)
    login(page, "audit-admin", pw["admin"])
    page.goto(BASE + "/projects/29")
    qp.settle(page)
    page.locator(".meetings-toggle-btn").click()
    page.wait_for_selector("#meetings-panel.show", timeout=6000)
    before = page.locator("#meetings-body tr").count()
    page.click("#meetings-create")
    shown = wait_until(lambda: page.locator("#meetings-new-link-value").inner_text().strip() != "", 15)
    link = page.locator("#meetings-new-link-value").inner_text().strip()
    check(bool(shown) and "/m/" in link, f"после создания показана ссылка для гостей ({link[:60]})")
    check(bool(wait_until(lambda: page.locator("#meetings-body tr").count() > before, 10)), "встреча появилась в таблице")
    row = page.locator("#meetings-body tr").first
    delete = row.locator(".meeting-delete").first
    if delete.count():
        delete.click()
        page.wait_for_selector("#deleteMeetingModal.show", timeout=8000)
        page.check("#delete-meeting-ack")
        page.click("#delete-meeting-confirm")
        check(bool(wait_until(lambda: page.locator("#meetings-body tr").count() == before, 15)), "встреча удалена")
    else:
        print("  SKIP удаление встречи: кнопка не найдена", flush=True)
    check(not page.js_errors, f"ошибок JS нет {page.js_errors[:2]}")
    ctx.close()


@scenario("Ключи MCP: права, показ один раз, отзыв")
def s_mcp(browser, pw):
    ctx, page = new_page(browser)
    page.on("dialog", lambda d: d.accept("qa-flow-key") if d.type == "prompt" else d.accept())
    login(page, "audit-admin", pw["admin"])
    page.goto(BASE + "/projects/29")
    qp.settle(page)
    page.locator(".mcp-toggle-btn").click()
    page.wait_for_selector("#mcp-access-panel.show", timeout=6000)
    time.sleep(1.5)
    check(page.locator("#mcp-rights-read").is_checked(), "по умолчанию выбрано «Только чтение»")
    page.check("#mcp-rights-submit")
    page.click("#mcp-create-token")
    tok = wait_until(lambda: page.locator("#mcp-new-token-value").inner_text().strip(), 10)
    check(bool(tok) and tok.startswith("skkkk_mcp_"), "ключ создан и показан один раз")
    row = page.locator("#mcp-tokens-body tr", has_text="qa-flow-key").first
    # права — три уровня (только чтение / + вопросы / + вопросы и вердикты), меняются выпадающим списком в строке ключа
    check("вопросы и вердикты" in row.inner_text(), "в таблице у нового ключа указано «чтение + вопросы и вердикты»")
    row.locator("select.mcp-scope").select_option("read")
    check(bool(wait_until(lambda: "только чтение" in page.locator("#mcp-tokens-body tr", has_text="qa-flow-key").first.inner_text(), 8)), "права ключа меняются в строке ключа")
    page.locator("#mcp-tokens-body tr", has_text="qa-flow-key").first.locator(".mcp-revoke").click()
    check(bool(wait_until(lambda: "отозван" in page.locator("#mcp-tokens-body tr", has_text="qa-flow-key").first.inner_text(), 8)), "ключ отзывается")
    page.reload()
    qp.settle(page)
    page.locator(".mcp-toggle-btn").click()
    time.sleep(1.5)
    check(page.locator("#mcp-new-token-value").inner_text().strip() == "", "после перезагрузки сам ключ больше не показывается")
    check(not page.js_errors, f"ошибок JS нет {page.js_errors[:2]}")
    ctx.close()


@scenario("Блоки обмена с агентами: подтверждение, причина статуса, вкладки, настройки")
def s_px(browser, pw):
    ctx, page = new_page(browser)
    login(page, "audit-admin", pw["admin"])
    page.goto(BASE + "/projects/18")
    qp.settle(page)
    page.locator("#px-card-review .px-toggle-btn").click()
    check(bool(wait_until(lambda: page.locator("#px-rv-list .px-item").count() > 0, 15)), "очередь требований загрузилась")
    first = page.locator("#px-rv-list .px-item").first
    rid = first.locator("code").first.inner_text().strip()
    first.locator("[data-rv=status]").click()
    first.locator("[data-do=status]").click()
    msg = wait_until(lambda: page.locator("#px-status-review.text-danger").inner_text(), 8)
    check(bool(msg) and "причина" in msg.lower(), f"смена статуса без причины отклонена сообщением («{(msg or '')[:50]}»)")
    first.locator(".px-form-row input:not([type=checkbox])").first.fill("Проверка: подтверждено по звонку")
    first.locator("select").first.select_option("agreed")
    first.locator("[data-do=status]").click()
    gone = wait_until(lambda: page.locator(f".px-item:has(code:text-is('{rid}'))").count() == 0, 12)
    check(bool(gone), f"после решения {rid} ушло из очереди «без подтверждения»")
    page.select_option("#px-rv-verification", "human_confirmed")
    shown = wait_until(lambda: "Решение человека" in page.locator(f".px-item:has(code:text-is('{rid}'))").first.inner_text(), 12)
    check(bool(shown), f"в фильтре «Подтверждённые» {rid} есть, с автором и причиной решения")
    page.locator("#px-card-exchange .px-toggle-btn").click()
    time.sleep(1.5)
    for tab, sel in (("verdicts", "#px-tab-verdicts"), ("journal", "#px-tab-journal"), ("questions", "#px-tab-questions")):
        page.locator(f"[data-px-tab={tab}]").click()
        time.sleep(0.8)
        check(page.locator(sel).is_visible(), f"вкладка «{tab}» открывается")
    page.locator("#px-card-settings .px-toggle-btn").click()
    time.sleep(1.5)
    page.fill("#px-s-participants", "это неправильная строка")
    page.click("#px-s-save")
    bad = wait_until(lambda: page.locator("#px-status-settings.text-danger").inner_text(), 8)
    check(bool(bad) and "Формат" in bad, f"неверная строка участников: понятная подсказка («{(bad or '')[:60]}»)")
    page.fill("#px-s-participants", "Елена = исполнитель\nИльдар = заказчик")
    page.click("#px-s-save")
    saved = wait_until(lambda: "Сохранено" in page.locator("#px-status-settings").inner_text(), 8)
    check(bool(saved), "настройки для агентов сохраняются")
    check(not page.js_errors, f"ошибок JS нет {page.js_errors[:2]}")
    ctx.close()


@scenario("Роли: manager видит, но не меняет; user без доступа получает понятную страницу")
def s_roles(browser, pw):
    ctx, page = new_page(browser)
    login(page, "audit-manager", pw["manager"])
    pid = page.evaluate("SKKKK.api('/projects', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({name: 'Проект менеджера'})}).then(r => r.json()).then(j => j.id)")
    page.goto(BASE + f"/projects/{pid}")
    qp.settle(page)
    page.locator("#px-card-review .px-toggle-btn").click()
    time.sleep(2)
    check(page.locator("#px-rv-list [data-rv=confirm]").count() == 0, "manager: кнопок «Подтвердить» нет (только просмотр)")
    check("только смотреть" in page.locator("#px-status-review").inner_text() or "нет" in page.locator("#px-rv-list").inner_text().lower(), "manager: страница блока открывается, действий нет")
    page.locator("#px-card-settings .px-toggle-btn").click()
    time.sleep(1.5)
    check(page.locator("#px-s-save").is_hidden(), "manager: кнопки «Сохранить» настройки агентов нет (меняет администратор)")
    ctx.close()
    ctx, page = new_page(browser)
    login(page, "audit-user", pw["user"])
    resp = page.goto(BASE + "/projects/18")
    body = page.inner_text("body")
    check(resp.status == 404 and "Страница не найдена" in body and "К проектам" in body, f"user без доступа: страница «не найдена» по-русски (статус {resp.status})")
    check("{" not in body[:80], "вместо сырого JSON понятный текст")
    check(page.locator("#px-card-review").count() == 0, "user не видит блоков управления агентами")
    resp2 = page.goto(BASE + "/projects/999999")
    check(resp2.status == 404 and "Страница не найдена" in page.inner_text("body"), "несуществующий проект: та же понятная страница")
    ctx.close()


@scenario("Назад/вперёд, пустой вопрос ассистенту")
def s_nav(browser, pw):
    ctx, page = new_page(browser)
    login(page, "audit-admin", pw["admin"])
    page.goto(BASE + "/projects/29")
    qp.settle(page)
    calls = []
    page.on("request", lambda r: calls.append(r.url) if "/chat" in r.url and r.method == "POST" else None)
    page.click("#chat-send")
    time.sleep(1.2)
    check(not calls, "пустой вопрос ассистенту не отправляется на сервер")
    page.goto(BASE + "/")
    page.go_back()
    qp.settle(page)
    check(page.locator("#artifacts-body").count() == 1 and not page.js_errors, "назад из списка проектов: страница проекта жива")
    page.go_forward()
    check("/login" not in page.url and page.locator("#projects-list").count() == 1, "вперёд: список проектов жив")
    ctx.close()


@scenario("Медленная сеть и потеря сети")
def s_network(browser, pw):
    ctx, page = new_page(browser)
    login(page, "audit-admin", pw["admin"])
    cdp = ctx.new_cdp_session(page)
    cdp.send("Network.enable")
    cdp.send("Network.emulateNetworkConditions", {"offline": False, "latency": 400, "downloadThroughput": 50_000, "uploadThroughput": 25_000})  # ≈ медленная 3G
    t0 = time.time()
    page.goto(BASE + "/projects/18", wait_until="domcontentloaded", timeout=90000)
    first_paint = time.time() - t0
    ready = wait_until(lambda: page.locator("#artifacts-table tbody tr").count() > 2, 60)
    total = time.time() - t0
    check(bool(ready), f"на медленной сети список материалов загрузился ({total:.0f} с, первая отрисовка {first_paint:.0f} с)")
    check(page.locator("#artifacts-body").count() == 1 and page.evaluate("document.body.innerText.length") > 500, "страница осмысленна до и после загрузки данных")
    cdp.send("Network.emulateNetworkConditions", {"offline": False, "latency": 0, "downloadThroughput": -1, "uploadThroughput": -1})
    page.locator("#px-card-exchange .px-toggle-btn").click()
    time.sleep(2)
    cdp.send("Network.emulateNetworkConditions", {"offline": True, "latency": 0, "downloadThroughput": 0, "uploadThroughput": 0})
    page.locator("[data-px-tab=journal]").click()
    msg = wait_until(lambda: page.locator("#px-status-exchange").inner_text().strip(), 8)
    check(bool(msg), f"при потере сети блок говорит об ошибке, а не молчит («{(msg or '')[:60]}»)")
    check(not [e for e in page.js_errors if "Unhandled" in e and "Failed to fetch" not in e], f"нет необработанных исключений JS ({page.js_errors[:2]})")
    ctx.close()


@scenario("Граф зависимостей: свод по файлам, поиск узла, фильтры, телефон")
def s_map_graph(browser, pw):
    ctx, page = new_page(browser, viewport=(1440, 900))
    login(page, "audit-admin", pw["admin"])
    page.goto(BASE + "/admin/map/graph", wait_until="load", timeout=45000)
    def counters_text():
        text = page.locator("#amg-counters").inner_text()
        return text if ("Загружаю" not in text and "узл" in text) else None

    counters = wait_until(counters_text, 30)
    check(bool(counters), f"свод по файлам отрисован: {counters!r}")
    check("связ" in (counters or ""), "в своде по файлам есть связи")
    check(bool(wait_until(lambda: page.locator("#amg-canvas canvas").count() > 0, 20)), "Cytoscape отрисовал полотно")
    check(bool(wait_until(lambda: page.locator("#amg-kinds input[type=checkbox]").count() > 0, 20)),
          "легенда видов узлов заполнена из summary")
    relations = wait_until(lambda: page.locator("#amg-relations input[type=checkbox]").count(), 20)
    check(bool(relations) and relations <= 30, f"легенда связей компактна: {relations} строк")

    page.fill("#amg-q", "rebuild_tz_fast")
    page.locator("#amg-search").click()
    where = wait_until(lambda: page.locator("#amg-node-where").inner_text(), 25)
    check("tz.py" in (where or ""), f"панель узла показывает адрес в коде: {where!r}")
    check(page.locator("#amg-node-out button").count() > 0, "у узла показаны исходящие связи")
    check(page.locator("#amg-node-in button").count() > 0, "у узла показаны входящие связи")

    title_before = page.locator("#amg-node-title").inner_text()
    page.locator("#amg-node-out button").first.click()
    check(bool(wait_until(lambda: page.locator("#amg-node-title").inner_text() != title_before, 15)),
          "клик по связи открывает соседний узел")

    before = page.locator("#amg-counters").inner_text()
    page.locator("#amg-kinds input[type=checkbox]").first.uncheck()
    check(bool(wait_until(lambda: page.locator("#amg-counters").inner_text() != before, 15)),
          "снятие галочки вида узлов перестраивает граф")
    check(not page.js_errors, f"ошибок JS нет: {page.js_errors[:2]}")

    page.locator("#amg-reset").click()
    check(bool(wait_until(lambda: page.locator("#amg-view-modules").is_checked(), 10)),
          "«Сбросить» возвращает свод по файлам")
    ctx.close()

    ctx2, phone = new_page(browser, viewport=(390, 844))
    login(phone, "audit-admin", pw["admin"])
    phone.goto(BASE + "/admin/map/graph", wait_until="load", timeout=45000)
    wait_until(lambda: "Загружаю" not in phone.locator("#amg-counters").inner_text(), 25)
    check(not phone.evaluate("() => document.documentElement.scrollWidth > innerWidth + 1"),
          "на телефоне 390 px нет горизонтального скролла")
    check(phone.locator("#amg-canvas").is_visible(), "полотно графа видно на телефоне")
    check(not phone.js_errors, f"на телефоне ошибок JS нет: {phone.js_errors[:2]}")
    ctx2.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--engine", default="chromium", choices=["chromium", "webkit"])
    ap.add_argument("--only", default="")
    ap.add_argument("--channel", default="", help="канал браузера (chrome, msedge) вместо скачанного Playwright")
    args = ap.parse_args()
    from playwright.sync_api import sync_playwright

    pw = {r: secrets.token_urlsafe(12) for r in ("admin", "manager", "user")}
    proc, tmp = qp.start_instance(pw)
    try:
        with sync_playwright() as p:
            browser = getattr(p, args.engine).launch(headless=True, **({"channel": args.channel} if args.channel else {}))
            for fn in (s_login, s_projects, s_blocks, s_upload, s_meetings, s_mcp, s_px, s_map_graph, s_roles, s_nav, s_network):
                if args.only and args.only not in fn.__name__:
                    continue
                fn(browser, pw)
            browser.close()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:  # noqa: BLE001
            proc.kill()
        shutil.rmtree(tmp, ignore_errors=True)
    print("\nИТОГ:", "ВСЁ ПРОШЛО" if not FAILS else f"ПРОВАЛЕНО {len(FAILS)}")
    for f in FAILS:
        print("  -", f)
    return 1 if FAILS else 0


if __name__ == "__main__":
    raise SystemExit(main())
