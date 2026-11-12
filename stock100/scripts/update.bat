@echo off
REM Update stock100 to the latest code on GitHub (config.yaml and data are kept).
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
cd /d "%~dp0.."
where py >nul 2>nul && (set PYEXE=py) || (set PYEXE=python)
%PYEXE% -m stock100 update
pause
