@echo off
rem claude-t.cmd: claude-t for cmd. Runs claude-t.ps1 beside it with Windows PowerShell; same arguments as claude.
rem -ExecutionPolicy Bypass applies to this one run of this one script, not to the machine's policy.
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0claude-t.ps1" %*
exit /b %ERRORLEVEL%
