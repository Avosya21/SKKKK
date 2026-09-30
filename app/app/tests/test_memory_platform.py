"""Платформа памяти решений (docs/plans/PLATFORM-FOUNDATION-PLAN.md)."""


def test_memory_platform_flag_project_overrides_env():
    """ПФ0: настройка проекта memory_platform главнее общей; по умолчанию выключено."""
    from app.app.config import get_settings
    from app.app.services import memory_platform

    class P:
        def __init__(self, data):
            self._d = data

        def settings(self):
            return self._d

    off = get_settings().model_copy(update={"memory_platform": False})
    on = get_settings().model_copy(update={"memory_platform": True})
    assert memory_platform.enabled(off) is False and memory_platform.enabled(on) is True
    assert memory_platform.enabled(off, P({"memory_platform": True})) is True
    assert memory_platform.enabled(on, P({"memory_platform": False})) is False
    assert memory_platform.enabled(off, P({})) is False and memory_platform.enabled(off, P({"memory_platform": "on"})) is True
