#!/bin/bash
# VW自動モデリングWebツールをローカル起動（自分のMac専用・認証なし）
cd "$(dirname "$0")/backend"
echo "ブラウザで http://localhost:8000 を開いてください（停止は Ctrl+C）"
python3 -m uvicorn main:app --port 8000
