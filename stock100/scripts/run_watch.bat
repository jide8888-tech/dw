@echo off
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
cd /d "%~dp0.."
title stock100 5분 감시
REM 장중 5분 감시: 창에 신호가 표시되고 data\watch\날짜_실시간감시.xlsx 가 5분마다 갱신됩니다. 15:30 이후 자동 종료.
where py >nul 2>nul && (set PYEXE=py) || (set PYEXE=python)
%PYEXE% -m stock100 watch
