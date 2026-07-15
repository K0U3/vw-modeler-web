"""
main.py — VW 自動モデリング Web ツール（社内ローカル用 FastAPI）

起動:
  cd backend
  uvicorn main:app --reload --port 8000
ブラウザで http://localhost:8000 を開く。
"""

import tempfile
import os
import secrets
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, UploadFile, File, Form, Depends, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

import build_3d_model as engine

# ── アクセス制限（Basic認証）──
# 環境変数 VW_TOOL_USER / VW_TOOL_PASS を設定した時だけ認証が有効になる。
# 未設定なら従来どおり認証なし（自分のMacでの一人利用）。
# 社内公開する時:
#   VW_TOOL_USER=muji VW_TOOL_PASS=好きなパスワード \
#   python3 -m uvicorn main:app --host 0.0.0.0 --port 8000
# → 同じネットワークの人が http://<このMacのIP>:8000 を開くとID/パスワードを要求される
_AUTH_USER = os.environ.get("VW_TOOL_USER")
_AUTH_PASS = os.environ.get("VW_TOOL_PASS")
_security = HTTPBasic()


def _check_auth(credentials: HTTPBasicCredentials = Depends(_security)):
    ok = (secrets.compare_digest(credentials.username, _AUTH_USER)
          and secrets.compare_digest(credentials.password, _AUTH_PASS))
    if not ok:
        raise HTTPException(status_code=401, detail="認証が必要です",
                            headers={"WWW-Authenticate": "Basic"})


_deps = [Depends(_check_auth)] if (_AUTH_USER and _AUTH_PASS) else []

app = FastAPI(title="VW 自動モデリング", dependencies=_deps)

FRONTEND = Path(__file__).resolve().parent.parent / "frontend" / "index.html"


@app.get("/", response_class=HTMLResponse)
def index():
    return FRONTEND.read_text(encoding="utf-8")


@app.post("/generate")
async def generate(
    file: UploadFile = File(...),
    ch: Optional[int] = Form(None),   # 未指定なら図面の天井高注記(天井高/CH表記)から自動検出
    sill: int = Form(900),
    head: int = Form(2400),
    sill_fix: int = Form(150),
    head_fix: int = Form(3300),
    blue_color: int = Form(5),
    wall_layers: str = Form("躯体,0,外壁,内壁,壁,壁・建具"),
    part_layers: str = Form("躯体,外壁,内壁,壁,壁・建具,間仕切"),
    tategu_layer: str = Form("建具"),
    beam_layer: str = Form("梁"),
    beam_w: int = Form(200),
    beam_d: int = Form(400),
    furniture_layer: str = Form("家具"),
    muji_lib: str = Form("/Users/aikawawakou/Documents/MUJIHOUSE/MUJI家具 (1) v2021.vwx"),
    sash_layer: str = Form("インナーサッシ"),
    insul_layer: str = Form("断熱"),
    haki_min_w: int = Form(1500),
    door_head: int = Form(2000),
    furn_box_h: int = Form(700),   # 該当なし家具の簡易ボリューム高さ
    fr: int = Form(60),     # 窓枠見付
    fd: int = Form(70),     # 窓枠見込み
    gt: int = Form(20),     # ガラス厚
    sill_hiki: int = Form(900),
    head_hiki: int = Form(2000),
    sill_haki: int = Form(0),
    head_haki: int = Form(2000),
):
    # アップロードされた DXF を一時ファイルに保存
    suffix = Path(file.filename or "in.dxf").suffix or ".dxf"
    data = await file.read()
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
    try:
        tmp.write(data)
        tmp.close()
        overrides = {
            "SILL": sill, "HEAD": head,
            "SILL_FIX": sill_fix, "HEAD_FIX": head_fix,
            "BLUE_COLOR": blue_color,
            "WALL_LAYERS": set(s.strip() for s in wall_layers.split(",") if s.strip()),
            "PART_LAYERS": set(s.strip() for s in part_layers.split(",") if s.strip()),
            "TATEGU_LAYER": tategu_layer.strip(),
            "BEAM_LAYER": beam_layer.strip(),
            "BEAM_W": beam_w,
            "BEAM_D": beam_d,
            "FURNITURE_LAYER": furniture_layer.strip(),
            "MUJI_LIB": muji_lib.strip(),
            "SASH_LAYER": sash_layer.strip(),
            "INSUL_LAYER": insul_layer.strip(),
            "HAKI_MIN_W": haki_min_w,
            "DOOR_HEAD": door_head,
            "FURN_BOX_H": furn_box_h,
            "FR": fr, "FD": fd, "GT": gt,
            "SILL_HIKI": sill_hiki,
            "HEAD_HIKI": head_hiki,
            "SILL_HAKI": sill_haki,
            "HEAD_HAKI": head_haki,
        }
        if ch is not None:
            overrides["CH"] = ch
        script, summary = engine.build_script(tmp.name, overrides)
        out_name = (Path(file.filename or "model").stem) + "_model_vw.py"
        return JSONResponse({
            "ok": True,
            "filename": out_name,
            "script": script,
            "summary": summary,
        })
    except engine.CeilingHeightRequired as e:
        # 天井高が特定できない → フロントでCH入力を促す
        return JSONResponse({"ok": False, "needs_ch": True, "error": str(e)},
                            status_code=422)
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    finally:
        os.unlink(tmp.name)
