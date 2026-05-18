@echo off
REM PowerShell does not run scripts from the current folder by name alone — use:
REM   .\build-idf.bat build
REM   .\build-idf.bat flash
REM If idf.py says the active Python differs from the one used at configure time, run once:
REM   .\build-idf.bat fullclean
REM then build again (same ESP-IDF export session you plan to use).
REM esp-sr movemodel.py prints UTF-8 box-drawing chars; Windows cp1252 breaks without this.
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
if "%IDF_PATH%"=="" (
  echo Set IDF_PATH to your ESP-IDF first, then run export.bat from that IDF.
  exit /b 1
)
python "%IDF_PATH%\tools\idf.py" -C "%~dp0." %*
