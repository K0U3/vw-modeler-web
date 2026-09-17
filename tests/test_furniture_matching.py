import ast
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from test_furniture_placement import engine, make_plan
import ezdxf


class FurnitureMatchingTests(unittest.TestCase):
    def test_name_keeps_dimensions_and_prefers_exact(self):
        c=engine.load_catalog()
        for name in ['木製テーブルW1400','木製テーブルW1800','冷蔵庫中','冷蔵庫大',
                     '箱型キッチン浅型2100','組合わせキッチン2100',
                     'マットレスベッド_シングル','木製デスク1100_550']:
            hit,score=engine.match_by_name(name,c)
            self.assertEqual(hit['name'],name)
            self.assertEqual(score,100)
        hit,_=engine.match_by_name('木製テーブルW1500',c)
        self.assertIsNone(hit)

    def test_every_library_identifier_roundtrips(self):
        c=engine.load_catalog()
        for part in c:
            hit,_=engine.match_by_name(part.get('vw_name') or part['block'],c)
            self.assertEqual(hit,part,part['name'])

    def test_identical_sizes_require_choice(self):
        c=[{'name':'冷蔵庫A','block':'A','category':'家電','w':600,'d':630},
           {'name':'収納棚','block':'B','category':'収納','w':600,'d':630}]
        self.assertIsNone(engine.match_by_size(600,630,c)[0])
        f={'name':'冷蔵庫','kind':'insert','w':600,'d':630,'local_w':600,'local_d':630,'angle':180}
        hit,review,_,angle=engine.furniture_match(f,c)
        self.assertEqual(hit['name'],'冷蔵庫A')
        self.assertEqual(angle,180)
        self.assertIn('要確認',review)

    def test_dimension_tolerance_applies_to_each_edge(self):
        c=[{'name':'fixture','w':1000,'d':100,'category':'その他'}]
        self.assertIsNone(engine.match_by_size(1000,200,c)[0])

    def test_source_rotation_survives_size_matching(self):
        c=[{'name':'オーク材チェア','block':'chair','category':'チェア','w':440,'d':496}]
        for angle in [0,90,180,270,35]:
            d=ezdxf.new(); b=d.blocks.new('anonymous')
            b.add_lwpolyline([(0,0),(440,0),(440,496),(0,496)],close=True)
            d.modelspace().add_blockref('anonymous',(3000,5000),dxfattribs={'layer':'家具','rotation':angle})
            f=engine.extract_furniture(d,0,0)[0]
            self.assertEqual((f['local_w'],f['local_d']),(440,496))
            hit,_,_,got=engine.furniture_match(f,c)
            self.assertIsNotNone(hit)
            self.assertEqual(got,angle)

    def test_mirror_and_explicit_unknown_model_need_review(self):
        for name,mirror in [('冷蔵庫大',True),('木製テーブルW1500',False),('箱型2100×650',False)]:
            f={'name':name,'w':1400,'d':800,'angle':0,'mirrored':mirror}
            hit,review,_,_=engine.furniture_match(f,engine.load_catalog())
            self.assertIsNone(hit)
            self.assertIn('要確認',review)

    def test_runtime_replacement_rotation_and_skip(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'test.dxf'; make_plan(p)
            script,_=engine.build_script(p,{'CH':2400})
        fn=next(n for n in ast.walk(ast.parse(script)) if isinstance(n,ast.FunctionDef) and n.name=='place_furniture')
        env={'vs':SimpleNamespace(Symbol=Mock(),LNewObj=Mock(return_value='handle')),
             'FURN_OVERRIDES':{},'OX':1000,'OY':2000,'ensure_symbol':lambda n:True,
             'paint_white':lambda h:None,'_fix_to_center':lambda *a:None,
             '_placed_syms':[],'fallback_box':Mock(),'_fb_count':[0],'num_label':Mock()}
        exec(compile(ast.Module(body=[fn],type_ignores=[]),'runtime','exec'),env)
        env['FURN_OVERRIDES'][1]={'angle':180,'dx':50,'dy':-50}
        env['place_furniture'](1,'chair',3000,4000,0,440,496,760,0,100,20)
        name,(x,y),angle=env['vs'].Symbol.call_args.args
        self.assertAlmostEqual(x,4150); self.assertAlmostEqual(y,5970)
        self.assertEqual(angle,180)
        env['FURN_OVERRIDES'][1]={'angle':90,'part':{'name':'new part ','w':600,'d':800,'h':900,'z0':0,'cx':10,'cy':20}}
        env['place_furniture'](1,None,3000,4000,0,100,100,700)
        name,(x,y),angle=env['vs'].Symbol.call_args.args
        self.assertEqual(name,'new part ')
        self.assertAlmostEqual(x,4020); self.assertAlmostEqual(y,5990)
        env['FURN_OVERRIDES'][1]={'skip':True};env['vs'].Symbol.reset_mock()
        env['place_furniture'](1,'chair',0,0,0,440,496,760)
        env['vs'].Symbol.assert_not_called()

if __name__=='__main__':unittest.main()
