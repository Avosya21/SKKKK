"""Presence симметричен (пункт 1.1): у сотрудника есть heartbeat и leave."""
from common import *

c, _ = api_login("admin")
mid, tok = new_live_meeting(c)
ident = c.post(f"/projects/{PID}/meetings/{mid}/join").json()["identity"]
base = f"/projects/{PID}/meetings/{mid}"
check(c.post(f"{base}/heartbeat", json={"identity": ident}).status_code == 204, "heartbeat сотрудника")
check(c.post(f"{base}/heartbeat", json={"identity": "member:999999"}).status_code == 403, "чужой identity -> 403")
check(c.post(f"{base}/leave", json={"identity": ident}).status_code == 204, "leave сотрудника")
c.post(f"{base}/end")
