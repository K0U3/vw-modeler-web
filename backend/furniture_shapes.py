"""Reusable plan-symbol recognition, independent of drawing name and block IDs.

Only detailed, verified planar symbols qualify. Plain rectangles and ambiguous
matches remain unassigned; dimensions alone are never enough to choose a part.
"""
import json
import math
from functools import lru_cache
from pathlib import Path

from ezdxf.disassemble import recursive_decompose
from ezdxf.path import make_path

GRID = 64


def describe_insert(entity):
    if entity.dxftype() != 'INSERT':
        return None
    if (entity.dxf.xscale <= 0 or entity.dxf.yscale <= 0
            or entity.dxf.extrusion.z < 0):
        return None  # reflection needs independent verification
    local = entity.copy()
    local.dxf.insert = (0, 0, 0)
    local.dxf.rotation = 0
    points, paths, corners = [], 0, 0
    try:
        for primitive in recursive_decompose(local.virtual_entities()):
            if primitive.dxftype() not in ('LINE', 'ARC', 'CIRCLE', 'ELLIPSE',
                                           'SPLINE', 'POLYLINE', 'LWPOLYLINE'):
                continue
            path = make_path(primitive)
            vertices = list(path.flattening(1.0))
            if len(vertices) < 2:
                continue
            paths += 1
            corners += len(vertices)
            for a, b in zip(vertices, vertices[1:]):
                length = math.hypot(b.x-a.x, b.y-a.y)
                steps = max(1, math.ceil(length / 4))
                if len(points) + steps > 50000:
                    return None
                points.extend((a.x+(b.x-a.x)*i/steps, a.y+(b.y-a.y)*i/steps)
                              for i in range(steps+1))
        # A table outline can equally represent storage or a kitchen.
        if not points or (paths <= 1 and corners <= 5):
            return None
        xs, ys = zip(*points)
        w, d = max(xs)-min(xs), max(ys)-min(ys)
        if min(w, d) < 100 or max(w, d) > 3500:
            return None
        cx, cy = (max(xs)+min(xs))/2, (max(ys)+min(ys))/2
        unit = max(w, d) / GRID
        pixels = sorted({(round((x-cx)/unit), round((y-cy)/unit)) for x,y in points})
        return {'w': round(w, 2), 'd': round(d, 2), 'pixels': pixels}
    except (ValueError, TypeError, AttributeError, ZeroDivisionError):
        return None


@lru_cache(maxsize=1)
def load_templates():
    path = Path(__file__).with_name('furniture_shapes.json')
    return json.loads(path.read_text(encoding='utf-8')) if path.is_file() else []


def match_shape(description, templates=None):
    if not description:
        return None
    pixels = {tuple(p) for p in description['pixels']}
    candidates = []
    for template in load_templates() if templates is None else templates:
        for quarter in range(4):
            w, d = template['shape']['w'], template['shape']['d']
            if quarter % 2:
                w, d = d, w
            if max(abs(w-description['w'])/w, abs(d-description['d'])/d) > .03:
                continue
            target = set()
            for x,y in template['shape']['pixels']:
                for _ in range(quarter):
                    x,y = -y,x
                target.add((x,y))
            score = len(target & pixels) / max(1, len(target | pixels))
            if score >= .94:
                candidates.append((score, template, quarter * 90))
    candidates.sort(key=lambda c: -c[0])
    if not candidates:
        return None
    best = candidates[0]
    # Equivalent profiles from repeated chairs are harmless. Competing parts or
    # competing orientations for an asymmetric part must be reviewed.
    def placement_key(candidate):
        _, template, rotation = candidate
        return sorted((p['symbol'], round((p.get('angle',0)+rotation)%360, 2),
                       round(p.get('dx',0)*math.cos(math.radians(rotation))-p.get('dy',0)*math.sin(math.radians(rotation)),2),
                       round(p.get('dx',0)*math.sin(math.radians(rotation))+p.get('dy',0)*math.cos(math.radians(rotation)),2),
                       p.get('mirror_y',False)) for p in template['parts'])
    key = placement_key(best)
    if any(best[0]-c[0] < .03 and placement_key(c) != key for c in candidates[1:]):
        return None
    return best[1]['parts'], best[2]


def expand_shape_matches(items, catalog):
    known = {p.get('vw_name') or p['block'] for p in catalog}
    result = []
    for item in items:
        match = None if item.get('profile_source') else match_shape(item.get('shape'))
        if not match:
            result.append(item)
            continue
        parts, quarter = match
        if any(p['symbol'] not in known for p in parts):
            result.append(item)
            continue
        rotation = item.get('angle',0) + quarter
        c,s = math.cos(math.radians(rotation)), math.sin(math.radians(rotation))
        for part in parts:
            dx,dy = part.get('dx',0),part.get('dy',0)
            result.append({**item, 'name':part['symbol'],
                           'x':item['x']+c*dx-s*dy, 'y':item['y']+s*dx+c*dy,
                           'angle':(rotation+part.get('angle',0))%360,
                           'mirrored':False, 'mirror_y':part.get('mirror_y',False),
                           'profile_source':item.get('name'), 'shape_matched':True})
    # Nested inserts can repeat the same complete symbol. Keep each physical
    # part once while preserving adjacent modules and explicitly supplied parts.
    kept = []
    result.sort(key=lambda f: bool(f.get('shape_matched')))
    for item in result:
        if item.get('shape_matched') and any(
                other.get('name') == item['name']
                and math.hypot(other['x']-item['x'], other['y']-item['y']) < 5
                and abs((other.get('angle',0)-item.get('angle',0)+180)%360-180) < 1
                for other in kept):
            continue
        kept.append(item)
    return kept
