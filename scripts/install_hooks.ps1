<#
.SYNOPSIS
    Установить (или снять) git-хуки проекта: обновление индекса после коммита и слияния, проверка карты проекта перед коммитом.

.DESCRIPTION
    Добавляет блоки между маркерами в .git/hooks; существующее содержимое хуков (например, Git LFS) не трогается.

    PrimaryAnalitics-INDEX (post-commit, post-merge): обновляет индекс проекта в фоне, только изменённые файлы; ошибка не ломает git.
    PrimaryAnalitics-PRIVACY (pre-commit): scripts/scan_personal_data.py --staged — в файлах коммита не должно быть имён людей и названий
      компаний из базы портала (находки видны только в консоли); совпадение отклоняет коммит. Без базы портала пропускается.
    PrimaryAnalitics-DATA (pre-commit): scripts/check_no_data_files.py --staged — в коммит не попадают БД, каталог data/, личные списки,
      документы, медиа и веса моделей; исключения — синтетические фикстуры тестов и примеры настроек.
    PrimaryAnalitics-MAP (pre-commit, commit-msg): карта проекта должна соответствовать коммиту.
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
# Проверки проекта работают на Python из venv; без venv они пропускаются.
PRIMARY_ANALITICS_TOP="$(git rev-parse --show-toplevel)"
PRIMARY_ANALITICS_PY="$PRIMARY_ANALITICS_TOP/venv/Scripts/python.exe"
[ -x "$PRIMARY_ANALITICS_PY" ] || PRIMARY_ANALITICS_PY="$(git rev-parse --path-format=absolute --git-common-dir)/../venv/Scripts/python.exe"
'@
$preCommitBlock = $mapHead + @'

if [ -x "$PRIMARY_ANALITICS_PY" ] && [ -f "$PRIMARY_ANALITICS_TOP/scripts/check_map_staged.py" ]; then
  "$PRIMARY_ANALITICS_PY" "$PRIMARY_ANALITICS_TOP/scripts/check_map_staged.py" || exit 1
else
  echo "PrimaryAnalitics: venv не найден, проверка карты проекта пропущена" >&2
fi
'@
$commitMsgBlock = $mapHead + @'

if [ -x "$PRIMARY_ANALITICS_PY" ] && [ -f "$PRIMARY_ANALITICS_TOP/scripts/check_map_commit_msg.py" ]; then
  "$PRIMARY_ANALITICS_PY" "$PRIMARY_ANALITICS_TOP/scripts/check_map_commit_msg.py" "$1" || exit 1
fi
'@

$privacyBlock = $mapHead + @'

if [ -x "$PRIMARY_ANALITICS_PY" ] && [ -f "$PRIMARY_ANALITICS_TOP/scripts/scan_personal_data.py" ]; then
  "$PRIMARY_ANALITICS_PY" "$PRIMARY_ANALITICS_TOP/scripts/scan_personal_data.py" --staged || {
    echo "PrimaryAnalitics: в коммите имена людей или названия компаний из базы портала. Замените их вымышленными и повторите коммит (DECISIONS: приватность)." >&2
    exit 1
  }
fi
'@

$dataBlock = $mapHead + @'

if [ -x "$PRIMARY_ANALITICS_PY" ] && [ -f "$PRIMARY_ANALITICS_TOP/scripts/check_no_data_files.py" ]; then
  "$PRIMARY_ANALITICS_PY" "$PRIMARY_ANALITICS_TOP/scripts/check_no_data_files.py" --staged || {
    echo "PrimaryAnalitics: в коммит попадают БД или рабочие данные проектов. Уберите их из индекса." >&2
    exit 1
  }
fi
'@

$hooks = @(
    @{ Name = 'post-commit'; Tag = 'PrimaryAnalitics-INDEX'; Body = $indexBlock },
    @{ Name = 'post-merge';  Tag = 'PrimaryAnalitics-INDEX'; Body = $indexBlock },
    @{ Name = 'pre-commit';  Tag = 'PrimaryAnalitics-MAP';   Body = $preCommitBlock },
    @{ Name = 'pre-commit';  Tag = 'PrimaryAnalitics-PRIVACY'; Body = $privacyBlock },
    @{ Name = 'pre-commit';  Tag = 'PrimaryAnalitics-DATA';  Body = $dataBlock },
    @{ Name = 'commit-msg';  Tag = 'PrimaryAnalitics-MAP';   Body = $commitMsgBlock }
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
