import unittest,tempfile,json,hashlib
from unittest.mock import patch
import publish_generation as publication
from pathlib import Path
from publish_generation import publish_generation,PublicationError

class PublicationTests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
  self.root=Path(self.temp.name);self.store=self.root/'published'
  self.pipeline='a'*64
 def batch(self,name,x=0,atlas=b'\x89PNG\r\n\x1a\nfixture'):
  root=self.root/name;root.mkdir()
  snapshot=hashlib.sha256(name.encode()).hexdigest()
  mesh={'positions':[x*16,0,0,x*16+1,0,0,x*16,1,0],'uvs':[0,0,1,0,0,1],
        'colors':[1]*9,'indices':[0,1,2]}
  blocks={'format':'UIE_BLOCKS1','x':x*16,'z':0,'width':16,'depth':16,'height':256,
          'registry':{'0':'minecraft:air'},'runs':[[65536,0,0]]}
  m={'format':1,'snapshotFormat':'UIE2','snapshotSha256':snapshot,'atlas':'atlas.png',
     'generatedAt':'2026-09-30T00:00:00Z','bakedLighting':True,
     'bounds':{'min':[x*16,0,0],'max':[x*16+16,256,16]},
     'coverage':{'renderedBlocks':1,'unsupportedBlocks':0,'emptyBlocks':0,'unsupportedNames':[]},
     'chunks':[{'x':x,'z':0,'file':'chunk.json'}],
     'blockInfo':{'format':'UIE_BLOCKS1','snapshotSha256':snapshot,'chunks':[{'x':x,'z':0,'file':'blocks.json'}]}}
  blockfile=f'blocks/{snapshot}/chunk_{x}_0.json';m['blockInfo']['chunks'][0]['file']=blockfile
  (root/blockfile).parent.mkdir(parents=True)
  for filename,value in [('manifest.json',m),('chunk.json',mesh),(blockfile,blocks)]:
   (root/filename).write_text(json.dumps(value))
  (root/'atlas.png').write_bytes(atlas)
  return {'geometry':root,'snapshotSha256':snapshot,'coreChunks':[[x,0]]}
 def publish(self,batches,previous=None,**kwargs):
  return publish_generation(batches,self.store,world='uie',dimension='0',pipeline_key=self.pipeline,expected_revision=previous,
                            expected_chunks=kwargs.pop('expected_chunks',[c for b in batches for c in b['coreChunks']]),**kwargs)
 def edit(self,batch,filename,change):
  if filename=='blocks.json':filename=json.loads((batch['geometry']/'manifest.json').read_text())['blockInfo']['chunks'][0]['file']
  p=batch['geometry']/filename;doc=json.loads(p.read_text());change(doc);p.write_text(json.dumps(doc))
 def test_two_batches_complete_hashes_and_deterministic_order(self):
  a,b=self.batch('a'),self.batch('b',1)
  pointer=self.publish([a,b]);old=(self.store/'current.json').read_bytes()
  target=self.store/'generations'/pointer['generation']
  inventory=json.loads((target/'COMPLETE.json').read_bytes())
  for n,h in inventory['files'].items():self.assertEqual(hashlib.sha256((target/n).read_bytes()).hexdigest(),h)
  m=json.loads((self.store/pointer['manifest']).read_bytes())
  self.assertEqual([c['x'] for c in m['chunks']],[0,1])
  self.assertEqual(len(m['blockInfo']['chunks']),2)
  again=self.publish([b,a],pointer['generation']);self.assertEqual(again,pointer)
  self.assertEqual((self.store/'current.json').read_bytes(),old)
 def test_bad_mesh_preserves_previous_pointer_and_release(self):
  a=self.batch('a');old=self.publish([a]);raw=(self.store/'current.json').read_bytes()
  b=self.batch('b',1);self.edit(b,'chunk.json',lambda d:d.update(indices=[0,1,100]))
  with self.assertRaises(PublicationError):self.publish([a,b],old['generation'])
  self.assertEqual((self.store/'current.json').read_bytes(),raw)
  self.assertTrue((self.store/old['manifest']).exists())
  self.assertFalse((self.store/'.publish.lock').exists())
 def test_atlas_mismatch_and_duplicates_rejected(self):
  a=self.batch('a');b=self.batch('b',1,b'\x89PNG\r\n\x1a\nother')
  with self.assertRaisesRegex(PublicationError,'atlases'):self.publish([a,b])
  c=self.batch('c')
  with self.assertRaisesRegex(PublicationError,'Duplicate'):self.publish([a,c])
  self.assertFalse((self.store/'current.json').exists())
 def test_provenance_and_chunk_sets_rejected(self):
  a=self.batch('a');a['snapshotSha256']='b'*64
  with self.assertRaisesRegex(PublicationError,'provenance'):self.publish([a])
  b=self.batch('b',1);self.edit(b,'manifest.json',lambda d:d['blockInfo'].update(chunks=[]))
  with self.assertRaisesRegex(PublicationError,'Missing block'):self.publish([b])
 def test_torn_block_runs_rejected(self):
  a=self.batch('a');self.edit(a,'blocks.json',lambda d:d.update(runs=[[65535,0,0]]))
  with self.assertRaisesRegex(PublicationError,'incomplete'):self.publish([a])
 def test_nonfinite_mesh_rejected(self):
  a=self.batch('a');self.edit(a,'chunk.json',lambda d:d.update(positions=[float('nan')]*9))
  with self.assertRaisesRegex(PublicationError,'Nonfinite'):self.publish([a])
 def test_path_escape_rejected(self):
  a=self.batch('a');self.edit(a,'manifest.json',lambda d:d['chunks'][0].update(file='../secret.json'))
  with self.assertRaisesRegex(PublicationError,'Unsafe'):self.publish([a])
 def test_stale_base_and_active_lock_rejected(self):
  a=self.batch('a');p=self.publish([a]);raw=(self.store/'current.json').read_bytes()
  with self.assertRaisesRegex(PublicationError,'Stale'):self.publish([a])
  (self.store/'.publish.lock').write_text('active')
  with self.assertRaisesRegex(PublicationError,'lock'):self.publish([a],p['generation'])
  self.assertEqual((self.store/'current.json').read_bytes(),raw)
 def test_damaged_immutable_generation_is_not_overwritten(self):
  a=self.batch('a');p=self.publish([a]);target=self.store/p['manifest'];target.write_text('damaged')
  with self.assertRaisesRegex(PublicationError,'damaged'):self.publish([a],p['generation'])
  self.assertEqual(target.read_text(),'damaged')
 def test_input_store_overlap_rejected(self):
  a=self.batch('a');self.store=a['geometry']/'published'
  with self.assertRaisesRegex(PublicationError,'overlap'):self.publish([a])
 def test_omitted_batch_and_core_are_rejected_without_staging_leak(self):
  a=self.batch('a')
  with self.assertRaisesRegex(PublicationError,'all expected'):self.publish([a],expected_chunks=[[0,0],[1,0]])
  a['coreChunks'].append([1,0])
  with self.assertRaisesRegex(PublicationError,'Expected core'):self.publish([a])
  self.assertEqual(list(self.store.glob('.staging-*')),[])
 def test_invalid_registry_rejected(self):
  a=self.batch('a');self.edit(a,'blocks.json',lambda d:d['registry'].update({'01':'unused'}))
  with self.assertRaisesRegex(PublicationError,'registry'):self.publish([a])
 def test_source_changed_after_copy_preserves_old_pointer(self):
  a=self.batch('a');previous=self.publish([a]);old=(self.store/'current.json').read_bytes()
  b=self.batch('b',1);write=publication._write;changed=False
  def fault(path,raw):
   nonlocal changed
   write(path,raw)
   if path.parent.name=='chunks' and not changed:
    changed=True
    (a['geometry']/'atlas.png').write_bytes(b'\x89PNG\r\n\x1a\nchanged')
  with patch.object(publication,'_write',fault):
   with self.assertRaises(PublicationError):self.publish([a,b],previous['generation'])
  self.assertEqual((self.store/'current.json').read_bytes(),old)
  self.assertEqual(list(self.store.glob('.staging-*')),[])
 def test_disk_reserve_failure_keeps_old_publication(self):
  a=self.batch('a');p=self.publish([a]);old=(self.store/'current.json').read_bytes()
  from types import SimpleNamespace
  with patch.object(publication.shutil,'disk_usage',return_value=SimpleNamespace(free=100)):
   with self.assertRaisesRegex(PublicationError,'disk reserve'):self.publish([a],p['generation'],min_free_bytes=100)
  self.assertEqual((self.store/'current.json').read_bytes(),old)
  self.assertEqual(list(self.store.glob('.staging-*')),[])
  self.assertFalse((self.store/'.publish.lock').exists())
 def test_mid_build_write_budget_cleanup_and_retry(self):
  a=self.batch('a');p=self.publish([a]);old=(self.store/'current.json').read_bytes()
  # Atlas is written, then the chunk exceeds this per-invocation byte limit.
  with self.assertRaisesRegex(PublicationError,'write budget'):self.publish([a],p['generation'],max_write_bytes=20)
  self.assertEqual((self.store/'current.json').read_bytes(),old)
  self.assertEqual(list(self.store.glob('.staging-*')),[])
  self.assertEqual(self.publish([a],p['generation']),p)
 def test_invalid_disk_limits_rejected(self):
  a=self.batch('a')
  for kwargs in ({'min_free_bytes':-1},{'min_free_bytes':True},{'max_write_bytes':-1},{'max_write_bytes':1.2}):
   with self.subTest(kwargs=kwargs),self.assertRaises(PublicationError):self.publish([a],**kwargs)
 def test_compressed_only_chunks_are_lossless_and_hashed(self):
  import gzip
  a=self.batch('a');p=self.publish([a],compress_chunks=True);base=self.store/p['manifest'];m=json.loads(base.read_bytes())
  directory=base.parent;inventory=json.loads((directory/'COMPLETE.json').read_bytes())
  for entries,original in ((m['chunks'],'chunk.json'),(m['blockInfo']['chunks'],f"blocks/{a['snapshotSha256']}/chunk_0_0.json")):
   name=entries[0]['file'];compressed=directory/(name+'.gz')
   self.assertFalse((directory/name).exists())
   self.assertEqual(gzip.decompress(compressed.read_bytes()),(a['geometry']/original).read_bytes())
   self.assertEqual(hashlib.sha256(compressed.read_bytes()).hexdigest(),inventory['files'][name+'.gz'])
  self.assertEqual(self.publish([a],p['generation'],compress_chunks=True),p)
 def test_incremental_generation_reuses_only_identical_files(self):
  a=self.batch('a');p=self.publish([a],compress_chunks=True)
  old_dir=(self.store/p['manifest']).parent
  old_manifest=json.loads((old_dir/'manifest.json').read_bytes());old_chunk=old_dir/(old_manifest['chunks'][0]['file']+'.gz')
  original_chunk=old_chunk.read_bytes()
  self.edit(a,'chunk.json',lambda d:d.update(colors=[.5]*9))
  q=self.publish([a],p['generation'],compress_chunks=True,reuse_unchanged=True)
  new_dir=(self.store/q['manifest']).parent
  self.assertNotEqual(p['generation'],q['generation'])
  self.assertTrue((old_dir/'atlas.png').samefile(new_dir/'atlas.png'))
  blocks=old_manifest['blockInfo']['chunks'][0]['file']+'.gz'
  self.assertTrue((old_dir/blocks).samefile(new_dir/blocks))
  new_chunk=json.loads((new_dir/'manifest.json').read_bytes())['chunks'][0]['file']+'.gz'
  self.assertFalse(old_chunk.samefile(new_dir/new_chunk))
  self.assertEqual(old_chunk.read_bytes(),original_chunk)
 def test_link_failure_falls_back_and_corruption_refuses_reuse(self):
  import errno
  a=self.batch('a');p=self.publish([a]);old=(self.store/'current.json').read_bytes()
  with patch.object(publication.os,'link',side_effect=OSError(errno.EXDEV,'test cross device')):
   self.assertEqual(self.publish([a],p['generation'],reuse_unchanged=True),p)
  (self.store/p['manifest']).parent.joinpath('atlas.png').write_bytes(b'corrupt')
  with self.assertRaisesRegex(PublicationError,'Previous file is damaged'):
   self.publish([a],p['generation'],reuse_unchanged=True)
  self.assertEqual((self.store/'current.json').read_bytes(),old)
  self.assertEqual(list(self.store.glob('.staging-*')),[])
 def test_link_limit_falls_back_to_budgeted_copy(self):
  import errno
  a=self.batch('a');p=self.publish([a]);old_dir=(self.store/p['manifest']).parent
  self.edit(a,'chunk.json',lambda d:d.update(colors=[.5]*9))
  with patch.object(publication.os,'link',side_effect=OSError(errno.EMLINK,'test link limit')):
   q=self.publish([a],p['generation'],reuse_unchanged=True)
  new_dir=(self.store/q['manifest']).parent
  self.assertNotEqual(p['generation'],q['generation'])
  self.assertFalse((old_dir/'atlas.png').samefile(new_dir/'atlas.png'))
  self.assertEqual((old_dir/'atlas.png').read_bytes(),(new_dir/'atlas.png').read_bytes())
 def written_bytes(self,batches):
  # Bytes a fresh non-reusing publication writes: every generation file plus current.json.
  store=self.store;self.store=self.root/'measure'
  try:p=self.publish(batches)
  finally:self.store=store
  measure=self.root/'measure'
  total=sum(f.stat().st_size for f in (measure/'generations'/p['generation']).rglob('*') if f.is_file())
  return p,total+len((measure/'current.json').read_bytes())
 def assert_unchanged(self,old,generations):
  self.assertEqual((self.store/'current.json').read_bytes(),old)
  self.assertEqual(sorted(d.name for d in (self.store/'generations').iterdir()),sorted(generations))
  self.assertEqual(list(self.store.glob('.staging-*'))+list(self.store.glob('.current-*')),[])
  self.assertFalse((self.store/'.publish.lock').exists())
 def test_pointer_budget_boundary_is_checked_before_promotion(self):
  a=self.batch('a');p=self.publish([a]);old=(self.store/'current.json').read_bytes()
  b=self.batch('b',1);expected,total=self.written_bytes([a,b])
  # Content and inventory fit in total-1; only the pointer does not, so no generation may be promoted.
  with self.assertRaisesRegex(PublicationError,'write budget'):self.publish([a,b],p['generation'],max_write_bytes=total-1)
  self.assert_unchanged(old,[p['generation']])
  # Exactly total succeeds: the already written inventory is not counted twice.
  self.assertEqual(self.publish([a,b],p['generation'],max_write_bytes=total),expected)
 def test_pointer_free_space_is_checked_before_promotion(self):
  from types import SimpleNamespace
  a=self.batch('a');p=self.publish([a]);old=(self.store/'current.json').read_bytes()
  b=self.batch('b',1);write=publication._write;staged=False
  def fault(path,raw):
   nonlocal staged
   write(path,raw)
   if path.name=='COMPLETE.json':staged=True
  usage=lambda path:SimpleNamespace(free=0 if staged else 10**12)
  with patch.object(publication,'_write',fault),patch.object(publication.shutil,'disk_usage',usage):
   with self.assertRaisesRegex(PublicationError,'disk reserve'):self.publish([a,b],p['generation'],min_free_bytes=0)
  self.assert_unchanged(old,[p['generation']])
 def test_partial_pointer_write_and_replace_failure_clean_own_temp(self):
  import errno
  a=self.batch('a');p=self.publish([a]);old=(self.store/'current.json').read_bytes()
  b=self.batch('b',1);write=publication._write
  def torn(path,raw):
   if not path.name.startswith('.current-'):return write(path,raw)
   with path.open('xb') as f:f.write(raw[:5])
   raise OSError(errno.ENOSPC,'test disk full')
  with patch.object(publication,'_write',torn):
   with self.assertRaises(OSError):self.publish([a,b],p['generation'])
  # Unpredictable failure after promotion: the complete generation stays, unpublished.
  generations=[d.name for d in (self.store/'generations').iterdir()]
  self.assertEqual(len(generations),2)
  self.assert_unchanged(old,generations)
  with patch.object(publication.os,'replace',side_effect=OSError(errno.EIO,'test replace failure')):
   with self.assertRaises(OSError):self.publish([a,b],p['generation'])
  self.assert_unchanged(old,generations)
  # Retry reuses the verified immutable generation and publishes it.
  q=self.publish([a,b],p['generation'])
  self.assertIn(q['generation'],generations)
  self.assertEqual(json.loads((self.store/'current.json').read_bytes()),q)

 def test_replace_eexist_cleans_own_pointer_temp(self):
  import errno
  a=self.batch('replace-exists');pointer=self.publish([a]);old=(self.store/'current.json').read_bytes()
  with patch.object(publication.os,'replace',side_effect=FileExistsError(errno.EEXIST,'replace fault')):
   with self.assertRaises(FileExistsError):self.publish([a],pointer['generation'])
  self.assertEqual((self.store/'current.json').read_bytes(),old)
  self.assertEqual(list(self.store.glob('.current-*.tmp')),[])
  self.assertEqual(list(self.store.glob('.staging-*')),[])

if __name__=='__main__':unittest.main()
