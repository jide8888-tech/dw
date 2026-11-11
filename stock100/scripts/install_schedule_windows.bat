@echo off
REM Register stock100 tasks in Windows Task Scheduler (weekdays):
REM   04:00 morning preview, 08:50 intraday 5-minute watch, 18:30 evening review
set "DIR=%~dp0"
schtasks /Create /F /TN "stock100_morning" /SC WEEKLY /D MON,TUE,WED,THU,FRI /ST 04:00 /TR "\"%DIR%run_morning.bat\""
schtasks /Create /F /TN "stock100_watch" /SC WEEKLY /D MON,TUE,WED,THU,FRI /ST 08:50 /TR "\"%DIR%run_watch.bat\""
schtasks /Create /F /TN "stock100_evening" /SC WEEKLY /D MON,TUE,WED,THU,FRI /ST 18:30 /TR "\"%DIR%run_evening.bat\""
echo.
echo Registered tasks:
schtasks /Query /TN stock100_morning
schtasks /Query /TN stock100_watch
schtasks /Query /TN stock100_evening
pause
