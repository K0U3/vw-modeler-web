"""DXF → generated VW calls: layout, rotation, fallback, and request isolation."""
import ast
import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import ezdxf

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
import build_3d_model as engine
from main import app


def make_plan(path, furniture=True, layer='家具01'):
    doc = ezdxf.new('R2010')
    ms = doc.modelspace()
    # Nonzero origin exercises drawing-to-model coordinate normalization.
    for x1, y1, x2, y2 in [(10000, 20000, 20000, 20200),
                           (10000, 27800, 20000, 28000),
                           (10000, 20000, 10200, 28000),
                           (19800, 20000, 20000, 28000)]:
        ms.add_lwpolyline([(x1,y1),(x2,y1),(x2,y2),(x1,y2)],
                          close=True, dxfattribs={'layer':'躯体'})
    ms.add_text('CH2400', dxfattribs={'insert':(11000,27000),'height':100})
    expected = {}
    if furniture:
        cat = engine.load_catalog()
        for category, pos, angle in [('テーブル', (13000,23000), 90),
                                     ('ソファ', (17000,23000), 0),
                                     ('ベッド', (17000,26000), 0)]:
            item = next(it for it in cat if it['category'] == category)
            w, d = item.get('w_geo') or item['w'], item.get('d_geo') or item['d']
            blk = doc.blocks.new(item['block'])
            blk.add_lwpolyline([(-w/2,-d/2),(w/2,-d/2),(w/2,d/2),(-w/2,d/2)], close=True)
            ms.add_blockref(item['block'],pos,dxfattribs={'layer':layer,'rotation':angle})
            expected[category] = pos
        # Deliberately outside the catalog dimension tolerance.
        ms.add_lwpolyline([(12000,25000),(12110,25000),(12110,26400),(12000,26400)],
                          close=True,dxfattribs={'layer':layer})
        expected['box'] = (12055,25700)
    doc.saveas(path)
    return expected


def calls(script, name):
    tree = ast.parse(script)
    return [n.value for n in tree.body[1].body
            if isinstance(n,ast.Expr) and isinstance(n.value,ast.Call)
            and isinstance(n.value.func,ast.Name) and n.value.func.id == name]


async def request(fields, data):
    boundary = 'furniture-test-boundary'
    body = b''
    for key, value in fields.items():
        body += (f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n').encode()
    body += (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="plan.dxf"\r\n'
             'Content-Type: application/octet-stream\r\n\r\n').encode() + data
    body += f'\r\n--{boundary}--\r\n'.encode()
    messages = []
    async def receive():
        return {'type':'http.request','body':body,'more_body':False}
    async def send(message):
        messages.append(message)
    scope = {'type':'http','asgi':{'version':'3.0'},'http_version':'1.1',
             'method':'POST','scheme':'http','path':'/generate','raw_path':b'/generate',
             'query_string':b'', 'root_path':'',
             'headers':[(b'content-type',f'multipart/form-data; boundary={boundary}'.encode())],
             'client':('127.0.0.1',1234),'server':('test',80)}
    await app(scope,receive,send)
    status = next(m['status'] for m in messages if m['type']=='http.response.start')
    result = json.loads(b''.join(m.get('body',b'') for m in messages if m['type']=='http.response.body'))
    return status,result


class FurniturePlacementTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)/'plan.dxf'
        self.expected = make_plan(self.path)

    def generate(self, **overrides):
        script,summary = engine.build_script(self.path, {'CH':2400,**overrides})
        compile(script,'generated.py','exec')
        return script,summary

    def test_default_places_all_types_in_drawing(self):
        script,s = self.generate()
        self.assertEqual(s['furniture_mode'],'plan')
        self.assertEqual((s['furniture'],s['beds'],s['sofas'],s['furniture_boxed']),(1,1,1,1))
        self.assertEqual(s['furniture_total'],4)
        ox,oy = s['origin']
        furniture = calls(script,'place_furniture')
        table = next(c for c in furniture if ast.literal_eval(c.args[1]) is not None)
        self.assertEqual(tuple(ast.literal_eval(a) for a in table.args[2:5]),
                         (13000-ox,23000-oy,90))
        box = next(c for c in furniture if ast.literal_eval(c.args[1]) is None)
        self.assertEqual(tuple(ast.literal_eval(a) for a in box.args[2:4]),(12055-ox,25700-oy))
        for category,marker in [('ベッド','ベッド本体'),('ソファ','座面')]:
            line=next(line.strip() for line in script.splitlines() if marker in line and line.strip().startswith('rect('))
            c=ast.parse(line).body[0].value
            x1,y1,x2,y2=[ast.literal_eval(a) for a in c.args[:4]]
            self.assertEqual(((x1+x2)/2+ox,(y1+y2)/2+oy),self.expected[category])

    def test_lineup_moves_every_type_and_next_call_resets(self):
        script,s=self.generate(FURN_LINEUP=True)
        self.assertEqual(s['furniture_mode'],'lineup')
        for c in calls(script,'place_furniture'):
            self.assertGreater(ast.literal_eval(c.args[2]),s['bbox'][2])
        for marker in ['ベッド本体','座面']:
            line=next(line.strip() for line in script.splitlines() if marker in line and line.strip().startswith('rect('))
            self.assertGreater(ast.literal_eval(ast.parse(line).body[0].value.args[0]),s['bbox'][2])
        self.test_default_places_all_types_in_drawing()

    def test_no_furniture(self):
        make_plan(self.path,furniture=False)
        script,s=self.generate()
        self.assertEqual(s['furniture_total'],0)
        self.assertEqual(calls(script,'place_furniture'),[])

    def test_api_modes_validation_and_custom_library(self):
        for fields,expected in [({'furniture_mode':'lineup'},'lineup'),({},'plan'),({'furniture_mode':'plan'},'plan')]:
            status,result=asyncio.run(request({'ch':'2400',**fields},self.path.read_bytes()))
            self.assertEqual(status,200,result)
            self.assertEqual(result['summary']['furniture_mode'],expected)
            compile(result['script'],'api_generated.py','exec')
        status,result=asyncio.run(request({'furniture_mode':'invalid'},self.path.read_bytes()))
        self.assertEqual(status,422)
        with patch.object(engine,'MUJI_LIB',engine.MUJI_LIB),patch.object(engine,'FURNITURE_LAYER',engine.FURNITURE_LAYER):
            make_plan(self.path,layer='FF01')
            status,result=asyncio.run(request({'ch':'2400','furniture_layer':'FF',
                                              'muji_lib':'/custom/library.vwx'},self.path.read_bytes()))
            self.assertEqual(status,200,result)
            self.assertEqual(result['summary']['furniture_total'],4)
            self.assertIn("MUJI_LIB = '/custom/library.vwx'",result['script'])

if __name__=='__main__':
    unittest.main()
