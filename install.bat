@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
echo === Установка musicdl ===
echo.

where py >nul 2>nul
if errorlevel 1 goto nopython

if exist ".venv\Scripts\python.exe" goto install
echo [1/4] Создаю окружение Python...
py -3 -m venv .venv
if errorlevel 1 goto fail

:install
echo [2/4] Устанавливаю программу и spotDL, это займёт несколько минут...
".venv\Scripts\python.exe" -m pip install --upgrade pip
if errorlevel 1 goto fail
".venv\Scripts\python.exe" -m pip install -e .
if errorlevel 1 goto fail

echo [3/4] Проверяю ffmpeg, при необходимости скачиваю...
".venv\Scripts\python.exe" -c "from spotdl.utils.ffmpeg import is_ffmpeg_installed as ok, download_ffmpeg as get; print('ffmpeg: ' + ('уже установлен' if ok() else str(get())))"
if errorlevel 1 echo Не удалось скачать ffmpeg сейчас. Программа попробует сделать это сама при первом скачивании.

echo [4/4] Создаю ярлык musicdl на рабочем столе...
powershell -NoProfile -ExecutionPolicy Bypass -Command "$s = (New-Object -ComObject WScript.Shell).CreateShortcut([Environment]::GetFolderPath('Desktop') + '\musicdl.lnk'); $s.TargetPath = '%~dp0.venv\Scripts\pythonw.exe'; $s.Arguments = '-m musicdl.gui'; $s.WorkingDirectory = '%~dp0'; $s.Description = 'musicdl'; $s.Save()"
if errorlevel 1 echo Ярлык создать не удалось. Запускайте программу файлом start.bat.

echo.
echo Готово! Запускайте программу ярлыком musicdl на рабочем столе или файлом start.bat.
pause
exit /b 0

:nopython
echo Python не найден.
echo Установите Python с сайта python.org, поставив галочку "Add python.exe to PATH",
echo затем снова запустите install.bat.
pause
exit /b 1

:fail
echo.
echo Что-то пошло не так. Сделайте снимок экрана этого окна и отправьте его.
pause
exit /b 1
