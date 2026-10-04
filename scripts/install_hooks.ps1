<#
.SYNOPSIS
    Установить (или снять) git-хуки проекта: обновление индекса после коммита и слияния, проверка карты проекта перед коммитом.

.DESCRIPTION
    Добавляет блоки между маркерами в .git/hooks; существующее содержимое хуков (например, Git LFS) не трогается.

    SKKKK-INDEX (post-commit, post-merge): обновляет индекс проекта в фоне, только изменённые файлы; ошибка не ломает git.
    SKKKK-MAP (pre-commit, commit-msg): карта проекта должна соответствовать коммиту.
      pre-commit  -> scripts/check_map_staged.py: сверяет карту с содержимым индекса git (роуты, таблицы БД, переменные
                     окружения, файлы кода, реестр модулей, манифест); расхождение отклоняет коммит.
      commit-msg  -> scripts/check_map_commit_msg.py: если меняются роутеры, модели, настройки, шаблоны или JS/CSS, а карты в
                     коммите нет, нужна строка «Карта: не требуется — причина».
    Без venv проверка карты пропускается с предупреждением (хук не должен ломать работу на машине без окружения).
    Хуки лежат вне git, поэтому ставятся один раз на каждую копию репозитория: scripts\start.ps1 и restart.ps1 проверяют это
    и ставят сами; состояние показывает scripts\index_doctor.py.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\scripts\install_hooks.ps1
    powershell -ExecutionPolicy Bypass -File .\scripts\install_hooks.ps1 -Remove
#>
[CmdletBinding()]
param([switch]$Remove)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$hooksDir = Join-Path $root '.git\hooks'
if (-not (Test-Path -LiteralPath $hooksDir)) { throw "Нет каталога хуков: $hooksDir" }

$indexBlock = @'
# Обновить индекс проекта в фоне; результат не влияет на git.
( cd "$(git rev-parse --show-toplevel)" && venv/Scripts/python.exe scripts/index_project_map.py --changed --quiet >/dev/null 2>&1 & ) || true
'@

$mapHead = @'
# Карта проекта должна соответствовать коммиту. Без venv проверка пропускается.
SKKKK_TOP="$(git rev-parse --show-toplevel)"
SKKKK_PY="$SKKKK_TOP/venv/Scripts/python.exe"
[ -x "$SKKKK_PY" ] || SKKKK_PY="$(git rev-parse --path-format=absolute --git-common-dir)/../venv/Scripts/python.exe"
'@
$preCommitBlock = $mapHead + @'

if [ -x "$SKKKK_PY" ] && [ -f "$SKKKK_TOP/scripts/check_map_staged.py" ]; then
  "$SKKKK_PY" "$SKKKK_TOP/scripts/check_map_staged.py" || exit 1
else
  echo "SKKKK: venv не найден, проверка карты проекта пропущена" >&2
fi
'@
$commitMsgBlock = $mapHead + @'

if [ -x "$SKKKK_PY" ] && [ -f "$SKKKK_TOP/scripts/check_map_commit_msg.py" ]; then
  "$SKKKK_PY" "$SKKKK_TOP/scripts/check_map_commit_msg.py" "$1" || exit 1
fi
'@

$hooks = @(
    @{ Name = 'post-commit'; Tag = 'SKKKK-INDEX'; Body = $indexBlock },
    @{ Name = 'post-merge';  Tag = 'SKKKK-INDEX'; Body = $indexBlock },
    @{ Name = 'pre-commit';  Tag = 'SKKKK-MAP';   Body = $preCommitBlock },
    @{ Name = 'commit-msg';  Tag = 'SKKKK-MAP';   Body = $commitMsgBlock }
)

foreach ($hook in $hooks) {
    $path = Join-Path $hooksDir $hook.Name
    $begin = "# >>> $($hook.Tag) >>>"
    $end = "# <<< $($hook.Tag) <<<"
    $text = if (Test-Path -LiteralPath $path) { [IO.File]::ReadAllText($path) } else { "#!/bin/sh`n" }
    $pattern = [regex]::Escape($begin) + '[\s\S]*?' + [regex]::Escape($end) + '\r?\n?'
    $clean = [regex]::Replace($text, $pattern, '')
    if ($Remove) {
        [IO.File]::WriteAllText($path, $clean, (New-Object Text.UTF8Encoding($false)))
        Write-Host "снят: $($hook.Name)"
        continue
    }
    $new = $clean.TrimEnd() + "`n" + $begin + "`n" + $hook.Body.TrimEnd() + "`n" + $end + "`n"
    [IO.File]::WriteAllText($path, $new.Replace("`r`n", "`n"), (New-Object Text.UTF8Encoding($false)))
    Write-Host "установлен: $($hook.Name)"
}
