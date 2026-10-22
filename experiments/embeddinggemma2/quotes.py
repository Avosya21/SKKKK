"""Цитаты тем памяти решений: python quotes.py 23 480 ..."""
import sys
from gold_tools import db
c = db()
for sid in sys.argv[1:]:
    t = c.execute("SELECT project_id, title FROM subjects WHERE id=?", (sid,)).fetchone()
    print(f"== S{sid} p{t[0]} {t[1]}")
    for r in c.execute("SELECT e.id, e.kind, substr(e.occurred_at,1,10), e.artifact_id, e.anchor, e.quote FROM decision_events e WHERE subject_id=? AND is_active AND quote IS NOT NULL ORDER BY occurred_at", (sid,)):
        print(f"  e{r[0]} {r[1]} {r[2]} a{r[3]} {r[4]}: {(r[5] or '')[:260]}")
