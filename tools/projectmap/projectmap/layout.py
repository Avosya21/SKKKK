"""Раскладка графа заранее: координаты каждого узла считает инструмент, просмотрщик их только рисует.

Раскладка в браузере на тысячах узлов занимает десятки секунд, поэтому она посчитана здесь и записана в данные карты.
Два уровня: сначала группы (подсистемы) — круги, размер которых зависит от числа узлов, а расстояние между ними — от связей
между группами, затем узлы внутри каждой группы силовой раскладкой (притяжение по связям, отталкивание, лёгкая тяга к центру
круга). Результат воспроизводим: начальные позиции берутся из генератора с фиксированным зерном.
"""

from __future__ import annotations

import math
from collections import defaultdict
from typing import Iterable

import numpy as np

SEED = 7
NODE_AREA = 520.0  # площадь круга группы на один узел: чем больше, тем просторнее раскладка
GROUP_GAP = 60.0  # зазор между кругами групп


def _repulse_overlaps(pos: np.ndarray, radius: np.ndarray, gap: float, rounds: int = 400) -> None:
    """Раздвинуть круги так, чтобы они не пересекались (с зазором gap)."""
    n = len(pos)
    for _ in range(rounds):
        delta = pos[:, None, :] - pos[None, :, :]
        dist = np.sqrt((delta ** 2).sum(-1)) + 1e-9
        need = radius[:, None] + radius[None, :] + gap
        overlap = np.clip(need - dist, 0, None)
        np.fill_diagonal(overlap, 0)
        if overlap.max() < 0.5:
            return
        push = (delta / dist[..., None]) * (overlap[..., None] / 2)
        pos += push.sum(axis=1) * 0.6 + np.random.default_rng(1).normal(0, 1e-3, (n, 2))


def layout_groups(sizes: dict[str, int], links: dict[tuple[str, str], float]) -> dict[str, tuple[float, float, float]]:
    """Круги групп: {группа: (x, y, радиус)}. Связанные группы ближе друг к другу, круги не пересекаются."""
    names = sorted(sizes)
    if not names:
        return {}
    radius = np.array([max(40.0, math.sqrt(sizes[g] * NODE_AREA / math.pi)) for g in names])
    n = len(names)
    rng = np.random.default_rng(SEED)
    pos = rng.uniform(-1, 1, (n, 2)) * radius.sum() / 3
    index = {g: i for i, g in enumerate(names)}
    weight = np.zeros((n, n))
    for (a, b), w in links.items():
        if a in index and b in index and a != b:
            weight[index[a], index[b]] += w
            weight[index[b], index[a]] += w
    weight = np.log1p(weight)
    scale = weight.max() or 1.0
    weight /= scale
    for step in range(300):
        delta = pos[:, None, :] - pos[None, :, :]
        dist = np.sqrt((delta ** 2).sum(-1)) + 1e-9
        ideal = radius[:, None] + radius[None, :] + GROUP_GAP * 2
        pull = weight * (dist - ideal)  # связанные тянутся друг к другу до расстояния «рядом»
        push = np.clip(ideal * 1.2 - dist, 0, None) * (1 - weight * 0.5)  # несвязанные расталкиваются
        force = -(delta / dist[..., None]) * (pull - push)[..., None]
        pos += force.sum(axis=1) * (0.15 * (1 - step / 300) + 0.02)
        pos -= pos.mean(axis=0) * 0.02  # слабая тяга к общему центру
    _repulse_overlaps(pos, radius, GROUP_GAP)
    return {g: (float(pos[i, 0]), float(pos[i, 1]), float(radius[i])) for g, i in index.items()}


def _spiral(count: int, inner: float, outer: float) -> list[tuple[float, float]]:
    """Точки по спирали (золотой угол) в кольце от inner до outer: ровно, без наложений."""
    golden = math.pi * (3 - math.sqrt(5))
    return [(math.cos(k * golden) * (r := math.sqrt(inner ** 2 + (outer ** 2 - inner ** 2) * (k + 0.5) / count)), math.sin(k * golden) * r) for k in range(count)]


def _layout_inside(ids: list[str], edges: list[tuple[str, str, float]], radius: float, weights: dict[str, float]) -> dict[str, tuple[float, float]]:
    """Раскладка узлов одной группы внутри круга радиусом radius, центр круга — начало координат.

    Связанные узлы — силовая раскладка (притяжение по связям, отталкивание, тяга к центру), занимает центр круга;
    узлы без связей внутри группы — по спирали в кольце вокруг, чтобы не сливаться с остальными.
    """
    linked_ids = sorted({i for a, b, _ in edges for i in (a, b) if i in set(ids)})
    lonely = [i for i in sorted(ids) if i not in set(linked_ids)]
    result: dict[str, tuple[float, float]] = {}
    inner_radius = radius * (0.72 if lonely and linked_ids else 1.0)
    if len(linked_ids) == 1:
        result[linked_ids[0]] = (0.0, 0.0)
    elif linked_ids:
        n = len(linked_ids)
        index = {i: k for k, i in enumerate(linked_ids)}
        rng = np.random.default_rng(SEED + n)
        angle = rng.uniform(0, 2 * math.pi, n)
        spread = inner_radius * np.sqrt(rng.uniform(0.05, 1, n)) * 0.8
        pos = np.stack([np.cos(angle) * spread, np.sin(angle) * spread], axis=1)
        adjacency = np.zeros((n, n))
        for a, b, w in edges:
            if a in index and b in index and a != b:
                adjacency[index[a], index[b]] += w
                adjacency[index[b], index[a]] += w
        adjacency = np.log1p(adjacency)
        degree = np.maximum((adjacency > 0).sum(axis=1), 1)
        adjacency = adjacency / np.sqrt(degree[:, None] * degree[None, :]) * math.sqrt(degree.mean())  # у хаба притяжение к каждому соседу слабее
        mass = np.array([1.0 + math.log1p(weights.get(i, 0.0)) for i in linked_ids])
        ideal = inner_radius * math.sqrt(math.pi / n) * 1.1  # типичное расстояние между соседними узлами
        for step in range(220):
            delta = pos[:, None, :] - pos[None, :, :]
            dist = np.sqrt((delta ** 2).sum(-1)) + 1e-9
            unit = delta / dist[..., None]
            repel = (ideal * ideal) / dist * mass[None, :]
            attract = adjacency * dist * dist / ideal
            force = (unit * (repel - attract)[..., None]).sum(axis=1)
            force -= pos * 0.6 * ideal / max(inner_radius, 1.0)
            length = np.sqrt((force ** 2).sum(-1)) + 1e-9
            temperature = ideal * 0.9 * (1 - step / 220) + 0.05
            pos += force / length[:, None] * np.minimum(length, temperature)[:, None]
        dist = np.sqrt((pos ** 2).sum(-1))
        # связанные узлы заполняют свой круг: расстояние до центра наполовину выравнивается по рангу (равномерная плотность), наполовину сохраняется
        rank = np.argsort(np.argsort(dist)) + 0.5
        target = inner_radius * 0.92 * np.sqrt(rank / n)
        scaled = dist * (inner_radius * 0.85 / max(float(np.percentile(dist, 90)), 1e-6))
        blended = 0.5 * target + 0.5 * scaled
        pos *= (blended / np.maximum(dist, 1e-9))[:, None]
        dist = np.sqrt((pos ** 2).sum(-1))
        over = dist > inner_radius * 0.98
        pos[over] *= (inner_radius * 0.98 / dist[over])[:, None]
        result.update({i: (float(pos[index[i], 0]), float(pos[index[i], 1])) for i in linked_ids})
    if lonely:
        ring = _spiral(len(lonely), inner_radius * 1.02 if linked_ids else 0.0, radius * 0.95)
        result.update({i: ring[k] for k, i in enumerate(lonely)})
    return result


def layout_nodes(group_of: dict[str, str], edges: Iterable[tuple[str, str, float]], weight: dict[str, float] | None = None) -> tuple[dict[str, tuple[float, float]], dict[str, tuple[float, float, float]]]:
    """Координаты узлов и кругов групп. group_of — узел -> группа; edges — (узел, узел, вес); weight — «масса» узла (например fan-in)."""
    weight = weight or {}
    edges = [e for e in edges if e[0] in group_of and e[1] in group_of]
    members: dict[str, list[str]] = defaultdict(list)
    for node, group in group_of.items():
        members[group].append(node)
    links: dict[tuple[str, str], float] = defaultdict(float)
    inner: dict[str, list[tuple[str, str, float]]] = defaultdict(list)
    for a, b, w in edges:
        ga, gb = group_of[a], group_of[b]
        if ga == gb:
            inner[ga].append((a, b, w))
        else:
            links[(ga, gb)] += w
    circles = layout_groups({g: len(v) for g, v in members.items()}, links)
    positions: dict[str, tuple[float, float]] = {}
    for group, ids in members.items():
        cx, cy, radius = circles[group]
        local = _layout_inside(sorted(ids), inner[group], radius, weight)
        for node, (x, y) in local.items():
            positions[node] = (round(cx + x, 1), round(cy + y, 1))
    return positions, {g: (round(x, 1), round(y, 1), round(r, 1)) for g, (x, y, r) in circles.items()}
