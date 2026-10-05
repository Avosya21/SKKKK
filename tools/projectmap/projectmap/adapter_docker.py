"""Адаптер Docker: сервисы compose-файлов как узлы `service` и их связи с файлами проекта.

Для каждого сервиса: образ, порты, контекст сборки, переменные окружения. Связи: файл compose `declares` сервис; сервис `runs`
Dockerfile сборки и файлы проекта, названные в `command` / `entrypoint`; `refers_to` файлы из `env_file`; `declares` переменные окружения сервиса.
"""

from __future__ import annotations

import posixpath
import re
from typing import Any

import yaml

from projectmap.adapters import Adapter, AdapterContext
from projectmap.graphbuf import env_id, file_id, service_id

COMPOSE_NAMES = ("docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml")


def _composes(ctx: AdapterContext) -> list[str]:
    return [p for p in ctx.paths() if p.rsplit("/", 1)[-1] in COMPOSE_NAMES]


def _applies(ctx: AdapterContext) -> bool:
    return bool(_composes(ctx))


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    return [str(v) for v in value]


def _env_names(value: Any) -> list[str]:
    if isinstance(value, dict):
        return [str(k) for k in value]
    return [str(v).split("=", 1)[0] for v in (value or [])]


def _rel(base: str, target: str) -> str:
    return posixpath.normpath(posixpath.join(base, target)) if base else posixpath.normpath(target)


def _run(ctx: AdapterContext) -> None:
    files = set(ctx.facts)
    count = 0
    for path in _composes(ctx):
        try:
            data = yaml.safe_load(ctx.text(path) or "") or {}
        except yaml.YAMLError:
            continue
        base = path.rsplit("/", 1)[0] if "/" in path else ""
        for name, spec in (data.get("services") or {}).items():
            spec = spec or {}
            build = spec.get("build")
            context = build if isinstance(build, str) else (build or {}).get("context", ".") if build else ""
            node = ctx.add_node(service_id(path, name), "service", str(name), path, None, sub="сервис",
                                image=str(spec.get("image", "")), ports=", ".join(_as_list(spec.get("ports"))), build=str(context or ""))
            ctx.edges.add(file_id(path), node, "declares")
            count += 1
            if build:
                dockerfile = "Dockerfile" if isinstance(build, str) else build.get("dockerfile", "Dockerfile")
                target = _rel(_rel(base, str(context)), str(dockerfile))
                if target in files:
                    ctx.edges.add(node, file_id(target), "runs")
            for env_file in _as_list(spec.get("env_file")):
                if _rel(base, env_file) in files:
                    ctx.edges.add(node, file_id(_rel(base, env_file)), "refers_to")
            command = " ".join(_as_list(spec.get("command")) + _as_list(spec.get("entrypoint")))
            for token in re.findall(r"[\w./\-]+\.\w+", command):
                for candidate in (_rel(base, token), _rel(_rel(base, str(context or ".")), token)):
                    if candidate in files:
                        ctx.edges.add(node, file_id(candidate), "runs")
                        break
            for env in _env_names(spec.get("environment")):
                ctx.add_node(env_id(env), "env", env, path, None, sub="окружение")
                ctx.edges.add(node, env_id(env), "declares")
    ctx.notes.append(f"сервисов Docker: {count}")


ADAPTER = Adapter("docker", "Сервисы Docker (compose)", _applies, _run)
