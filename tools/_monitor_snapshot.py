import sqlite3
from pathlib import Path

db = Path(r"C:\1C_NEW_SKKKK\data\app.db")
con = sqlite3.connect(str(db))
con.row_factory = sqlite3.Row
print("recent jobs:")
for r in con.execute(
    "select id, artifact_id, status, attempts, substr(ifnull(error_message,''),1,120) as err "
    "from jobs order by id desc limit 8"
):
    print(dict(r))
print("error artifacts:")
for r in con.execute(
    "select artifact_id, original_filename, status, substr(ifnull(error_message,''),1,120) as err "
    "from artifacts where status='error' order by id desc limit 5"
):
    print(dict(r))
