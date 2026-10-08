"""Локальная модель по профилю tools/lmstudio/qwen-profile.json — те же функции, что у портала (services/llm_keeper.py).

  venv\\Scripts\\python.exe scripts\\llm_model.py status    # загружена ли, совпадает ли с профилем, включена ли автозагрузка
  venv\\Scripts\\python.exe scripts\\llm_model.py load      # загрузить по профилю (или сверить уже загруженную)
  venv\\Scripts\\python.exe scripts\\llm_model.py unload    # выгрузить
  venv\\Scripts\\python.exe scripts\\llm_model.py jit-off   # выключить автозагрузку LM Studio (нужен перезапуск сервера LM Studio)

Код выхода 0 — успех; 1 — ошибка (текст в выводе).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.app.config import Settings  # noqa: E402
from app.app.services import llm_keeper  # noqa: E402


def jit_off() -> bool:
    """True — файл изменён (автозагрузка была включена)."""
    path = llm_keeper.lmstudio_home() / ".internal" / "http-server-config.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("justInTimeModelLoading") is False:
        return False
    data["justInTimeModelLoading"] = False
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return True


def main(argv: list[str]) -> int:
    cmd = argv[1] if len(argv) > 1 else "status"
    settings = Settings()
    try:
        if cmd == "load":
            llm_keeper.ensure_loaded(settings, reason="scripts/llm_model.py")
        elif cmd == "unload":
            llm_keeper.unload(settings, reason="scripts/llm_model.py")
        elif cmd == "jit-off":
            print("автозагрузка выключена" if jit_off() else "автозагрузка уже была выключена")
            return 0
        elif cmd != "status":
            print(__doc__)
            return 1
        llm_keeper._status_cache.update(at=-1e9, value=None)
        print(json.dumps(llm_keeper.keeper_status(settings), ensure_ascii=False))
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"ошибка: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
