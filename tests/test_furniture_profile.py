"""Drawing-bound, explicitly selected assemblies must survive overlap filtering."""
import asyncio
import copy
import hashlib
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

import ezdxf
from test_furniture_placement import engine, make_plan, request


class FurnitureProfileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'plan.dxf'
        make_plan(self.path, furniture=False)
        doc = ezdxf.readfile(self.path)
        block = doc.blocks.new('anonymous-storage')
        block.add_lwpolyline([(-750,-300),(750,-300),(750,300),(-750,300)],close=True)
        doc.modelspace().add_blockref('anonymous-storage',(15000,24000),dxfattribs={'layer':'家具'})
        doc.saveas(self.path)
        self.catalog = engine.load_catalog()
        self.part = next(it for it in self.catalog if it['name']=='スタッキングシェルフ_2段引出ユニット_')
        self.symbol = self.part.get('vw_name') or self.part['block']
        self.profile = {'version':1,'dxf_sha256':hashlib.sha256(self.path.read_bytes()).hexdigest(),
                        'assignments':{'anonymous-storage':[
                            {'symbol':self.symbol,'dx':dx,'dy':0,'angle':0}
                            for dx in [-562.5,-187.5,187.5,562.5]]}}

    def test_four_adjacent_modules_preserve_plan_center(self):
        script, summary = engine.build_script(self.path,{'CH':2400},self.profile)
        rows = [r for r in summary['furniture_list'] if r['matched']]
        self.assertEqual(len(rows),4)
        self.assertEqual([r['x'] for r in rows],[14437.5,14812.5,15187.5,15562.5])
        self.assertEqual({r['y'] for r in rows},{24000})
        self.assertEqual(sum(r['x'] for r in rows)/4,15000)
        self.assertEqual({r['source'] for r in rows},{'anonymous-storage'})
        self.assertNotIn('def fallback_box',script)
        self.assertEqual(summary['furniture_boxed'],0)
        # Request-local profile does not persist into the next drawing request.
        _, plain = engine.build_script(self.path,{'CH':2400})
        self.assertEqual(plain['furniture'],0)

    def test_builtin_profile_applies_without_upload_and_explicit_takes_precedence(self):
        original_is_file = Path.is_file
        original_read = Path.read_text
        def is_file(path):
            return True if path.parent.name == 'furniture_profiles' else original_is_file(path)
        def read(path, *args, **kwargs):
            if path.parent.name == 'furniture_profiles':
                import json
                return json.dumps(self.profile)
            return original_read(path, *args, **kwargs)
        with patch.object(Path, 'is_file', is_file), patch.object(Path, 'read_text', read):
            _, summary = engine.build_script(self.path, {'CH':2400})
            self.assertEqual(summary['furniture'], 4)
            explicit = copy.deepcopy(self.profile)
            explicit['assignments']['anonymous-storage'] = explicit['assignments']['anonymous-storage'][:1]
            _, summary = engine.build_script(self.path, {'CH':2400}, explicit)
            self.assertEqual(summary['furniture'], 1)

    def test_wrong_drawing_unknown_symbol_and_missing_source_rejected(self):
        for mutate in [lambda p:p.update(dxf_sha256='0'*64),
                       lambda p:p['assignments']['anonymous-storage'][0].update(symbol='unknown'),
                       lambda p:p['assignments'].update({'missing-source':[{'symbol':self.symbol}]})]:
            p=copy.deepcopy(self.profile);mutate(p)
            with self.assertRaises(ValueError):
                engine.apply_furniture_profile([{'name':'anonymous-storage','x':1,'y':2}],self.catalog,p,self.path)

    def test_invalid_coordinates_rejected(self):
        for value in [float('nan'),float('inf'),True,'100',10001]:
            p=copy.deepcopy(self.profile)
            p['assignments']['anonymous-storage'][0]['dx']=value
            with self.assertRaises(ValueError):
                engine.apply_furniture_profile([{'name':'anonymous-storage','x':1,'y':2}],self.catalog,p,self.path)

    def test_profile_uploaded_through_api(self):
        status,result=asyncio.run(request({'ch':'2400'},self.path.read_bytes(),self.profile))
        self.assertEqual(status,200,result)
        self.assertEqual(result['summary']['furniture'],4)
        self.profile['dxf_sha256']='0'*64
        status,result=asyncio.run(request({'ch':'2400'},self.path.read_bytes(),self.profile))
        self.assertFalse(result['ok'])
        self.assertIn('一致しません',result['error'])

if __name__=='__main__':unittest.main()
