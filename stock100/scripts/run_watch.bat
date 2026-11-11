@echo off
REM Intraday 5-minute watch. Signals appear in this window and
REM data\watch\<date>_*.xlsx is refreshed every 5 minutes. Exits after 15:30.
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
cd /d "%~dp0.."
title stock100 watch
where py >nul 2>nul && (set PYEXE=py) || (set PYEXE=python)
%PYEXE% -m stock100 watch
