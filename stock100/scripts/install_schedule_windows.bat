@echo off
REM 평일 04:00 새벽 예습, 18:30 장 마감 후 복습/추적을 Windows 작업 스케줄러에 등록합니다.
chcp 65001 >nul
set DIR=%~dp0
schtasks /Create /F /TN "stock100_morning" /SC WEEKLY /D MON,TUE,WED,THU,FRI /ST 04:00 /TR "\"%DIR%run_morning.bat\""
schtasks /Create /F /TN "stock100_evening" /SC WEEKLY /D MON,TUE,WED,THU,FRI /ST 18:30 /TR "\"%DIR%run_evening.bat\""
echo 등록 완료. 확인: schtasks /Query /TN stock100_morning
pause
