"""
build_catalog.py — 家具ライブラリ DXF からカタログ JSON を生成
================================================================
MUJI家具.dxf のような「家具シンボル集」DXF を読み込み、
各ブロックの 名前 / 寸法(W×D) / カテゴリ を抽出して
furniture_catalog.json に保存する。

このカタログを build_3d_model.py が読み込み、
間取り図の家具フットプリント寸法に最も近い家具を自動選定する。

使い方:
    python3 build_catalog.py "<家具ライブラリ.dxf>" [出力.json]
依存: pip3 install ezdxf
"""

import sys
import json
import re
from pathlib import Path
from ezdxf import recover


def block_bbox(blk):
    """ブロック内エンティティの bbox から W, D を求める"""
    xs, ys = [], []
    for e in blk:
        t = e.dxftype()
        try:
            if t == 'LINE':
                xs += [e.dxf.start.x, e.dxf.end.x]
                ys += [e.dxf.start.y, e.dxf.end.y]
            elif t == 'LWPOLYLINE':
                for p in e.get_points():
                    xs.append(p[0]); ys.append(p[1])
            elif t == 'POLYLINE':
                for v in e.vertices:
                    xs.append(v.dxf.location.x); ys.append(v.dxf.location.y)
            elif t in ('CIRCLE', 'ARC'):
                c, r = e.dxf.center, e.dxf.radius
                xs += [c.x - r, c.x + r]; ys += [c.y - r, c.y + r]
            elif t == 'INSERT':
                xs.append(e.dxf.insert.x); ys.append(e.dxf.insert.y)
        except Exception:
            pass
    if not xs or not ys:
        return None
    return round(max(xs) - min(xs)), round(max(ys) - min(ys))


def categorize(name):
    """ブロック名からカテゴリを推定（マッチング時の絞り込み用）"""
    n = name
    table = [
        ('キッチン', 'キッチン'),
        ('ベッド', 'ベッド'), ('マットレス', 'ベッド'), ('布団', 'ベッド'),
        ('ソファ', 'ソファ'), ('ユニットソファ', 'ソファ'),
        ('チェア', 'チェア'), ('スツール', 'チェア'),
        ('ベンチ', 'チェア'), ('Chair', 'チェア'), ('Pouf', 'チェア'),
        ('テーブル', 'テーブル'), ('Table', 'テーブル'), ('デスク', 'テーブル'),
        ('シェルフ', '収納'), ('収納', '収納'), ('コの字', '収納'),
        ('チェスト', '収納'), ('ワゴン', '収納'), ('ラック', '収納'),
        ('トイレ', '衛生'), ('TOTO', '衛生'), ('CES', '衛生'),
        ('洗面', '衛生'), ('UB_', '衛生'), ('SANR', '衛生'),
        ('Plumbing', '衛生'), ('Grohe', '衛生'), ('手洗', '衛生'),
        ('電子レンジ', '家電'), ('冷蔵', '家電'), ('洗濯', '家電'),
        ('Bamboo', '植栽'), ('BAMBOO', '植栽'), ('Tree', '植栽'), ('PLNT', '植栽'),
        ('アクセサリー', '小物'), ('フック', '小物'),
        ('タオル', '小物'), ('スニーカー', '小物'),
    ]
    for key, cat in table:
        if key in n:
            return cat
    return 'その他'


def clean_name(raw):
    """ '名前 - 別名-12345-_3D_' のような表記を整形（先頭の正式名だけ残す）"""
    base = raw.split(' - ')[0].strip()
    base = re.sub(r'-\d+$', '', base).strip()
    return base or raw


# 全角→半角（数字・英字）変換テーブル
_Z2H = str.maketrans(
    '０１２３４５６７８９ｃｍｗｄｈＣＭＷＤＨ×',
    '0123456789cmwdhCMWDHx')

# ベッド/布団のサイズ呼称 → (W, D) mm（標準値）
_BED_SIZE = {
    'スモール': (830, 1980), 'シングル': (980, 1980),
    'セミダブル': (1200, 1980), 'ダブル': (1400, 1980),
    'クイーン': (1600, 1980), 'キング': (1800, 1980),
}


def _strip_codes(n):
    """品番・EAN・参照コードを除去（誤検出の元を断つ）"""
    n = re.sub(r'\d{6,}', ' ', n)          # 6桁以上の数値=品番/EAN
    n = re.sub(r'_rfa\S*', ' ', n)          # Revit品番 _rfa-50733xx
    n = re.sub(r'_dwg\S*', ' ', n)          # _dwg-xxxxx
    n = re.sub(r'_3D_\S*', ' ', n)          # _3D_ サフィックス
    n = re.sub(r'-\d{3,5}-', ' ', n)        # -123456- 区切りコード
    return n


def shelf_size_from_name(name):
    """MUJI ユニットシェルフ系の命名から (w, d) を返す。最優先で使う。
    命名規則:
      ・ユニットシェルフ_25_58X  → 奥行250 × 幅580（_奥行_幅, cm×10）
      ・ユニットシェルフ_25_86X  → 奥行250 × 幅860
      ・ユニットシェルフ43/58/86X → 奥行440固定 × 幅430/580/860（裸2桁=幅cm）
    （幅・奥行はユーザー提供のシェルフ一覧 D/W ラベルに準拠）
    返り値 (w, d) or None"""
    n = name
    # _奥行_幅（例: _25_58 → D250 W580）
    m = re.search(r'ユニットシェルフ_(\d{2})_(\d{2})', n)
    if m:
        d = int(m.group(1)) * 10
        w = int(m.group(2)) * 10
        return w, d
    # 裸2桁（例: 43/58/86 → 幅430/580/860・奥行440固定）
    m = re.search(r'ユニットシェルフ(\d{2})(?!\d)', n)
    if m:
        w = int(m.group(1)) * 10
        return w, 440
    return None


def size_from_name(name):
    """名前に埋まった寸法を抽出。返り値 (w, d) or None。
    3DSOLID で幾何 bbox が取れない家具用のフォールバック。
    品番・EAN を除去してから高信頼パターンのみ採用する。"""
    n = _strip_codes(name.translate(_Z2H))
    # ベッド呼称（最優先・確実）
    for key, (w, d) in _BED_SIZE.items():
        if key in n:
            return w, d
    # "w48_d48"（Pouf 等）→ cm 表記の幅×奥行
    m = re.search(r'w\s*(\d{2,3})[_ ]d\s*(\d{2,3})', n)
    if m:
        return int(m.group(1)) * 10, int(m.group(2)) * 10
    # "180_80" / "160 80" / "70_70" → cm 表記（両値とも3桁以下）
    m = re.search(r'(?<!\d)(\d{2,3})[_x ](\d{2,3})(?!\d)', n)
    if m and int(m.group(1)) <= 300 and int(m.group(2)) <= 300:
        return int(m.group(1)) * 10, int(m.group(2)) * 10
    # "1800650" / "1500650" → 連結 mm（幅4桁＋奥行3桁）
    m = re.search(r'(?<!\d)(\d{4})(\d{3})(?!\d)', n)
    if m:
        w, d = int(m.group(1)), int(m.group(2))
        if 600 <= w <= 4000 and 200 <= d <= 1500:
            return w, d
    # "1800_650" / "1800x800" → mm 表記の幅×奥行
    m = re.search(r'(?<!\d)(\d{3,4})[_x ](\d{3,4})(?!\d)', n)
    if m:
        w, d = int(m.group(1)), int(m.group(2))
        if 200 <= w <= 4000 and 200 <= d <= 4000:
            return w, d
    # "幅98cm" / "98cm" → cm 表記（×10）
    m = re.search(r'幅?\s*(\d{2,3})\s*cm', n)
    if m:
        w = int(m.group(1)) * 10
        if 200 <= w <= 4000:
            return w, w
    # "幅980" / "W800" / "φ1200" → mm 表記
    m = re.search(r'(?:幅|W|w|φ|Φ)\s*(\d{3,4})', n)
    if m:
        w = int(m.group(1))
        if 200 <= w <= 4000:
            return w, w
    # キッチン等：明確なサイズ語 + 4桁（"キッチン2100"）
    m = re.search(r'(?:キッチン|テーブル|デスク|シェルフ|収納)\D{0,4}(\d{4})', n)
    if m:
        w = int(m.group(1))
        if 600 <= w <= 4000:
            return w, w
    return None


def build_catalog(dxf_path):
    doc, _ = recover.readfile(str(dxf_path))
    items = []
    seen = set()
    for blk in doc.blocks:
        if blk.name.startswith('*'):
            continue
        ents = list(blk)
        if not ents:
            continue
        nm = clean_name(blk.name)
        # ⓪ MUJI シェルフは命名規則の幅×奥行を最優先（実測・汎用パースより信頼）
        bb = shelf_size_from_name(blk.name)
        dim_source = 'shelf'
        if bb is None:
            # ① 幾何 bbox（LINE/POLYLINE 等の 2D 図形から）
            bb = block_bbox(blk)
            dim_source = 'geometry'
        if bb is None or (bb[0] < 30 and bb[1] < 30):
            # ② 3DSOLID 等で幾何が取れない → 名前から寸法を推定
            bb = size_from_name(nm) or size_from_name(blk.name)
            dim_source = 'name'
        if bb is None:
            # ③ 寸法不明でもカタログには載せる（手動補正用）
            w, d = 0, 0
            dim_source = 'unknown'
        else:
            w, d = bb
        if nm in seen:
            continue
        seen.add(nm)
        items.append({
            'block': blk.name,        # VW で vs.Symbol に渡す実名
            'name': nm,               # 表示・マッチ用の整形名
            'w': w, 'd': d,
            'dim_source': dim_source,  # geometry / name / unknown
            'category': categorize(blk.name),
            'elements': len(ents),
        })
    items.sort(key=lambda x: (x['category'], -x['w'] * x['d']))
    return items


def main():
    if len(sys.argv) < 2:
        print('使い方: python3 build_catalog.py "<家具ライブラリ.dxf>" [出力.json]')
        sys.exit(1)
    dxf = sys.argv[1]
    out = sys.argv[2] if len(sys.argv) > 2 else \
        str(Path(__file__).parent / 'furniture_catalog.json')
    items = build_catalog(dxf)
    Path(out).write_text(
        json.dumps(items, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f'[DONE] → {out}')
    print(f'  家具カタログ {len(items)} 件')
    cats, srcs = {}, {}
    for it in items:
        cats[it['category']] = cats.get(it['category'], 0) + 1
        srcs[it['dim_source']] = srcs.get(it['dim_source'], 0) + 1
    print('  カテゴリ別:')
    for c, n in sorted(cats.items(), key=lambda x: -x[1]):
        print(f'    {c}: {n}')
    print('  寸法ソース:', ' / '.join(f'{k}={v}' for k, v in srcs.items()))


if __name__ == '__main__':
    main()
