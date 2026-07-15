"""Vercel サーバーレスエントリ: backend の FastAPI アプリをそのまま公開する"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from main import app  # noqa: E402,F401  (Vercel が `app` を ASGI として検出)
