"""Охват карты: какая доля существующего в проекте описана и связана в графе, по слоям.

Проба (`Probe`) независимо от графа находит, что в проекте есть (`total`), и проверяет, сколько из этого есть в графе
(`covered`). Охват — измеримая полнота: у каждой пробы своя цель, ниже цели слой считается незакрытым.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

from projectmap.schema import LAYER_IDS, layers_by_id

MISSING_LIMIT = 200  # сколько примеров пропусков хранить в отчёте


@dataclass
class Coverage:
    """Результат одной пробы: что есть в проекте, сколько из этого в графе и чего не хватает."""

    layer: str
    probe: str
    title: str
    total: int
    covered: int
    missing: list[str] = field(default_factory=list)
    note: str = ""
    target: float | None = None

    @property
    def ratio(self) -> float | None:
        """Доля охваченного; None, если в проекте нечего охватывать (проба не применима)."""
        return None if self.total == 0 else self.covered / self.total

    @property
    def status(self) -> str:
        """`n/a` — нечего охватывать; `ok` — цель достигнута или не задана; `gap` — ниже цели."""
        if self.ratio is None:
            return "n/a"
        if self.target is not None and self.ratio < self.target:
            return "gap"
        return "ok"

    def to_dict(self) -> dict[str, Any]:
        return {"layer": self.layer, "probe": self.probe, "title": self.title, "total": self.total, "covered": self.covered,
                "ratio": self.ratio, "target": self.target, "status": self.status, "note": self.note,
                "missing": self.missing[:MISSING_LIMIT]}


Probe = Callable[[Any], list[Coverage]]  # аргумент — ProjectContext (модуль context)


def make_coverage(layer: str, probe: str, title: str, expected: Iterable[str], present: set[str], note: str = "") -> Coverage:
    """Охват по списку ожидаемых имён: покрыто то, что есть в `present`; пропуски — для отчёта."""
    expected = list(dict.fromkeys(expected))
    missing = [item for item in expected if item not in present]
    return Coverage(layer, probe, title, len(expected), len(expected) - len(missing), sorted(missing)[:MISSING_LIMIT], note)


@dataclass
class Report:
    coverages: list[Coverage]

    def by_layer(self) -> dict[str, list[Coverage]]:
        out: dict[str, list[Coverage]] = {layer: [] for layer in LAYER_IDS}
        for item in self.coverages:
            out.setdefault(item.layer, []).append(item)
        return out

    def gaps(self) -> list[Coverage]:
        return [c for c in self.coverages if c.status == "gap"]

    def layer_ratio(self, layer: str) -> float | None:
        """Средний охват слоя по применимым пробам; None, если проб нет."""
        ratios = [c.ratio for c in self.coverages if c.layer == layer and c.ratio is not None]
        return sum(ratios) / len(ratios) if ratios else None

    def to_dict(self) -> dict[str, Any]:
        return {"layers": {layer: self.layer_ratio(layer) for layer in LAYER_IDS},
                "gaps": len(self.gaps()), "coverages": [c.to_dict() for c in self.coverages]}


def _pct(value: float | None) -> str:
    return "—" if value is None else f"{value * 100:.0f}%"


STATUS_MARK = {"ok": "✓", "gap": "✗", "n/a": "·"}


def render_text(report: Report) -> str:
    """Отчёт для консоли: по слоям, у каждой пробы охват, цель и число пропусков."""
    layers = layers_by_id()
    lines: list[str] = []
    for layer_id, items in report.by_layer().items():
        if not items:
            lines.append(f"{layers[layer_id].title}: проб нет")
            continue
        lines.append(f"{layers[layer_id].title}: {_pct(report.layer_ratio(layer_id))}")
        for c in items:
            target = f", цель {_pct(c.target)}" if c.target is not None else ""
            lines.append(f"  {STATUS_MARK[c.status]} {c.title}: {c.covered}/{c.total} ({_pct(c.ratio)}{target})"
                         + (f" — {c.note}" if c.note else ""))
    lines.append(f"Ниже цели: {len(report.gaps())}")
    return "\n".join(lines)


def render_markdown(report: Report, *, examples: int = 8) -> str:
    """Отчёт для документа: сводка по слоям, таблица проб и примеры пропусков."""
    layers = layers_by_id()
    lines = ["| Слой | Охват | Ниже цели |", "|---|---|---|"]
    for layer_id, items in report.by_layer().items():
        gap = sum(1 for c in items if c.status == "gap")
        lines.append(f"| {layers[layer_id].title} | {_pct(report.layer_ratio(layer_id))} | {gap if items else '—'} |")
    lines += ["", "| Слой | Проба | Есть в проекте | В графе | Охват | Цель | |", "|---|---|---|---|---|---|---|"]
    for layer_id, items in report.by_layer().items():
        for c in items:
            lines.append(f"| {layers[layer_id].title} | {c.title} | {c.total} | {c.covered} | {_pct(c.ratio)} | {_pct(c.target)} | {STATUS_MARK[c.status]} |")
    shown = [c for c in report.coverages if c.missing]
    if shown:
        lines += ["", "**Примеры пропусков**", ""]
        for c in shown:
            sample = ", ".join(f"`{m}`" for m in c.missing[:examples])
            more = f" и ещё {len(c.missing) - examples}" if len(c.missing) > examples else ""
            lines.append(f"- {c.title}: {sample}{more}" + (f" ({c.note})" if c.note else ""))
    return "\n".join(lines)
