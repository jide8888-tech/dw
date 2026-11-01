@echo off
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
cd /d "%~dp0.."
if not exist data\logs mkdir data\logs
REM py 런처가 있으면 py 사용 (setx PY_PYTHON 3.14 로 기본 버전 지정), 없으면 python
where py >nul 2>nul && (set PYEXE=py) || (set PYEXE=python)
%PYEXE% -m stock100 evening >> data\logs\evening.log 2>&1
