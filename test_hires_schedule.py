import copy,json,math,os,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
import hires_schedule as hs

GiB=1024**3
JR=hs.JOB_RESERVE_BYTES
FLOOR=20*GiB
RESERVE=24*GiB
FREE=100*GiB
T=1_760_000_000

def region(rx,rz,kind='missing',distance=None):
    return {'job':[rx,rz],'kind':kind,'distance':math.hypot(rx,rz) if distance is None else distance}
def select(pending,ledger,now=T,**kwargs):
    args=dict(max_jobs=1,free_bytes=FREE,floor_bytes=FLOOR,reserve_bytes=RESERVE);args.update(kwargs)
    return hs.select(pending,ledger,now=now,**args)
def jobs(selected):return [tuple(item['job']) for item in selected]
def entry(attempts,last,result,since=0):
    return {'pendingSince':since,'attempts':attempts,'lastAttemptAt':last,'lastResult':result}
def ledger_of(regions,last_class=None):return {'format':hs.FORMAT,'lastClass':last_class,'regions':regions}

class SelectTests(unittest.TestCase):
    def test_coverage_by_distance_alternates_with_updates(self):
        pending=[region(5,0),region(1,0),region(3,0),region(9,9,'changed')]
        selected,ledger,telemetry=select(pending,None,max_jobs=4)
        self.assertEqual(jobs(selected),[(1,0),(9,9),(3,0),(5,0)])  # coverage, update, then coverage (no updates left)
        self.assertEqual(ledger['lastClass'],'coverage')
        self.assertEqual(telemetry['selected'],{'coverage':3,'update':1})
        self.assertEqual(selected[1],region(9,9,'changed'))
    def test_updates_are_fifo_by_pending_since_and_ties_deterministic(self):
        _,ledger,_=select([region(7,7,'changed')],None,now=T,free_bytes=0)  # nothing fits: only registers A
        selected,_,_=select([region(7,7,'changed'),region(0,1,'changed')],ledger,now=T+60)
        self.assertEqual(jobs(selected),[(7,7)])                     # older pending first despite larger distance
        tie=[region(2,0,distance=1),region(-1,0,distance=1),region(0,-3,distance=1)]
        self.assertEqual(jobs(select(tie,None,max_jobs=3)[0]),[(-1,0),(0,-3),(2,0)])
        self.assertEqual(select(tie,None,max_jobs=3),select(list(reversed(tie)),None,max_jobs=3))
    def test_last_class_persists_across_runs(self):
        pending=[region(1,0),region(0,0,'changed')]
        first,ledger,_=select(pending,None)
        self.assertEqual((jobs(first),ledger['lastClass']),([(1,0)],'coverage'))
        ledger=hs.finish(ledger,[1,0],ok=True,now=T+1)
        second,ledger,_=select([region(2,0),region(0,0,'changed')],ledger,now=T+2)
        self.assertEqual((jobs(second),ledger['lastClass']),([(0,0)],'update'))

    def test_hot_spawn_region_does_not_starve_missing_coverage(self):
        missing={(10+i,0) for i in range(5)};ledger=None;hot=0;now=T
        for _ in range(10):
            pending=[region(0,0,'changed',0)]+[region(*job) for job in sorted(missing)]
            selected,ledger,_=select(pending,ledger,now=now)
            for item in selected:
                ledger=hs.finish(ledger,item['job'],ok=True,now=now+300)
                if item['kind']=='missing':missing.discard(tuple(item['job']))
                else:hot+=1                                       # spawn changes again after every render
            now+=600
        self.assertEqual(missing,set());self.assertEqual(hot,5)
    def test_crashed_attempt_backs_off_even_when_fingerprint_changes(self):
        selected,ledger,_=select([region(0,0),region(5,0)],None)
        self.assertEqual(jobs(selected),[(0,0)])                     # ledger persisted, then the worker crashed
        self.assertEqual(ledger['regions']['0,0'],entry(1,T,'attempted',T))
        later,ledger2,telemetry=select([region(0,0,'changed'),region(5,0)],ledger,now=T+60)
        self.assertEqual(jobs(later),[(5,0)]);self.assertEqual(telemetry['backedOff'],1)
        self.assertEqual(ledger2['regions']['0,0']['attempts'],1)    # history kept across the kind change
        retry,ledger3,_=select([region(0,0,'changed')],ledger2,now=T+600)
        self.assertEqual(jobs(retry),[(0,0)]);self.assertEqual(ledger3['regions']['0,0']['attempts'],2)
    def test_permanent_failure_backoff_doubles_and_caps_without_blocking_others(self):
        for attempts,delay in [(1,600),(2,1200),(3,2400),(8,76800),(9,86400),(40,86400),(hs.MAX_ATTEMPTS,86400)]:
            with self.subTest(attempts=attempts):
                ledger=ledger_of({'0,0':entry(attempts,1000,'failed')})
                pending=[region(0,0,'failed',0),region(9,0)]
                waiting,_,_=select(pending,ledger,now=1000+delay-1,max_jobs=2)
                self.assertEqual(jobs(waiting),[(9,0)])               # others progress meanwhile
                due,after,_=select(pending,ledger,now=1000+delay,max_jobs=2)
                self.assertIn((0,0),jobs(due))
                self.assertEqual(after['regions']['0,0']['attempts'],min(attempts+1,hs.MAX_ATTEMPTS))
        ledger=ledger_of({'0,0':entry(1,T,'failed')})
        self.assertEqual(jobs(select([region(0,0,'failed')],ledger,now=T-10**6)[0]),[])  # clock went back: waits
    def test_failure_backoff_counts_from_failure_end(self):
        _,ledger,_=select([region(0,0)],None)
        ledger=hs.finish(ledger,[0,0],ok=False,now=T+300)
        self.assertEqual(ledger['regions']['0,0'],entry(1,T+300,'failed',T))
        self.assertEqual(jobs(select([region(0,0)],ledger,now=T+899)[0]),[])
        self.assertEqual(jobs(select([region(0,0)],ledger,now=T+900)[0]),[(0,0)])
    def test_success_then_dirty_again_restarts_pending_period_and_waiting_ones_keep_theirs(self):
        selected,ledger,_=select([region(0,0,'changed'),region(1,1,'changed')],None)
        self.assertEqual(jobs(selected),[(0,0)])
        ledger=hs.finish(ledger,[0,0],ok=True,now=T+300)
        self.assertEqual(ledger['regions']['0,0'],entry(0,T,'ok',T))
        _,ledger,_=select([region(0,0,'changed'),region(1,1,'changed')],ledger,now=T+1000,free_bytes=0)
        self.assertEqual(ledger['regions']['0,0'],entry(0,None,None,T+1000))  # genuinely fresh, unattempted
        self.assertEqual(ledger['regions']['1,1']['pendingSince'],T)  # still pending: preserved
        self.assertEqual(jobs(select([region(0,0,'changed'),region(1,1,'changed')],ledger,now=T+1001)[0]),[(1,1)])
    def test_dirty_again_after_success_keeps_its_age_while_blocked(self):
        _,ledger,_=select([region(0,0,'changed')],None,now=T)
        ledger=hs.finish(ledger,[0,0],ok=True,now=T+300)
        for now in (T+1000,T+2000,T+3000):                           # disk blocked: nothing dispatched
            _,ledger,_=select([region(0,0,'changed'),region(5,5,'changed',0)],ledger,now=now,free_bytes=0)
            self.assertEqual(ledger['regions']['0,0'],entry(0,None,None,T+1000))
        self.assertEqual(ledger['regions']['5,5']['pendingSince'],T+1000)
        late=region(9,9,'changed',0)                                 # newer and closer must not overtake
        chosen,_,_=select([region(0,0,'changed'),region(5,5,'changed',0),late],ledger,now=T+4000,max_jobs=2)
        self.assertEqual(jobs(chosen),[(0,0),(5,5)])                 # same age and distance: (rx, rz) decides
        _,other_queue,_=select([region(0,0,'changed'),region(1,0)],ledger,now=T+5000)   # other queue chosen
        self.assertEqual(other_queue['regions']['0,0']['pendingSince'],T+1000)
    def test_only_the_current_dirty_set_is_kept(self):
        ledger=ledger_of({'0,0':entry(3,T,'failed'),'4,4':entry(0,None,None),'5,5':entry(0,T,'ok')})
        _,after,_=select([region(0,0,'changed')],ledger,now=T+1,free_bytes=0)
        self.assertEqual(set(after['regions']),{'0,0'});self.assertEqual(after['regions']['0,0']['attempts'],3)

    def test_disk_reservation_is_charged_for_every_selected_job(self):
        updates=[region(i,0,'changed') for i in range(6)]
        self.assertEqual(len(select(updates,None,max_jobs=5,free_bytes=FLOOR+3*JR)[0]),3)
        self.assertEqual(len(select(updates,None,max_jobs=5,free_bytes=FLOOR+JR)[0]),1)  # equality allowed
        self.assertEqual(len(select(updates,None,max_jobs=5,free_bytes=FLOOR+JR-1)[0]),0)
        mixed=[region(1,0),region(2,0),region(0,0,'changed')]
        selected,_,telemetry=select(mixed,None,max_jobs=3,free_bytes=RESERVE+JR-1)
        self.assertEqual(jobs(selected),[(0,0)])                     # coverage needs the larger reserve
        self.assertTrue(telemetry['budgetBlocked']['coverage'])
        self.assertEqual(telemetry['reservedBytes'],JR);self.assertEqual(telemetry['freeAfterReserve'],RESERVE-1)
        selected,_,_=select(mixed,None,max_jobs=3,free_bytes=RESERVE+2*JR)
        self.assertEqual(jobs(selected),[(1,0),(0,0)])               # second coverage would cross the reserve
        high_floor=select(mixed,None,max_jobs=3,free_bytes=30*GiB,floor_bytes=30*GiB-JR,reserve_bytes=0)[0]
        self.assertEqual(len(high_floor),1)                          # coverage floor = max(reserve, floor)
        custom=select(updates,None,max_jobs=5,free_bytes=FLOOR+10,job_reserve_bytes=5)[0]
        self.assertEqual(len(custom),2)

    def test_pure_idempotent_and_no_double_dispatch(self):
        pending=[region(0,0),region(1,0,'changed')];ledger=ledger_of({'1,0':entry(0,None,None,T-5)})
        saved=(copy.deepcopy(pending),copy.deepcopy(ledger))
        first=select(pending,ledger,max_jobs=2)
        self.assertEqual(first,select(pending,ledger,max_jobs=2))
        self.assertEqual((pending,ledger),saved)
        again,_,_=select(pending,first[1],max_jobs=2)                # same tick: already attempted, not re-sent
        self.assertEqual(again,[])
        first[0][0]['job'][0]=99;first[1]['regions']['0,0']['attempts']=7
        self.assertEqual((pending,ledger),saved)                     # outputs do not alias inputs
    def test_finish_rules(self):
        _,ledger,_=select([region(0,0),region(1,0)],None)
        saved=copy.deepcopy(ledger)
        done=hs.finish(ledger,(0,0),ok=True,now=T+5)
        self.assertEqual(ledger,saved)
        self.assertEqual(hs.finish(done,[0,0],ok=True,now=T+9),done)   # idempotent
        with self.assertRaisesRegex(ValueError,'matching'):hs.finish(done,[0,0],ok=False,now=T+9)
        failed=hs.finish(ledger,[0,0],ok=False,now=T+5)
        self.assertEqual(hs.finish(failed,[0,0],ok=False,now=T+50),failed)
        with self.assertRaisesRegex(ValueError,'matching'):hs.finish(failed,[0,0],ok=True,now=T+9)
        for job in ([1,0],[7,7]):                                    # never attempted / unknown
            with self.assertRaisesRegex(ValueError,'matching'):hs.finish(ledger,job,ok=True,now=T)
        for bad in ({'ok':1,'now':T},{'ok':True,'now':float('nan')}):
            with self.assertRaises(ValueError):hs.finish(ledger,[0,0],**bad)
    def test_argument_validation(self):
        good=[region(0,0)]
        for kwargs in ({'max_jobs':0},{'max_jobs':17},{'max_jobs':True},{'now':True},{'now':float('nan')},
                       {'now':10**400},{'free_bytes':-1},{'free_bytes':1.5},{'floor_bytes':True},{'reserve_bytes':None},
                       {'job_reserve_bytes':0}):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ValueError):select(good,None,**kwargs)
        bad_pending=[[region(0,0),region(0,0,'changed')],[{**region(0,0),'kind':'stale'}],[{**region(0,0),'distance':True}],
                     [{**region(0,0),'distance':float('nan')}],[{**region(0,0),'distance':-1}],
                     [{**region(0,0),'distance':10**400}],[{**region(0,0),'distance':hs.MAX_DISTANCE+1}],
                     [{**region(0,0),'distance':1e300}],[{**region(0,0),'distance':float('inf')}],[{**region(0,0),'job':[1.0,2]}],
                     [{**region(0,0),'job':[True,0]}],[{**region(0,0),'job':[0]}],[{**region(0,0),'job':[10**7,0]}],
                     [{**region(0,0),'extra':1}],(region(0,0),),'x']
        for pending in bad_pending:
            with self.subTest(pending=str(pending)[:60]):
                with self.assertRaises(ValueError):select(pending,None)
        self.assertEqual(len(select([{**region(0,0),'distance':hs.MAX_DISTANCE}],None)[0]),1)  # boundary accepted
        with self.assertRaises(ValueError):select(good,{'format':hs.FORMAT})
        with patch.object(hs,'MAX_BYTES',100):
            with self.assertRaisesRegex(ValueError,'capacity'):select([region(i,0) for i in range(5)],None)

class LedgerTests(unittest.TestCase):
    def valid(self):return select([region(0,0),region(1,0,'changed')],None,max_jobs=1)[1]
    def test_canonical_roundtrip_and_strict_rejection(self):
        doc=self.valid();raw=hs.serialize(doc);self.assertEqual(hs.parse(raw),doc)
        text=raw.decode();variant=lambda d:json.dumps(d,sort_keys=True,separators=(',',':')).encode()
        good=doc['regions']['0,0']
        regions=lambda **c:variant({**doc,'regions':{**doc['regions'],'0,0':{**good,**c}}})
        bad={'indent':json.dumps(doc,sort_keys=True,indent=1).encode(),
             'extra':variant({**doc,'x':1}),'format':variant({**doc,'format':'X'}),'class':variant({**doc,'lastClass':'surface'}),
             'key':variant({**doc,'regions':{'01,2':good}}),'key spaces':variant({**doc,'regions':{'1, 2':good}}),
             'entry extra':regions(extra=1),'attempts bool':regions(attempts=True),'attempts negative':regions(attempts=-1),
             'attempted without attempts':regions(attempts=0),'ok with attempts':regions(lastResult='ok'),
             'none with time':regions(lastResult=None,attempts=0),'result':regions(lastResult='crashed'),
             'time bool':regions(pendingSince=False),'time huge':regions(pendingSince=10**400),'time negative':regions(lastAttemptAt=-1),
             'nan':text.replace(f'"pendingSince":{T}','"pendingSince":NaN',1).encode(),
             'duplicate':text.replace('{"format"','{"format":"x","format"',1).encode(),
             'list':b'[]','empty':b'','utf8':b'\xff','deep':b'['*30000+b']'*30000,'not bytes':text}
        for name,value in bad.items():
            with self.subTest(name):
                with self.assertRaises(ValueError):hs.parse(value)
        with patch.object(hs,'MAX_REGIONS',1):
            with self.assertRaises(ValueError):hs.parse(raw)
        with patch.object(hs,'MAX_BYTES',len(raw)-1):
            with self.assertRaises(ValueError):hs.parse(raw)
        many={f'{i},0':entry(0,None,None,T) for i in range(hs.MAX_REGIONS)}
        self.assertEqual(len(hs.parse(hs.serialize(ledger_of(many)))['regions']),hs.MAX_REGIONS)  # fits 4 MiB

    def test_load_missing_valid_corrupt_and_symlink(self):
        with tempfile.TemporaryDirectory(prefix='uie-schedule-') as temp:
            out=Path(temp).resolve()
            self.assertIsNone(hs.load(out))
            raw=hs.serialize(self.valid());(out/hs.NAME).write_bytes(raw)
            self.assertEqual(hs.load(out),hs.parse(raw))
            (out/hs.NAME).write_bytes(raw[:-1])
            with self.assertRaises(ValueError):hs.load(out)              # never silently reset
            self.assertEqual((out/hs.NAME).read_bytes(),raw[:-1])
            (out/hs.NAME).unlink();(out/hs.NAME).mkdir()
            with self.assertRaises(ValueError):hs.load(out)
            (out/hs.NAME).rmdir();target=out/'elsewhere.json';target.write_bytes(raw)
            try:os.symlink(target,out/hs.NAME)
            except (OSError,NotImplementedError) as error:self.skipTest(f'symlinks unavailable: {error}')
            with self.assertRaises(ValueError):hs.load(out)
            (out/hs.NAME).unlink();link=out.parent/f'{out.name}-link'
            try:os.symlink(out,link,target_is_directory=True)
            except (OSError,NotImplementedError) as error:self.skipTest(f'symlinks unavailable: {error}')
            try:
                with self.assertRaises(ValueError):hs.load(link)
            finally:
                try:os.unlink(link)
                except OSError:os.rmdir(link)
