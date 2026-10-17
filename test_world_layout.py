import os
from pathlib import Path
import tempfile
import unittest
import world_layout as layout


class LayoutTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.world=Path(self.temp.name)/'world';self.world.mkdir()
    def region(self,base='.',name='r.0.0.mca'):
        p=self.world/base/'region';p.mkdir(parents=True,exist_ok=True);(p/name).write_bytes(b'fixture')
    def scan(self,*args,**kwargs):return layout.discover(self.world,*args,**kwargs)
    def test_standard_and_empty_dimensions_readonly(self):
        self.region();self.region('DIM-1');self.region('DIM1');self.region('DIM-28','r.-2.3.mca')
        (self.world/'DIM5').mkdir()
        before={str(p):p.read_bytes() for p in self.world.rglob('*') if p.is_file()}
        result=self.scan();self.assertEqual([d['key'] for d in result['dimensions']],['-28','-1','0','1','5'])
        self.assertEqual(result['warnings'],[]);self.assertFalse(result['dimensions'][-1]['regionDirectoryExists'])
        self.assertEqual(before,{str(p):p.read_bytes() for p in self.world.rglob('*') if p.is_file()})
        self.assertEqual(result,self.scan())
    def test_archives_and_invalid_numeric_names_not_selected(self):
        for name in ('DIM1-smazano','DIMx','DIM01','DIM-0','DIM0','DIM2147483648'):self.region(name)
        result=self.scan();self.assertEqual([d['key'] for d in result['dimensions']],['0'])
        self.assertEqual(len(result['warnings']),6)
    def test_nonstandard_requires_explicit_mapping(self):
        self.region('TROPICS')
        self.assertEqual(len(self.scan()['dimensions']),1)
        result=self.scan([dict(key='x:tropics',path='TROPICS')])
        self.assertEqual(result['dimensions'][1]['regionPath'],'TROPICS/region')
        self.assertEqual(result['dimensions'][1]['regions'],1)
        with self.assertRaises(layout.LayoutError):self.scan([dict(key='42',path='TROPICS')])
    def test_duplicate_keys_and_path_aliases(self):
        self.region()
        for mappings in ([dict(key='0',path='.')]*2,[dict(key='x:alias',path='.')]):
            with self.assertRaises(layout.LayoutError):self.scan(mappings)
        self.assertEqual(self.scan([dict(key='0',path='.')])['dimensions'][0]['source'],'explicit')
    def test_reject_path_traversal_absolute_and_windows_paths(self):
        for path in ('..','/tmp','C:/tmp','a/../b','a//b','a\\b','a/','./a',''):
            with self.subTest(path=path),self.assertRaises(layout.LayoutError):self.scan([dict(key='x:test',path=path)])
    def test_key_and_mapping_schema(self):
        for key in ('','01','-0','x:','x:a/b','x:space here',False,2):
            with self.subTest(key=key),self.assertRaises(layout.LayoutError):self.scan([dict(key=key,path='.')])
        with self.assertRaises(layout.LayoutError):self.scan({'0':'.'})
        with self.assertRaises(layout.LayoutError):self.scan([dict(key='0',path='.',extra=True)])
    def test_missing_or_non_directory_and_non_file_region(self):
        with self.assertRaises(layout.LayoutError):self.scan([dict(key='x:no',path='missing')])
        (self.world/'DIM5').write_bytes(b'x')
        with self.assertRaises(layout.LayoutError):self.scan()
        (self.world/'DIM5').unlink();self.region();(self.world/'region/r.1.0.mca').mkdir()
        with self.assertRaises(layout.LayoutError):self.scan()
    def test_entry_budget_and_noncanonical_region_warning(self):
        self.region();self.region(name='r.01.0.mca');self.region(name='notes.txt')
        result=self.scan();self.assertEqual(result['dimensions'][0]['regions'],1);self.assertEqual(len(result['warnings']),2)
        with self.assertRaises(layout.LayoutError):self.scan(max_entries=2)
        for n in (True,0,1000001):
            with self.assertRaises(layout.LayoutError):self.scan(max_entries=n)
    @unittest.skipUnless(os.name=='posix','POSIX symlinks')
    def test_linked_dimension_and_nested_custom_path(self):
        outside=Path(self.temp.name)/'outside';outside.mkdir()
        (self.world/'DIM-1').symlink_to(outside,target_is_directory=True)
        with self.assertRaises(layout.LayoutError):self.scan()
        (self.world/'DIM-1').unlink();(self.world/'custom').symlink_to(outside,target_is_directory=True)
        with self.assertRaises(layout.LayoutError):self.scan([dict(key='x:test',path='custom/nested')])
    @unittest.skipUnless(os.name=='posix','POSIX symlinks')
    def test_linked_region_directory_or_file(self):
        outside=Path(self.temp.name)/'outside';outside.mkdir()
        (self.world/'region').symlink_to(outside,target_is_directory=True)
        with self.assertRaises(layout.LayoutError):self.scan()
        (self.world/'region').unlink();self.region()
        (self.world/'region/r.1.0.mca').symlink_to(self.world/'region/r.0.0.mca')
        with self.assertRaises(layout.LayoutError):self.scan()


if __name__=='__main__':unittest.main()
