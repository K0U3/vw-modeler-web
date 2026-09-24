"""Reusable detailed plan symbols: pose, ambiguity, and non-rectangle evidence."""
import math
import unittest
import ezdxf
from test_furniture_placement import engine
from furniture_shapes import describe_insert, match_shape, expand_shape_matches


class ShapeTests(unittest.TestCase):
    def make_symbol(self, name, position=(0,0), angle=0):
        doc=ezdxf.new()
        block=doc.blocks.new(name)
        block.add_lwpolyline([(0,0),(600,0),(600,800),(0,800)],close=True)
        block.add_line((0,120),(600,120))
        block.add_circle((110,200),45)
        return doc.modelspace().add_blockref(name,position,dxfattribs={'rotation':angle})

    def test_renamed_translated_rotated_symbol_keeps_type_and_pose(self):
        reference=self.make_symbol('source')
        symbol=engine.load_catalog()[0].get('vw_name') or engine.load_catalog()[0]['block']
        template={'shape':describe_insert(reference),'parts':[{'symbol':symbol,'dx':100,'dy':0,'angle':90}]}
        renamed=self.make_symbol('unrelated-name',(19000,-7000),37)
        shape=describe_insert(renamed)
        self.assertEqual(match_shape(shape,[template]),(template['parts'],0))
        from unittest.mock import patch
        with patch('furniture_shapes.load_templates',return_value=[template]):
            items=expand_shape_matches([{'name':'unrelated-name','x':19000,'y':-7000,'angle':37,'shape':shape}],engine.load_catalog())
        self.assertEqual(len(items),1)
        self.assertAlmostEqual(items[0]['x'],19000+100*math.cos(math.radians(37)))
        self.assertAlmostEqual(items[0]['y'],-7000+100*math.sin(math.radians(37)))
        self.assertEqual(items[0]['angle'],127)
        self.assertEqual(items[0]['name'],symbol)

    def test_outline_alone_and_reflection_are_not_guessed(self):
        e=self.make_symbol('test')
        e.dxf.xscale=-1
        self.assertIsNone(describe_insert(e))
        doc=ezdxf.new();b=doc.blocks.new('rectangle')
        b.add_lwpolyline([(0,0),(600,0),(600,800),(0,800)],close=True)
        self.assertIsNone(describe_insert(doc.modelspace().add_blockref('rectangle',(0,0))))

    def test_same_dimensions_different_details_and_conflicting_parts_rejected(self):
        e=self.make_symbol('test');shape=describe_insert(e)
        template={'shape':shape,'parts':[{'symbol':'chair'}]}
        competing={'shape':shape,'parts':[{'symbol':'fridge'}]}
        self.assertIsNone(match_shape(shape,[template,competing]))
        doc=ezdxf.new();b=doc.blocks.new('different')
        b.add_lwpolyline([(0,0),(600,0),(600,800),(0,800)],close=True)
        b.add_line((0,400),(600,400));b.add_circle((300,600),90)
        different=describe_insert(doc.modelspace().add_blockref('different',(0,0)))
        self.assertIsNone(match_shape(different,[template]))

if __name__=='__main__':unittest.main()
