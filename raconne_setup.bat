@echo off
chcp 65001 > nul
cd /d %~dp0
echo [1/2] 必要な部品をインストールしています...
python -m pip install python-dotenv playwright
echo [2/2] ブラウザをインストールしています...
python -m playwright install chromium
echo.
echo 準備完了です。.env に ID とパスワードを入力してください。
pause
