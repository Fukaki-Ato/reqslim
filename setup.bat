@echo off
chcp 65001 >nul
setlocal EnableExtensions
cd /d "%~dp0"

rem One-click assembly: find a real Python, run the installer, offer to launch.
rem Everything is done with labels and goto instead of parenthesised blocks:
rem %PY% can expand to a path containing parentheses ("Program Files (x86)"),
rem which closes a block early and produces an error that looks unrelated.

echo.
echo   reqslim - ZCode 接入装配
echo   ========================
echo.

if not exist "tools\install_zcode.py" goto :wrong_dir

set "DRY="
call :scan_dry %*

rem Find an interpreter that actually runs. `where python` is not enough: on a
rem machine without Python it matches the Microsoft Store stub, which opens the
rem store and still exits 0, so the failure only shows up after "installing".
set "PY="
call :probe py -3
if not defined PY call :probe python
if not defined PY call :probe python3
if not defined PY goto :no_python

%PY% -c "import sys;print('  python      v' + sys.version.split()[0] + '  ' + sys.executable)"
echo.
echo   正在读取本机 ZCode 的 provider 表并生成启动器...
echo.

%PY% tools\install_zcode.py %*
if not errorlevel 1 goto :installed

echo.
echo   装配没有完成，上面是安装器的原始报错。
echo   最常见的原因是 ZCode 装在非常规位置。
echo.
set "ZCODE_DIR="
set /p "ZCODE_DIR=  输入 ZCode 安装目录后回车（直接回车放弃）: "
if not defined ZCODE_DIR goto :failed
echo.
%PY% tools\install_zcode.py %* --zcode-dir "%ZCODE_DIR%"
if errorlevel 1 goto :failed

:installed
if defined DRY goto :dry_done
if not exist "start-zcode.bat" goto :failed
echo.
choice /c yn /n /t 10 /d y /m "  装配完成。现在启动 ZCode? [Y/n] "
if errorlevel 2 goto :done
echo.
call "start-zcode.bat"
if errorlevel 1 goto :launch_failed
echo.
echo   已启动：代理在独立窗口里运行，ZCode 正常打开。
echo   看数据：双击 zcode-stats.bat，或打开 http://127.0.0.1:8788/v1/report
timeout /t 6 /nobreak >nul
exit /b 0

:launch_failed
echo.
echo   ZCode 未启动：reqslim 代理没有起来，原因见上面的提示。
echo   处理后重新双击 start-zcode.bat 即可。
pause
exit /b 1

:dry_done
echo.
echo   dry run 完成，没有写任何文件。
echo.
pause
exit /b 0

:done
echo.
echo   装配完成。下次直接双击 start-zcode.bat 即可。
echo   首次验证：双击 zcode-stats.bat，看到 requests 在涨就说明接通了。
echo.
pause
exit /b 0

:no_python
echo   没有找到可用的 Python 3.9+。
echo.
echo   请先安装 Python：https://www.python.org/downloads/
echo   安装时勾选 "Add python.exe to PATH"，然后重新双击本脚本。
echo.
pause
exit /b 1

:wrong_dir
echo   这个脚本要放在 reqslim 目录里运行（和 tools\ 同级）。
echo   当前目录：%CD%
echo.
pause
exit /b 1

:failed
echo.
echo   装配失败。可以手动指定 ZCode 安装目录重试：
echo     %PY% tools\install_zcode.py --zcode-dir "D:\Zcode"
echo.
pause
exit /b 1

rem ---------------------------------------------------------------- helpers
:probe
rem %1 = command, %2 = optional leading argument (e.g. "py -3")
set "CAND=%~1"
if not "%~2"=="" set "CAND=%~1 %~2"
rem One command, three checks: it starts, it is new enough, and it can import
rem the package from this folder. A stub or a 3.8 interpreter fails all three.
%CAND% -c "import sys;assert sys.version_info>=(3,9);import reqslim.zcode" >nul 2>nul
if errorlevel 1 exit /b 0
set "PY=%CAND%"
exit /b 0

:scan_dry
if "%~1"=="" exit /b 0
if /i "%~1"=="--dry-run" set "DRY=1"
shift
goto :scan_dry
