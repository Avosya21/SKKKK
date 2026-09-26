"""Роли на одной учётке не перебивают друг друга (пункт 1.2)."""
from common import *

adm, ra = api_login("admin")
mgr, rm = api_login("manager")  # менеджер входит ПОСЛЕ админа
check(ra["role"] == "admin" and rm["role"] == "manager", "роли в ответе логина")
check(adm.get("/admin/conflicts").status_code != 403, "админ после входа менеджера сохраняет права")
check(mgr.get("/admin/conflicts").status_code == 403, "менеджеру админка закрыта")
