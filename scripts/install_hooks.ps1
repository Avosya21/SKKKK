<#
.SYNOPSIS
    Установить (или снять) git-хуки, которые после коммита и слияния обновляют индекс проекта.

.DESCRIPTION
    Добавляет в .git/hooks/post-commit и post-merge блок между маркерами SKKKK-INDEX. Существующее содержимое
    хуков (например, Git LFS) не трогается. Обновление идёт в фоне и только для изменённых файлов, не блокирует git.
    Ошибка индексации никогда не ломает коммит.

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

$begin = '# >>> SKKKK-INDEX >>>'
$end = '# <<< SKKKK-INDEX <<<'
$block = @"
$begin
# Обновить индекс проекта в фоне; результат не влияет на git.
( cd "`$(git rev-parse --show-toplevel)" && venv/Scripts/python.exe scripts/index_project_map.py --changed --quiet >/dev/null 2>&1 & ) || true
$end
"@

foreach ($name in @('post-commit', 'post-merge')) {
    $path = Join-Path $hooksDir $name
    $text = if (Test-Path -LiteralPath $path) { [IO.File]::ReadAllText($path) } else { "#!/bin/sh`n" }
    $pattern = [regex]::Escape($begin) + '[\s\S]*?' + [regex]::Escape($end) + '\r?\n?'
    $clean = [regex]::Replace($text, $pattern, '')
    if ($Remove) {
        [IO.File]::WriteAllText($path, $clean, (New-Object Text.UTF8Encoding($false)))
        Write-Host "снят: $name"
        continue
    }
    $new = $clean.TrimEnd() + "`n" + $block + "`n"
    [IO.File]::WriteAllText($path, $new.Replace("`r`n", "`n"), (New-Object Text.UTF8Encoding($false)))
    Write-Host "установлен: $name"
}
