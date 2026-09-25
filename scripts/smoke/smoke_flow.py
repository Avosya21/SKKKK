"""Песочница: загрузка документа -> встреча -> гость и сотрудник входят (API)."""
from common import *

c, _ = api_login("admin")
doc = (
    "Протокол встречи по проекту песочницы.\n\nРешили: обмен с Bitrix24 делаем через коннектор раз в час.\n"
    "Договорились: тестовый контур подключает Петров до пятницы.\n"
)
r = c.post(
    f"/projects/{PID}/upload",
    files=[("files", ("smoke_protocol.txt", doc.encode(), "text/plain"))],
    data={"source_type": "document"},
)
check(r.status_code in (200, 201, 202), f"загрузка документа ({r.status_code})")
mid, tok = new_live_meeting(c)
g = c.post(f"/api/meetings/public/{tok}/join", json={"display_name": "Гость-смоук", "guest_id": "smoke-1"})
check(g.status_code == 200 and g.json()["identity"].startswith("guest:"), "гость входит")
s = c.post(f"/projects/{PID}/meetings/{mid}/join")
check(s.status_code == 200 and s.json()["identity"].startswith("member:"), "сотрудник входит")
check(c.post(f"/projects/{PID}/meetings/{mid}/end").status_code in (200, 204), "встреча завершается")
