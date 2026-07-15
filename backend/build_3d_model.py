"""
build_3d_model.py  (汎用版 v7)
================================================================
VectorWorks の意匠図 DXF を読み込み、3D モデリング用の
VW Python スクリプトを自動生成する汎用ツール。

■ 使い方
    python3 build_3d_model.py <input.dxf> [output_vw.py]
    例) python3 build_3d_model.py ~/Desktop/物件A.dxf

    output 省略時は <input>_model_vw.py を同じ場所に出力。

■ 前提（VW 側の手順）
    1. VW で対象図面を開く
    2. ファイル > 取り出す > DXF/DWG 取り出し（形式: DXF テキスト）
    3. デスクトップ等に DXF を保存
    4. 本スクリプトを実行 → _model_vw.py を生成
    5. VW のツール > ツールマクロ > 新規(Python) に貼り付けて実行

■ 自動でやること
    - 天井高を図面の注記(TEXT/MTEXT)から自動検出（"天井高2400" / "CH≒2400" / "CH:3670" 等）
      ※既定値は無い。ユーザー指定も図面注記も無ければ処理を止めて確認を求める（--ch で指定）
    - 部屋ラベル検出（トイレ/浴室/洗面所/キッチン/リビング/ダイニング）→ チェックリスト出力
    - 内壁（間仕切り壁）を立ち上げ:
        閉ポリライン壁 + 薄壁(25〜60mm) + 平行LINEペア壁(壁厚50〜350mm)
        建具ARC位置でドア開口を抜き、垂れ壁(DOOR_HEAD〜CH)を生成
    - 躯体の二重外形（外周輪郭+内周輪郭の同心矩形）は4辺の壁帯に変換して立ち上げ
    - グリッド原点を *D 寸法ブロックから自動検出
    - 躯体ポリラインを実頂点形状で立ち上げ（bbox でなく実輪郭）
    - 西面外壁（縦長・西端）→ 腰壁の連続窓
    - 東面の青線（建具 color=5 縦線）→ 大 FIX 窓（実位置）
    - 南北の主外壁ライン → 建具開口位置に腰窓
    - 外部階段・建物外（東に大きく離れた要素）は出力しない

■ 図面が違うとき調整するポイント（このファイル上部の定数）
    - WALL_LAYERS   : 躯体を含むレイヤー名
    - BLUE_COLOR    : 窓を示す線色（既定 5=青）
    - SILL/HEAD 等  : 窓高さ
    - STAIR_GAP     : 「本体からこれ以上東に離れたら外部」とみなす距離

依存: pip3 install ezdxf
================================================================
"""

import sys
import re
import unicodedata
from pathlib import Path
from collections import Counter
import ezdxf
from ezdxf import recover


class CeilingHeightRequired(ValueError):
    """天井高が特定できない（ユーザー指定なし・図面注記なし）"""


# ─────────────────────────────────────────────
# 調整パラメータ
# ─────────────────────────────────────────────
CH = None          # 天井高 mm（既定値なし。ユーザー指定 or 図面注記から決定）
FH = 150           # 床スラブ厚 mm

# 西面：腰壁の連続窓
SILL = 900
HEAD = 2400
RIB_MULLION = 2000

# 東面：大FIX窓（青線位置）
SILL_FIX = 150
HEAD_FIX = 3300
FIX_MULLION = 2500

# 腰高引き違い窓
SILL_HIKI = 900
HEAD_HIKI = 2000

# 掃き出し引き違い窓
SILL_HAKI = 0
HEAD_HAKI = 2000

# 窓枠（全窓種共通: 枠=FR見付×FD見込みの縦横角材、ガラス=GT厚の薄板）
FR = 60    # 枠見付 mm
FD = 70    # 枠見込み mm（壁厚の中心に納める）
GT = 20    # ガラス厚 mm

# レイヤー・色
#   '一般' は家具・凡例・設備外形のノイズ源なので既定から除外（必要なら Web UI で追加）
WALL_LAYERS = {'躯体', '0', '外壁', '内壁', '壁', '壁・建具'}
TATEGU_LAYER = '建具'
BLUE_COLOR = 5     # 窓を示す線色（ACI 5=青）

# 内壁（間仕切り）検出
#   '一般'/'0' は凡例・図枠の誤検出源なのでペアリング対象から除外（WALL_LAYERSとは別集合）
PART_LAYERS = {'躯体', '外壁', '内壁', '壁', '壁・建具', '間仕切'}
PART_T_MIN, PART_T_MAX = 50, 350   # 壁厚レンジ mm
PART_OVL_MIN = 300                 # 平行線の投影オーバーラップ最小 mm
PART_AXIS_TOL = 10                 # 水平/垂直判定の許容 mm
THIN_MIN, THIN_MAX = 25, 60        # 薄壁閉ポリの短辺レンジ（MIN_SIDE未満の壁を拾う）
DOOR_HEAD = 2000                   # ドア開口高（垂れ壁下端・建具枠上端）mm
# 開き戸ARC半径レンジ mm。全レイヤーを走査するため下限550で椅子等の小円弧を除外
# （実図面でドアARCは 建具 以外に 一般・家具01 レイヤーにも乗っている）
DOOR_ARC_R_MIN, DOOR_ARC_R_MAX = 550, 1200
BLK_MIN_SIDE = 600                 # ブロックbbox短辺がこれ未満は建具等の部品とみなし壁抽出から除外

# クラス別立ち上げ（建具ユニット・インナーサッシ・断熱）
SASH_LAYER = 'インナーサッシ'       # サッシINSERT → win_unit窓
INSUL_LAYER = '断熱'               # SOLID/閉ポリ → そのまま立ち上げ
HAKI_MIN_W = 1500                  # サッシ幅がこれ以上なら掃き出し窓、未満なら腰高窓
LEAF_T_MIN, LEAF_T_MAX = 20, 60    # 扉パネルの図面厚みレンジ mm
DOOR_U_T_MIN, DOOR_U_T_MAX = 40, 300   # 建具ユニットbbox短辺レンジ mm
DOOR_U_LEN_MIN = 500               # 建具ユニットbbox長辺の最小 mm

# 梁
BEAM_LAYER = '梁'
BEAM_W = 200       # 梁幅 mm（計画方向の断面寸法）
BEAM_D = 400       # 梁せい mm（天井から下がる垂直高さ）

# 家具
FURN_LINEUP = True         # True: 家具は図面位置に置かず、モデルの右横に整列して並べる
FURN_LINEUP_GAP = 600      # 整列時の間隔 mm
FURN_LINEUP_ROW_W = 12000  # 整列1行の幅 mm
FURNITURE_LAYER = '家具'   # 前方一致（'家具01' 等も対象）
FURN_BOX_H = 700           # カタログ該当なし家具の簡易ボリューム高さ mm
FURN_EXTRA_LAYERS = {'一般'}   # 家具レイヤー外の家具フットプリント走査対象（建物外形が取れた図面のみ）
ROOM_ASSIGN_R = 4000       # 家具→部屋ラベルの割り当て最大距離 mm
# ベッド標準寸法（マットレス幅±80 × 長さ1900〜2100 なら「ベッド」カテゴリ優先）
BED_WIDTHS = (830, 980, 1200, 1400, 1580)
BED_LEN_MIN, BED_LEN_MAX = 1900, 2100
# ベッドはシンボルを使わず簡易ボリューム+枕で表現する
BED_H = 350        # ベッド本体高さ mm
PILLOW_W = 500     # 枕の幅（ベッド短辺方向） mm
PILLOW_D = 300     # 枕の奥行（ベッド長辺方向） mm
PILLOW_H = 100     # 枕の高さ mm

# 1住戸に1つしか無い設備（重複マッチは最良1件のみ配置、残りは簡易ボリューム）
SINGLE_ROOM_KW = {
    'トイレ': ('トイレ', 'WC', '便所'),
    'キッチン': ('キッチン', '台所', 'DK', 'LDK', 'KIT'),
    '洗面台': ('洗面', '脱衣'),
}

# ソファはシンボルを使わず「座面+背もたれ+脚」の簡易ボリュームで表現する
SOFA_LEG_H = 150      # 脚の高さ mm
SOFA_LEG_W = 60       # 脚の角材断面 mm
SOFA_SEAT_H = 400     # 座面の上端高さ mm
SOFA_BACK_T = 150     # 背もたれの厚み mm
SOFA_BACK_H = 750     # 背もたれの上端高さ mm

# ── レイヤー判定の一般化（物件ごとにレイヤー名が違っても動くように） ──
# 明示セット（WALL_LAYERS等）に完全一致しなくても、キーワード包含で判定する。
# 寸法・通り芯・文字などの紛らわしいレイヤーは除外キーワードで弾く
LAYER_EXCLUDE_KW = ('寸法', '芯', '文字', '記号', 'DEFPOINT', 'ハッチ', '面積')
WALL_LAYER_KW = ('壁', '躯体', '間仕切')
# 壁判定からさらに除外（天井=梁ライン・サッシ/窓・家具・設備・断熱・梁は壁ではない）
WALL_EXCLUDE_KW = LAYER_EXCLUDE_KW + ('天井', 'サッシ', '窓', '家具', '設備', '断熱', '梁')
# 点線・破線の線種名。点線ライン=梁の表記なので壁は立ち上げない
DASH_WORDS_BEAM = ('DASH', 'HIDDEN', 'PHANTOM', 'DOT', '破線', '点線')   # 梁として拾う線種
DASH_WORDS = DASH_WORDS_BEAM + ('鎖線', '一点鎖')   # 壁から除外する線種（芯線含む）


def _is_excluded_layer(name):
    u = name.upper()
    return any(k in u for k in LAYER_EXCLUDE_KW)


def _is_wall_layer(name):
    """壁レイヤーか（明示セット or キーワード包含）。
    天井（梁ライン）・サッシ・家具・設備などのレイヤーは壁として扱わない"""
    if name in WALL_LAYERS:
        return True
    u = name.upper()
    if any(k in u for k in WALL_EXCLUDE_KW):
        return False
    return any(k in name for k in WALL_LAYER_KW)


def _is_partition_layer(name):
    """内壁ペアリング対象レイヤーか（天井・サッシ等は対象外）"""
    if name in PART_LAYERS:
        return True
    u = name.upper()
    if any(k in u for k in WALL_EXCLUDE_KW):
        return False
    return any(k in name for k in WALL_LAYER_KW)


def _is_sash_layer(name):
    return name == SASH_LAYER or (not _is_excluded_layer(name)
                                  and ('サッシ' in name or '窓' in name))


def _is_insul_layer(name):
    return name == INSUL_LAYER or (not _is_excluded_layer(name) and '断熱' in name)


def _is_ceil_layer(name):
    return name == CEIL_LAYER or (not _is_excluded_layer(name) and '天井' in name)


def _is_beam_layer(name):
    return name == BEAM_LAYER or (not _is_excluded_layer(name) and '梁' in name)


def _is_furniture_layer(name):
    return name.startswith(FURNITURE_LAYER) or \
        (not _is_excluded_layer(name) and '家具' in name)


def _is_fixture_layer(name):
    """設備機器レイヤーか（洗面台・キッチン・UB等をカタログ照合するため）"""
    return not _is_excluded_layer(name) and '設備' in name


def _is_tategu_layer(name):
    """建具レイヤーか（'壁・建具' のような壁兼用レイヤーは除く）"""
    if name == TATEGU_LAYER:
        return True
    return not _is_excluded_layer(name) and '建具' in name and '壁' not in name

# 玄関扉（片開き）
ENT_DOOR_T = 40            # 玄関扉パネル厚 mm
ENT_ARC_SEARCH_R = 3000    # 玄関ラベルから開き戸ARCを探す半径 mm

# ズレ防止・セルフチェック関連
HEAL_T = 100        # 補完壁（未カバー壁線の自動補完）の厚み mm
HEAL_COVER = 0.4    # この被覆率未満の図面壁線を補完対象にする
DRAW_GUIDE = True   # 図面ガイド線（赤）を3Dモデルレイヤーに重ね描きする
GUIDE_MAX = 800     # ガイド線の最大本数

WALL_LINE_T = 100     # 壁ラインをそのまま立ち上げる時の厚み mm（線が壁面の中心）

# 部屋天井・バルコニー
DRAW_CEILINGS = False  # 部屋天井プレートは貼らない（ユーザー指示。天井梁は別途生成する）
CEIL_T = 50           # 部屋天井プレートの厚み mm（DRAW_CEILINGS=True時のみ使用）
PARAPET_H = 1100      # バルコニー手すり壁の高さ mm
PARAPET_T = 100       # バルコニー手すり壁の厚み mm
BALCONY_KW = ('バルコニー', 'ベランダ', 'テラス')
ROOM_SEED_KW = ('トイレ', 'WC', '浴室', 'UB', '洗面', '脱衣', 'キッチン', 'LDK', 'DK',
                'リビング', 'ダイニング', '玄関', '廊下', 'WIC', 'クローゼット',
                '個室', '寝室', '洋室', '和室', 'ホール', '納戸')

# 天井梁（「天井」クラスまたは点線の線 = 梁ライン。梁下端は近傍の CH≒ 注記を参照）
CEIL_LAYER = '天井'
CEIL_BEAM_W = 200          # 単線梁ラインを幅に展開する既定値 mm
CEIL_PAIR_MIN, CEIL_PAIR_MAX = 80, 800   # 平行2線を梁エッジペアとみなす間隔 mm
CH_TEXT_SEARCH_R = 2500    # 梁ラインから CH≒ 注記を探す半径 mm
# 点線（DASHED等）を梁として拾うレイヤー（設備のダクト点線等を誤検出しないよう限定）
DASH_BEAM_LAYERS = {'天井', '躯体', '一般', '梁'}
# MUJI家具ライブラリ（VW2021ネイティブ形式）。生成スクリプトが実行時にここから
# シンボル定義を自動インポートする。インポート不可なら W×D×H の簡易ボックスで代替
MUJI_LIB = '/Users/aikawawakou/Documents/MUJIHOUSE/MUJI家具 (1) v2021.vwx'

# 形状判定の閾値
MIN_SIDE = 60      # bbox 短辺がこれ未満は無視（細線ノイズ）
EDGE_TOL = 400     # 「外周ライン」とみなす端からの許容距離
WALL_T = 200       # 人工窓帯の見込（壁厚）
STAIR_GAP = 2500   # 本体東端からこれ以上離れた塊は外部とみなす
WIN_W_MIN, WIN_W_MAX = 700, 1700   # 南北の窓開口幅レンジ


# ─────────────────────────────────────────────
# DXF 読み込み補助
# ─────────────────────────────────────────────
def _bulge_arc(p1, p2, bulge, n=8):
    """バルジ付きセグメント → 円弧をn分割した中間点列（p1の次からp2の手前まで）"""
    import math
    ang = 4.0 * math.atan(bulge)
    dx, dy = p2[0] - p1[0], p2[1] - p1[1]
    chord = math.hypot(dx, dy)
    if chord < 1e-9 or abs(ang) < 1e-9:
        return []
    r = chord / (2.0 * math.sin(abs(ang) / 2.0))
    mx, my = (p1[0] + p2[0]) / 2.0, (p1[1] + p2[1]) / 2.0
    h = r * math.cos(ang / 2.0)          # 弦の中点→中心の距離（符号はバルジ向き）
    nx, ny = -dy / chord, dx / chord     # 弦の左法線
    sgn = 1.0 if bulge > 0 else -1.0
    cx, cy = mx - nx * h * sgn, my - ny * h * sgn
    a1 = math.atan2(p1[1] - cy, p1[0] - cx)
    out = []
    for i in range(1, n):
        a = a1 + ang * i / n
        out.append((cx + r * math.cos(a), cy + r * math.sin(a)))
    return out


def get_pts(e):
    """ポリラインの頂点列と閉判定。
    - バルジ（円弧セグメント）は8分割で展開（曲面壁の追従）
    - 閉フラグが無くても始点=終点なら閉ポリとみなす（重複端点は除去）"""
    pts, closed, bulges = [], False, []
    if e.dxftype() == 'LWPOLYLINE':
        raw = list(e.get_points())          # (x, y, start_w, end_w, bulge)
        pts = [(p[0], p[1]) for p in raw]
        bulges = [p[4] if len(p) > 4 else 0 for p in raw]
        closed = bool(e.closed)
    elif e.dxftype() == 'POLYLINE':
        vtx = [v for v in e.vertices if v.dxftype() != 'ENDBLK']
        pts = [(v.dxf.location.x, v.dxf.location.y) for v in vtx]
        bulges = [getattr(v.dxf, 'bulge', 0) or 0 for v in vtx]
        closed = bool(e.is_closed)
    else:
        return [], False
    if not pts:
        return [], False
    # 実質閉じている開ポリ（始点=終点）
    if not closed and len(pts) >= 4 and \
            abs(pts[0][0] - pts[-1][0]) < 1 and abs(pts[0][1] - pts[-1][1]) < 1:
        pts = pts[:-1]
        bulges = bulges[:-1]
        closed = True
    # バルジ展開
    if any(abs(b) > 1e-9 for b in bulges):
        out = []
        n = len(pts)
        last = n if closed else n - 1
        for i in range(n):
            out.append(pts[i])
            if i < last and abs(bulges[i]) > 1e-9:
                out += _bulge_arc(pts[i], pts[(i + 1) % n], bulges[i])
        pts = out
    return pts, closed


def bbox_of(pts):
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return [min(xs), min(ys), max(xs), max(ys)]


def parse_dim_text(text):
    if not text:
        return None
    clean = re.sub(r'\\[A-Za-z][^;]*;', '', text)
    clean = re.sub(r'[≒〜～]', '', clean).strip()
    try:
        return round(float(clean))
    except ValueError:
        return None


def _plain_text(e):
    """TEXT/MTEXT から整形コード（\\f...; や \\U+XXXX 等）を除いた素のテキストを取る"""
    try:
        if e.dxftype() == 'MTEXT':
            return e.plain_text()
        return e.dxf.text or ''
    except Exception:
        return getattr(e.dxf, 'text', '') or ''


def _iter_texts(doc):
    """全ブロック（*Model_Space 含む）の TEXT/MTEXT を NFKC 正規化して列挙"""
    for txt, _, _ in _iter_texts_pos(doc):
        yield txt


def _iter_texts_pos(doc):
    """TEXT/MTEXT を (NFKC正規化テキスト, x, y) で列挙。
    INSERT変換を適用したワールド座標で返す"""
    for e in iter_world(doc, expand_parts=True):
        if e.dxftype() not in ('TEXT', 'MTEXT'):
            continue
        txt = _plain_text(e)
        if not txt:
            continue
        ins = e.dxf.insert
        yield unicodedata.normalize('NFKC', txt), ins.x, ins.y


# "天井高2400" / "CH≒2400" / "CH:3670" / "C.H=2.4m" 等（NFKC正規化後に適用）
CH_PAT = re.compile(
    r'(?:天井高さ?|\bC\.?H\.?)\s*[:=≒~〜≈]?\s*([\d,]+(?:\.\d+)?)\s*(mm|m)?',
    re.IGNORECASE)


def detect_ch_positions(doc, xo=0, yo=0, lo=1000, hi=6000):
    """CH/天井高 注記を位置つきで収集: [(値mm, x, y), ...]（グリッド座標）。
    梁下の CH≒1700 等も含めるため下限は1000。"""
    out = []
    for txt, tx, ty in _iter_texts_pos(doc):
        for m in CH_PAT.finditer(txt):
            raw, unit = m.group(1), m.group(2)
            try:
                v = float(raw.replace(',', ''))
            except ValueError:
                continue
            if (unit and unit.lower() == 'm') or v < 100:   # メートル表記 → mm換算
                v *= 1000
            v = round(v)
            if lo <= v <= hi:
                out.append((v, tx - xo, ty - yo))
    return out


def detect_ceiling_heights(doc):
    """図面の天井高注記を全て収集する。
    返り値: (採用CH or None, [(値, 件数), ...] 件数降順)
    採用値 = 最頻値（同数なら大きい方）。下がり天井・梁下の注記が混在しても
    居室の主天井高が最も多く記載される想定。1800〜6000mm のみ有効。"""
    vals = [v for v, _, _ in detect_ch_positions(doc, lo=1800, hi=6000)]
    if not vals:
        return None, []
    cnt = Counter(vals)
    best = max(cnt.items(), key=lambda kv: (kv[1], kv[0]))[0]
    return best, cnt.most_common()


# 部屋ラベル → 家具カテゴリの事前分布（家具の寸法照合をその部屋らしいカテゴリに絞る）
ROOM_PRIOR_KEYWORDS = [
    ({'衛生'}, ('トイレ', 'WC', '便所', '浴室', 'UB', '風呂')),
    ({'衛生', '家電'}, ('洗面', '脱衣')),
    ({'キッチン', '家電', '収納'}, ('キッチン', '台所', 'KIT')),
    ({'ソファ', 'テーブル', 'チェア', '収納', '家電'}, ('リビング', '居間')),
    ({'テーブル', 'チェア', '収納'}, ('ダイニング',)),
    ({'キッチン', '家電', 'ソファ', 'テーブル', 'チェア', '収納'}, ('LDK', 'LD', 'DK')),
    ({'ベッド', '収納', 'テーブル', 'チェア'}, ('個室', '寝室', '洋室', '和室')),
    ({'収納'}, ('玄関', '廊下', 'WIC', 'クローゼット', '押入')),
]


def detect_room_priors(doc, xo, yo):
    """部屋ラベルの位置とカテゴリ集合を収集: [(カテゴリ集合, x, y), ...]"""
    pts = []
    for txt, tx, ty in _iter_texts_pos(doc):
        u = txt.upper()
        cats = set()
        for cs, kws in ROOM_PRIOR_KEYWORDS:
            if any(k in u for k in kws):
                cats |= cs
        if cats:
            pts.append((cats, tx - xo, ty - yo))
    return pts


def _is_bed_size(w, d):
    """マットレス標準寸法（幅±80 × 長さ1900〜2100、向き両対応）か"""
    for a, b in ((w, d), (d, w)):
        if BED_LEN_MIN <= b <= BED_LEN_MAX and \
                any(abs(a - bw) <= 80 for bw in BED_WIDTHS):
            return True
    return False


# 基本6室のラベル辞書（NFKC正規化・大文字化した文字列に部分一致）
ROOM_KEYWORDS = [
    ('トイレ', ('トイレ', 'WC', '便所')),
    ('浴室', ('浴室', 'UB', '風呂', 'ユニットバス', 'バス')),
    ('洗面所', ('洗面', '脱衣')),
    ('キッチン', ('キッチン', '台所', 'DK', 'LDK', 'KIT')),
    ('リビング', ('リビング', '居間', 'LDK', 'LD')),
    ('ダイニング', ('ダイニング', 'LDK', 'DK')),
]


def detect_rooms(doc):
    """部屋名ラベルを検出し、基本6室の有無チェックリストを返す。
    返り値: (found: {部屋: マッチしたラベル}, missing: [部屋, ...])"""
    texts = [t.upper() for t in _iter_texts(doc)]
    found = {}
    for room, kws in ROOM_KEYWORDS:
        for t in texts:
            hit = next((kw for kw in kws if kw in t), None)
            if hit:
                found[room] = t.strip()[:20]
                break
    missing = [room for room, _ in ROOM_KEYWORDS if room not in found]
    return found, missing


def detect_grid(doc):
    """*D 寸法ブロックから グリッド原点(左下)とグリッド範囲を推定

    返り値: xo, yo, gx_max, gy_max
      xo,yo      = グリッド原点（DXF生座標）
      gx_max     = グリッド X 幅（正規化後の最大値 ≒ 建物幅）
      gy_max     = グリッド Y 幅（正規化後の最大値 ≒ 建物高）
    """
    h_spans, v_spans = [], []
    for blk in doc.blocks:
        if not blk.name.startswith('*D'):
            continue
        lines = [e for e in blk if e.dxftype() == 'LINE']
        mtexts = [e for e in blk if e.dxftype() == 'MTEXT']
        if not lines:
            continue
        xs = [l.dxf.start.x for l in lines] + [l.dxf.end.x for l in lines]
        ys = [l.dxf.start.y for l in lines] + [l.dxf.end.y for l in lines]
        xsp, ysp = max(xs) - min(xs), max(ys) - min(ys)
        dv = None
        for m in mtexts:
            v = parse_dim_text(m.text)
            if v and v > 100:
                dv = v
                break
        if xsp > ysp and xsp > 500:
            h_spans.append((round(min(xs)), round(max(xs)), dv))
        elif ysp > xsp and ysp > 500:
            v_spans.append((round(min(ys)), round(max(ys)), dv))

    # X: 最頻スパン値を持つ寸法の端点 = 主グリッド列
    xv = [v for _, _, v in h_spans if v]
    main_x = Counter(xv).most_common(1)[0][0] if xv else None
    raw_x = sorted(set(p for x1, x2, v in h_spans
                       if (main_x is None or v == main_x) for p in (x1, x2)))
    # Y: 最頻スパン値を持つ寸法の端点 = 主グリッド行
    yv = [v for _, _, v in v_spans if v]
    main_y = Counter(yv).most_common(1)[0][0] if yv else None
    raw_y = sorted(set(p for y1, y2, v in v_spans
                       if (main_y is None or v == main_y) for p in (y1, y2)))

    xo = min(raw_x) if raw_x else 0
    yo = min(raw_y) if raw_y else 0
    gx_max = (max(raw_x) - xo) if raw_x else 0
    gy_max = (max(raw_y) - yo) if raw_y else 0
    return xo, yo, gx_max, gy_max


def _block_min_side(blk):
    """ブロック内容のbbox短辺。建具・記号など小型部品ブロックの判定に使う"""
    xs, ys = [], []
    for e in blk:
        t = e.dxftype()
        if t == 'LINE':
            s, en = e.dxf.start, e.dxf.end
            xs += [s.x, en.x]
            ys += [s.y, en.y]
        elif t in ('POLYLINE', 'LWPOLYLINE'):
            pts, _ = get_pts(e)
            xs += [p[0] for p in pts]
            ys += [p[1] for p in pts]
    if not xs:
        return 0
    return min(max(xs) - min(xs), max(ys) - min(ys))


def _is_part_block(blk):
    """壁抽出から除外すべき部品ブロックか（引戸・建具ユニット・記号等）。
    *Model_Space と、平面図全体を包むデザインレイヤーブロックは対象外（=壁抽出する）"""
    if blk.name == '*Model_Space':
        return False
    return _block_min_side(blk) < BLK_MIN_SIDE


def iter_world(doc, expand_parts=False, depth=8):
    """modelspace起点でINSERTをワールド座標に展開してエンティティを列挙する。
    従来の「全ブロックをローカル座標のまま走査」は、原点以外に挿入・回転された
    ブロック（UBユニット・AWデータ等）の中身が躯体・壁・建具と相互にズレる原因だった。
    ezdxf の virtual_entities() で挿入点・回転・スケールを適用した実座標コピーを得る。

    expand_parts=False: 部品ブロック（bbox短辺<BLK_MIN_SIDE、建具ユニット・記号等）の
      中身は展開しない（INSERTとしてのみ返す。建具・家具として別途処理するため）
    expand_parts=True: すべて展開（ドアARC・扉レクト・テキスト等、部品内も見る用途）
    INSERT エンティティ自体は展開の有無に関わらず（変換適用済みの状態で）返す。"""
    size_cache = {}
    ext_cache = {}

    def _small(name):
        # 部品ブロック判定。ただし「INSERTを内包するブロック」はグループ（家具セット等）
        # なので大きさに関わらず必ず展開する（中身の家具が見えなくなるのを防ぐ）。
        # 直描き図形のみのブロックはネスト込み実extentの短辺で判定
        if name not in size_cache:
            if name not in doc.blocks:
                size_cache[name] = True
            elif any(be.dxftype() == 'INSERT' for be in doc.blocks[name]):
                size_cache[name] = False   # グループノード → 展開
            else:
                bb = _block_extent(doc, name, cache=ext_cache)
                size_cache[name] = (bb is None
                                    or min(bb[2] - bb[0], bb[3] - bb[1]) < BLK_MIN_SIDE)
        return size_cache[name]

    def walk(entities, d):
        for e in entities:
            if e.dxftype() == 'INSERT':
                yield e
                if d <= 0:
                    continue
                if not expand_parts and _small(e.dxf.name):
                    continue
                try:
                    yield from walk(e.virtual_entities(), d - 1)
                except Exception:
                    pass
            else:
                yield e

    yield from walk(doc.modelspace(), depth)


def load_polys(doc, xo, yo):
    """躯体ポリラインを実頂点（グリッド座標）で取得。INSERT変換適用済みのワールド座標"""
    polys = []
    for e in iter_world(doc):
        if e.dxftype() not in ('POLYLINE', 'LWPOLYLINE'):
            continue
        if not _is_wall_layer(getattr(e.dxf, 'layer', '')):
            continue
        pts, closed = get_pts(e)
        if len(pts) < 3:
            continue
        g = [(round(x - xo), round(y - yo)) for x, y in pts]
        bb = bbox_of(g)
        if min(bb[2] - bb[0], bb[3] - bb[1]) < MIN_SIDE:
            continue
        polys.append({'layer': e.dxf.layer, 'pts': g, 'bbox': bb})
    return polys


# ─────────────────────────────────────────────
# 内壁（間仕切り壁）抽出
# ─────────────────────────────────────────────
def load_hatch_polys(doc, xo, yo, existing):
    """壁レイヤーのHATCH（塗り）境界から壁ポリを復元する。
    閉ポリラインが無くHATCHだけで壁が描かれた図面への対応。
    既存ポリと6割超重なるものは重複なので捨てる"""
    polys = []
    for e in iter_world(doc):
        if e.dxftype() != 'HATCH':
            continue
        if not _is_wall_layer(getattr(e.dxf, 'layer', '')):
            continue
        try:
            paths = e.paths.paths
        except Exception:
            continue
        for path in paths:
            pts = []
            try:
                if hasattr(path, 'vertices'):          # PolylinePath
                    pts = [(v[0], v[1]) for v in path.vertices]
                elif hasattr(path, 'edges'):           # EdgePath
                    for ed in path.edges:
                        if hasattr(ed, 'start'):
                            pts.append((ed.start[0], ed.start[1]))
            except Exception:
                continue
            if len(pts) < 3:
                continue
            g = [(round(x - xo), round(y - yo)) for x, y in pts]
            bb = bbox_of(g)
            if min(bb[2] - bb[0], bb[3] - bb[1]) < MIN_SIDE:
                continue
            if any(_ix_ratio(bb, p['bbox']) > 0.6 and _ix_ratio(p['bbox'], bb) > 0.6
                   for p in existing):
                continue   # 既に閉ポリで拾えている壁
            polys.append({'layer': e.dxf.layer, 'pts': g, 'bbox': bb})
    return polys


def _scan_intervals(poly, y):
    """多角形とy水平線の交差x区間（[x1,x2],...）をスキャンラインで求める"""
    xs = []
    n = len(poly)
    for i in range(n):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % n]
        if (y1 <= y < y2) or (y2 <= y < y1):
            xs.append(x1 + (y - y1) * (x2 - x1) / (y2 - y1))
    xs.sort()
    return [(xs[i], xs[i + 1]) for i in range(0, len(xs) - 1, 2)]


def _sub_intervals(base, holes):
    """区間集合 base から holes を引く"""
    out = list(base)
    for h1, h2 in holes:
        nxt = []
        for b1, b2 in out:
            if h2 <= b1 or h1 >= b2:
                nxt.append((b1, b2))
            else:
                if b1 < h1:
                    nxt.append((b1, h1))
                if h2 < b2:
                    nxt.append((h2, b2))
        out = nxt
    return out


def _edge_walls(pts, closed=True):
    """ポリラインの辺=壁のラインを『そのまま』返す（厚みは付けない）。
    生成側で厚みなしの垂直面（ラインの押し出し）として立ち上げる。
    軸整列の辺は縮退矩形[x1,y1,x2,y2]（x1==x2 か y1==y2。開口カットに使える形）、
    斜めの辺は端点そのまま[[x1,y1,x2,y2]]で返す"""
    import math
    rects, diags = [], []
    n = len(pts)
    rng = n if closed else n - 1
    for i in range(rng):
        x1, y1 = pts[i]
        x2, y2 = pts[(i + 1) % n]
        L = math.hypot(x2 - x1, y2 - y1)
        if L < 50:
            continue
        if abs(y1 - y2) <= 5:      # 横辺（縮退矩形: y1==y2）
            rects.append([round(min(x1, x2)), round(y1),
                          round(max(x1, x2)), round(y1)])
        elif abs(x1 - x2) <= 5:    # 縦辺（縮退矩形: x1==x2）
            rects.append([round(x1), round(min(y1, y2)),
                          round(x1), round(max(y1, y2))])
        else:                      # 斜め辺
            diags.append([round(x1), round(y1), round(x2), round(y2)])
    return rects, diags


def ring_rects(outer, inner, grid=50):
    """外形ポリゴンと内形ポリゴンの間（リング=躯体壁）を実形状どおりに矩形分解する。
    スキャンラインで行ごとの区間を出し、同一区間は縦に結合。
    bbox4辺帯の近似で南面などが過大になる問題の根本対応"""
    ys = [p[1] for p in outer]
    y1, y2 = min(ys), max(ys)
    rows = []
    y = y1 + grid / 2.0
    while y < y2:
        iv = _sub_intervals(_scan_intervals(outer, y), _scan_intervals(inner, y))
        rows.append((y - grid / 2.0,
                     [(a, b) for a, b in iv if b - a >= 20]))
        y += grid
    # 同一x区間の行を縦に結合
    rects = []
    open_runs = {}   # (round(x1), round(x2)) -> [y開始, y終了]
    for ry, ivs in rows + [(y2, [])]:
        cur = {(round(a), round(b)) for a, b in ivs}
        for key in list(open_runs):
            if key not in cur:
                r0, r1 = open_runs.pop(key)
                rects.append([key[0], round(r0), key[1], round(r1)])
        for key in cur:
            if key in open_runs:
                open_runs[key][1] = ry + grid
            else:
                open_runs[key] = [ry, ry + grid]
    for key, (r0, r1) in open_runs.items():
        rects.append([key[0], round(r0), key[1], round(r1)])
    return rects


def outline_bands(pts, t=100):
    """ペア無し輪郭線を、輪郭に沿った厚みtの帯壁矩形に変換する（破棄しない）。
    軸整列の辺のみ（斜め辺はスキップ）"""
    bands = []
    n = len(pts)
    for i in range(n):
        x1, y1 = pts[i]
        x2, y2 = pts[(i + 1) % n]
        if abs(y1 - y2) <= 10 and abs(x1 - x2) >= 200:     # 横辺
            bands.append([round(min(x1, x2)), round(y1 - t / 2),
                          round(max(x1, x2)), round(y1 + t / 2)])
        elif abs(x1 - x2) <= 10 and abs(y1 - y2) >= 200:   # 縦辺
            bands.append([round(x1 - t / 2), round(min(y1, y2)),
                          round(x1 + t / 2), round(max(y1, y2))])
    return bands


def _poly_area(pts):
    """多角形面積（shoelace）"""
    s = 0.0
    for i in range(len(pts)):
        x1, y1 = pts[i]
        x2, y2 = pts[(i + 1) % len(pts)]
        s += x1 * y2 - x2 * y1
    return abs(s) / 2.0


def split_outlines(polys):
    """壁帯ポリと「塗り率の高い閉ポリ」を分離する。
    壁帯は輪郭が壁厚分の帯なので塗り率が低い。塗り率が高く短辺が大きいものは
    RC躯体の外形線・部屋輪郭・家具外形などで、そのまま押し出すと中身の詰まった
    ブロックになるため分離が必須（例: 建物外形12400x7136、ベッド外形1030x2020）。"""
    bands, outlines = [], []
    for p in polys:
        bb = p['bbox']
        w, h = bb[2] - bb[0], bb[3] - bb[1]
        fill = _poly_area(p['pts']) / (w * h) if w > 0 and h > 0 else 0
        if fill > 0.9 and max(w, h) <= 1300:
            bands.append(p)   # 大型RC柱（700〜1300角・塗り率≈1）は壁として立ち上げる
        elif fill > 0.6 and min(w, h) > 700:
            # 壁帯は輪郭が薄いので塗り率<0.4程度。0.6超かつ短辺700超は家具・輪郭
            outlines.append(p)
        else:
            bands.append(p)
    return bands, outlines


def outline_frames(outlines):
    """二重外形（RC躯体の外形線+内形線）を『壁ラインそのまま』立ち上げる。
    それぞれの輪郭線を線中心・厚WALL_LINE_Tの壁に変換（図面の線と壁面が一致）。
    ペアにならなかった建物スケールの単独輪郭も同様に立ち上げ、
    家具スケールのものは破棄リストで返す（家具照合フローが扱う）。
    返り値: (frames[縮退矩形=ライン], frame_quads[斜めライン端点], dropped, envelopes)"""
    frames, frame_quads, used, envelopes = [], [], set(), []
    outlines = sorted(
        outlines,
        key=lambda p: -(p['bbox'][2] - p['bbox'][0]) * (p['bbox'][3] - p['bbox'][1]))
    for i, outer in enumerate(outlines):
        if i in used:
            continue
        ob = outer['bbox']
        for j in range(i + 1, len(outlines)):
            if j in used:
                continue
            ib = outlines[j]['bbox']
            gaps = (ib[0] - ob[0], ib[1] - ob[1], ob[2] - ib[2], ob[3] - ib[3])
            if all(40 <= gp <= 1000 for gp in gaps):
                used.add(i)
                used.add(j)
                envelopes.append(list(ob))
                # 外形線・内形線それぞれを「壁ラインそのまま」立ち上げる
                r1, q1 = _edge_walls(outer['pts'])
                r2, q2 = _edge_walls(outlines[j]['pts'])
                frames += r1 + r2
                frame_quads += q1 + q2
                break
    dropped = []
    for k in range(len(outlines)):
        if k in used:
            continue
        p = outlines[k]
        bb = p['bbox']
        if min(bb[2] - bb[0], bb[3] - bb[1]) > 2400:
            # 建物・部屋スケールの単独輪郭 → 壁ラインそのまま立ち上げ（破棄しない）
            r1, q1 = _edge_walls(p['pts'])
            frames += r1
            frame_quads += q1
        else:
            dropped.append(p)   # 家具スケール → 家具照合フローに任せる
    return frames, frame_quads, dropped, envelopes


def _is_step_polyline(g):
    """段差マーク（Z形: 長い線→短い直交コネクタ→長い線）の開ポリラインか。
    床レベル差の表記であり壁・梁ではないので無視する"""
    if len(g) != 4:
        return False
    seg = [(g[i + 1][0] - g[i][0], g[i + 1][1] - g[i][1]) for i in range(3)]
    lens = [max(abs(dx), abs(dy)) for dx, dy in seg]
    if not (lens[1] <= 600 and lens[0] >= 800 and lens[2] >= 800):
        return False
    h0 = abs(seg[0][0]) >= abs(seg[0][1])   # 外側2本が同方向・コネクタが直交
    h2 = abs(seg[2][0]) >= abs(seg[2][1])
    h1 = abs(seg[1][0]) >= abs(seg[1][1])
    return h0 == h2 and h1 != h0


def _step_mark_lines(raw):
    """バラ線で描かれた段差マークのインデックス集合を返す。
    短い直交コネクタの両端点が、平行で投影の重ならない長線2本の端点に接続していればZ形"""
    step = set()

    def near(p, q):
        return abs(p[0] - q[0]) <= 15 and abs(p[1] - q[1]) <= 15

    for i, (x1, y1, x2, y2) in enumerate(raw):
        li = max(abs(x2 - x1), abs(y2 - y1))
        if li > 600:
            continue
        conn_h = abs(x2 - x1) >= abs(y2 - y1)
        hits = []   # (端点index, 長線j)
        for j, (a1, b1, a2, b2) in enumerate(raw):
            if j == i:
                continue
            lj = max(abs(a2 - a1), abs(b2 - b1))
            if lj < 800:
                continue
            long_h = abs(a2 - a1) >= abs(b2 - b1)
            if long_h == conn_h:   # 長線はコネクタと直交方向
                continue
            for pi, pe in enumerate(((x1, y1), (x2, y2))):
                if near(pe, (a1, b1)) or near(pe, (a2, b2)):
                    hits.append((pi, j))
        ends = {pi for pi, _ in hits}
        longs = {j for _, j in hits}
        if len(ends) == 2 and len(longs) >= 2:
            step.add(i)
            step.update(longs)
    return step


def _seg_pool(doc, xo, yo):
    """内壁ペアリング用の軸整列セグメントと、薄壁閉ポリの矩形を収集する。
    対象: PART_LAYERS 上の LINE・開ポリラインの辺・薄壁閉ポリ（短辺25〜60mm）。
    閉ポリで短辺MIN_SIDE以上のものは load_polys が拾うためここでは扱わない。
    段差マーク（Z形の床レベル差表記）は壁ではないので除外する。"""
    hsegs, vsegs, thin_rects = [], [], []
    raw_lines = []

    def add_seg(x1, y1, x2, y2):
        if abs(y1 - y2) <= PART_AXIS_TOL and abs(x1 - x2) >= PART_OVL_MIN:
            hsegs.append(((y1 + y2) / 2.0, min(x1, x2), max(x1, x2)))
        elif abs(x1 - x2) <= PART_AXIS_TOL and abs(y1 - y2) >= PART_OVL_MIN:
            vsegs.append(((x1 + x2) / 2.0, min(y1, y2), max(y1, y2)))

    dash_words = DASH_WORDS
    for e in iter_world(doc):
        if not _is_partition_layer(getattr(e.dxf, 'layer', '')):
            continue
        t = e.dxftype()
        if t not in ('LINE', 'POLYLINE', 'LWPOLYLINE'):
            continue
        # 点線は梁・下がり天井等の表記であり壁ではない
        if any(wd in _linetype_of(doc, e) for wd in dash_words):
            continue
        if t == 'LINE':
            s, en = e.dxf.start, e.dxf.end
            raw_lines.append((s.x - xo, s.y - yo, en.x - xo, en.y - yo))
        else:
            pts, closed = get_pts(e)
            if len(pts) < 2:
                continue
            g = [(x - xo, y - yo) for x, y in pts]
            bb = bbox_of(g)
            short = min(bb[2] - bb[0], bb[3] - bb[1])
            if closed and len(pts) >= 3:
                if THIN_MIN <= short < THIN_MAX and \
                        max(bb[2] - bb[0], bb[3] - bb[1]) >= 400:
                    thin_rects.append([round(bb[0]), round(bb[1]),
                                       round(bb[2]), round(bb[3])])
                continue   # 閉ポリの辺はペアリングに流さない（load_polysと二重化するため）
            if _is_step_polyline(g):
                continue   # 段差マーク
            for k in range(len(g) - 1):
                add_seg(g[k][0], g[k][1], g[k + 1][0], g[k + 1][1])

    step_ids = _step_mark_lines(raw_lines)
    for i, (x1, y1, x2, y2) in enumerate(raw_lines):
        if i not in step_ids:
            add_seg(x1, y1, x2, y2)
    return hsegs, vsegs, thin_rects


def _pair_walls(segs):
    """平行セグメントを壁厚レンジ[PART_T_MIN, PART_T_MAX]で最近接ペアリング。
    greedy・1セグメント1回のみ使用（壁芯+両面線の三重線が二重壁になるのを防ぐ）。
    返り値: [(pos1, pos2, ovl1, ovl2), ...]"""
    segs = sorted(segs)
    used = [False] * len(segs)
    walls = []
    for i, (p1, a1, a2) in enumerate(segs):
        if used[i]:
            continue
        best_j, best_gap = None, None
        for j in range(i + 1, len(segs)):
            if used[j]:
                continue
            p2, b1, b2 = segs[j]
            gap = p2 - p1
            if gap > PART_T_MAX:
                break
            if gap < PART_T_MIN:
                continue
            if min(a2, b2) - max(a1, b1) < PART_OVL_MIN:
                continue
            if best_gap is None or gap < best_gap:
                best_j, best_gap = j, gap
        if best_j is None:
            continue
        p2, b1, b2 = segs[best_j]
        used[i] = used[best_j] = True
        walls.append((p1, p2, max(a1, b1), min(a2, b2)))
    # 第2パス: 未使用セグメントを使用済み相手とも組ませる
    # （片面が連続1本・反対面が分割2本の壁で、残区間が取り逃されるのを防ぐ。
    #   重複はビルダー側の _ix_ratio マージで除去される）
    for i, (p1, a1, a2) in enumerate(segs):
        if used[i]:
            continue
        best, best_gap = None, None
        for j, (p2, b1, b2) in enumerate(segs):
            if j == i:
                continue
            gap = abs(p2 - p1)
            if not (PART_T_MIN <= gap <= PART_T_MAX):
                continue
            if min(a2, b2) - max(a1, b1) < PART_OVL_MIN:
                continue
            if best_gap is None or gap < best_gap:
                lo, hi = (p1, p2) if p1 < p2 else (p2, p1)
                best, best_gap = (lo, hi, max(a1, b1), min(a2, b2)), gap
        if best:
            used[i] = True
            walls.append(best)
    return walls


def _collect_thin_polys(doc, xo, yo):
    """扉パネル候補: 全ブロックの薄い閉ポリ（厚15〜80mm×長さ400mm+）のbbox一覧。
    玄関扉の閉じ位置判定と実パネル取得に使う"""
    out = []
    for e in iter_world(doc, expand_parts=True):
        if e.dxftype() not in ('POLYLINE', 'LWPOLYLINE'):
            continue
        pts, closed = get_pts(e)
        if not closed or len(pts) < 3:
            continue
        g = [(x - xo, y - yo) for x, y in pts]
        bb = bbox_of(g)
        t = min(bb[2] - bb[0], bb[3] - bb[1])
        length = max(bb[2] - bb[0], bb[3] - bb[1])
        if 15 <= t <= 80 and length >= 400:
            out.append([round(v) for v in bb])
    return out


def extract_door_arcs(doc, xo, yo):
    """開き戸ARC → ドア情報（吊元・弦の両端）。内壁の開口抜きに使う。
    ドアARCは建具レイヤーとは限らない（一般・家具01等にも実在）ため
    全レイヤーを走査し、半径レンジ（550〜1200）で家具の小円弧を除外する"""
    import math
    doors = []
    for e in iter_world(doc, expand_parts=True):
        if e.dxftype() != 'ARC':
            continue
        r = e.dxf.radius
        if not (DOOR_ARC_R_MIN <= r <= DOOR_ARC_R_MAX):
            continue
        c = e.dxf.center
        sa = math.radians(e.dxf.start_angle)
        ea = math.radians(e.dxf.end_angle)
        # ミラー配置のARCはOCS座標（extrusion.z<0）で返る → WCSはxを反転
        flip = False
        try:
            flip = e.dxf.extrusion[2] < 0
        except Exception:
            pass
        pts3 = [(c.x, c.y),
                (c.x + r * math.cos(sa), c.y + r * math.sin(sa)),
                (c.x + r * math.cos(ea), c.y + r * math.sin(ea))]
        if flip:
            pts3 = [(-px, py) for px, py in pts3]
        doors.append({
            'hinge': (pts3[0][0] - xo, pts3[0][1] - yo),
            'p1': (pts3[1][0] - xo, pts3[1][1] - yo),
            'p2': (pts3[2][0] - xo, pts3[2][1] - yo),
            'r': r,
        })
    return doors


def _xform_pt(e, x, y):
    """INSERTの挿入変換（スケール→回転→平行移動→OCS反転）をブロックローカル点に適用。
    ミラー配置は extrusion.z=-1 のOCSで表現されるため、X軸反転まで含めて処理する"""
    import math
    sx = getattr(e.dxf, 'xscale', 1) or 1
    sy = getattr(e.dxf, 'yscale', 1) or 1
    r = math.radians(getattr(e.dxf, 'rotation', 0))
    px, py = x * sx, y * sy
    wx = e.dxf.insert.x + px * math.cos(r) - py * math.sin(r)
    wy = e.dxf.insert.y + px * math.sin(r) + py * math.cos(r)
    try:
        if e.dxf.extrusion[2] < 0:   # ミラーOCS: WCSのxはOCSの-x
            wx = -wx
    except Exception:
        pass
    return (wx, wy)


def _xform_bbox(e, bb, xo, yo):
    """ブロックローカルbboxをワールドbbox（グリッド座標）へ変換"""
    pts = [_xform_pt(e, bb[0], bb[1]), _xform_pt(e, bb[0], bb[3]),
           _xform_pt(e, bb[2], bb[1]), _xform_pt(e, bb[2], bb[3])]
    xs = [p[0] - xo for p in pts]
    ys = [p[1] - yo for p in pts]
    return [round(min(xs)), round(min(ys)), round(max(xs)), round(max(ys))]


def extract_door_units(doc, xo, yo):
    """壁レイヤー上の建具ユニットINSERT → 3方枠+扉パネル用の情報を抽出。
    ユニット判定: bbox短辺40〜300mm・長辺500mm以上（引戸・引き違い戸の帯形状）。
    扉パネル: ブロック内の閉ポリで厚み20〜60mm・長さ300mm以上・ユニット全長の9割未満
    （ユニット外形線・レール線9mm・框・ガラス5mmは除く）"""
    units = []
    for e in iter_world(doc):
        if e.dxftype() != 'INSERT':
            continue
        _lay = getattr(e.dxf, 'layer', '')
        if not (_is_wall_layer(_lay) or _is_tategu_layer(_lay)):
            continue
        # 家具と同じ厳密ワールドbbox（円弧の実範囲・ミラーOCS・ネスト全対応）
        wb = _insert_world_bbox(e)
        if not wb:
            continue
        wbb = [round(wb[0] - xo), round(wb[1] - yo),
               round(wb[2] - xo), round(wb[3] - yo)]
        w, d = wbb[2] - wbb[0], wbb[3] - wbb[1]
        if not (DOOR_U_T_MIN <= min(w, d) <= DOOR_U_T_MAX
                and max(w, d) >= DOOR_U_LEN_MIN):
            continue
        run = max(w, d)
        # 扉パネル: サブツリーをワールド座標で展開して薄い閉ポリを拾う
        panels = []

        def _collect_panels(e0, depth=0):
            try:
                ents = list(e0.virtual_entities())
            except Exception:
                return
            for pe in ents:
                t = pe.dxftype()
                if t == 'INSERT' and depth < 4:
                    _collect_panels(pe, depth + 1)
                    continue
                if t not in ('POLYLINE', 'LWPOLYLINE'):
                    continue
                pts, closed = get_pts(pe)
                if not closed or len(pts) < 3:
                    continue
                pb = bbox_of(pts)
                pw, pd = pb[2] - pb[0], pb[3] - pb[1]
                if not (LEAF_T_MIN <= min(pw, pd) <= LEAF_T_MAX):
                    continue
                if max(pw, pd) < 300 or max(pw, pd) > run * 0.9:
                    continue
                panels.append([round(pb[0] - xo), round(pb[1] - yo),
                               round(pb[2] - xo), round(pb[3] - yo)])

        _collect_panels(e)
        units.append({'bbox': wbb, 'panels': panels, 'block': e.dxf.name})
    return units


def extract_sashes(doc, xo, yo):
    """インナーサッシレイヤーのINSERT → 窓ユニット位置。
    幅 HAKI_MIN_W 以上は掃き出し窓、未満は腰高窓として扱う"""
    sashes = []
    for e in iter_world(doc):
        if e.dxftype() != 'INSERT':
            continue
        if not _is_sash_layer(getattr(e.dxf, 'layer', '')):
            continue
        wb = _insert_world_bbox(e)   # 家具と同じ厳密ワールドbbox
        if not wb:
            continue
        wbb = [round(wb[0] - xo), round(wb[1] - yo),
               round(wb[2] - xo), round(wb[3] - yo)]
        w = max(wbb[2] - wbb[0], wbb[3] - wbb[1])
        if w < 300:
            continue
        sashes.append({'bbox': wbb, 'w': w,
                       'kind': 'hakidashi' if w >= HAKI_MIN_W else 'hikichigai',
                       'block': e.dxf.name})
    return sashes


def extract_insulation(doc, xo, yo):
    """断熱レイヤーの SOLID / 閉ポリライン → フットプリント（そのまま立ち上げ）"""
    items = []
    for e in iter_world(doc, expand_parts=True):
        if not _is_insul_layer(getattr(e.dxf, 'layer', '')):
            continue
        t = e.dxftype()
        if t == 'SOLID':
            # SOLIDの頂点順は 0-1-3-2 で四角形になる（DXF仕様）
            vs_ = [e.dxf.vtx0, e.dxf.vtx1, e.dxf.vtx3, e.dxf.vtx2]
            pts = []
            for v in vs_:
                p = (round(v.x - xo), round(v.y - yo))
                if not pts or pts[-1] != p:
                    pts.append(p)
            if len(pts) >= 3:
                items.append({'pts': pts, 'bbox': bbox_of(pts)})
        elif t in ('POLYLINE', 'LWPOLYLINE'):
            pts, closed = get_pts(e)
            if closed and len(pts) >= 3:
                g = [(round(x - xo), round(y - yo)) for x, y in pts]
                items.append({'pts': g, 'bbox': bbox_of(g)})
    return items


def extract_furniture_extra(doc, xo, yo, envelopes):
    """家具レイヤー外（一般等）の家具フットプリントを拾う。
    ベッド・デスク等が一般レイヤーの閉ポリで描かれる図面への対応。
    誤検出防止のため建物外形が特定できた図面のみ・外形内のみ・家具サイズ帯のみ"""
    if not envelopes:
        return []
    items = []
    for e in iter_world(doc):
        if getattr(e.dxf, 'layer', '') not in FURN_EXTRA_LAYERS:
            continue
        if e.dxftype() not in ('POLYLINE', 'LWPOLYLINE'):
            continue
        pts, closed = get_pts(e)
        if not closed or len(pts) < 3:
            continue
        g = [(x - xo, y - yo) for x, y in pts]
        bb = bbox_of(g)
        w = round(bb[2] - bb[0])
        d = round(bb[3] - bb[1])
        if not (300 <= min(w, d) <= 2300 and max(w, d) <= 3200):
            continue
        if not any(_ix_ratio(bb, env) > 0.5 for env in envelopes):
            continue
        items.append({'kind': 'foot', 'name': None, '_bb': bb,
                      'x': round((bb[0] + bb[2]) / 2),
                      'y': round((bb[1] + bb[3]) / 2),
                      'angle': 0, 'w': w, 'd': d})
    # 入れ子・重ね描きの重複除去（ベッド外形+マットレス+布団の多重輪郭対策）
    # 面積の大きい順に採用し、既採用と5割超重なるものは捨てる
    items.sort(key=lambda f: -(f['w'] * f['d']))
    kept = []
    for f in items:
        if not any(_ix_ratio(f['_bb'], k['_bb']) > 0.5
                   or _ix_ratio(k['_bb'], f['_bb']) > 0.5 for k in kept):
            kept.append(f)
    for f in kept:
        del f['_bb']
    return kept


def _linetype_of(doc, e):
    """エンティティの実効線種名（ByLayerはレイヤーテーブルを参照）を大文字で返す"""
    lt = getattr(e.dxf, 'linetype', '') or ''
    if lt.upper() in ('', 'BYLAYER'):
        try:
            lt = doc.layers.get(e.dxf.layer).dxf.linetype or ''
        except Exception:
            lt = ''
    return lt.upper()


def extract_ceiling_beams(doc, xo, yo, ch_positions, wall_bbs=None):
    """「天井」レイヤーの線と、点線（DASHED等）で描かれた線 = 梁ライン。
    平行2線（間隔80〜800）は梁エッジのペアとして1本の梁に、単線は幅CEIL_BEAM_Wに展開。
    梁下端 = 最寄りの CH≒ 注記値（半径CH_TEXT_SEARCH_R内・天井高未満のもの）。
    注記が見つからなければ既定の梁せい BEAM_D で天井から下げる（bottom=None）"""
    hsegs, vsegs = [], []

    def add_seg(x1, y1, x2, y2):
        if abs(y1 - y2) <= PART_AXIS_TOL and abs(x1 - x2) >= 400:
            hsegs.append(((y1 + y2) / 2.0, min(x1, x2), max(x1, x2)))
        elif abs(x1 - x2) <= PART_AXIS_TOL and abs(y1 - y2) >= 400:
            vsegs.append(((x1 + x2) / 2.0, min(y1, y2), max(y1, y2)))

    dash_words = DASH_WORDS
    for e in iter_world(doc):
        if e.dxftype() != 'LINE':
            continue
        lay = getattr(e.dxf, 'layer', '')
        is_ceil = _is_ceil_layer(lay)
        is_dash = (lay in DASH_BEAM_LAYERS
                   and any(wd in _linetype_of(doc, e) for wd in DASH_WORDS_BEAM))
        if not (is_ceil or is_dash):
            continue
        s, en = e.dxf.start, e.dxf.end
        add_seg(s.x - xo, s.y - yo, en.x - xo, en.y - yo)

    beams = []

    def add_beam(x1, y1, x2, y2):
        lo_x, hi_x = min(x1, x2), max(x1, x2)
        lo_y, hi_y = min(y1, y2), max(y1, y2)
        bottom, bd = None, CH_TEXT_SEARCH_R
        for v, tx, ty in ch_positions:
            # 梁矩形への最短距離（中心距離だと長い梁で注記を拾い漏れる）
            ddx = max(lo_x - tx, 0, tx - hi_x)
            ddy = max(lo_y - ty, 0, ty - hi_y)
            dist = (ddx * ddx + ddy * ddy) ** 0.5
            if dist < bd:
                bd, bottom = dist, v
        beams.append({'x1': round(lo_x), 'y1': round(lo_y),
                      'x2': round(hi_x), 'y2': round(hi_y), 'bottom': bottom})

    for segs, horiz in ((hsegs, True), (vsegs, False)):
        paired_idx = set()
        segs_sorted = sorted(segs)
        for i, (p1, a1, a2) in enumerate(segs_sorted):
            if i in paired_idx:
                continue
            for j in range(i + 1, len(segs_sorted)):
                if j in paired_idx:
                    continue
                p2, b1, b2 = segs_sorted[j]
                gap = p2 - p1
                if gap > CEIL_PAIR_MAX:
                    break
                if gap < CEIL_PAIR_MIN:
                    continue
                if min(a2, b2) - max(a1, b1) < 400:
                    continue
                paired_idx.add(i)
                paired_idx.add(j)
                lo, hi = max(a1, b1), min(a2, b2)
                if horiz:
                    add_beam(lo, p1, hi, p2)
                else:
                    add_beam(p1, lo, p2, hi)
                break
        for i, (p1, a1, a2) in enumerate(segs_sorted):
            if i in paired_idx:
                continue
            # 単線: 梁幅の記載は無く「壁から梁ラインまでが梁の幅」（ユーザー確認済み）。
            # 最寄りの平行な壁面（1200mm以内・投影5割以上重なる）から線までを梁にする
            edge = None
            best_d = 1200
            for wb in (wall_bbs or []):
                if horiz:
                    ovl = min(a2, wb[2]) - max(a1, wb[0])
                    cands = (wb[1], wb[3])
                else:
                    ovl = min(a2, wb[3]) - max(a1, wb[1])
                    cands = (wb[0], wb[2])
                if ovl < (a2 - a1) * 0.5:
                    continue
                for c in cands:
                    d = abs(c - p1)
                    if 50 <= d < best_d:
                        best_d, edge = d, c
            if edge is not None:
                lo, hi = min(p1, edge), max(p1, edge)
                if horiz:
                    add_beam(a1, lo, a2, hi)
                else:
                    add_beam(lo, a1, hi, a2)
            elif horiz:   # 壁が見つからない → 既定幅で展開
                add_beam(a1, p1 - CEIL_BEAM_W / 2, a2, p1 + CEIL_BEAM_W / 2)
            else:
                add_beam(p1 - CEIL_BEAM_W / 2, a1, p1 + CEIL_BEAM_W / 2, a2)
    return beams


def self_check(doc, xo, yo, model_polys, model_rects):
    """生成ジオメトリとDXF図面のセルフチェック（突き合わせ）。
    図面の壁系レイヤーの線・ポリライン辺を50mmグリッドセルに落とし、
    生成した壁・窓・建具の輪郭セルと比較する。
    返り値: {'recall': 図面線の再現率, 'precision': 生成側の適合率,
             'offset': 系統ズレ[dx,dy]mm or None, 'warnings': [...]}"""
    GRID = 50

    def seg_cells(x1, y1, x2, y2, acc):
        n = max(1, int(max(abs(x2 - x1), abs(y2 - y1)) // GRID))
        for i in range(n + 1):
            t = i / n
            acc.add((int((x1 + (x2 - x1) * t) // GRID),
                     int((y1 + (y2 - y1) * t) // GRID)))

    # 図面側（正解）: 壁系レイヤーの閉ポリライン辺のみ。
    # 単線は見切り線・段差マーク・芯線であり壁ではない（ユーザー確認済み）
    ref = set()
    for e in iter_world(doc):
        if not _is_wall_layer(getattr(e.dxf, 'layer', '')):
            continue
        t = e.dxftype()
        if t in ('LINE', 'POLYLINE', 'LWPOLYLINE') and \
                any(wd in _linetype_of(doc, e) for wd in DASH_WORDS):
            continue   # 点線=梁の表記。壁ではないので正解側から除外
        if t in ('POLYLINE', 'LWPOLYLINE'):
            pts, closed = get_pts(e)
            if len(pts) < 2:
                continue
            g = [(x - xo, y - yo) for x, y in pts]
            rng = len(g) if closed and len(g) >= 3 else len(g) - 1
            for k in range(rng):
                p, q = g[k], g[(k + 1) % len(g)]
                seg_cells(p[0], p[1], q[0], q[1], ref)

    # 生成側: 壁ポリ実頂点 + 壁矩形（外周帯・内壁・建具枠・窓帯）の輪郭
    model = set()
    for pts in model_polys:
        for k in range(len(pts)):
            p, q = pts[k], pts[(k + 1) % len(pts)]
            seg_cells(p[0], p[1], q[0], q[1], model)
    for r in model_rects:
        x1, y1, x2, y2 = r[:4]
        seg_cells(x1, y1, x2, y1, model)
        seg_cells(x2, y1, x2, y2, model)
        seg_cells(x2, y2, x1, y2, model)
        seg_cells(x1, y2, x1, y1, model)

    if not ref or not model:
        return {'recall': None, 'precision': None, 'offset': None,
                'warnings': ['セルフチェック不能（図面または生成物の壁が空）']}

    def near(c, s):
        ci, cj = c
        for di in (-1, 0, 1):
            for dj in (-1, 0, 1):
                if (ci + di, cj + dj) in s:
                    return True
        return False

    recall = sum(1 for c in ref if near(c, model)) / len(ref)
    precision = sum(1 for c in model if near(c, ref)) / len(model)

    # 系統オフセット走査（±400mm・100mm刻み）: 全体を平行移動した方が一致率が
    # 大きく上がる場合は座標変換のバグか原点不一致 → 警告
    base = len(model & ref)
    best, best_score = (0, 0), base
    for dx in range(-8, 9, 2):
        for dy in range(-8, 9, 2):
            if dx == 0 and dy == 0:
                continue
            shifted = {(ci + dx, cj + dy) for ci, cj in model}
            score = len(shifted & ref)
            if score > best_score:
                best, best_score = (dx, dy), score
    offset = None
    if best != (0, 0) and best_score > base * 1.2 and best_score > len(model) * 0.3:
        offset = [best[0] * GRID, best[1] * GRID]

    warnings = []
    if recall < 0.7:
        warnings.append(f'図面の壁線の再現率が低い（{recall:.0%}）— 未生成の壁がある可能性')
    if precision < 0.5:
        warnings.append(f'生成側の適合率が低い（{precision:.0%}）— 図面に無い壁を生成している可能性')
    if offset:
        warnings.append(f'系統ズレ検出 dx={offset[0]}mm dy={offset[1]}mm — 座標変換を確認')
    return {'recall': round(recall, 3), 'precision': round(precision, 3),
            'offset': offset, 'warnings': warnings}


def _geom_cells(model_polys, model_rects, grid=50):
    """生成ジオメトリ（壁）の輪郭を占有セル集合に落とす"""
    cells = set()

    def seg(x1, y1, x2, y2):
        n = max(1, int(max(abs(x2 - x1), abs(y2 - y1)) // grid))
        for i in range(n + 1):
            t = i / n
            cells.add((int((x1 + (x2 - x1) * t) // grid),
                       int((y1 + (y2 - y1) * t) // grid)))

    for pts in model_polys:
        for k in range(len(pts)):
            p, q = pts[k], pts[(k + 1) % len(pts)]
            seg(p[0], p[1], q[0], q[1])
    for r in model_rects:
        x1, y1, x2, y2 = r[:4]
        seg(x1, y1, x2, y1)
        seg(x2, y1, x2, y2)
        seg(x2, y2, x1, y2)
        seg(x1, y2, x1, y1)
    return cells


def _fill_box_cells(cells, bb, grid=50):
    """矩形範囲を塗りつぶしてセル集合に追加（開口の栓・境界用）"""
    for ci in range(int(bb[0] // grid), int(bb[2] // grid) + 1):
        for cj in range(int(bb[1] // grid), int(bb[3] // grid) + 1):
            cells.add((ci, cj))


def _flood(seed, blocked, bound, grid=50, cap=60000):
    """壁セルを境界に4近傍フラッドフィル。cap超過（囲われていない）は None"""
    from collections import deque
    s = (int(seed[0] // grid), int(seed[1] // grid))
    if s in blocked:   # ラベルが壁の上に載っている場合は近くの空きセルへ
        found = None
        for r in range(1, 5):
            for di in range(-r, r + 1):
                for dj in range(-r, r + 1):
                    c = (s[0] + di, s[1] + dj)
                    if c not in blocked:
                        found = c
                        break
                if found:
                    break
            if found:
                break
        if not found:
            return None
        s = found
    b1, b2 = (int(bound[0] // grid) - 1, int(bound[1] // grid) - 1), \
             (int(bound[2] // grid) + 1, int(bound[3] // grid) + 1)
    seen = {s}
    q = deque([s])
    while q:
        ci, cj = q.popleft()
        for di, dj in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            c = (ci + di, cj + dj)
            if c in seen or c in blocked:
                continue
            if not (b1[0] <= c[0] <= b2[0] and b1[1] <= c[1] <= b2[1]):
                continue
            seen.add(c)
            if len(seen) > cap:
                return None
            q.append(c)
    return seen


def _runs_to_rects(cells, grid=50):
    """セル集合を行ラン→縦結合で矩形リスト（mm）にする"""
    rows = {}
    for ci, cj in cells:
        rows.setdefault(cj, []).append(ci)
    if not rows:
        return []
    rects = []
    open_runs = {}
    for cj in range(min(rows), max(rows) + 2):
        cur = set()
        run = None
        for ci in sorted(rows.get(cj, [])):
            if run and ci == run[1] + 1:
                run[1] = ci
            else:
                if run:
                    cur.add((run[0], run[1]))
                run = [ci, ci]
        if run:
            cur.add((run[0], run[1]))
        for key in list(open_runs):
            if key not in cur:
                j0 = open_runs.pop(key)
                rects.append([key[0] * grid, j0 * grid,
                              (key[1] + 1) * grid, cj * grid])
        for key in cur:
            open_runs.setdefault(key, cj)
    return rects


def _region_perimeter_rects(cells, envelopes, grid=50):
    """領域の外周辺のうち建物側でない辺を、厚みPARAPET_Tの手すり壁矩形にする"""
    edges = {'N': {}, 'S': {}, 'W': {}, 'E': {}}
    for ci, cj in cells:
        if (ci, cj + 1) not in cells:
            edges['N'].setdefault(cj, []).append(ci)
        if (ci, cj - 1) not in cells:
            edges['S'].setdefault(cj, []).append(ci)
        if (ci - 1, cj) not in cells:
            edges['W'].setdefault(ci, []).append(cj)
        if (ci + 1, cj) not in cells:
            edges['E'].setdefault(ci, []).append(cj)

    def near_env(x, y):
        return any(ev[0] - 200 <= x <= ev[2] + 200
                   and ev[1] - 200 <= y <= ev[3] + 200 for ev in envelopes)

    rects = []
    for side in ('N', 'S', 'W', 'E'):
        for pos, arr in edges[side].items():
            run = None
            for v in sorted(arr) + [10 ** 9]:
                if run and v == run[1] + 1:
                    run[1] = v
                    continue
                if run:
                    a1, a2 = run[0] * grid, (run[1] + 1) * grid
                    if a2 - a1 >= 300:
                        if side == 'N':
                            r = [a1, (pos + 1) * grid - PARAPET_T,
                                 a2, (pos + 1) * grid]
                        elif side == 'S':
                            r = [a1, pos * grid, a2, pos * grid + PARAPET_T]
                        elif side == 'W':
                            r = [pos * grid, a1, pos * grid + PARAPET_T, a2]
                        else:
                            r = [(pos + 1) * grid - PARAPET_T, a1,
                                 (pos + 1) * grid, a2]
                        cx, cy = (r[0] + r[2]) / 2, (r[1] + r[3]) / 2
                        if not near_env(cx, cy):   # 建物側の辺には手すりを立てない
                            rects.append([round(v2) for v2 in r])
                run = [v, v]
    return rects


def _snap_band(bb, wall_bbs):
    """サッシ・建具の帯を所属壁に吸着させる（中心と厚みを壁に正規化）。
    ブロックbboxが額縁などで壁からずれていても、窓・建具が壁の中に納まる"""
    horiz = (bb[2] - bb[0]) >= (bb[3] - bb[1])
    best, best_gap = None, 300
    for wb in wall_bbs:
        wt = min(wb[2] - wb[0], wb[3] - wb[1])
        if wt > 500 or wt < 40:
            continue
        if ((wb[2] - wb[0]) >= (wb[3] - wb[1])) != horiz:
            continue   # 走り方向が違う壁
        if horiz:
            ovl = min(bb[2], wb[2]) - max(bb[0], wb[0])
            gap = abs((bb[1] + bb[3]) / 2 - (wb[1] + wb[3]) / 2)
        else:
            ovl = min(bb[3], wb[3]) - max(bb[1], wb[1])
            gap = abs((bb[0] + bb[2]) / 2 - (wb[0] + wb[2]) / 2)
        if ovl >= 300 and gap < best_gap:
            best, best_gap = wb, gap
    if best is None:
        return bb
    if horiz:
        return [bb[0], best[1], bb[2], best[3]]
    return [best[0], bb[1], best[2], bb[3]]


def scale_check(doc):
    """*D寸法ブロックの記載値/実測スパン比の中央値（単位ミス検出用）。
    1.0付近が正常。10≒cm図面、25.4≒inch。判定不能は None"""
    ratios = []
    for blk in doc.blocks:
        if not blk.name.startswith('*D'):
            continue
        lines = [e for e in blk if e.dxftype() == 'LINE']
        mtexts = [e for e in blk if e.dxftype() == 'MTEXT']
        if not lines:
            continue
        xs = [l.dxf.start.x for l in lines] + [l.dxf.end.x for l in lines]
        ys = [l.dxf.start.y for l in lines] + [l.dxf.end.y for l in lines]
        span = max(max(xs) - min(xs), max(ys) - min(ys))
        if span < 50:   # 図面単位がcm/inchでも検定できるよう閾値は小さく
            continue
        for m in mtexts:
            v = parse_dim_text(m.text)
            if v and v > 100:
                ratios.append(v / span)
                break
    if len(ratios) < 3:
        return None
    ratios.sort()
    return ratios[len(ratios) // 2]


def heal_walls(doc, xo, yo, model_polys, model_rects, open_boxes, envelopes):
    """セルフチェック連動の自己修復: 図面の壁線のうち生成物に覆われていない
    軸整列セグメントを、厚みHEAL_Tの補完壁として返す。
    「図面にある壁がモデルに無い」を機械的に潰す最後の網"""
    GRID = 50

    def seg_cells(x1, y1, x2, y2, acc):
        n = max(1, int(max(abs(x2 - x1), abs(y2 - y1)) // GRID))
        for i in range(n + 1):
            t = i / n
            acc.add((int((x1 + (x2 - x1) * t) // GRID),
                     int((y1 + (y2 - y1) * t) // GRID)))

    model = set()
    for pts in model_polys:
        for k in range(len(pts)):
            p, q = pts[k], pts[(k + 1) % len(pts)]
            seg_cells(p[0], p[1], q[0], q[1], model)
    for r in model_rects:
        x1, y1, x2, y2 = r[:4]
        seg_cells(x1, y1, x2, y1, model)
        seg_cells(x2, y1, x2, y2, model)
        seg_cells(x2, y2, x1, y2, model)
        seg_cells(x1, y2, x1, y1, model)
    grown = set()
    for ci, cj in model:
        for di in (-1, 0, 1):
            for dj in (-1, 0, 1):
                grown.add((ci + di, cj + dj))

    # 図面の壁線セグメント収集。
    # 閉ポリライン（壁輪郭）の辺のみを対象にする —— 単線は見切り線・段差マーク・
    # 芯線の可能性があり、壁として補完してはいけない（ユーザー確認済み）
    raw = []
    for e in iter_world(doc):
        if not _is_wall_layer(getattr(e.dxf, 'layer', '')):
            continue
        if e.dxftype() not in ('POLYLINE', 'LWPOLYLINE'):
            continue
        if any(wd in _linetype_of(doc, e) for wd in DASH_WORDS):
            continue
        pts, closed = get_pts(e)
        if not closed or len(pts) < 3:
            continue
        g = [(x - xo, y - yo) for x, y in pts]
        for k in range(len(g)):
            raw.append((g[k][0], g[k][1],
                        g[(k + 1) % len(g)][0], g[(k + 1) % len(g)][1]))

    heal = []
    for i, (x1, y1, x2, y2) in enumerate(raw):
        if max(abs(x2 - x1), abs(y2 - y1)) < 400:
            continue
        if min(abs(x2 - x1), abs(y2 - y1)) > 10:
            continue   # 斜め線はv1対象外
        cells = set()
        seg_cells(x1, y1, x2, y2, cells)
        cover = sum(1 for c in cells if c in grown) / len(cells)
        if cover >= HEAL_COVER:
            continue
        if abs(y2 - y1) <= 10:   # 横線（ラインのまま＝縮退矩形）
            band = [round(min(x1, x2)), round(y1),
                    round(max(x1, x2)), round(y1)]
        else:                    # 縦線
            band = [round(x1), round(min(y1, y2)),
                    round(x1), round(max(y1, y2))]

        def _seg_in_box(bd, ob):
            return (min(bd[2], ob[2]) - max(bd[0], ob[0]) > -1
                    and min(bd[3], ob[3]) - max(bd[1], ob[1]) > -1
                    and min(bd[2], ob[2]) - max(bd[0], ob[0])
                    + min(bd[3], ob[3]) - max(bd[1], ob[1]) > 100)
        if any(_seg_in_box(band, ob) for ob in open_boxes):
            continue   # サッシ・建具の開口部は塞がない
        if envelopes and not any(
                _ix_ratio(band, [ev[0] - 50, ev[1] - 50, ev[2] + 50, ev[3] + 50]) > 0.3
                for ev in envelopes):
            continue   # 建物外
        heal.append(band)

    # 補完壁同士の重複除去
    heal.sort(key=lambda r: -((r[2] - r[0]) * (r[3] - r[1])))
    kept = []
    for r in heal:
        if not any(_ix_ratio(r, k) > 0.6 for k in kept):
            kept.append(r)
        if len(kept) >= 200:
            break
    return kept


def _cut_band_for_windows(bb, sash_list, expand=200):
    """帯bbox（壁・断熱）をサッシ窓の位置で分割する。
    返り値: (窓なし区間の矩形リスト, [(サッシidx, 窓区間の矩形), ...])"""
    horiz = (bb[2] - bb[0]) >= (bb[3] - bb[1])
    lo, hi = (bb[0], bb[2]) if horiz else (bb[1], bb[3])
    cross = ((bb[1] + bb[3]) / 2) if horiz else ((bb[0] + bb[2]) / 2)
    ivs = []
    for idx, s in enumerate(sash_list):
        sb = s['bbox']
        if horiz:
            if not (sb[1] - expand <= cross <= sb[3] + expand):
                continue
            a1, a2 = max(lo, sb[0]), min(hi, sb[2])
        else:
            if not (sb[0] - expand <= cross <= sb[2] + expand):
                continue
            a1, a2 = max(lo, sb[1]), min(hi, sb[3])
        if a2 - a1 > 100:
            ivs.append((a1, a2, idx))
    if not ivs:
        return [list(bb)], []
    ivs.sort()
    full, wins_ = [], []
    pos = lo
    for a1, a2, idx in ivs:
        a1 = max(a1, pos)
        if a1 - pos >= 50:
            full.append([round(pos), bb[1], round(a1), bb[3]] if horiz
                        else [bb[0], round(pos), bb[2], round(a1)])
        if a2 > a1:
            wins_.append((idx, [round(a1), bb[1], round(a2), bb[3]] if horiz
                          else [bb[0], round(a1), bb[2], round(a2)]))
        pos = max(pos, a2)
    if hi - pos >= 50:
        full.append([round(pos), bb[1], round(hi), bb[3]] if horiz
                    else [bb[0], round(pos), bb[2], round(hi)])
    return full, wins_


def _cut_strip(rect_, boxes):
    """壁帯矩形を、重なる開口bbox（サッシ・建具）位置で長手方向に分割して残片を返す"""
    x1, y1, x2, y2 = rect_
    horiz = (x2 - x1) >= (y2 - y1)
    lo, hi = (x1, x2) if horiz else (y1, y2)
    ivs = []
    for b in boxes:
        if b[0] < x2 and b[2] > x1 and b[1] < y2 and b[3] > y1:
            iv = (max(lo, b[0] if horiz else b[1]),
                  min(hi, b[2] if horiz else b[3]))
            if iv[1] > iv[0]:
                ivs.append(iv)
    if not ivs:
        return [rect_]
    ivs.sort()
    merged = [list(ivs[0])]
    for a, b in ivs[1:]:
        if a <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    out, pos = [], lo
    for a, b in merged:
        if a - pos >= 50:
            out.append([round(pos), y1, round(a), y2] if horiz
                       else [x1, round(pos), x2, round(a)])
        pos = b
    if hi - pos >= 50:
        out.append([round(pos), y1, round(hi), y2] if horiz
                   else [x1, round(pos), x2, round(hi)])
    return out


def _ix_ratio(r, bb):
    """矩形rのうちbbと重なる面積比（0〜1）"""
    ix = min(r[2], bb[2]) - max(r[0], bb[0])
    iy = min(r[3], bb[3]) - max(r[1], bb[1])
    if ix <= 0 or iy <= 0:
        return 0.0
    area = (r[2] - r[0]) * (r[3] - r[1])
    return (ix * iy) / area if area > 0 else 0.0


def cut_door_openings(rect, doors):
    """内壁矩形をドア位置で分割する。
    返り値: (segments, openings)  いずれも壁長手方向の区間 (a1, a2) リスト。
    ドアの吊元と戸先（弦の端点のうち壁帯に載っている方）の間を開口とする。"""
    x1, y1, x2, y2 = rect
    horiz = (x2 - x1) >= (y2 - y1)
    lo, hi = (x1, x2) if horiz else (y1, y2)
    # 許容差±30: T字接合部で隣の壁のドアを誤って拾わないよう狭くとる
    band = (y1 - 30, y2 + 30) if horiz else (x1 - 30, x2 + 30)
    cuts = []
    for d in doors:
        hx, hy = d['hinge']
        h_axis, h_perp = (hx, hy) if horiz else (hy, hx)
        if not (band[0] <= h_perp <= band[1] and lo - 30 <= h_axis <= hi + 30):
            continue
        for p in (d['p1'], d['p2']):
            p_axis, p_perp = (p[0], p[1]) if horiz else (p[1], p[0])
            if band[0] <= p_perp <= band[1] and abs(p_axis - h_axis) > 100 \
                    and lo - 30 <= p_axis <= hi + 30:
                cuts.append((min(h_axis, p_axis), max(h_axis, p_axis)))
                break
    if not cuts:
        return [(lo, hi)], []
    # 重複区間マージ → 壁を区間分割
    cuts = sorted(cuts)
    merged = [list(cuts[0])]
    for c1, c2 in cuts[1:]:
        if c1 <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], c2)
        else:
            merged.append([c1, c2])
    segments, openings, pos = [], [], lo
    for c1, c2 in merged:
        c1, c2 = max(c1, lo), min(c2, hi)
        if c2 <= c1:
            continue
        if c1 - pos >= 50:
            segments.append((round(pos), round(c1)))
        openings.append((round(c1), round(c2)))
        pos = c2
    if hi - pos >= 50:
        segments.append((round(pos), round(hi)))
    return segments, openings


def extract_south_north_windows(doc, xo, yo):
    """建具レイヤーの横線（幅レンジ内）→ 南北の窓開口"""
    arc_bbox, lines = [], []
    for e in iter_world(doc, expand_parts=True):
        if not _is_tategu_layer(getattr(e.dxf, 'layer', '')):
            continue
        if e.dxftype() == 'ARC':
            c, r = e.dxf.center, e.dxf.radius
            arc_bbox.append([c.x - r - xo, c.y - r - yo,
                             c.x + r - xo, c.y + r - yo])
        elif e.dxftype() == 'LINE':
            s, en = e.dxf.start, e.dxf.end
            lines.append((round(s.x - xo), round(s.y - yo),
                          round(en.x - xo), round(en.y - yo)))
    return arc_bbox, lines


def extract_blue_vlines(doc, xo, yo):
    """建具レイヤーの青色(縦線) → 東面 FIX 窓位置"""
    out = []
    for e in iter_world(doc, expand_parts=True):
        if e.dxftype() != 'LINE':
            continue
        if not _is_tategu_layer(getattr(e.dxf, 'layer', '')):
            continue
        if e.dxf.color != BLUE_COLOR:
            continue
        s, en = e.dxf.start, e.dxf.end
        x1, y1 = round(s.x - xo), round(s.y - yo)
        x2, y2 = round(en.x - xo), round(en.y - yo)
        if abs(x1 - x2) > 10:      # 縦線のみ
            continue
        out.append({'gx': (x1 + x2) // 2, 'y1': min(y1, y2), 'y2': max(y1, y2)})
    return out


def extract_beams(doc, xo, yo):
    """梁レイヤーのLINE/LWPOLYLINE → 梁フットプリントリスト"""
    beams = []
    for e in iter_world(doc, expand_parts=True):
        if not _is_beam_layer(getattr(e.dxf, 'layer', '')):
            continue
        if e.dxftype() == 'LINE':
            s, en = e.dxf.start, e.dxf.end
            x1 = round(s.x - xo); y1 = round(s.y - yo)
            x2 = round(en.x - xo); y2 = round(en.y - yo)
            if abs(x2 - x1) < 50 and abs(y2 - y1) < 50:
                continue
            # 中心線 → 梁幅分だけ垂直方向へ展開
            if abs(x2 - x1) >= abs(y2 - y1):  # 横梁
                yc = (y1 + y2) // 2
                beams.append({'x1': min(x1, x2), 'y1': yc - BEAM_W // 2,
                              'x2': max(x1, x2), 'y2': yc + BEAM_W // 2,
                              'label': '横梁'})
            else:  # 縦梁
                xc = (x1 + x2) // 2
                beams.append({'x1': xc - BEAM_W // 2, 'y1': min(y1, y2),
                              'x2': xc + BEAM_W // 2, 'y2': max(y1, y2),
                              'label': '縦梁'})
        elif e.dxftype() in ('LWPOLYLINE', 'POLYLINE'):
            pts, _ = get_pts(e)
            if len(pts) < 3:
                continue
            g = [(round(x - xo), round(y - yo)) for x, y in pts]
            bb = bbox_of(g)
            if min(bb[2] - bb[0], bb[3] - bb[1]) < 50:
                continue
            beams.append({'x1': bb[0], 'y1': bb[1], 'x2': bb[2], 'y2': bb[3],
                          'label': '梁(bbox)'})
    return beams


def _block_extent(doc, name, depth=0, cache=None):
    """ブロック内容のbbox（ローカル座標）。ネストINSERTは6段まで追跡
    （VWの入れ子グループはベッド等で6階層になる実例がある）"""
    if cache is None:
        cache = {}
    if name in cache:
        return cache[name]
    cache[name] = None   # 循環参照ガード
    if depth > 6 or name not in doc.blocks:
        cache.pop(name, None)   # 深さ超過は恒久キャッシュしない（浅い経路で再評価できるように）
        return None
    import math
    xs, ys = [], []
    for e in doc.blocks[name]:
        t = e.dxftype()
        if t == 'LINE':
            s, en = e.dxf.start, e.dxf.end
            xs += [s.x, en.x]
            ys += [s.y, en.y]
        elif t in ('POLYLINE', 'LWPOLYLINE'):
            pts, _ = get_pts(e)
            xs += [p[0] for p in pts]
            ys += [p[1] for p in pts]
        elif t in ('CIRCLE', 'ARC'):
            c, r = e.dxf.center, e.dxf.radius
            xs += [c.x - r, c.x + r]
            ys += [c.y - r, c.y + r]
        elif t == 'INSERT':
            sub = _block_extent(doc, e.dxf.name, depth + 1, cache)
            if sub:
                rot = math.radians(getattr(e.dxf, 'rotation', 0))
                sx = getattr(e.dxf, 'xscale', 1) or 1
                sy = getattr(e.dxf, 'yscale', 1) or 1
                for px, py in ((sub[0], sub[1]), (sub[0], sub[3]),
                               (sub[2], sub[1]), (sub[2], sub[3])):
                    px, py = px * sx, py * sy
                    xs.append(e.dxf.insert.x + px * math.cos(rot) - py * math.sin(rot))
                    ys.append(e.dxf.insert.y + px * math.sin(rot) + py * math.cos(rot))
    if not xs:
        return None
    bb = (min(xs), min(ys), max(xs), max(ys))
    cache[name] = bb
    return bb


def _block_footprint(doc, name):
    """ブロック内の最大閉ポリのbbox（ローカル）。注記・引出線でextentが膨らむのを防ぐ。
    面積がextentの5割未満なら None（フットプリントとみなさない）"""
    if name not in doc.blocks:
        return None
    best, best_area = None, 0
    for e in doc.blocks[name]:
        if e.dxftype() not in ('POLYLINE', 'LWPOLYLINE'):
            continue
        pts, closed = get_pts(e)
        if not closed or len(pts) < 3:
            continue
        bb = bbox_of(pts)
        area = (bb[2] - bb[0]) * (bb[3] - bb[1])
        if area > best_area:
            best, best_area = bb, area
    if best is None:
        return None
    ext = _block_extent(doc, name)
    if ext:
        ext_area = (ext[2] - ext[0]) * (ext[3] - ext[1])
        if ext_area > 0 and best_area < ext_area * 0.5:
            return None
    return best


def _insert_world_bbox(ins, depth=0):
    """INSERTサブツリーのワールドbboxを ezdxf.bbox.extents で厳密計算する。
    ・SPLINE/ELLIPSE/HATCH も含む（無視すると椅子等が丸ごと欠落していた）
    ・ARCは実円弧範囲（全円近似だと最大657mmの幻ズレ）
    ・ミラーOCS(extrusion=-1)も ezdxf 側で正しくWCS変換される
    寸法注記の混入を防ぐため TEXT系は除外する"""
    skip = ('TEXT', 'MTEXT', 'ATTDEF', 'ATTRIB', 'WIPEOUT', 'POINT', 'DIMENSION')
    prims = []

    def collect(e0, d):
        try:
            ents = list(e0.virtual_entities())
        except Exception:
            return
        for e in ents:
            t = e.dxftype()
            if t in skip:
                continue
            if t == 'INSERT':
                if d < 6:
                    collect(e, d + 1)
                continue
            prims.append(e)

    collect(ins, depth)
    if not prims:
        return None
    try:
        from ezdxf import bbox as _bbox
        ext = _bbox.extents(prims, fast=True)
        if not ext.has_data:
            return None
        return (ext.extmin.x, ext.extmin.y, ext.extmax.x, ext.extmax.y)
    except Exception:
        # フォールバック: 主要エンティティの手計算（旧方式・ARCは全円近似）
        xs, ys = [], []
        for e in prims:
            t = e.dxftype()
            if t == 'LINE':
                xs += [e.dxf.start.x, e.dxf.end.x]
                ys += [e.dxf.start.y, e.dxf.end.y]
            elif t in ('POLYLINE', 'LWPOLYLINE'):
                pts, _ = get_pts(e)
                xs += [p[0] for p in pts]
                ys += [p[1] for p in pts]
            elif t in ('CIRCLE', 'ARC'):
                c, r = e.dxf.center, e.dxf.radius
                xs += [c.x - r, c.x + r]
                ys += [c.y - r, c.y + r]
        if not xs:
            return None
        return (min(xs), min(ys), max(xs), max(ys))


def extract_furniture(doc, xo, yo):
    """家具レイヤーから家具を抽出。
    ・INSERT       → ブロック名つき（名前マッチ用）+ ブロック実寸（寸法マッチ用）
    ・閉ポリライン  → フットプリント矩形（寸法マッチ用）
    返り値: [{'kind','name','x','y','angle','w','d'}, ...]
    x,y は家具のbbox中心（無名グループでも代替シンボル/ボックスが正位置に載る）"""
    import math
    items = []
    ext_cache = {}
    # セットコンテナ判定: 家具/設備INSERTを2個以上直接内包するブロックは
    # 「複数家具のグループ」なので自身は配置せず、中身を個別に照合する
    containers = set()
    for blk in doc.blocks:
        kids = sum(1 for be in blk
                   if be.dxftype() == 'INSERT'
                   and (_is_furniture_layer(getattr(be.dxf, 'layer', ''))
                        or _is_fixture_layer(getattr(be.dxf, 'layer', ''))))
        if kids >= 2:
            containers.add(blk.name)
    for e in iter_world(doc):
        layer = getattr(e.dxf, 'layer', '')
        t = e.dxftype()
        # 設備機器レイヤーはINSERTのみ拾う（線が大量にあるため輪郭は対象外）
        if not (_is_furniture_layer(layer)
                or (_is_fixture_layer(layer) and t == 'INSERT')):
            continue
        if t == 'INSERT' and e.dxf.name in containers:
            continue   # セットコンテナ自身は家具ではない（中身を個別配置）
        if t == 'INSERT':
            ins = e.dxf.insert
            rot = getattr(e.dxf, 'rotation', 0)
            x, y = ins.x - xo, ins.y - yo
            w = d = None
            # ワールドbboxを ezdxf に直接計算させる（ミラー・多段ネストでもズレない）
            wbb = _insert_world_bbox(e)
            if wbb:
                w = round(wbb[2] - wbb[0])
                d = round(wbb[3] - wbb[1])
                x = (wbb[0] + wbb[2]) / 2 - xo
                y = (wbb[1] + wbb[3]) / 2 - yo
                if min(w, d) < 100:      # 記号・照明マーク等はスキップ
                    continue
                if min(w, d) > 2300 or max(w, d) > 3200:
                    continue   # 家具サイズを超える（設備のコンテナブロック等）
            items.append({
                'kind': 'insert',
                'name': e.dxf.name,
                'x': round(x),
                'y': round(y),
                'angle': round(rot, 1),
                'w': w, 'd': d,
            })
        elif t in ('LWPOLYLINE', 'POLYLINE'):
            pts, _ = get_pts(e)
            if len(pts) < 3:
                continue
            g = [(x - xo, y - yo) for x, y in pts]
            bb = bbox_of(g)
            w = round(bb[2] - bb[0]); d = round(bb[3] - bb[1])
            if min(w, d) < 100:        # 細線・記号はスキップ
                continue
            items.append({
                'kind': 'foot', 'name': None,
                'x': round((bb[0] + bb[2]) / 2),
                'y': round((bb[1] + bb[3]) / 2),
                'angle': 0, 'w': w, 'd': d,
            })
    return items


# ─────────────────────────────────────────────
# 家具カタログ照合
# ─────────────────────────────────────────────
_CATALOG = None


def load_catalog():
    """furniture_catalog.json を読み込む（無ければ空リスト）"""
    global _CATALOG
    if _CATALOG is None:
        import json
        p = Path(__file__).parent / 'furniture_catalog.json'
        _CATALOG = json.loads(p.read_text(encoding='utf-8')) if p.exists() else []
    return _CATALOG


def _norm(s):
    """マッチ用に正規化（コード・区切り除去、小文字化）"""
    s = re.sub(r'\d{4,}', '', s)
    s = re.sub(r'[_\-\s\.]', '', s)
    return s.lower()


def match_by_name(name, catalog):
    """ブロック名 → カタログ最良一致（部分一致＋2-gram 重なり）"""
    q = _norm(name)
    if not q:
        return None, 0
    best, best_score = None, 0
    qg = set(q[i:i + 2] for i in range(len(q) - 1)) or {q}
    for it in catalog:
        cn = _norm(it['name'])
        if not cn:
            continue
        if cn == q or cn in q or q in cn:
            score = 100
        else:
            cg = set(cn[i:i + 2] for i in range(len(cn) - 1)) or {cn}
            score = len(qg & cg) * 100 // max(len(qg), len(cg))
        if score > best_score:
            best, best_score = it, score
    return (best, best_score) if best_score >= 40 else (None, best_score)


def _match_dims(it):
    """寸法照合に使う実効寸法。
    シェルフは命名規則寸法が正、それ以外はACIS実測（3Dソリッドbbox）を優先。
    2D図形由来の w/d はキッチン等で注記込みの過大値になるため最後の手段"""
    if it.get('dim_source') == 'shelf':
        return it['w'], it['d']
    if it.get('h_source') == 'acis' and it.get('w_geo') and it.get('d_geo'):
        return it['w_geo'], it['d_geo']
    return it['w'], it['d']


def match_by_size(w, d, catalog, prior_cats=None):
    """寸法 → カタログ最良一致（向き両対応・許容誤差 各辺平均±15%）。
    prior_cats があればまずそのカテゴリ内で照合し、無ければ全体から。
    合わない家具は無理にマッチさせず呼び出し側で簡易ボリュームにする。
    返り値: (hit, err, swapped)  swapped=True は縦横を入れ替えて一致
    （配置時にシンボルを+90度回転して図面の向きに合わせる）"""
    def _best_in(items):
        best, best_err, best_sw = None, 1e18, False
        for it in items:
            mw, md = _match_dims(it)
            if it.get('dim_source') == 'unknown' or not mw or not md:
                continue
            e1 = abs(mw - w) + abs(md - d)
            e2 = abs(mw - d) + abs(md - w)
            err = min(e1, e2)
            if err < best_err:
                best, best_err, best_sw = it, err, e2 < e1
        return best, best_err, best_sw

    if prior_cats:
        best, err, sw = _best_in([it for it in catalog
                                  if it['category'] in prior_cats])
        if best and err <= (w + d) * 0.3:
            return best, err, sw
    best, err, sw = _best_in(catalog)
    if best and err <= (w + d) * 0.3:
        return best, err, sw
    return None, err, False


def flat(pts):
    return ', '.join(f'{x}, {y}' for x, y in pts)


# ─────────────────────────────────────────────
# メイン生成
# ─────────────────────────────────────────────
def build_script(dxf_path, overrides=None):
    """DXF から VW Python スクリプト文字列を生成し (script, summary) を返す。
    overrides で CH/SILL/HEAD/WALL_LAYERS 等を上書きできる（Web UI 用）。"""
    globals()['CH'] = None   # Web常駐プロセスで前リクエストの検出値を持ち越さない
    if overrides:
        g = globals()
        for k, v in overrides.items():
            if k in g and v is not None:
                g[k] = v

    try:
        doc = ezdxf.readfile(str(dxf_path))        # ASCII/バイナリDXF両対応
    except Exception:
        doc, _ = recover.readfile(str(dxf_path))   # 壊れ気味のASCII DXFを救済

    # 天井高の解決: (a)ユーザー明示指定 > (b)図面注記 > (c)特定不能なら停止して確認
    ch_detected = None
    ch_values = []
    if CH is None:
        ch_detected, ch_values = detect_ceiling_heights(doc)
        if ch_detected:
            globals()['CH'] = ch_detected
    if CH is None:
        raise CeilingHeightRequired(
            '天井高を特定できません。図面に天井高注記（"天井高2400"・"CH≒2400"・"CH:3670"等）が'
            '見つからないため、CH を指定してください。')

    # 部屋ラベルのチェックリスト（基本6室）
    rooms_found, rooms_missing = detect_rooms(doc)

    xo, yo, gx_max, gy_max = detect_grid(doc)
    scale_ratio = scale_check(doc)   # 単位ミス検出（1.0付近が正常）
    polys = load_polys(doc, xo, yo)
    polys += load_hatch_polys(doc, xo, yo, polys)   # HATCH塗りだけの壁も復元

    # 外部階段・建物外 = グリッド東端より STAIR_GAP 以上東の塊を除外
    # （グリッド未検出 gx_max=0 の図面では全滅を防ぐためカットしない）
    stair_cut = (gx_max + STAIR_GAP) if gx_max > 0 else float('inf')
    polys = [p for p in polys if p['bbox'][0] < stair_cut]

    # 輪郭線（塗り率の高い大型閉ポリ=RC躯体の外形線等）を壁帯から分離し、
    # 同心ペアは外周4辺の壁帯に変換。ペア無しはそのまま押し出すと
    # 建物全体が中身の詰まったブロックになるため破棄（コメントで報告）
    polys, _outlines = split_outlines(polys)
    frames, frame_quads, outline_dropped, envelopes = outline_frames(_outlines)
    frames = [[round(v) for v in f] for f in frames]

    # 外形が特定できた図面では建物外の壁を出力しない
    # （バルコニー・外部廊下・隣住戸の輪郭線が壁として立ち上がり
    #   「モデルがズレて見える」原因になるのを防ぐ）
    if envelopes:
        def _in_building(bb):
            return any(_ix_ratio(bb, [env[0] - 50, env[1] - 50,
                                      env[2] + 50, env[3] + 50]) > 0.3
                       for env in envelopes)

        def _seg_in_building(x1, y1, x2, y2, margin=300):
            cx_, cy_ = (x1 + x2) / 2, (y1 + y2) / 2
            return any(env[0] - margin <= cx_ <= env[2] + margin
                       and env[1] - margin <= cy_ <= env[3] + margin
                       for env in envelopes)
        polys = [p for p in polys if _in_building(p['bbox'])]
        frames = [f for f in frames
                  if _seg_in_building(f[0], f[1], f[2], f[3])]
        frame_quads = [q for q in frame_quads
                       if _seg_in_building(q[0], q[1], q[2], q[3])]

    if not polys and not frames:
        raise ValueError('躯体ポリラインが見つかりません。WALL_LAYERS を確認してください。')

    _extent_boxes = [p['bbox'] for p in polys] + frames + \
        [[min(q[0], q[2]), min(q[1], q[3]), max(q[0], q[2]), max(q[1], q[3])]
         for q in frame_quads]
    fx1 = min(b[0] for b in _extent_boxes)
    fy1 = min(b[1] for b in _extent_boxes)
    fx2 = max(b[2] for b in _extent_boxes)
    fy2 = max(b[3] for b in _extent_boxes)

    def is_h(p):  # 横長
        bb = p['bbox']
        return (bb[2] - bb[0]) >= (bb[3] - bb[1])

    def is_v(p):  # 縦長
        return not is_h(p)

    # 外周面の判定はグリッド端（通り芯）基準でロバストに
    #   西面 = グリッド X 最小(0)付近の縦長外壁
    #   南面 = グリッド Y 最小(0)付近の横長最長壁
    #   北面 = グリッド Y 最大付近の横長最長壁
    west = [p for p in polys if is_v(p) and p['bbox'][2] <= EDGE_TOL
            and (p['bbox'][3] - p['bbox'][1]) > 2000
            and (p['bbox'][2] - p['bbox'][0]) < 500]   # 薄い縦外壁のみ
    south_cands = [p for p in polys if is_h(p) and p['bbox'][3] <= EDGE_TOL]
    north_cands = [p for p in polys if is_h(p) and p['bbox'][1] >= gy_max - EDGE_TOL]
    south_main = max(south_cands, key=lambda p: p['bbox'][2] - p['bbox'][0], default=None)
    north_main = max(north_cands, key=lambda p: p['bbox'][2] - p['bbox'][0], default=None)

    special = set(id(p) for p in west)
    if south_main:
        special.add(id(south_main))
    if north_main:
        special.add(id(north_main))
    walls = [p for p in polys if id(p) not in special]

    # 南北窓開口
    arc_bbox, lines = extract_south_north_windows(doc, xo, yo)

    def horiz_windows(face_y_test):
        ws = {}
        for x1, y1, x2, y2 in lines:
            if abs(y1 - y2) > 10:
                continue
            w = abs(x2 - x1)
            if not (WIN_W_MIN <= w <= WIN_W_MAX):
                continue
            cy, cx = (y1 + y2) // 2, (x1 + x2) // 2
            if not face_y_test(cy):
                continue
            if any(b[0] <= cx <= b[2] and b[1] <= cy <= b[3] for b in arc_bbox):
                continue
            ws[(min(x1, x2), max(x1, x2))] = {'x1': min(x1, x2), 'x2': max(x1, x2)}
        return sorted(ws.values(), key=lambda w: w['x1'])

    mid_y = gy_max / 2
    swins = horiz_windows(lambda cy: cy < mid_y) if south_main else []
    nwins = horiz_windows(lambda cy: cy >= mid_y) if north_main else []
    # 東面 FIX 窓 = グリッド X 東端付近の青縦線
    ewins = [w for w in extract_blue_vlines(doc, xo, yo)
             if w['gx'] >= gx_max - EDGE_TOL]
    beams = extract_beams(doc, xo, yo)

    # ── 内壁（間仕切り壁）: LINEペア + 薄壁閉ポリ ──
    hsegs, vsegs, thin_rects = _seg_pool(doc, xo, yo)
    part_rects = []
    for (p1, p2, a1, a2) in _pair_walls(hsegs):   # 水平壁: yがpos, xが長手
        part_rects.append([round(a1), round(p1), round(a2), round(p2)])
    for (p1, p2, a1, a2) in _pair_walls(vsegs):   # 垂直壁: xがpos, yが長手
        part_rects.append([round(p1), round(a1), round(p2), round(a2)])
    part_rects += thin_rects

    # 建物範囲外を除外（グリッド未検出の軸はカットしない）
    if gx_max > 0:
        part_rects = [r for r in part_rects
                      if r[0] < stair_cut
                      and (gy_max == 0 or r[1] < gy_max + STAIR_GAP)]
    if envelopes:   # 外形が分かる図面では建物外の内壁（隣住戸・バルコニー側）も除外
        part_rects = [r for r in part_rects
                      if any(_ix_ratio(r, [env[0] - 50, env[1] - 50,
                                           env[2] + 50, env[3] + 50]) > 0.3
                             for env in envelopes)]

    # 立ち上げ済みの壁（躯体ポリ・外周帯・外周窓帯壁）と重複する内壁は破棄
    solid_bbs = [p['bbox'] for p in polys] + frames + \
        [[min(q[0], q[2]), min(q[1], q[3]), max(q[0], q[2]), max(q[1], q[3])]
         for q in frame_quads]
    solid_bbs += [p['bbox'] for p in west]
    if south_main:
        solid_bbs.append(south_main['bbox'])
    if north_main:
        solid_bbs.append(north_main['bbox'])
    part_rects = [r for r in part_rects
                  if not any(_ix_ratio(r, bb) > 0.5 for bb in solid_bbs)]

    # 内壁同士の重複マージ（面積の大きい順に採用）
    part_rects.sort(key=lambda r: -((r[2] - r[0]) * (r[3] - r[1])))
    kept = []
    for r in part_rects:
        if not any(_ix_ratio(r, k) > 0.6 for k in kept):
            kept.append(r)
    part_rects = kept

    # ドア開口を抜く（建具レイヤーの開き戸ARC位置）
    doors = extract_door_arcs(doc, xo, yo)
    part_walls = []
    for r in part_rects:
        segments, openings = cut_door_openings(r, doors)
        t = min(r[2] - r[0], r[3] - r[1])
        part_walls.append({'rect': r, 'horiz': (r[2] - r[0]) >= (r[3] - r[1]),
                           't': t, 'segments': segments, 'openings': openings})

    # ── クラス別立ち上げ: 建具ユニット・インナーサッシ・断熱 ──
    door_units = extract_door_units(doc, xo, yo)
    sashes = extract_sashes(doc, xo, yo)
    insul = extract_insulation(doc, xo, yo)
    if gx_max > 0:
        door_units = [u for u in door_units if u['bbox'][0] < stair_cut]
        sashes = [s for s in sashes if s['bbox'][0] < stair_cut]
        insul = [i for i in insul if i['bbox'][0] < stair_cut]

    # ── 玄関扉（片開き・必ず1箇所）: 玄関ラベル最寄りの開き戸ARC → 3方枠+扉 ──
    entrance = None
    ent_label = None
    for txt, tx, ty in _iter_texts_pos(doc):
        if '玄関' in txt:
            ent_label = (tx - xo, ty - yo)
            break
    ent_arc = None
    if doors:
        if ent_label:
            in_r = [dr for dr in doors
                    if ((dr['hinge'][0] - ent_label[0]) ** 2
                        + (dr['hinge'][1] - ent_label[1]) ** 2) ** 0.5 <= ENT_ARC_SEARCH_R]
            if in_r:
                ent_arc = min(in_r, key=lambda dr: (dr['hinge'][0] - ent_label[0]) ** 2
                              + (dr['hinge'][1] - ent_label[1]) ** 2)
        if ent_arc is None and ent_label is None:
            ent_arc = max(doors, key=lambda dr: dr['r'])   # ラベル無し図面は最大半径ARC
    if ent_arc:
        hx, hy = ent_arc['hinge']
        # 扉の閉じ位置 = 90度スイングでは弦の両端とも軸整列なので、
        # 「図面に描かれた扉レクト（薄い閉ポリ）が吊元→端点の線上にある方」を閉じ位置とする
        cands = []
        for p in (ent_arc['p1'], ent_arc['p2']):
            dx, dy = p[0] - hx, p[1] - hy
            if min(abs(dx), abs(dy)) <= 0.3 * max(abs(dx), abs(dy), 1):
                cands.append(p)
        if not cands:
            cands = [ent_arc['p1']]
        thin_polys = _collect_thin_polys(doc, xo, yo)
        p, leaf_bb = cands[0], None
        for cp in cands:
            corr = [min(hx, cp[0]) - 100, min(hy, cp[1]) - 100,
                    max(hx, cp[0]) + 100, max(hy, cp[1]) + 100]
            hits = [tb for tb in thin_polys if _ix_ratio(tb, corr) > 0.6]
            if hits:
                p = cp
                leaf_bb = max(hits, key=lambda tb: (tb[2] - tb[0]) * (tb[3] - tb[1]))
                break
        if abs(p[0] - hx) >= abs(p[1] - hy):   # 横走りの開口
            ebb = [round(min(hx, p[0])), round(hy - WALL_T / 2),
                   round(max(hx, p[0])), round(hy + WALL_T / 2)]
            leaf = leaf_bb or [ebb[0] + FR, round(hy - ENT_DOOR_T / 2),
                               ebb[2] - FR, round(hy + ENT_DOOR_T / 2)]
        else:                                   # 縦走りの開口
            ebb = [round(hx - WALL_T / 2), round(min(hy, p[1])),
                   round(hx + WALL_T / 2), round(max(hy, p[1]))]
            leaf = leaf_bb or [round(hx - ENT_DOOR_T / 2), ebb[1] + FR,
                               round(hx + ENT_DOOR_T / 2), ebb[3] - FR]
        entrance = {'bbox': ebb, 'panels': [leaf], 'block': '玄関扉(片開き)'}
        door_units.append(entrance)

    # サッシ・建具の帯を所属壁に吸着（中心・厚みを壁に正規化 → 窓・建具と壁のズレ防止）
    _snap_bbs = [pp['bbox'] for pp in polys] + frames
    for s in sashes:
        s['bbox'] = _snap_band(s['bbox'], _snap_bbs)
    for u in door_units:
        u['bbox'] = _snap_band(u['bbox'], _snap_bbs)

    # ── 天井梁: 「天井」レイヤーの線・点線ライン（梁下端 = 最寄りの CH≒ 注記） ──
    ch_positions = [p for p in detect_ch_positions(doc, xo, yo, lo=1000, hi=6000)
                    if p[0] < CH]   # 天井高未満の注記のみ = 梁下・下がり天井
    ceil_beams = extract_ceiling_beams(doc, xo, yo, ch_positions,
                                       [pp['bbox'] for pp in polys] + frames)
    # 同じ梁が躯体点線と天井破線で二重に描かれている場合の重複除去
    _cb_kept = []
    for b_ in sorted(ceil_beams, key=lambda b2: -((b2['x2'] - b2['x1'])
                                                  * (b2['y2'] - b2['y1']))):
        r_ = [b_['x1'], b_['y1'], b_['x2'], b_['y2']]
        if not any(_ix_ratio(r_, [k['x1'], k['y1'], k['x2'], k['y2']]) > 0.6
                   for k in _cb_kept):
            _cb_kept.append(b_)
    ceil_beams = _cb_kept
    if gx_max > 0:
        ceil_beams = [b for b in ceil_beams if b['x1'] < stair_cut]

    # 引き込み戸（戸袋タイプ）対応: ユニットのうち壁閉ポリに覆われた区間は戸袋なので、
    # 枠・垂れ壁・壁開口は「壁の無い区間 = 実開口」だけに立てる。
    # 戸袋内に描かれた収納時の扉（点線表記）パネルも除外する
    wall_bbs_all = [pp['bbox'] for pp in polys]
    for u in door_units:
        b = u['bbox']
        segs_open = _cut_strip(b, wall_bbs_all)
        horiz_u = (b[2] - b[0]) >= (b[3] - b[1])
        if segs_open:
            key = (lambda r: r[2] - r[0]) if horiz_u else (lambda r: r[3] - r[1])
            u['open'] = max(segs_open, key=key)
        else:
            u['open'] = b
        if u['open'] != b and u['panels']:
            o = u['open']
            kept_panels = []
            for pb in u['panels']:
                if horiz_u:
                    ov = min(pb[2], o[2]) - max(pb[0], o[0])
                    keep = ov >= 0.5 * (pb[2] - pb[0])
                else:
                    ov = min(pb[3], o[3]) - max(pb[1], o[1])
                    keep = ov >= 0.5 * (pb[3] - pb[1])
                if keep:
                    kept_panels.append(pb)
            u['panels'] = kept_panels

    # 外周帯・内壁をサッシ/建具ユニット位置で開口。
    # 窓は「壁に窓サイズの穴」を開ける: 全高カットではなく、窓の下（〜腰高）と
    # 上（まぐさ〜CH）の壁ラインを sash_strips として残す（WIN_OVERRIDES連動）
    open_boxes = [s['bbox'] for s in sashes] + [u['open'] for u in door_units]
    sash_strips = {i: [] for i in range(len(sashes))}   # サッシidx → 壁ライン区間
    if open_boxes:
        door_boxes = [u['open'] for u in door_units]
        new_frames = []
        for f in frames:
            horiz_f = (f[2] - f[0]) >= (f[3] - f[1])
            lo, hi = (f[0], f[2]) if horiz_f else (f[1], f[3])
            cross = (f[1] + f[3]) / 2 if horiz_f else (f[0] + f[2]) / 2
            ivs = []   # (a1, a2, sash_idx or None)
            for idx, s in enumerate(sashes):
                b = s['bbox']
                if horiz_f:
                    if not (b[1] - 1 <= cross <= b[3] + 1):
                        continue
                    a1, a2 = max(lo, b[0]), min(hi, b[2])
                else:
                    if not (b[0] - 1 <= cross <= b[2] + 1):
                        continue
                    a1, a2 = max(lo, b[1]), min(hi, b[3])
                if a2 > a1:
                    ivs.append((a1, a2, idx))
            for b in door_boxes:
                if horiz_f:
                    if not (b[1] - 1 <= cross <= b[3] + 1):
                        continue
                    a1, a2 = max(lo, b[0]), min(hi, b[2])
                else:
                    if not (b[0] - 1 <= cross <= b[2] + 1):
                        continue
                    a1, a2 = max(lo, b[1]), min(hi, b[3])
                if a2 > a1:
                    ivs.append((a1, a2, None))
            if not ivs:
                new_frames.append(f)
                continue
            ivs.sort()
            pos = lo
            for a1, a2, idx in ivs:
                a1 = max(a1, pos)
                if a1 - pos >= 50:
                    new_frames.append([pos, f[1], a1, f[3]] if horiz_f
                                      else [f[0], pos, f[2], a1])
                if a2 > a1 and idx is not None:
                    seg = ([round(a1), f[1], round(a2), f[3]] if horiz_f
                           else [f[0], round(a1), f[2], round(a2)])
                    sash_strips[idx].append(seg)
                pos = max(pos, a2)
            if hi - pos >= 50:
                new_frames.append([pos, f[1], hi, f[3]] if horiz_f
                                  else [f[0], pos, f[2], hi])
        frames = [[round(v) for v in f] for f in new_frames]
        for w in part_walls:
            r = w['rect']
            new_segs = []
            for s1, s2 in w['segments']:
                band = [s1, r[1], s2, r[3]] if w['horiz'] else [r[0], s1, r[2], s2]
                for cut in _cut_strip(band, open_boxes):
                    new_segs.append((cut[0], cut[2]) if w['horiz'] else (cut[1], cut[3]))
            w['segments'] = new_segs

    # 家具：図面から抽出 → カタログ照合で配置シンボルを選定
    #   精度向上: 部屋ラベル位置からカテゴリ事前分布（洗面所→衛生等）、
    #   ベッド標準寸法は「ベッド」カテゴリ優先、一般レイヤーの家具フットプリントも走査
    catalog = load_catalog()
    furniture = extract_furniture(doc, xo, yo)
    furniture += extract_furniture_extra(doc, xo, yo, envelopes)
    room_pts = detect_room_priors(doc, xo, yo)

    def _prior_cats(f):
        if f.get('w') and f.get('d') and _is_bed_size(f['w'], f['d']):
            return {'ベッド'}
        best, bd = None, ROOM_ASSIGN_R
        for cats, rx, ry in room_pts:
            dist = ((f['x'] - rx) ** 2 + (f['y'] - ry) ** 2) ** 0.5
            if dist < bd:
                bd, best = dist, cats
        return best

    def _single_kind(cat, name):
        """1住戸1つの水回り設備か（トイレ/キッチン/洗面台）"""
        if cat == 'キッチン':
            return 'キッチン'
        if 'トイレ' in name:
            return 'トイレ'
        if '洗面台' in name or '手洗' in name:
            return '洗面台'
        return None

    placed, boxed, unmatched, beds_simple, sofas_simple = [], [], [], [], []
    for f in furniture:
        hit, err, via, sw = None, 0, None, False
        prior = _prior_cats(f)
        if f['kind'] == 'insert' and f['name']:
            hit, _ = match_by_name(f['name'], catalog)
            via = '名前'
            if not hit and f['w']:        # 名前で外れたら寸法で（無名グループ対応）
                hit, err, sw = match_by_size(f['w'], f['d'], catalog, prior)
                via = '寸法'
        elif f['kind'] == 'foot':
            hit, err, sw = match_by_size(f['w'], f['d'], catalog, prior)
            via = '寸法'
        # 部屋に合わない水回り設備は割り当てない（寝室の椅子がトイレになる等の誤爆防止）
        if hit and via == '寸法' and prior and hit['category'] not in prior \
                and _single_kind(hit['category'], hit['name']):
            hit = None
        # ベッドはシンボルを使わず「簡易ボリューム+枕」で表現する
        is_bed = (hit is not None and hit['category'] == 'ベッド') or \
                 (hit is None and f.get('w') and f.get('d')
                  and _is_bed_size(f['w'], f['d']))
        if is_bed:
            # f['w'],f['d'] はワールド寸法（回転込み）なのでスワップ不要。
            # カタログ寸法フォールバック時のみ図面INSERT回転で入替える
            bw = f.get('w')
            bd = f.get('d')
            if not (bw and bd):
                bw = _match_dims(hit)[0] if hit else 1000
                bd = _match_dims(hit)[1] if hit else 2000
                if abs(((f['angle'] % 180) + 180) % 180 - 90) <= 10:
                    bw, bd = bd, bw
            beds_simple.append({
                'bbox': [round(f['x'] - bw / 2), round(f['y'] - bd / 2),
                         round(f['x'] + bw / 2), round(f['y'] + bd / 2)],
                'label': hit['name'] if hit else 'ベッド(寸法判定)',
            })
            continue
        # ソファもシンボルを使わず「座面+背もたれ+脚」の簡易ボリュームで表現する
        if hit is not None and hit['category'] == 'ソファ':
            sw_ = f.get('w')
            sd_ = f.get('d')
            if not (sw_ and sd_):   # ワールド寸法が無い時だけカタログ寸法+回転入替
                sw_ = _match_dims(hit)[0]
                sd_ = _match_dims(hit)[1]
                if abs(((f['angle'] % 180) + 180) % 180 - 90) <= 10:
                    sw_, sd_ = sd_, sw_
            sofas_simple.append({
                'bbox': [round(f['x'] - sw_ / 2), round(f['y'] - sd_ / 2),
                         round(f['x'] + sw_ / 2), round(f['y'] + sd_ / 2)],
                'label': hit['name'][:30],
            })
            continue
        # 寸法乖離ガード: マッチ品の実寸が図面寸と25%超ずれるなら
        # シンボルを無理に置かず図面寸の簡易ボリュームにする（はみ出しズレ防止）
        if hit and via == '寸法' and f.get('w') and f.get('d'):
            mw_, md_ = _match_dims(hit)
            fw_, fd_ = (f['w'], f['d']) if not sw else (f['d'], f['w'])
            if mw_ and md_ and (abs(mw_ - fw_) > fw_ * 0.25
                                or abs(md_ - fd_) > fd_ * 0.25):
                hit = None
        if hit:
            # 配置回転: 寸法照合は「ワールド寸法 vs カタログ局所寸法」の比較なので、
            # 必要な回転は sw の有無だけで決まる（図面INSERT回転はワールド寸法に織込み済み）。
            # 名前照合（同一シンボル）は図面INSERTの回転に追従する
            if via == '寸法':
                ang = 90.0 if sw else 0.0
            else:
                ang = round((f['angle'] + (90 if sw else 0)) % 360, 1)
            placed.append({**f, 'angle': ang,
                           'block': hit['block'], 'matched': hit['name'],
                           'cat': hit['category'], 'via': via, 'err': err,
                           'vw': hit.get('vw_name') or hit['block'],
                           'h': hit.get('h') or 700,
                           'z0': hit.get('z0') or 0,
                           # ボックス代替の実寸はACIS実測(w_geo/d_geo)を優先
                           'bw': hit.get('w_geo') or hit.get('w') or f.get('w') or 600,
                           'bd': hit.get('d_geo') or hit.get('d') or f.get('d') or 600,
                           'cx0': hit.get('cx') or 0,
                           'cy0': hit.get('cy') or 0})
        elif f.get('w') and f.get('d'):
            boxed.append(f)      # 該当なし → 図面実寸の簡易ボリューム（無理にインポートしない）
        else:
            unmatched.append(f)  # 寸法も取れない → コメントで報告のみ

    # ベッド・ソファの重複除去（INSERT経由と輪郭経由の二重取り・入れ子輪郭対策）
    def _dedupe_boxes(items):
        items.sort(key=lambda b: -((b['bbox'][2] - b['bbox'][0])
                                   * (b['bbox'][3] - b['bbox'][1])))
        kept = []
        for b in items:
            if not any(_ix_ratio(b['bbox'], k['bbox']) > 0.5
                       or _ix_ratio(k['bbox'], b['bbox']) > 0.5 for k in kept):
                kept.append(b)
        return kept

    beds_simple = _dedupe_boxes(beds_simple)
    sofas_simple = _dedupe_boxes(sofas_simple)

    # 1住戸に1つしか無い設備（トイレ・キッチン・洗面台）: 最良1件のみ配置し、残りは簡易ボリュームへ
    room_label_pts = {k: [] for k in SINGLE_ROOM_KW}
    for txt, tx, ty in _iter_texts_pos(doc):
        u = txt.upper()
        for k, kws in SINGLE_ROOM_KW.items():
            if any(kw in u for kw in kws):
                room_label_pts[k].append((tx - xo, ty - yo))

    single_groups = {}
    for p in placed:
        g = _single_kind(p['cat'], p['matched'])
        if g:
            single_groups.setdefault(g, []).append(p)
    demote = []
    for g, items in single_groups.items():
        if len(items) <= 1:
            continue
        pts = room_label_pts.get(g) or []

        def _rank(p):
            if pts:
                return min((p['x'] - rx) ** 2 + (p['y'] - ry) ** 2 for rx, ry in pts)
            return p['err']
        items.sort(key=_rank)
        demote += items[1:]   # 該当部屋のラベルに最も近い1件だけ残す
    if demote:
        _demote_ids = set(map(id, demote))
        placed = [p for p in placed if id(p) not in _demote_ids]
        for p in demote:
            if p.get('w') and p.get('d'):
                boxed.append(p)
            else:
                unmatched.append(p)

    # トイレは図面の「トイレ」ラベル位置に設置する（共通で必ず1つ）
    toilet_pts = room_label_pts.get('トイレ') or []
    if toilet_pts:
        lx, ly = toilet_pts[0]
        cur = next((p for p in placed
                    if _single_kind(p['cat'], p['matched']) == 'トイレ'), None)
        if cur is None:
            # 図面からトイレが照合できなくても、カタログのトイレをラベル位置に置く
            t_item = next((it for it in catalog if 'トイレ' in it['name']), None)
            if t_item:
                placed.append({'kind': 'label', 'name': None,
                               'x': round(lx), 'y': round(ly), 'angle': 0,
                               'w': None, 'd': None,
                               'block': t_item['block'], 'matched': t_item['name'],
                               'cat': t_item['category'], 'via': 'ラベル位置', 'err': 0,
                               'vw': t_item.get('vw_name') or t_item['block'],
                               'h': t_item.get('h') or 1000,
                               'z0': t_item.get('z0') or 0,
                               'bw': t_item.get('w_geo') or t_item.get('w') or 700,
                               'bd': t_item.get('d_geo') or t_item.get('d') or 800})
        elif ((cur['x'] - lx) ** 2 + (cur['y'] - ly) ** 2) ** 0.5 > 600:
            cur['x'], cur['y'] = round(lx), round(ly)   # ラベル位置に寄せる
            cur['via'] = 'ラベル位置'

    # 建具がある位置には壁を立ち上げない: ユニットbboxに6割以上収まる壁ポリを除去
    _unit_bbs = [u['bbox'] for u in door_units] + [s['bbox'] for s in sashes]
    _removed_walls = [p for p in walls
                      if any(_ix_ratio(p['bbox'], ub) > 0.6 for ub in _unit_bbs)]
    if _removed_walls:
        _rm_ids = set(map(id, _removed_walls))
        walls = [p for p in walls if id(p) not in _rm_ids]

    # 引き戸を「内包」する一枚壁（帯状閉ポリ）は建具の実開口で分割する
    # （壁の方が大きい場合は上の6割判定に掛からず、扉が壁に埋まるため）
    wall_cut_rects = []
    _open_bbs = [u.get('open', u['bbox']) for u in door_units] + \
                [s['bbox'] for s in sashes]
    _to_cut = []
    for p in walls:
        bb = p['bbox']
        if min(bb[2] - bb[0], bb[3] - bb[1]) > 400:
            continue   # 帯壁のみ対象（複雑形状ポリは対象外）
        fill = _poly_area(p['pts']) / max(1, (bb[2] - bb[0]) * (bb[3] - bb[1]))
        if fill < 0.8:
            continue
        hits = [ob for ob in _open_bbs
                if _ix_ratio(ob, bb) > 0.5 or _ix_ratio(
                    [max(ob[0], bb[0]), max(ob[1], bb[1]),
                     min(ob[2], bb[2]), min(ob[3], bb[3])], ob) > 0.5]
        hits = [ob for ob in _open_bbs
                if min(ob[2], bb[2]) - max(ob[0], bb[0]) > 100
                and min(ob[3], bb[3]) - max(ob[1], bb[1]) > 30]
        if hits:
            _to_cut.append((p, hits))
    for p, hits in _to_cut:
        walls = [w2 for w2 in walls if id(w2) != id(p)]
        wall_cut_rects += _cut_strip(list(p['bbox']), hits)

    # 窓の正面にある帯壁ポリ・壁片は「窓サイズの穴あき」に分割する（窓が壁で隠れない）
    wall_win_cuts = []   # (サッシidx, 窓区間の矩形)
    _to_wincut = []
    for p in walls:
        bb = p['bbox']
        if min(bb[2] - bb[0], bb[3] - bb[1]) > 400:
            continue
        fill = _poly_area(p['pts']) / max(1, (bb[2] - bb[0]) * (bb[3] - bb[1]))
        if fill < 0.8:
            continue
        full, wins_ = _cut_band_for_windows(bb, sashes)
        if wins_:
            _to_wincut.append((p, full, wins_))
    for p, full, wins_ in _to_wincut:
        walls = [w2 for w2 in walls if id(w2) != id(p)]
        wall_cut_rects += full
        wall_win_cuts += wins_
    _wcr2 = []
    for r in wall_cut_rects:
        full, wins_ = _cut_band_for_windows(r, sashes)
        if wins_:
            _wcr2 += full
            wall_win_cuts += wins_
        else:
            _wcr2.append(r)
    wall_cut_rects = _wcr2

    # 断熱も窓の正面は「窓サイズの穴あき」に分割する（窓が断熱で隠れない）
    insul_win_cuts = []
    _new_insul = []
    for it in insul:
        bb = it['bbox']
        if min(bb[2] - bb[0], bb[3] - bb[1]) <= 400:
            full, wins_ = _cut_band_for_windows(bb, sashes)
            if wins_:
                for fr in full:
                    _new_insul.append({'pts': [(fr[0], fr[1]), (fr[2], fr[1]),
                                               (fr[2], fr[3]), (fr[0], fr[3])],
                                       'bbox': fr})
                insul_win_cuts += wins_
                continue
        _new_insul.append(it)
    insul = _new_insul

    # ── セルフチェック: 生成ジオメトリをDXF図面と突き合わせ ──
    _chk_polys = [p['pts'] for p in walls] + \
        [[(q[0], q[1]), (q[2], q[3])] for q in frame_quads]
    _chk_rects = list(frames) + list(wall_cut_rects)
    for w in part_walls:
        r_ = w['rect']
        for s1, s2 in w['segments']:
            _chk_rects.append([s1, r_[1], s2, r_[3]] if w['horiz']
                              else [r_[0], s1, r_[2], s2])
    _chk_rects += [u.get('open', u['bbox']) for u in door_units]
    _chk_rects += [s['bbox'] for s in sashes]
    for p in west:
        _chk_rects.append(p['bbox'])
    if south_main:
        _chk_rects.append(south_main['bbox'])
    if north_main:
        _chk_rects.append(north_main['bbox'])


    # セルフチェック連動の自己修復: 未カバーの図面壁線を補完壁として追加
    # 建具・サッシの位置には壁を立てない（開口部だけでなくユニット全域を除外）
    heal_exclude = [s['bbox'] for s in sashes] + [u['bbox'] for u in door_units]
    heal_rects = heal_walls(doc, xo, yo, _chk_polys, _chk_rects,
                            heal_exclude, envelopes)
    _chk_rects += heal_rects

    check = self_check(doc, xo, yo, _chk_polys, _chk_rects)
    if scale_ratio and not (0.95 <= scale_ratio <= 1.05):
        check['warnings'].append(
            f'寸法記載値と実測の比が {scale_ratio:.2f}'
            '（cm図面/inch等の単位ミスの疑い）— DXF書き出し単位を確認')
    check['scale_ratio'] = scale_ratio

    # ── 部屋天井（CH注記に追従）とバルコニー ──
    # 壁+建具+サッシで囲まれた領域をラベル位置からフラッドフィルで特定する
    _blocked = _geom_cells(_chk_polys, _chk_rects)
    grown_blocked = set()
    for ci, cj in _blocked:
        for di in (-1, 0, 1):
            for dj in (-1, 0, 1):
                grown_blocked.add((ci + di, cj + dj))
    for u in door_units:            # 建具・サッシは領域境界（開口から隣室へ漏れない栓）
        b_ = u['bbox']
        _fill_box_cells(grown_blocked,
                        [b_[0] - 150, b_[1] - 150, b_[2] + 150, b_[3] + 150])
    for s in sashes:
        b_ = s['bbox']
        _fill_box_cells(grown_blocked,
                        [b_[0] - 150, b_[1] - 150, b_[2] + 150, b_[3] + 150])
    for w in part_walls:            # 内壁のドア開口も栓をする
        r_ = w['rect']
        for o1, o2 in w['openings']:
            _fill_box_cells(grown_blocked,
                            [o1, r_[1], o2, r_[3]] if w['horiz']
                            else [r_[0], o1, r_[2], o2])

    _room_bound = envelopes[0] if envelopes else [fx1, fy1, fx2, fy2]
    ch_all = detect_ch_positions(doc, xo, yo, lo=1000, hi=6000)

    # 梁下として使われたCH注記（梁矩形の近く）は部屋のCHではない → 部屋判定から除外
    def _near_beam(tx, ty):
        for b_ in ceil_beams:
            ddx = max(b_['x1'] - tx, 0, tx - b_['x2'])
            ddy = max(b_['y1'] - ty, 0, ty - b_['y2'])
            if (ddx * ddx + ddy * ddy) ** 0.5 < 400:
                return True
        return False

    room_ch_notes = [(v, tx, ty) for v, tx, ty in ch_all if not _near_beam(tx, ty)]

    # シード = 部屋ラベル（最優先）+ 梁下でないCH注記
    room_seeds = []                 # (x, y, ラベル)
    for txt, tx, ty in _iter_texts_pos(doc):
        u_ = txt.upper().strip()
        if any(k in u_ for k in ROOM_SEED_KW) and len(u_) <= 12:
            room_seeds.append((tx - xo, ty - yo, u_[:10]))
    for v, tx, ty in room_ch_notes:
        room_seeds.append((tx, ty, f'CH{v}'))

    ceilings = []          # {'v', 'rects', 'label', 'known'}
    rooms_no_ch = []
    rooms_flood_fail = []
    claimed = set()
    if not DRAW_CEILINGS:
        room_seeds = []   # 天井は貼らない（梁は extract_ceiling_beams で別途生成）
    for sx, sy, lbl in room_seeds:
        c0 = (int(sx // 50), int(sy // 50))
        if c0 in claimed:
            continue
        region = _flood((sx, sy), grown_blocked, _room_bound, cap=200000)
        if not region or len(region) < 40:   # 特定不能 or 0.1㎡未満
            rooms_flood_fail.append(lbl)
            continue
        if len(region & claimed) > len(region) * 0.3:
            continue   # 既存の部屋とほぼ同じ領域
        claimed |= region
        # 部屋のCH = 領域内にある（梁下でない）CH注記の最頻値（同数なら大きい方）
        in_vals = [v for v, tx, ty in room_ch_notes
                   if (int(tx // 50), int(ty // 50)) in region]
        if in_vals:
            cnt_ = Counter(in_vals)
            v_room = max(cnt_.items(), key=lambda kv: (kv[1], kv[0]))[0]
        else:
            v_room = None
        ceilings.append({'v': v_room or CH, 'rects': _runs_to_rects(region),
                         'label': lbl, 'known': v_room is not None})
        if v_room is None:
            rooms_no_ch.append(lbl)
    if rooms_no_ch:
        check['warnings'].append(
            'CH不明の部屋（最頻値' + str(CH) + 'を適用）: ' + ' / '.join(rooms_no_ch))
    if rooms_flood_fail:
        check['warnings'].append(
            '天井領域を特定できない部屋（囲われていない/広すぎ）: '
            + ' / '.join(dict.fromkeys(rooms_flood_fail)))

    # バルコニー: ラベルからフラッドフィル（境界=図面の全壁線+見切り線+サッシ）
    balconies = []         # {'rects', 'parapets', 'label'}
    bal_boundary = set(grown_blocked)
    for e in iter_world(doc, expand_parts=True):
        if e.dxftype() != 'LINE':
            continue
        if _is_excluded_layer(getattr(e.dxf, 'layer', '')):
            continue
        s_, en_ = e.dxf.start, e.dxf.end
        n_ = max(1, int(max(abs(en_.x - s_.x), abs(en_.y - s_.y)) // 50))
        for i_ in range(n_ + 1):
            t_ = i_ / n_
            bal_boundary.add((int((s_.x + (en_.x - s_.x) * t_ - xo) // 50),
                              int((s_.y + (en_.y - s_.y) * t_ - yo) // 50)))
    for txt, tx, ty in _iter_texts_pos(doc):
        if not any(k in txt for k in BALCONY_KW):
            continue
        gx_, gy_ = tx - xo, ty - yo
        region = _flood((gx_, gy_), bal_boundary,
                        [fx1 - 5000, fy1 - 5000, fx2 + 5000, fy2 + 5000],
                        cap=25000)
        if not region or len(region) < 100:
            check['warnings'].append(
                f'バルコニー「{txt.strip()[:10]}」の領域を特定できない（手すり線で囲われていない）')
            continue
        balconies.append({'rects': _runs_to_rects(region),
                          'parapets': _region_perimeter_rects(region, envelopes),
                          'label': txt.strip()[:10]})

    # 図面ガイド線（照合オーバーレイ用）: 壁系レイヤーの線を収集（建物範囲内のみ）
    def _guide_ok(x1, y1, x2, y2):
        if not envelopes:
            return True
        cx_, cy_ = (x1 + x2) / 2, (y1 + y2) / 2
        return any(env[0] - 500 <= cx_ <= env[2] + 500
                   and env[1] - 500 <= cy_ <= env[3] + 500
                   for env in envelopes)

    guide_segs = []
    for e in iter_world(doc):
        if len(guide_segs) >= GUIDE_MAX:
            break
        if not _is_wall_layer(getattr(e.dxf, 'layer', '')):
            continue
        t = e.dxftype()
        if t in ('LINE', 'POLYLINE', 'LWPOLYLINE') and \
                any(wd in _linetype_of(doc, e) for wd in DASH_WORDS):
            continue   # 点線（梁表記）はガイドに含めない
        if t == 'LINE':
            s_, en_ = e.dxf.start, e.dxf.end
            if _guide_ok(s_.x - xo, s_.y - yo, en_.x - xo, en_.y - yo):
                guide_segs.append((round(s_.x - xo), round(s_.y - yo),
                                   round(en_.x - xo), round(en_.y - yo)))
        elif t in ('POLYLINE', 'LWPOLYLINE'):
            pts_, closed_ = get_pts(e)
            if len(pts_) < 2:
                continue
            g_ = [(round(x - xo), round(y - yo)) for x, y in pts_]
            rng_ = len(g_) if closed_ and len(g_) >= 3 else len(g_) - 1
            for k in range(rng_):
                if len(guide_segs) >= GUIDE_MAX:
                    break
                if _guide_ok(g_[k][0], g_[k][1],
                             g_[(k + 1) % len(g_)][0], g_[(k + 1) % len(g_)][1]):
                    guide_segs.append((g_[k][0], g_[k][1],
                                       g_[(k + 1) % len(g_)][0], g_[(k + 1) % len(g_)][1]))

    # VW実行時の自動位置合わせ用: DXFの壁系図形の外形（生座標）
    _ex1 = _ey1 = float('inf')
    _ex2 = _ey2 = float('-inf')
    for e in iter_world(doc):
        if not _is_wall_layer(getattr(e.dxf, 'layer', '')):
            continue
        t = e.dxftype()
        if t == 'LINE':
            s_, en_ = e.dxf.start, e.dxf.end
            for px, py in ((s_.x, s_.y), (en_.x, en_.y)):
                _ex1, _ey1 = min(_ex1, px), min(_ey1, py)
                _ex2, _ey2 = max(_ex2, px), max(_ey2, py)
        elif t in ('POLYLINE', 'LWPOLYLINE'):
            for px, py in get_pts(e)[0]:
                _ex1, _ey1 = min(_ex1, px), min(_ey1, py)
                _ex2, _ey2 = max(_ex2, px), max(_ey2, py)
    has_exp_bbox = _ex1 != float('inf')

    # ── 家具のセルフチェックと自動補正 ──
    furn_wall_bbs = [pp['bbox'] for pp in polys] + frames
    furn_snapped, furn_max_shift, furn_dedup = 0, 0, 0

    def _furn_rect(it, w_key='bw', d_key='bd'):
        # 図面のワールド寸法（w,d）が最優先。無ければカタログ寸法を配置回転で入替
        w_ = it.get('w')
        d_ = it.get('d')
        if not (w_ and d_):
            w_ = it.get(w_key) or 600
            d_ = it.get(d_key) or 600
            ang_ = it.get('angle') or 0
            if abs(((ang_ % 180) + 180) % 180 - 90) <= 10:
                w_, d_ = d_, w_
        return [it['x'] - w_ / 2, it['y'] - d_ / 2,
                it['x'] + w_ / 2, it['y'] + d_ / 2]

    # 【7】壁めり込みスナップ: 壁と3割超重なる家具を最小距離で退避（上限200mm）
    for it in placed + boxed:
        r = _furn_rect(it) if it in placed else _furn_rect(it, 'w', 'd')
        hitw = max(furn_wall_bbs, key=lambda bb: _ix_ratio(r, bb), default=None)
        if hitw is None or _ix_ratio(r, hitw) <= 0.3:
            continue
        moves = [(hitw[0] - r[2], 0), (hitw[2] - r[0], 0),
                 (0, hitw[1] - r[3]), (0, hitw[3] - r[1])]
        dx, dy = min(moves, key=lambda m: abs(m[0]) + abs(m[1]))
        if abs(dx) + abs(dy) <= 200:
            it['x'] = round(it['x'] + dx)
            it['y'] = round(it['y'] + dy)
            furn_snapped += 1
            furn_max_shift = max(furn_max_shift, abs(dx) + abs(dy))

    # 【10】家具同士の重複除外（入れ子INSERTの二重取り対策・面積大を優先）
    _all_f = [('p', it, _furn_rect(it)) for it in placed] +              [('b', it, _furn_rect(it, 'w', 'd')) for it in boxed]
    _all_f.sort(key=lambda t: -((t[2][2] - t[2][0]) * (t[2][3] - t[2][1])))
    _kept_r, _drop = [], set()
    for kind, it, r in _all_f:
        if any(_ix_ratio(r, k) > 0.6 and _ix_ratio(k, r) > 0.6 for k in _kept_r):
            _drop.add(id(it))
            furn_dedup += 1
        else:
            _kept_r.append(r)
    placed = [it for it in placed if id(it) not in _drop]
    boxed = [it for it in boxed if id(it) not in _drop]

    # 【5】家具セルフチェック: 図面寸との乖離とズレ量を集計して警告
    furn_dim_warn = sum(
        1 for it in placed
        if it.get('w') and it.get('d')
        and (abs(it['bw'] - it['w']) > it['w'] * 0.25
             and abs(it['bw'] - it['d']) > it['d'] * 0.25))
    check['furniture'] = {'placed': len(placed), 'boxed': len(boxed),
                          'snapped': furn_snapped, 'max_shift': furn_max_shift,
                          'dedup': furn_dedup, 'dim_warn': furn_dim_warn}
    if furn_snapped:
        check['warnings'].append(
            f'家具{furn_snapped}件を壁から退避（最大{furn_max_shift}mm）')
    if furn_dedup:
        check['warnings'].append(f'重複家具{furn_dedup}件を除外')

    # 【8】家具ガイド枠（緑）: 図面のフットプリントを重ね描きして照合できるように
    furn_guides = []
    for it in placed:
        furn_guides.append([round(v) for v in _furn_rect(it)])
    for it in boxed:
        furn_guides.append([round(v) for v in _furn_rect(it, 'w', 'd')])
    for bd_ in beds_simple:
        furn_guides.append(list(bd_['bbox']))
    for sf_ in sofas_simple:
        furn_guides.append(list(sf_['bbox']))

    # ── 家具をモデルの右横に整列（図面位置には置かない） ──
    if FURN_LINEUP:
        _lx0 = fx2 + 3000
        _ly = fy1
        _cx = _lx0
        _row_h = 0

        def _lay_dims(it):
            w_ = it.get('w') or it.get('bw') or 600
            d_ = it.get('d') or it.get('bd') or 600
            return w_, d_

        for it in placed + boxed:
            w_, d_ = _lay_dims(it)
            if _cx + w_ > _lx0 + FURN_LINEUP_ROW_W:
                _cx = _lx0
                _ly += _row_h + FURN_LINEUP_GAP
                _row_h = 0
            it['x'] = round(_cx + w_ / 2)
            it['y'] = round(_ly + d_ / 2)
            _cx += w_ + FURN_LINEUP_GAP
            _row_h = max(_row_h, d_)
        for bd_ in beds_simple + sofas_simple:
            b_ = bd_['bbox']
            w_, d_ = b_[2] - b_[0], b_[3] - b_[1]
            if _cx + w_ > _lx0 + FURN_LINEUP_ROW_W:
                _cx = _lx0
                _ly += _row_h + FURN_LINEUP_GAP
                _row_h = 0
            bd_['bbox'] = [round(_cx), round(_ly),
                           round(_cx + w_), round(_ly + d_)]
            _cx += w_ + FURN_LINEUP_GAP
            _row_h = max(_row_h, d_)

    # ── スクリプト生成 ──
    L = []
    a = L.append
    a('import vs')
    a('')
    a('# ' + '=' * 60)
    a(f'# 自動生成 3D モデル（汎用 v7）  source: {Path(dxf_path).name}')
    a(f'# 検出原点: x={xo}, y={yo}')
    if ch_detected:
        vals = ', '.join(f'{v}mm×{n}' for v, n in ch_values)
        a(f'# 天井高: {CH}mm（図面注記から検出。全検出値: {vals} → 最頻値を採用）')
    else:
        a(f'# 天井高: {CH}mm（ユーザー指定）')
    if rooms_found:
        a('# 部屋ラベル: ' + ' / '.join(f'{k}✓({v})' for k, v in rooms_found.items()))
    if rooms_missing:
        a(f'# ⚠ ラベル未検出の部屋: {" / ".join(rooms_missing)}（図面にラベルが無いか表記が異なる）')
    if check['recall'] is not None:
        a(f'# セルフチェック（DXF突き合わせ）: 図面壁線の再現率 {check["recall"]:.0%}'
          f' / 生成側適合率 {check["precision"]:.0%}'
          + (f' / 系統ズレ {check["offset"]}' if check['offset'] else ' / 系統ズレなし'))
    for wmsg in check['warnings']:
        a(f'# ⚠ セルフチェック: {wmsg}')
    a('# ' + '=' * 60)
    a('#  西面=腰壁連続窓 / 東面=青線FIX窓 / 南北=建具腰窓 / 内壁=LINEペア+薄壁+閉ポリ')
    a('#  外部階段・建物外は出力しない')
    a('')
    a('# ─── パラメータ ───')
    for k, v in [('CH', CH), ('FH', FH), ('DOOR_HEAD', DOOR_HEAD),
                 ('FURN_BOX_H', FURN_BOX_H),
                 ('BED_H', BED_H), ('PILLOW_H', PILLOW_H),
                 ('SOFA_LEG_H', SOFA_LEG_H), ('SOFA_LEG_W', SOFA_LEG_W),
                 ('SOFA_SEAT_H', SOFA_SEAT_H), ('SOFA_BACK_T', SOFA_BACK_T),
                 ('SOFA_BACK_H', SOFA_BACK_H),
                 ('SILL', SILL), ('HEAD', HEAD), ('RIB_MULLION', RIB_MULLION),
                 ('SILL_FIX', SILL_FIX), ('HEAD_FIX', HEAD_FIX), ('FIX_MULLION', FIX_MULLION),
                 ('SILL_HIKI', SILL_HIKI), ('HEAD_HIKI', HEAD_HIKI),
                 ('SILL_HAKI', SILL_HAKI), ('HEAD_HAKI', HEAD_HAKI),
                 ('FR', FR), ('FD', FD), ('GT', GT),
                 ('BEAM_W', BEAM_W), ('BEAM_D', BEAM_D)]:
        a(f'{k} = {v}')
    a('')
    a(f'# 図面原点補正: 内部座標はグリッド原点基準なので、描画時に元図面の座標へ戻す')
    a(f'# （これにより2D図面と3Dモデルの位置がズレない）')
    a(f'OX, OY = {xo}, {yo}')
    a('')
    a(f'DRAW_GUIDE = {DRAW_GUIDE}   # 図面ガイド線（赤）を重ね描きして照合できるようにする')
    if guide_segs:
        a('GUIDE_SEGS = [')
        for i in range(0, len(guide_segs), 4):
            row = ', '.join(str(t) for t in guide_segs[i:i + 4])
            a(f'    {row},')
        a(']')
    else:
        a('GUIDE_SEGS = []')
    if furn_guides:
        a('GUIDE_FURN = [')
        for i in range(0, len(furn_guides), 4):
            row = ', '.join(str(tuple(t)) for t in furn_guides[i:i + 4])
            a(f'    {row},')
        a(']')
    else:
        a('GUIDE_FURN = []')
    a('')
    a('# ─── ヘルパー ───')
    a('WHITE = (65535, 65535, 65535)')
    a('')
    a('def paint_white(obj):')
    a('    """壁・窓枠・建具を白のベタ塗りにする（白模型スタイル）"""')
    a('    if obj is None:')
    a('        return')
    a('    vs.SetFPat(obj, 1)          # ベタ塗り')
    a('    vs.SetFillFore(obj, WHITE)')
    a('    vs.SetFillBack(obj, WHITE)')
    a('')
    a('def poly(coords, h, z=0):')
    a('    if h <= 0:')
    a('        return')
    a('    vs.BeginXtrd(0, h)')
    a('    vs.BeginPoly()')
    a('    for i in range(0, len(coords), 2):')
    a('        vs.AddPoint(coords[i] + OX, coords[i + 1] + OY)')
    a('    vs.EndPoly()')
    a('    vs.EndXtrd()')
    a('    obj = vs.LNewObj()')
    a('    if z != 0:')
    a('        vs.Move3DObj(obj, 0, 0, z)')
    a('    paint_white(obj)')
    a('')
    a('def rect(x1, y1, x2, y2, h, z=0):')
    a('    if h <= 0 or x2 <= x1 or y2 <= y1:')
    a('        return')
    a('    vs.BeginXtrd(0, h)')
    a('    vs.Rect(x1 + OX, y1 + OY, x2 + OX, y2 + OY)')
    a('    vs.EndXtrd()')
    a('    obj = vs.LNewObj()')
    a('    if z != 0:')
    a('        vs.Move3DObj(obj, 0, 0, z)')
    a('    paint_white(obj)')
    a('')
    a('_RING_HS = []   # 外周壁ラインのハンドル（実測セルフチェック用）')
    a('')
    a('def wall_sheet(x1, y1, x2, y2, h, z=0):')
    a('    \"\"\"壁のラインをそのまま垂直面として立ち上げる（厚みなし）\"\"\"')
    a('    if h <= 0 or (x1 == x2 and y1 == y2):')
    a('        return')
    a('    vs.BeginXtrd(z, z + h)')
    a('    vs.MoveTo(x1 + OX, y1 + OY)')
    a('    vs.LineTo(x2 + OX, y2 + OY)')
    a('    vs.EndXtrd()')
    a('    _h = vs.LNewObj()')
    a('    paint_white(_h)')
    a('    _RING_HS.append(_h)')
    a('')
    a('def glass_rect(x1, y1, x2, y2, h, z=0):')
    a('    """ガラス: 白ベタの上に半透明をかけて区別する"""')
    a('    if h <= 0 or x2 <= x1 or y2 <= y1:')
    a('        return   # rectが図形を作らない条件では透明度も触らない')
    a('    rect(x1, y1, x2, y2, h, z)')
    a('    try:')
    a('        vs.SetOpacity(vs.LNewObj(), 35)')
    a('    except Exception:')
    a('        pass   # 古いVWでSetOpacityが無い場合はベタのまま')
    a('')
    a('def win_unit(x1, y1, x2, y2, sill, head, pitch=0, hiki=False):')
    a('    """全窓種共通の窓ユニット。')
    a('    枠   : 上下枠(横材) + 左右縦枠 + 方立/召し合わせ框(縦材) = FR見付 x FD見込みの角材ボリューム')
    a('    ガラス: GT厚の薄板ボリューム（枠内に納める。hiki=True は前後トラックに2枚+召し合わせ框）"""')
    a('    wh = head - sill')
    a('    if wh <= 2 * FR:')
    a('        return')
    a('    gz = sill + FR          # ガラス・縦材の下端Z')
    a('    gh = wh - 2 * FR        # ガラス・縦材の高さ')
    a('    if (x2 - x1) >= (y2 - y1):   # 横走りの窓')
    a('        yc = (y1 + y2) / 2.0')
    a('        yf1, yf2 = yc - FD / 2.0, yc + FD / 2.0   # 枠見込み（壁厚の中心）')
    a('        rect(x1, yf1, x2, yf2, FR, sill)          # 下枠（横材）')
    a('        rect(x1, yf1, x2, yf2, FR, head - FR)     # 上枠（横材）')
    a('        rect(x1, yf1, x1 + FR, yf2, gh, gz)       # 左縦枠')
    a('        rect(x2 - FR, yf1, x2, yf2, gh, gz)       # 右縦枠')
    a('        if pitch > 0:        # 方立（縦材・両端は縦枠があるので内側のみ）')
    a('            n = max(1, int(round((x2 - x1) / float(pitch))))')
    a('            for i in range(1, n):')
    a('                mx = x1 + (x2 - x1) * i // n')
    a('                rect(mx - FR // 2, yf1, mx + FR // 2, yf2, gh, gz)')
    a('        if hiki:             # 引き違い: 召し合わせ框 + 前後トラックのガラス2枚')
    a('            xmid = (x1 + x2) // 2')
    a('            rect(xmid - FR // 2, yf1, xmid + FR // 2, yf2, gh, gz)')
    a('            glass_rect(x1 + FR, yc - GT * 1.5, xmid, yc - GT * 0.5, gh, gz)   # ガラス(手前)')
    a('            glass_rect(xmid, yc + GT * 0.5, x2 - FR, yc + GT * 1.5, gh, gz)   # ガラス(奥)')
    a('        else:                # FIX: 1枚ガラス')
    a('            glass_rect(x1 + FR, yc - GT / 2.0, x2 - FR, yc + GT / 2.0, gh, gz)')
    a('    else:                    # 縦走りの窓（東西面）')
    a('        xc = (x1 + x2) / 2.0')
    a('        xf1, xf2 = xc - FD / 2.0, xc + FD / 2.0')
    a('        rect(xf1, y1, xf2, y2, FR, sill)          # 下枠（横材）')
    a('        rect(xf1, y1, xf2, y2, FR, head - FR)     # 上枠（横材）')
    a('        rect(xf1, y1, xf2, y1 + FR, gh, gz)       # 手前縦枠')
    a('        rect(xf1, y2 - FR, xf2, y2, gh, gz)       # 奥縦枠')
    a('        if pitch > 0:')
    a('            n = max(1, int(round((y2 - y1) / float(pitch))))')
    a('            for i in range(1, n):')
    a('                my = y1 + (y2 - y1) * i // n')
    a('                rect(xf1, my - FR // 2, xf2, my + FR // 2, gh, gz)')
    a('        if hiki:')
    a('            ymid = (y1 + y2) // 2')
    a('            rect(xf1, ymid - FR // 2, xf2, ymid + FR // 2, gh, gz)')
    a('            glass_rect(xc - GT * 1.5, y1 + FR, xc - GT * 0.5, ymid, gh, gz)   # ガラス(手前)')
    a('            glass_rect(xc + GT * 0.5, ymid, xc + GT * 1.5, y2 - FR, gh, gz)   # ガラス(奥)')
    a('        else:')
    a('            glass_rect(xc - GT / 2.0, y1 + FR, xc + GT / 2.0, y2 - FR, gh, gz)   # ガラス')
    a('')
    a('# 窓種別の既定値（ribbon=腰壁連続 / fix=大FIX / hiki=腰高引き違い / haki=掃き出し）')
    a('WIN_DEFAULTS = {')
    a("    'ribbon': {'sill': SILL, 'head': HEAD, 'pitch': RIB_MULLION, 'hiki': False},")
    a("    'fix':    {'sill': SILL_FIX, 'head': HEAD_FIX, 'pitch': FIX_MULLION, 'hiki': False},")
    a("    'hiki':   {'sill': SILL_HIKI, 'head': HEAD_HIKI, 'pitch': 0, 'hiki': True},")
    a("    'haki':   {'sill': SILL_HAKI, 'head': HEAD_HAKI, 'pitch': 0, 'hiki': True},")
    a('}')
    a('')
    a('# ── 窓の個別調整 ──────────────────────────────')
    a('# 窓番号（各 win() 行のコメント・スクリプト末尾の窓一覧を参照）をキーに')
    a("# sill(腰高)・head(まぐさ)・pitch(方立間隔)・hiki(引き違い) を上書きして再実行すると、")
    a('# 前回の生成物を消してから調整後のモデルを作り直す。')
    a("# 例) WIN_OVERRIDES = { 3: {'sill': 600, 'head': 2200}, 7: {'head': 2400} }")
    a('WIN_OVERRIDES = {')
    a('}')
    a('')
    _kinds = ', '.join(f"{i + 1}: '{('haki' if s['kind'] == 'hakidashi' else 'hiki')}'"
                       for i, s in enumerate(sashes))
    a('WIN_KINDS = {' + _kinds + '}   # サッシ窓番号→種別（壁の穴あけ用）')
    a('SASH_WIN_NOS = set(WIN_KINDS)   # 壁側が穴を開ける窓（腰壁/垂れ壁を作らない）')
    a('')
    a('def win(no, kind, x1, y1, x2, y2):')
    a('    """番号付き窓。サッシ窓（SASH_WIN_NOS）は壁側が窓サイズの穴を開けるので')
    a('    枠+ガラスのみ生成。それ以外（帯壁上の窓）は腰壁/垂れ壁も作る"""')
    a('    p = dict(WIN_DEFAULTS[kind])')
    a('    p.update(WIN_OVERRIDES.get(no, {}))')
    a('    if no not in SASH_WIN_NOS:')
    a("        if p['sill'] > 0:")
    a("            rect(x1, y1, x2, y2, p['sill'])                 # 腰壁")
    a("        rect(x1, y1, x2, y2, CH - p['head'], p['head'])     # 垂れ壁")
    a("    win_unit(x1, y1, x2, y2, p['sill'], p['head'], p['pitch'], p['hiki'])")
    a("    num_label('窓%d' % no, (x1 + x2) / 2, (y1 + y2) / 2)")
    a('')
    a('def win_wall_rects(no, x1, y1, x2, y2):')
    a('    \"\"\"窓正面の壁・断熱: 窓サイズの穴（sill〜head）を残して下と上のボリュームを立ち上げる\"\"\"')
    a('    p = dict(WIN_DEFAULTS[WIN_KINDS.get(no, "hiki")])')
    a('    p.update(WIN_OVERRIDES.get(no, {}))')
    a("    if p['sill'] > 0:")
    a("        rect(x1, y1, x2, y2, p['sill'])                 # 穴の下")
    a("    rect(x1, y1, x2, y2, CH - p['head'], p['head'])     # 穴の上")
    a('')
    a('def win_wall_strips(no, x1, y1, x2, y2):')
    a('    """窓位置の壁ライン: 窓サイズの穴（sill〜head）を残して下と上だけ立ち上げる。')
    a('    WIN_OVERRIDESでsill/headを変えると穴の大きさも連動する"""')
    a('    p = dict(WIN_DEFAULTS[WIN_KINDS.get(no, "hiki")])')
    a('    p.update(WIN_OVERRIDES.get(no, {}))')
    a("    if p['sill'] > 0:")
    a("        wall_sheet(x1, y1, x2, y2, p['sill'])                 # 穴の下（腰壁ライン）")
    a("    wall_sheet(x1, y1, x2, y2, CH - p['head'], p['head'])     # 穴の上（垂れ壁ライン）")
    a('')
    a('def beam(x1, y1, x2, y2):')
    a('    """梁: 天井面から BEAM_D 下がるフットプリント押し出し"""')
    a('    rect(x1, y1, x2, y2, BEAM_D, CH - BEAM_D)')
    a('')
    a('# ── 建具・天井梁の個別調整（窓のWIN_OVERRIDESと同様） ──')
    a("# 例) DOOR_OVERRIDES = { 2: {'head': 2100} }   # 建具2の枠高さを2100に")
    a('DOOR_OVERRIDES = {')
    a('}')
    a("# 例) BEAM_OVERRIDES = { 3: {'bottom': 1800} }   # 天井梁3の梁下を1800に")
    a('BEAM_OVERRIDES = {')
    a('}')
    a('')
    a('SHOW_NUMBERS = True   # 窓・建具・天井梁の番号をモデル内に文字表記する')
    a('')
    a('def num_label(txt, x, y):')
    a('    """要素番号をモデル内に文字表記（OVERRIDESのキー確認用）"""')
    a('    if not SHOW_NUMBERS:')
    a('        return')
    a('    try:')
    a('        vs.TextOrigin(x + OX, y + OY)')
    a('        vs.CreateText(txt)')
    a('    except Exception:')
    a('        pass')
    a('')
    a('def _door_h(no):')
    a("    return DOOR_OVERRIDES.get(no, {}).get('head', DOOR_HEAD)")
    a('')
    a('def door_frame(no, x1, y1, x2, y2):')
    a('    """建具の3方枠（両縦枠+上枠の簡易ボリューム）+ 上部垂れ壁。扉パネルは別途"""')
    a('    dh = _door_h(no)')
    a('    if (x2 - x1) >= (y2 - y1):   # 横走りの建具')
    a('        rect(x1, y1, x1 + FR, y2, dh)              # 縦枠')
    a('        rect(x2 - FR, y1, x2, y2, dh)              # 縦枠')
    a('        rect(x1 + FR, y1, x2 - FR, y2, FR, dh - FR)  # 上枠（横材）')
    a('    else:                        # 縦走りの建具')
    a('        rect(x1, y1, x2, y1 + FR, dh)              # 縦枠')
    a('        rect(x1, y2 - FR, x2, y2, dh)              # 縦枠')
    a('        rect(x1, y1 + FR, x2, y2 - FR, FR, dh - FR)  # 上枠（横材）')
    a('    rect(x1, y1, x2, y2, CH - dh, dh)              # 垂れ壁')
    a("    num_label('建具%d' % no, (x1 + x2) / 2, (y1 + y2) / 2)")
    a('')
    a('def door_panel(no, x1, y1, x2, y2):')
    a('    """扉パネル（図面の厚みのまま。高さは建具の枠に追従）"""')
    a('    rect(x1, y1, x2, y2, _door_h(no) - FR)')
    a('')
    a('def cbeam(no, x1, y1, x2, y2, bottom):')
    a('    """天井梁（番号調整可）。bottom=None は梁せいBEAM_Dで天井から下げる"""')
    a("    b = BEAM_OVERRIDES.get(no, {}).get('bottom', bottom)")
    a('    if b and b < CH:')
    a('        rect(x1, y1, x2, y2, CH - b, b)')
    a('    else:')
    a('        beam(x1, y1, x2, y2)')
    a("    num_label('梁%d' % no, (x1 + x2) / 2, (y1 + y2) / 2)")
    a('')
    if placed or boxed:
        a('# ─── 家具: MUJIライブラリから自動インポートして配置 ───')
        a('import unicodedata')
        a('')
        a(f'MUJI_LIB = {MUJI_LIB!r}')
        a('')
        a('def _nfc(s):')
        a('    return unicodedata.normalize("NFC", s)')
        a('')
        a('def _import_cb(resName):')
        a('    return 1   # 名前衝突時は置換（2=リネームはPythonでは使用禁止）')
        a('')
        a('_res_cache = None')
        a('def _lib():')
        a('    """ライブラリのシンボル一覧を1回だけ構築 → (listID, {NFC名: index})"""')
        a('    global _res_cache')
        a('    if _res_cache is None:')
        a('        names = {}')
        a('        try:')
        a('            lid, num = vs.BuildResourceListN(16, MUJI_LIB)   # 16=シンボル定義')
        a('            if lid != 0 and num > 0:')
        a('                for i in range(1, num + 1):')
        a('                    names[_nfc(vs.GetActualNameFromResourceList(lid, i))] = i')
        a('        except Exception:')
        a('            lid = 0')
        a('        _res_cache = (lid, names)')
        a('    return _res_cache')
        a('')
        a('_sym_cache = {}')
        a('def ensure_symbol(name):')
        a('    """シンボル定義を現在書類に確保。成功=True / 失敗=False（→ボックス代替）"""')
        a('    if not name:')
        a('        return False')
        a('    if name in _sym_cache:')
        a('        return _sym_cache[name]')
        a('    ok = False')
        a('    try:')
        a('        h = vs.GetObject(name)')
        a('        if h is not None and vs.GetTypeN(h) == 16:')
        a('            ok = True   # 既に書類内にある')
        a('        else:')
        a('            lid, names = _lib()')
        a('            idx = names.get(_nfc(name))')
        a('            if lid != 0 and idx is not None:')
        a('                vs.ImportResToCurFileN(lid, idx, _import_cb)')
        a('                h = vs.GetObject(name)')
        a('                ok = (h is not None and vs.GetTypeN(h) == 16)')
        a('    except Exception:')
        a('        ok = False')
        a('    _sym_cache[name] = ok')
        a('    return ok')
        a('')
        a('def fallback_box(cx, cy, angle, w, d, h, z0=0):')
        a('    """カタログ寸法 W×D×H の簡易3Dボックス（シンボル取込不可時の代替）"""')
        a('    vs.BeginXtrd(z0, z0 + h)')
        a('    vs.Rect((cx - w / 2.0 + OX, cy + d / 2.0 + OY), '
          '(cx + w / 2.0 + OX, cy - d / 2.0 + OY))')
        a('    vs.EndXtrd()')
        a('    box = vs.LNewObj()')
        a('    if angle:')
        a('        vs.HRotate(box, (cx + OX, cy + OY), angle)')
        a('    paint_white(box)   # 家具は全て白塗り')
        a('')
        a('_fb_count = [0]')
        a('')
        a("# 家具の個別調整: 番号をキーに dx/dy(mm)・angle を上書きして再実行")
        a("# 注意: angle上書き時は原点オフセット(ox,oy)が生成時角度のままのため初期位置が")
        a("# ずれるが、配置後の自動補正（_fix_to_center）が中心を目標へ寄せ直す")
        a("# 例) FURN_OVERRIDES = { 5: {'dx': 100, 'dy': -50, 'angle': 90} }")
        a('FURN_OVERRIDES = {')
        a('}')
        a('')
        a('def _hbb(h):')
        a('    """GetBBoxの返り値差（2点タプル/4値/3D点）を吸収して (x1,y1,x2,y2) を返す"""')
        a('    r = vs.GetBBox(h)')
        a('    if isinstance(r[0], (tuple, list)):')
        a('        p1, p2 = r')
        a('        return (min(p1[0], p2[0]), min(p1[1], p2[1]),')
        a('                max(p1[0], p2[0]), max(p1[1], p2[1]))')
        a('    bx1, by1, bx2, by2 = r[:4]')
        a('    return (min(bx1, bx2), min(by1, by2), max(bx1, bx2), max(by1, by2))')
        a('')
        a('_furn_stats = {"fix": 0, "max": 0, "unreliable": 0, "residual": 0}')
        a('_placed_syms = []   # (handle, 目標cx, 目標cy, 期待最大寸法) — 第2パス検証用')
        a('')
        a('def _fix_to_center(h, cx, cy, max_dim):')
        a('    """実bbox中心を測って目標中心へ補正。')
        a('    測定サイズが期待の2.5倍超（投影歪み等）や補正量2000mm超は不採用"""')
        a('    try:')
        a('        bx1, by1, bx2, by2 = _hbb(h)')
        a('    except Exception:')
        a('        _furn_stats["unreliable"] += 1')
        a('        return None')
        a('    msize = max(bx2 - bx1, by2 - by1)')
        a('    if msize > max_dim * 2.5 + 200:')
        a('        _furn_stats["unreliable"] += 1   # 測定が信用できない（ビュー等）')
        a('        return None')
        a('    ddx = (cx + OX) - (bx1 + bx2) / 2.0')
        a('    ddy = (cy + OY) - (by1 + by2) / 2.0')
        a('    dist = (ddx * ddx + ddy * ddy) ** 0.5')
        a('    if dist > 2000:')
        a('        _furn_stats["unreliable"] += 1')
        a('        return dist')
        a('    if dist > 5:')
        a('        vs.Move3DObj(h, ddx, ddy, 0)   # シンボルはHMoveでなくMove3DObjが安全')
        a('        _furn_stats["fix"] += 1')
        a('        _furn_stats["max"] = max(_furn_stats["max"], round(dist))')
        a('    return dist')
        a('')
        a('def place_furniture(no, name, cx, cy, angle, w, d, h, z0=0, ox=0, oy=0):')
        a('    ov = FURN_OVERRIDES.get(no, {})')
        a("    cx += ov.get('dx', 0)")
        a("    cy += ov.get('dy', 0)")
        a("    angle = ov.get('angle', angle)")
        a('    if name and ensure_symbol(name):')
        a('        # ox,oy = シンボル原点→フットプリント中心のオフセット（回転適用済み）')
        a('        vs.Symbol(name, (cx - ox + OX, cy - oy + OY), angle)')
        a('        _h = vs.LNewObj()')
        a('        paint_white(_h)   # 家具は全て白塗り')
        a('        _fix_to_center(_h, cx, cy, max(w, d))')
        a('        _placed_syms.append((_h, cx, cy, max(w, d)))')
        a('    else:')
        a('        fallback_box(cx, cy, angle, w, d, h, z0)')
        a('        _fb_count[0] += 1')
        a("    num_label('家具%d' % no, cx, cy)")
        a('')
    a("vs.Layer('3Dモデル')")
    a('# 再実行時は前回の生成物を消してから作り直す（WIN_OVERRIDES調整→再実行で反映）')
    a('_prev = vs.FActLayer()')
    a('while _prev:')
    a('    _nx = vs.NextObj(_prev)')
    a('    vs.DelObject(_prev)')
    a('    _prev = _nx')
    a('')
    a("_align_note = '位置合わせ: 補正なし'")
    if has_exp_bbox:
        a('# ── 図面との自動位置合わせ（セルフチェック） ──')
        a('# 書類内の壁・躯体クラス図形の実bboxを測り、DXF由来の期待bboxとの差で OX/OY を補正。')
        a('# DXF書き出し原点と書類の原点がズレていても、モデルが図面の真上に載る')
        a(f'EXP_BBOX = ({round(_ex1)}, {round(_ey1)}, {round(_ex2)}, {round(_ey2)})')
        a("ALIGN_CLASSES = ('躯体', '壁・建具', '壁', '間仕切')")
        a('_mbox = []')
        a('def _acc_bbox(h):')
        a('    try:')
        a('        r = vs.GetBBox(h)')
        a('        if isinstance(r[0], (tuple, list)):')
        a('            (bx1, by1), (bx2, by2) = r')
        a('        else:')
        a('            bx1, by1, bx2, by2 = r')
        a('        _mbox.append((min(bx1, bx2), min(by1, by2),')
        a('                      max(bx1, bx2), max(by1, by2)))')
        a('    except Exception:')
        a('        pass')
        a('def _lname(h):')
        a('    try:')
        a('        return vs.GetLName(vs.GetLayer(h))')
        a('    except Exception:')
        a("        return '?'")
        a('')
        a('_by_layer = {}')
        a('def _acc_layer(h):')
        a('    try:')
        a('        r = vs.GetBBox(h)')
        a('        if isinstance(r[0], (tuple, list)):')
        a('            (bx1, by1), (bx2, by2) = r')
        a('        else:')
        a('            bx1, by1, bx2, by2 = r')
        a('        ln = _lname(h)')
        a('        b = _by_layer.setdefault(ln, [1e18, 1e18, -1e18, -1e18])')
        a('        b[0] = min(b[0], bx1, bx2); b[1] = min(b[1], by1, by2)')
        a('        b[2] = max(b[2], bx1, bx2); b[3] = max(b[3], by1, by2)')
        a('    except Exception:')
        a('        pass')
        a('try:')
        a('    for _cl in ALIGN_CLASSES:')
        a('        vs.ForEachObject(_acc_layer, "C=\'" + _cl + "\'")')
        a('    # レイヤーごとに測り、DXFの外形サイズと一致するレイヤー（=平面図のレイヤー）で補正する。')
        a('    # 展開図・詳細図など他レイヤーの躯体図形が混ざっていても誤補正しない')
        a('    _ew = EXP_BBOX[2] - EXP_BBOX[0]')
        a('    _eh = EXP_BBOX[3] - EXP_BBOX[1]')
        a('    _best = None')
        a('    for _ln, _b in _by_layer.items():')
        a('        _sw = abs((_b[2] - _b[0]) - _ew)')
        a('        _sh = abs((_b[3] - _b[1]) - _eh)')
        a('        if _sw < 500 and _sh < 500:')
        a('            if _best is None or _sw + _sh < _best[0]:')
        a('                _best = (_sw + _sh, _ln, _b)')
        a('    if _best:')
        a('        _b = _best[2]')
        a('        _dx = ((_b[0] + _b[2]) - (EXP_BBOX[0] + EXP_BBOX[2])) / 2.0')
        a('        _dy = ((_b[1] + _b[3]) - (EXP_BBOX[1] + EXP_BBOX[3])) / 2.0')
        a('        if abs(_dx) > 10 or abs(_dy) > 10:')
        a("            OX += _dx")
        a("            OY += _dy")
        a("            _align_note = '位置合わせ: レイヤー「' + _best[1] + '」に合わせて dx=%d dy=%d を自動補正' % (round(_dx), round(_dy))")
        a('        else:')
        a("            _align_note = '位置合わせ: ズレなし（レイヤー「' + _best[1] + '」で確認）'")
        a('    elif _by_layer:')
        a("        _align_note = '位置合わせ: ⚠躯体クラスは見つかったが外形サイズがDXFと一致するレイヤーが無い'")
        a('    else:')
        a("        _align_note = '位置合わせ: ⚠躯体/壁クラスの図形が書類に見つからない（クラス名を確認）'")
        a('except Exception:')
        a('    pass')
        a('')
    a('# 作業中は計測可能ビューへ（3DビューだとGetBBoxがスクリーン投影bboxになり補正が狂う）')
    a('try:')
    a("    vs.DoMenuTextByName('Standard Views', 1)   # 1=Top/Plan（内部名・日本語版可）")
    a('except Exception:')
    a('    pass')
    a('try:')
    a('    if vs.GetProjection(vs.ActLayer()) != 6:   # 6=Plan投影でなければ')
    a('        vs.SetView(0, 0, 0, 0, 0, 0)           # 3D Top（VW公式Marionetteと同じ前処理）')
    a('except Exception:')
    a('    pass')
    a('')
    a('# 床スラブ')
    a(f'rect({fx1}, {fy1}, {fx2}, {fy2}, FH, -FH)')
    a('')

    # 窓番号の採番（生成順に1から。番号は win() 呼び出しと窓一覧コメントに出る）
    win_counter = [0]
    win_registry = []

    def emit_win(kind, wx1, wy1, wx2, wy2, note):
        win_counter[0] += 1
        n = win_counter[0]
        win_registry.append((n, kind, wx1, wy1, wx2, wy2, note))
        a(f"win({n}, '{kind}', {wx1}, {wy1}, {wx2}, {wy2})   # 窓{n} {note}")

    a(f'# 躯体壁 {len(walls)} 枚（実輪郭）')
    for p in walls:
        a(f'poly([{flat(p["pts"])}], CH)   # {p["layer"]} {p["bbox"]}')
    a('')

    if wall_cut_rects:
        a(f'# 建具・窓で分割した壁 {len(wall_cut_rects)} 片（扉・窓が壁に埋まらないように）')
        for r in wall_cut_rects:
            a(f'rect({r[0]}, {r[1]}, {r[2]}, {r[3]}, CH)   # 分割壁')
        a('')

    if wall_win_cuts or insul_win_cuts:
        a(f'# 窓正面の壁・断熱の穴あき（窓サイズ: WIN_OVERRIDESのsill/headに連動）')
        for idx, r in wall_win_cuts:
            a(f'win_wall_rects({idx + 1}, {r[0]}, {r[1]}, {r[2]}, {r[3]})'
              f'   # 窓{idx + 1} 穴上下（壁）')
        for idx, r in insul_win_cuts:
            a(f'win_wall_rects({idx + 1}, {r[0]}, {r[1]}, {r[2]}, {r[3]})'
              f'   # 窓{idx + 1} 穴上下（断熱）')
        a('')

    if frames or frame_quads:
        a(f'# 躯体外周壁 {len(frames) + len(frame_quads)} 片'
          f'（壁のラインをそのまま垂直面として立ち上げ・厚みなし）')
        for f in frames:
            a(f'wall_sheet({f[0]}, {f[1]}, {f[2]}, {f[3]}, CH)   # 外周壁ライン')
        for q in frame_quads:
            a(f'wall_sheet({q[0]}, {q[1]}, {q[2]}, {q[3]}, CH)   # 外周壁ライン(斜め)')
        a('')

    if any(sash_strips.values()):
        a('# 窓開口の上下壁ライン（壁に窓サイズの穴: WIN_OVERRIDESのsill/headに連動）')
        for idx, segs in sash_strips.items():
            for sgm in segs:
                a(f'win_wall_strips({idx + 1}, {sgm[0]}, {sgm[1]}, {sgm[2]}, {sgm[3]})'
                  f'   # 窓{idx + 1} の穴上下')
        a('')

    if heal_rects:
        a(f'# 補完壁 {len(heal_rects)} 本'
          f'（図面に有るのに未生成だった壁線を、ラインのまま垂直面で補完）')
        for r in heal_rects:
            a(f'wall_sheet({r[0]}, {r[1]}, {r[2]}, {r[3]}, CH)   # 補完壁')
        a('')

    if outline_dropped:
        a(f'# ⚠ 押し出し不可の輪郭線 {len(outline_dropped)} 本（同心ペア無し・中身が詰まるため除外）')
        for p in outline_dropped:
            a(f'#   除外: {p["layer"]} {p["bbox"]}')
        a('')

    if part_walls:
        n_seg = sum(len(w['segments']) for w in part_walls)
        n_open = sum(len(w['openings']) for w in part_walls)
        a(f'# 間仕切り内壁 {len(part_walls)} 本（壁片{n_seg} / ドア開口{n_open}）')
        for w in part_walls:
            r = w['rect']
            for s1, s2 in w['segments']:
                if w['horiz']:
                    a(f'rect({s1}, {r[1]}, {s2}, {r[3]}, CH)   # 内壁 t={w["t"]}')
                else:
                    a(f'rect({r[0]}, {s1}, {r[2]}, {s2}, CH)   # 内壁 t={w["t"]}')
            for o1, o2 in w['openings']:
                if w['horiz']:
                    a(f'rect({o1}, {r[1]}, {o2}, {r[3]}, CH - DOOR_HEAD, DOOR_HEAD)   # ドア垂れ壁')
                else:
                    a(f'rect({r[0]}, {o1}, {r[2]}, {o2}, CH - DOOR_HEAD, DOOR_HEAD)   # ドア垂れ壁')
        a('')

    if door_units:
        n_panel = sum(len(u['panels']) for u in door_units)
        a(f'# 建具 {len(door_units)} 箇所（3方枠 + 扉パネル{n_panel}枚は図面の厚みのまま押し出し。'
          f'引き込み戸は戸袋を除いた実開口のみ）')
        for di, u in enumerate(door_units, 1):
            b = u.get('open', u['bbox'])
            pocket = '（戸袋あり・実開口のみ）' if b != u['bbox'] else ''
            a(f'door_frame({di}, {b[0]}, {b[1]}, {b[2]}, {b[3]})   # 建具{di} {u["block"]}{pocket}')
            for p in u['panels']:
                t = min(p[2] - p[0], p[3] - p[1])
                a(f'door_panel({di}, {p[0]}, {p[1]}, {p[2]}, {p[3]})   # 扉パネル t={t}')
        a('')

    if sashes:
        n_haki = sum(1 for s in sashes if s['kind'] == 'hakidashi')
        a(f'# インナーサッシ窓 {len(sashes)} 箇所（幅{HAKI_MIN_W}以上=掃き出し{n_haki} / 未満=腰高{len(sashes) - n_haki}）')
        for s in sashes:
            b = s['bbox']
            kind = 'haki' if s['kind'] == 'hakidashi' else 'hiki'
            emit_win(kind, b[0], b[1], b[2], b[3], f'サッシ {s["block"]} 幅{s["w"]}')
        a('')

    if insul:
        a(f'# 断熱 {len(insul)} 枚（断熱レイヤーのSOLID/閉ポリをそのまま立ち上げ）')
        for it in insul:
            a(f'poly([{flat(it["pts"])}], CH)   # 断熱 {it["bbox"]}')
        a('')

    def emit_split(label, bb, wins):
        sy1, sy2 = bb[1], bb[3]
        x1, x2 = bb[0], bb[2]
        cuts = sorted(set([x1, x2]
                          + [w['x1'] for w in wins if x1 <= w['x1'] <= x2]
                          + [w['x2'] for w in wins if x1 <= w['x2'] <= x2]))
        for i in range(len(cuts) - 1):
            xa, xb = cuts[i], cuts[i + 1]
            mid = (xa + xb) / 2
            if any(w['x1'] <= mid <= w['x2'] for w in wins):
                emit_win('ribbon', xa, sy1, xb, sy2, f'{label} 腰窓')
            else:
                a(f'rect({xa}, {sy1}, {xb}, {sy2}, CH)   # {label} 壁')

    if south_main:
        a('# 南面外壁（建具位置に腰窓）')
        bb = south_main['bbox']
        inb = [w for w in swins if bb[0] - 50 <= w['x1'] and w['x2'] <= bb[2] + 50]
        emit_split('南面', bb, inb)
        for w in swins:
            if not (bb[0] - 50 <= w['x1'] and w['x2'] <= bb[2] + 50):
                emit_win('ribbon', w['x1'], bb[1], w['x2'], bb[3], '南面 腰窓(壁切れ目)')
        a('')

    if north_main:
        a('# 北面外壁（建具位置に腰窓）')
        bb = north_main['bbox']
        inb = [w for w in nwins if bb[0] - 50 <= w['x1'] and w['x2'] <= bb[2] + 50]
        emit_split('北面', bb, inb)
        for w in nwins:
            if not (bb[0] - 50 <= w['x1'] and w['x2'] <= bb[2] + 50):
                emit_win('ribbon', w['x1'], bb[1], w['x2'], bb[3], '北面 腰窓(壁切れ目)')
        a('')

    if west:
        a('# 西面ファサード（腰壁の連続窓）')
        for p in west:
            b = p['bbox']
            emit_win('ribbon', b[0], b[1], b[2], b[3], '西面連続窓')
        a('')

    if ewins:
        a(f'# 東面ファサード（青線FIX窓 {len(ewins)}枚・帯厚は所属壁に吸着）')
        for w in ewins:
            band = _snap_band([w['gx'] - WALL_T // 2, w['y1'],
                               w['gx'] + WALL_T // 2, w['y2']], _snap_bbs)
            emit_win('fix', band[0], band[1], band[2], band[3],
                     f'東面FIX gx={w["gx"]}')
        a('')

    if beams:
        a(f'# 梁 {len(beams)} 本（梁幅{BEAM_W} せい{BEAM_D}）')
        for b in beams:
            a(f'beam({b["x1"]}, {b["y1"]}, {b["x2"]}, {b["y2"]})   # {b["label"]}')
        a('')

    if ceil_beams:
        n_ch = sum(1 for b in ceil_beams if b['bottom'])
        a(f'# 天井梁 {len(ceil_beams)} 本（天井レイヤー/点線ライン。'
          f'梁下=CH≒注記参照 {n_ch}本 / 注記なし {len(ceil_beams) - n_ch}本はBEAM_D下がり）')
        for bi, b in enumerate(ceil_beams, 1):
            note = f'梁下CH≒{b["bottom"]}' if b['bottom'] else 'CH注記なし→BEAM_D'
            a(f'cbeam({bi}, {b["x1"]}, {b["y1"]}, {b["x2"]}, {b["y2"]}, '
              f'{b["bottom"]})   # 天井梁{bi} {note}')
        a('')

    if placed:
        where = 'モデル右横に整列（緑ガイド枠=図面上の本来位置）' if FURN_LINEUP else '図面位置に配置'
        a(f'# 家具 {len(placed)} 件 — {where}'
          f'（MUJIライブラリからシンボル自動インポート。取込不可は W×D×H ボックス代替）')
        import math as _math
        for fi, f in enumerate(placed, 1):
            _a = _math.radians(f['angle'])
            _ox = round(f['cx0'] * _math.cos(_a) - f['cy0'] * _math.sin(_a))
            _oy = round(f['cx0'] * _math.sin(_a) + f['cy0'] * _math.cos(_a))
            a(f"place_furniture({fi}, {f['vw']!r}, {f['x']}, {f['y']}, {f['angle']}, "
              f"{f['bw']}, {f['bd']}, {f['h']}, {f['z0']}, {_ox}, {_oy})"
              f"   # 家具{fi} [{f['cat']}] {f['matched']}  ←{f['via']}照合")
        a('')

    _fw_bbs = [pp['bbox'] for pp in polys] + frames

    def _wall_dist(px, py):
        best = 1e18
        for wb in _fw_bbs:
            ddx = max(wb[0] - px, 0, px - wb[2])
            ddy = max(wb[1] - py, 0, py - wb[3])
            best = min(best, (ddx * ddx + ddy * ddy) ** 0.5)
        return best

    if beds_simple:
        a(f'# ベッド {len(beds_simple)} 台（簡易ボリューム + 枕。シンボルは使わない。枕は壁に近い側=頭側）')
        for bd_ in beds_simple:
            bx1, by1, bx2, by2 = bd_['bbox']
            a(f'rect({bx1}, {by1}, {bx2}, {by2}, BED_H)   # ベッド本体 {bd_["label"]}')
            a(f"num_label('ベッド', {(bx1 + bx2) // 2}, {(by1 + by2) // 2})")
            w_, d_ = bx2 - bx1, by2 - by1
            if w_ >= d_:   # 長辺=X方向
                head_min = _wall_dist(bx1, (by1 + by2) / 2) <= _wall_dist(bx2, (by1 + by2) / 2)
                n_pil = 2 if d_ >= 1150 else 1
                pw = min(PILLOW_W, (d_ - 100 * (n_pil + 1)) / n_pil)
                for i in range(n_pil):
                    cy = by1 + d_ * (2 * i + 1) / (2 * n_pil)
                    px1 = bx1 + 100 if head_min else bx2 - 100 - PILLOW_D
                    a(f'rect({round(px1)}, {round(cy - pw / 2)}, '
                      f'{round(px1 + PILLOW_D)}, {round(cy + pw / 2)}, PILLOW_H, BED_H)   # 枕')
            else:          # 長辺=Y方向
                head_min = _wall_dist((bx1 + bx2) / 2, by1) <= _wall_dist((bx1 + bx2) / 2, by2)
                n_pil = 2 if w_ >= 1150 else 1
                pw = min(PILLOW_W, (w_ - 100 * (n_pil + 1)) / n_pil)
                for i in range(n_pil):
                    cx = bx1 + w_ * (2 * i + 1) / (2 * n_pil)
                    py1 = by1 + 100 if head_min else by2 - 100 - PILLOW_D
                    a(f'rect({round(cx - pw / 2)}, {round(py1)}, '
                      f'{round(cx + pw / 2)}, {round(py1 + PILLOW_D)}, PILLOW_H, BED_H)   # 枕')
        a('')

    if sofas_simple:
        a(f'# ソファ {len(sofas_simple)} 台（座面+背もたれ+脚の簡易ボリューム。背もたれは壁に近い辺）')
        for sf in sofas_simple:
            sx1, sy1, sx2, sy2 = sf['bbox']
            a(f'# ソファ {sf["label"]}')
            # 脚4本（四隅）
            for lx_, ly_ in ((sx1, sy1), (sx2 - SOFA_LEG_W, sy1),
                             (sx1, sy2 - SOFA_LEG_W), (sx2 - SOFA_LEG_W, sy2 - SOFA_LEG_W)):
                a(f'rect({lx_}, {ly_}, {lx_ + SOFA_LEG_W}, {ly_ + SOFA_LEG_W}, SOFA_LEG_H)   # 脚')
            # 座面
            a(f'rect({sx1}, {sy1}, {sx2}, {sy2}, SOFA_SEAT_H - SOFA_LEG_H, SOFA_LEG_H)   # 座面')
            # 背もたれ = 4辺のうち壁に最も近い辺
            edges = [('S', ((sx1 + sx2) / 2, sy1)), ('N', ((sx1 + sx2) / 2, sy2)),
                     ('W', (sx1, (sy1 + sy2) / 2)), ('E', (sx2, (sy1 + sy2) / 2))]
            side = min(edges, key=lambda t: _wall_dist(t[1][0], t[1][1]))[0]
            if side == 'S':
                bk = (sx1, sy1, sx2, sy1 + SOFA_BACK_T)
            elif side == 'N':
                bk = (sx1, sy2 - SOFA_BACK_T, sx2, sy2)
            elif side == 'W':
                bk = (sx1, sy1, sx1 + SOFA_BACK_T, sy2)
            else:
                bk = (sx2 - SOFA_BACK_T, sy1, sx2, sy2)
            a(f'rect({bk[0]}, {bk[1]}, {bk[2]}, {bk[3]}, '
              f'SOFA_BACK_H - SOFA_SEAT_H, SOFA_SEAT_H)   # 背もたれ')
        a('')

    if boxed:
        a(f'# 該当なし家具 {len(boxed)} 件（無理にシンボルは当てず図面実寸の簡易ボリューム 高さFURN_BOX_H）')
        for bi2, f in enumerate(boxed, len(placed) + 1):
            tag = f"INSERT '{f['name']}'" if f['kind'] == 'insert' \
                else 'フットプリント'
            # w,d はワールド寸法なので回転させない（回転すると転置の二重適用になる）
            a(f"place_furniture({bi2}, None, {f['x']}, {f['y']}, 0, "
              f"{f['w']}, {f['d']}, FURN_BOX_H)"
              f"   # 家具{bi2} 該当なし {tag} W{f['w']}xD{f['d']}")
        a('')

    if unmatched:
        a(f'# 未マッチ家具 {len(unmatched)} 件（カタログ該当なし・寸法も取得不能。手動配置してください）')
        for f in unmatched:
            tag = f"INSERT '{f['name']}'" if f['kind'] == 'insert' \
                else f"フットプリント W{f['w']}xD{f['d']}"
            a(f"#   未配置: {tag}  at ({f['x']}, {f['y']})")
        a('')

    if ceilings:
        n_known = sum(1 for c in ceilings if c['known'])
        a(f'# 部屋天井 {len(ceilings)} 室（CH注記に追従 {n_known}室 / '
          f'注記なし {len(ceilings) - n_known}室は最頻値{CH}・厚{CEIL_T}）')
        for c in ceilings:
            tag = '' if c['known'] else '（CH不明→最頻値）'
            for r in c['rects']:
                # +2mm: 梁下=天井高の梁と底面が同一平面になるZファイトを避ける
                a(f'rect({r[0]}, {r[1]}, {r[2]}, {r[3]}, {CEIL_T}, {c["v"] + 2})'
                  f'   # 天井 {c["label"]}{tag}')
        a('')

    if balconies:
        a(f'# バルコニー {len(balconies)} 箇所（床 + 手すり壁H{PARAPET_H}）')
        for bcn in balconies:
            for r in bcn['rects']:
                a(f'rect({r[0]}, {r[1]}, {r[2]}, {r[3]}, 100, -100)'
                  f'   # バルコニー床 {bcn["label"]}')
            for r in bcn['parapets']:
                a(f'rect({r[0]}, {r[1]}, {r[2]}, {r[3]}, {PARAPET_H})'
                  f'   # 手すり壁')
        a('')

    if guide_segs:
        a('# ─── 図面ガイド線（照合用オーバーレイ・赤）。不要なら冒頭の DRAW_GUIDE=False ───')
        a('if DRAW_GUIDE:')
        a('    try:')
        a('        _prev_cls = vs.ActiveClass()')
        a("        vs.NameClass('ガイド線')   # 専用クラス（VWのクラス表示OFFで消せる）")
        a('        vs.PenFore((65535, 0, 0))')
        a('    except Exception:')
        a('        _prev_cls = None')
        a('    for _g1, _g2, _g3, _g4 in GUIDE_SEGS:')
        a('        vs.MoveTo(_g1 + OX, _g2 + OY)')
        a('        vs.Line(_g3 - _g1, _g4 - _g2)')
        a('    try:')
        a('        vs.PenFore((0, 45000, 0))   # 家具フットプリント枠は緑')
        a('    except Exception:')
        a('        pass')
        a('    for _f1, _f2, _f3, _f4 in GUIDE_FURN:')
        a('        vs.MoveTo(_f1 + OX, _f2 + OY)')
        a('        vs.Line(_f3 - _f1, 0)')
        a('        vs.Line(0, _f4 - _f2)')
        a('        vs.Line(_f1 - _f3, 0)')
        a('        vs.Line(0, _f2 - _f4)')
        a('        _fcx = (_f1 + _f3) / 2.0 + OX')
        a('        _fcy = (_f2 + _f4) / 2.0 + OY')
        a('        vs.MoveTo(_fcx - 100, _fcy)   # 目標中心の十字')
        a('        vs.Line(200, 0)')
        a('        vs.MoveTo(_fcx, _fcy - 100)')
        a('        vs.Line(0, 200)')
        a('    try:')
        a('        vs.PenFore((0, 0, 0))')
        a('        if _prev_cls:')
        a('            vs.NameClass(_prev_cls)')
        a('    except Exception:')
        a('        pass')
        a('')

    if placed:
        a('# ─── 家具の第2パス検証: 全配置後に再測定し、残差があれば再補正 ───')
        a('for _h, _cx, _cy, _md in _placed_syms:')
        a('    _dist = _fix_to_center(_h, _cx, _cy, _md)')
        a('    if _dist is not None and _dist > 50:')
        a('        _furn_stats["residual"] += 1')
        a('')

    if win_registry:
        a('# ─── 窓一覧（WIN_OVERRIDES のキー = この窓番号） ───')
        for n, kind, wx1, wy1, wx2, wy2, note in win_registry:
            a(f'#   窓{n}: {kind}  ({wx1},{wy1})-({wx2},{wy2})  {note}')
        a('')

    a('# 仕上げ: アイソメビューへ（配置・補正・テキストはすべてTop系ビューで完了済み）')
    a('try:')
    a("    vs.DoMenuTextByName('Standard Views', 8)   # 8=Right Isometric")
    a('except Exception:')
    a('    try:')
    a('        vs.SetView(-45, -35.264, -30, 0, 0, 0)')
    a('    except Exception:')
    a('        pass')
    a('')

    # ── VW内 実測セルフチェック: モデル外周が「どこに描かれたか」を測って期待位置と比較 ──
    a('# ── 実測セルフチェック: モデル外周の実際の位置を測る ──')
    a("_ring_note = '外周実測: 対象なし'")
    a('if _RING_HS:')
    a('    _rx = []')
    a('    _ry = []')
    a('    for _rh in _RING_HS:')
    a('        try:')
    a('            _b1, _b2, _b3, _b4 = _hbb(_rh)')
    a('            _rx += [_b1, _b3]')
    a('            _ry += [_b2, _b4]')
    a('        except Exception:')
    a('            pass')
    a('    if _rx:')
    a(f'        _exp_ring = ({fx1} + OX, {fy1} + OY, {fx2} + OX, {fy2} + OY)')
    a('        _rdx = ((min(_rx) + max(_rx)) - (_exp_ring[0] + _exp_ring[2])) / 2.0')
    a('        _rdy = ((min(_ry) + max(_ry)) - (_exp_ring[1] + _exp_ring[3])) / 2.0')
    a('        if abs(_rdx) > 10 or abs(_rdy) > 10:')
    a("            _ring_note = '⚠外周実測: 期待位置から dx=%d dy=%d ずれて描画（VW側要因）' % (round(_rdx), round(_rdy))")
    a('        else:')
    a("            _ring_note = '外周実測: 期待位置に一致（ズレなし）'")
    a('')

    n_pseg = sum(len(w['segments']) for w in part_walls)
    rooms_line = 'OK 基本6室ラベルあり' if not rooms_missing \
        else '未検出: ' + '/'.join(rooms_missing)
    ent_line = '玄関扉あり' if entrance else '⚠玄関扉が検出できず'
    a('vs.AlrtDialog(')
    a(f"    '3D モデル生成完了\\n\\n'")
    a(f"    '躯体壁 {len(walls)} / 内壁 {len(part_walls)} / 外周帯 {len(frames)}\\n'")
    a(f"    '建具 {len(door_units)}（{ent_line}） / サッシ窓 {len(sashes)} / 断熱 {len(insul)}\\n'")
    a(f"    '窓 {len(win_registry)} / 天井梁 {len(ceil_beams)} / 天井 {len(ceilings)}室 / バルコニー {len(balconies)}\\n'")
    a(f"    '南窓 {len(swins)} / 北窓 {len(nwins)} / 東FIX {len(ewins)}\\n'")
    a(f"    '梁 {len(beams)} / 家具 配置{len(placed)} ベッド{len(beds_simple)} ソファ{len(sofas_simple)} 簡易{len(boxed)} 未マッチ{len(unmatched)}\\n'")
    a(f"    '天井高 {CH}mm" + (' (図面検出)' if ch_detected else ' (ユーザー指定)') + "\\n'")
    a(f"    '部屋: {rooms_line}'")
    a("    + '\\n' + _align_note")
    a("    + '\\n' + _ring_note")
    if placed:
        a("    + '\\n家具ボックス代替 ' + str(_fb_count[0]) + ' 件（シンボル取込不可分）'")
        a("    + '\\n家具位置補正 ' + str(_furn_stats['fix']) + '件(最大' + str(_furn_stats['max'])")
        a("    + 'mm) / 計測不能 ' + str(_furn_stats['unreliable']) + ' / 残差50mm超 '")
        a("    + str(_furn_stats['residual'])")
    a(')')
    a('')

    summary = {
        'origin': [xo, yo],
        'grid': [gx_max, gy_max],
        'ch': CH,
        'ch_detected': bool(ch_detected),
        'ch_values': [[v, n] for v, n in ch_values],
        'rooms_found': rooms_found,
        'rooms_missing': rooms_missing,
        'walls': len(walls),
        'partition_walls': len(part_walls),
        'partition_segments': n_pseg,
        'outline_frames': len(frames),
        'outline_dropped': len(outline_dropped),
        'heal_walls': len(heal_rects),
        'ceilings': len(ceilings),
        'rooms_no_ch': rooms_no_ch,
        'balconies': len(balconies),
        'doors': len(door_units),
        'door_panels': sum(len(u['panels']) for u in door_units),
        'entrance': bool(entrance),
        'sashes': len(sashes),
        'insulation': len(insul),
        'windows': len(win_registry),
        'window_list': [{'no': n, 'kind': k, 'bbox': [a1, b1, a2, b2], 'note': nt}
                        for n, k, a1, b1, a2, b2, nt in win_registry],
        'ceil_beams': len(ceil_beams),
        'south_win': len(swins),
        'north_win': len(nwins),
        'east_fix': len(ewins),
        'west_ribbon': len(west),
        'beams': len(beams),
        'furniture': len(placed),
        'furniture_boxed': len(boxed),
        'furniture_unmatched': len(unmatched),
        'beds': len(beds_simple),
        'sofas': len(sofas_simple),
        'check': check,
        'bbox': [fx1, fy1, fx2, fy2],
    }
    return '\n'.join(L), summary


def generate(dxf_path, out_path, ch=None):
    """CLI 用：スクリプトをファイルに書き出す"""
    overrides = {'CH': ch} if ch else None
    script, s = build_script(dxf_path, overrides)
    Path(out_path).write_text(script, encoding='utf-8')
    print(f'[DONE] → {out_path}')
    print(f"  検出原点: x={s['origin'][0]}, y={s['origin'][1]}")
    if s['ch_detected']:
        vals = ', '.join(f"{v}mm×{n}" for v, n in s['ch_values'])
        print(f"  天井高: {s['ch']}mm（図面検出: {vals}）")
    else:
        print(f"  天井高: {s['ch']}mm（ユーザー指定）")
    if s['rooms_found']:
        print('  部屋ラベル: ' + ' / '.join(f"{k}✓" for k in s['rooms_found']))
    if s['rooms_missing']:
        print(f"  ⚠ ラベル未検出: {' / '.join(s['rooms_missing'])}")
    print(f"  躯体壁 {s['walls']} / 内壁 {s['partition_walls']} / 外周帯 {s['outline_frames']}")
    print(f"  建具 {s['doors']}（扉パネル{s['door_panels']}・玄関扉{'あり' if s['entrance'] else '⚠なし'}） / サッシ窓 {s['sashes']} / 断熱 {s['insulation']}")
    print(f"  窓 {s['windows']}箇所 / 天井梁 {s['ceil_beams']}（番号調整: WIN/DOOR/BEAM_OVERRIDES）")
    print(f"  部屋天井 {s['ceilings']}室 / バルコニー {s['balconies']}"
          + (f" / ⚠CH不明: {'・'.join(s['rooms_no_ch'])}" if s['rooms_no_ch'] else ''))
    print(f"  家具 配置{s['furniture']} / ベッド{s['beds']}（簡易+枕） / ソファ{s['sofas']}（座面+背+脚） / 簡易ボリューム{s['furniture_boxed']} / 未マッチ{s['furniture_unmatched']}")
    print(f"  南窓 {s['south_win']} / 北窓 {s['north_win']} / 東FIX窓 {s['east_fix']} / 西面連続窓 {s['west_ribbon']}")
    print(f"  建物範囲 {s['bbox']}")
    chk = s.get('check') or {}
    if chk.get('recall') is not None:
        off = f" / 系統ズレ {chk['offset']}" if chk.get('offset') else ' / 系統ズレなし'
        print(f"  セルフチェック: 壁再現率 {chk['recall']:.0%} / 適合率 {chk['precision']:.0%}{off}"
              f" / 補完壁 {s.get('heal_walls', 0)}本")
    for wmsg in chk.get('warnings', []):
        print(f"  ⚠ {wmsg}")


def main():
    args = list(sys.argv[1:])
    ch = None
    if '--ch' in args:
        i = args.index('--ch')
        try:
            ch = int(args[i + 1])
        except (IndexError, ValueError):
            print('エラー: --ch には整数(mm)を指定してください（例: --ch 2400）',
                  file=sys.stderr)
            sys.exit(2)
        del args[i:i + 2]
    if not args:
        print('使い方: python3 build_3d_model.py <input.dxf> [output_vw.py] [--ch <mm>]')
        sys.exit(1)
    dxf = args[0]
    out = args[1] if len(args) > 1 else str(Path(dxf).with_suffix('')) + '_model_vw.py'
    try:
        generate(dxf, out, ch)
    except CeilingHeightRequired as e:
        if sys.stdin.isatty():
            print(f'\n{e}')
            for _ in range(3):
                raw = input('天井高CH(mm)を入力してください（例: 2400）: ').strip()
                try:
                    v = int(raw)
                except ValueError:
                    print('整数(mm)で入力してください。')
                    continue
                if not (1800 <= v <= 6000):
                    print('1800〜6000mmの範囲で入力してください。')
                    continue
                generate(dxf, out, v)
                return
            sys.exit(2)
        else:
            print(f'エラー: {e} --ch <mm> を指定してください', file=sys.stderr)
            sys.exit(2)


if __name__ == '__main__':
    main()
