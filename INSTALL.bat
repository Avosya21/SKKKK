@echo off
chcp 65001 >nul
title Установка проекта
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\bootstrap.ps1" %*
pause
