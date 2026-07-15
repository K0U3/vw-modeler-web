#!/bin/bash
# VW自動モデリングWebツールを「社内共有モード」で起動する
# （ダブルクリックで実行。止めるときはこのウィンドウで Ctrl+C）
#
# ↓↓ ID とパスワードはここで決める（好きな文字列に変更OK） ↓↓
ID="muji"
PASSWORD="mujiur2026"

cd "$(dirname "$0")/backend"
IP=$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null)
echo "=================================================="
echo "  共有相手に伝える情報"
echo "    URL       : http://${IP}:8000"
echo "    ID        : ${ID}"
echo "    パスワード : ${PASSWORD}"
echo "=================================================="
echo "（このウィンドウを閉じるかCtrl+Cで公開停止）"
echo ""
VW_TOOL_USER="${ID}" VW_TOOL_PASS="${PASSWORD}" \
python3 -m uvicorn main:app --host 0.0.0.0 --port 8000
