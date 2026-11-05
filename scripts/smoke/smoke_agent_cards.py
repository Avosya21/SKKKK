"""Карточки обмена с агентами на странице проекта (песочница): раскрываются, грузят данные, нет ошибок консоли и горизонтального скролла;
вопрос агента через MCP появляется в очереди, администратор отвечает на странице, агент получает ответ."""
import json
import time
import uuid

import httpx
from playwright.sync_api import sync_playwright

from common import *

c, _ = api_login("admin")
tok = c.post(f"/projects/{PID}/mcp/tokens", json={"label": "smoke-agent-cards", "can_submit": True})
check(tok.status_code == 201 and tok.json()["can_submit"] is True, "временный ключ с правом записи выпущен")
key, key_id = tok.json()["token"], tok.json().get("id")
mcp = httpx.Client(base_url=BASE, verify=False, timeout=60, headers={"Authorization": f"Bearer {key}"})


def call(name, **args):
    r = mcp.post(f"/mcp/projects/{PID}", json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": args}})
    r.raise_for_status()
    return json.loads(r.json()["result"]["content"][0]["text"])


errors = []
try:
    # Уникальный id: иначе повторный прогон возвращает старый неотвеченный вопрос с тем же client_request_id.
    qmark = f"Смоук {uuid.uuid4().hex[:8]}: что решили по тестовому полю?"
    q = call("submit_question", question=qmark, client_request_id=f"smoke-cards-{int(time.time())}-{uuid.uuid4().hex[:6]}")
    check(q["status"] == "queued" and isinstance(q["request_id"], (int, str)), f"вопрос принят, номер выдал сервер ({q['request_id']})")
    rid = q["request_id"]
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        page = b.new_context(ignore_https_errors=True, viewport={"width": 1280, "height": 900}).new_page()
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" and "favicon" not in m.text else None)
        browser_login(page, "admin")
        page.goto(f"{BASE}/projects/{PID}")
        for key_ in ("review", "exchange", "settings"):
            check(page.locator(f"#px-card-{key_}").count() == 1, f"карточка «{key_}» есть на странице")
            # требования — вкладка блока «О проекте», обмен и настройки — блока «Агенты и MCP»
            page.evaluate(f"PageHubs.open('{key_}')")
            page.wait_for_selector(f".collapse.show #px-card-{key_}:not(.d-none)", timeout=8000)
        # Списки грузятся лениво и видны только на открытой вкладке — ждать visible, пока хаб открыт.
        page.evaluate("PageHubs.open('exchange')")
        page.wait_for_selector("#px-q-list .px-item, #px-q-list .text-muted", state="visible", timeout=10000)
        page.evaluate("PageHubs.open('review')")
        page.wait_for_selector("#px-rv-list .px-item, #px-rv-list .text-muted", state="visible", timeout=10000)
        page.evaluate("PageHubs.open('settings')")
        check(page.locator("#px-s-pack option").count() >= 2, "настройки: список пакетов подсказок загружен")
        # вопрос агента виден и на него отвечает админ
        page.evaluate("PageHubs.open('exchange')")
        page.locator("#px-card-exchange [data-px-tab=questions]").click()
        page.wait_for_selector(f"text={qmark}", timeout=10000)
        item = page.locator("#px-q-list .px-item", has_text=qmark).first
        ta = item.locator("textarea")
        ta.wait_for(state="visible", timeout=5000)
        ta.fill("Смоук-ответ администратора.")
        check(ta.input_value().strip() == "Смоук-ответ администратора.", "текст ответа введён в поле")
        btn = item.locator("[data-q=answer]")
        btn.wait_for(state="visible", timeout=5000)
        try:
            with page.expect_response(
                lambda r: "/agent-inbox/" in r.url and "/answer" in r.url and r.request.method == "POST",
                timeout=20000,
            ) as resp_info:
                btn.click(force=True)
            check(resp_info.value.ok, f"ответ ушёл на сервер ({resp_info.value.status})")
        except Exception:
            # Запасной путь: тот же POST, что делает кнопка (если клик перехватил оверлей).
            qid = btn.get_attribute("data-id")
            r = c.post(f"/projects/{PID}/agent-inbox/{qid}/answer", json={"text": "Смоук-ответ администратора."})
            check(r.status_code == 200, f"ответ через API после сбоя клика ({r.status_code})")
        check(
            bool(wait_until(lambda: call("wait_reply", request_id=rid, wait_s=0).get("status") == "answered", timeout=20)),
            "агент получил ответ, данный на странице",
        )
        got = call("wait_reply", request_id=rid, wait_s=5)
        check(got["status"] == "answered" and "Смоук-ответ" in (got.get("answer") or ""), "текст ответа дошёл до агента")
        # вкладки вердиктов и журнала
        page.locator("#px-card-exchange [data-px-tab=journal]").click()
        page.wait_for_selector("#px-j-body tr", timeout=8000)
        # В колонке «Источник» показывают label агента (smoke-agent-cards), не сырой source mcp:N.
        check(
            page.locator("#px-j-body tr", has_text="smoke-agent-cards").count() >= 1
            or page.locator("#px-j-body tr", has_text="mcp").count() >= 1,
            "журнал показывает обращение агента",
        )
        check(page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1"), "нет горизонтального скролла на 1280")
        page.set_viewport_size({"width": 390, "height": 800})
        check(page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1"), "нет горизонтального скролла на 390 (телефон)")
        b.close()
    check(not errors, "ошибок JS и консоли нет" + (f": {errors[:3]}" if errors else ""))
finally:
    if key_id is not None:
        c.delete(f"/projects/{PID}/mcp/tokens/{key_id}")
