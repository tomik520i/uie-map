import hashlib,json,tempfile,unittest,os
from pathlib import Path
import uiemap_capacity as capacity
import hires_bundle as hb

class CapacityTests(unittest.TestCase):
    def setUp(self):
        t=tempfile.TemporaryDirectory();self.addCleanup(t.cleanup);self.root=Path(t.name).resolve()
        for name in ['world/region','catalog','sample/indexes','sample/objects']:(self.root/name).mkdir(parents=True)
        self.config=self.root/'config.json';self.config.write_text(json.dumps(dict(format='UIEMAP_CONFIG1',world=dict(id='test',path='world'),catalog='catalog',out='out',dimensions=[dict(key='0',title='Test',mode='surface')])))
    def region(self,locations):
        raw=bytearray(5*4096)
        for i,offset,count in locations:raw[i*4:i*4+4]=((offset<<8)|count).to_bytes(4,'big')
        p=self.root/'world/region/r.-1.-1.mca';p.write_bytes(raw);return p
    def sample(self):
        s=self.root/'sample';(s/hb.OWNED_MARKER).write_text('fixture')
        data=b'x'*100;sha=hashlib.sha256(data).hexdigest();(s/'objects'/(sha+'.uih')).write_bytes(data)
        doc=dict(format=hb.INDEX_FORMAT_V2,tileSize=64,rendererIdentity='a'*64,atlasSha256='b'*64,tiles={'0,0':sha},
            context=dict(world='sample',dimension='42',renderer='fixture',caves=False,layoutSha256='c'*64))
        raw=json.dumps(doc,sort_keys=True,separators=(',',':')).encode();sha=hashlib.sha256(raw).hexdigest();(s/'indexes'/(sha+'.json')).write_bytes(raw)
        (s/'current.json').write_text(json.dumps(dict(format=hb.POINTER_FORMAT,indexSha256=sha)));return s
    def test_sparse_negative_region_and_geometry_scenarios_without_writes(self):
        p=self.region([(0,2,1),(3,3,1),(4,4,1)]);sample=self.sample()
        before={p:p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        report=capacity.estimate(self.config,sample_store=sample,generations=3);row=report['dimensions'][0]
        self.assertEqual((row['savedChunks'],row['occupiedTiles'],row['sourceBytes']),(3,2,20480))
        self.assertEqual(row['geometryScenario']['retainedBytes'],600)
        self.assertEqual(before,{p:p.read_bytes() for p in self.root.rglob('*') if p.is_file()})
    def test_invalid_locations_and_overlapping_sectors_refused(self):
        for locations in [[(0,1,1)],[(0,2,0)],[(0,5,1)],[(0,2,2),(1,3,1)]]:
            with self.subTest(locations=locations),self.assertRaises(ValueError):capacity.region_header(self.region(locations))
    def test_corrupt_sample_and_cross_mode_estimate_refused(self):
        self.region([]);sample=self.sample();pointer=json.loads((sample/'current.json').read_text())
        target=sample/'indexes'/(pointer['indexSha256']+'.json');raw=target.read_bytes();target.write_bytes(b'{}')
        with self.assertRaises(ValueError):capacity.estimate(self.config,sample_store=sample)
        target.write_bytes(raw);doc=json.loads(self.config.read_text());doc['dimensions'][0]['mode']='caves';self.config.write_text(json.dumps(doc))
        with self.assertRaisesRegex(ValueError,'mode differs'):capacity.estimate(self.config,sample_store=sample)
    def test_empty_world_without_sample_has_no_invented_estimate(self):
        report=capacity.estimate(self.config);self.assertEqual(report['dimensions'][0]['renderSupport'],'empty')
        self.assertNotIn('geometryScenario',report['dimensions'][0])
    def test_output_usage_counts_shared_inode_once_across_layers(self):
        config=capacity.uiemap_config.load(self.config);outputs=config['dimensions'][0]['outputs']
        surface=Path(outputs['surface']);caves=Path(outputs['caves']);surface.mkdir(parents=True);caves.mkdir()
        (surface/'one').write_bytes(b'x'*100);os.link(surface/'one',caves/'shared');(caves/'two').write_bytes(b'y'*30)
        report=capacity.estimate(self.config,measure_output=True)['outputUsage']
        self.assertEqual(report['combinedUniqueFileBytes'],130)
        measured=[row for row in report['outputs'] if row['state']=='measured']
        self.assertEqual(sum(row['uniqueFileBytes'] for row in measured),230)
        self.assertEqual(len([row for row in report['outputs'] if row['state']=='not-created']),1)
        with self.assertRaisesRegex(ValueError,'budget'):capacity.output_usage(config,max_entries=1)
    @unittest.skipUnless(os.name=='posix','POSIX symlinks')
    def test_output_symlink_is_not_followed(self):
        config=capacity.uiemap_config.load(self.config);surface=Path(config['dimensions'][0]['outputs']['surface']);surface.mkdir(parents=True)
        (surface/'linked').symlink_to(self.root/'world',target_is_directory=True)
        with self.assertRaisesRegex(ValueError,'Linked'):capacity.output_usage(config)

if __name__=='__main__':unittest.main()
