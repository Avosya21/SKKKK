"""Командная строка projectmap: версия, объявленные команды, код выхода нереализованных."""

from __future__ import annotations

import pytest

from projectmap import __version__
from projectmap.cli import COMMANDS, build_parser, main


def test_version_command_prints_the_package_version(capsys):
    assert main(["version"]) == 0
    assert capsys.readouterr().out.strip() == f"projectmap {__version__}"


@pytest.mark.parametrize("command", sorted(COMMANDS))
def test_planned_commands_exist_but_say_when_they_arrive(command, capsys):
    assert main([command]) == 2
    err = capsys.readouterr().err
    assert command in err and "этапе" in err


def test_every_planned_command_is_listed_in_help():
    text = build_parser().format_help()
    for command in COMMANDS:
        assert command in text
