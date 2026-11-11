@echo off
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
cd /d "%~dp0.."
if not exist data\logs mkdir data\logs
REM Use the py launcher if present (choose the version once with: setx PY_PYTHON 3.14), else python
where py >nul 2>nul && (set PYEXE=py) || (set PYEXE=python)
%PYEXE% -m stock100 evening >> data\logs\evening.log 2>&1
