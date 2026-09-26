@echo off
setlocal
set "ROOT=%~dp0"
where py >nul 2>nul && (
  py -3 "%ROOT%setup_wizard.py"
  goto :done
)
where python >nul 2>nul && (
  python "%ROOT%setup_wizard.py"
  goto :done
)
echo BitByte needs Python 3.10 or newer. Install it from https://www.python.org/downloads/windows/ and run this again.
:done
if errorlevel 1 pause
endlocal
