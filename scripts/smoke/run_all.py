"""Запуск всего смоук-набора. Код выхода 0 — всё зелёное (SKIP не считается провалом)."""
import os
import subprocess
import sys
from pathlib import Path

here = Path(__file__).parent
rows, failed = [], 0
for name in ("smoke_perm", "smoke_presence", "smoke_flow", "smoke_room", "smoke_duplicate_tab", "smoke_guest_lobby", "smoke_tiles", "smoke_double_click", "smoke_stable_dom", "smoke_console", "smoke_forms", "smoke_end_confirm", "smoke_counters", "smoke_delete_live_guest", "smoke_widths_a11y", "smoke_agent_exchange", "smoke_agent_cards"):
    r = subprocess.run(
        [sys.executable, str(here / f"{name}.py")], capture_output=True, text=True, encoding="utf-8", cwd=here,
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )
    status = "OK" if r.returncode == 0 else ("SKIP" if r.returncode == 77 else "FAIL")
    failed += status == "FAIL"
    rows.append((name, status))
    if status == "FAIL":
        print(r.stdout[-800:], r.stderr[-800:])
print("\n".join(f"{s:5} {n}" for n, s in rows))
sys.exit(1 if failed else 0)
