"""Atomic public status document for a render worker; this does not run a worker.

Progress counts completed batches, not browser downloads. Only call lastPublishedAt with
the timestamp of a successful map-generation publication. updatedAt is just a heartbeat.
The caller must serialize writers; os.replace gives readers a complete document, not a queue lock.
"""
from pathlib import Path
from datetime import datetime,timezone
import json,math,os,tempfile,argparse

STATES={'not_configured','idle','queued','rendering','publishing','complete','error'}
def validate(data):
 if not isinstance(data,dict) or data.get('format')!='UIE_RENDER_STATUS1':raise ValueError('Invalid status format')
 if not {'state','updatedAt','completedBatches','totalBatches','etaSeconds','lastPublishedAt','message'}<=data.keys():raise ValueError('Missing status fields')
 if not isinstance(data.get('state'),str) or data['state'] not in STATES:raise ValueError('Invalid state')
 for key in ('updatedAt','lastPublishedAt'):
  value=data.get(key)
  if value is None and key=='lastPublishedAt':continue
  if not isinstance(value,str):raise ValueError('Invalid '+key)
  try:parsed=datetime.fromisoformat(value.replace('Z','+00:00'))
  except ValueError:raise ValueError('Invalid '+key) from None
  if parsed.tzinfo is None:raise ValueError(key+' must include timezone')
 for key,minimum in (('completedBatches',0),('totalBatches',1)):
  value=data.get(key)
  if value is not None and (type(value) is not int or not minimum<=value<=9007199254740991):raise ValueError('Invalid '+key)
 done,total=data.get('completedBatches'),data.get('totalBatches')
 if done is not None and total is not None and done>total:raise ValueError('Progress exceeds total')
 eta=data.get('etaSeconds')
 if eta is not None and (isinstance(eta,bool) or not isinstance(eta,(int,float)) or not math.isfinite(eta) or eta<0):raise ValueError('Invalid ETA')
 if not isinstance(data.get('message'),str) or len(data['message'].encode('utf-16-le'))//2>300:raise ValueError('Invalid message')
 return data

def write_status(target,data):
 validate(data);target=Path(target)
 normalized=dict(data)
 for key in ('updatedAt','lastPublishedAt'):
  if normalized[key] is not None:
   parsed=datetime.fromisoformat(normalized[key].replace('Z','+00:00'))
   normalized[key]=parsed.astimezone(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00','Z')
 raw=(json.dumps(normalized,ensure_ascii=False,allow_nan=False)+'\n').encode('utf-8')
 descriptor,name=tempfile.mkstemp(prefix='.'+target.name+'.',suffix='.tmp',dir=target.parent)
 try:
  with os.fdopen(descriptor,'wb') as stream:stream.write(raw);stream.flush();os.fsync(stream.fileno())
  os.replace(name,target)
 finally:
  if os.path.exists(name):os.unlink(name)

def main():
 p=argparse.ArgumentParser(description=__doc__)
 p.add_argument('--output',type=Path,required=True);p.add_argument('--state',choices=sorted(STATES),required=True)
 p.add_argument('--completed',type=int);p.add_argument('--total',type=int);p.add_argument('--eta-seconds',type=float)
 p.add_argument('--last-published-at');p.add_argument('--message',default='')
 a=p.parse_args()
 previous=json.loads(a.output.read_text(encoding='utf-8')) if a.output.exists() else None
 if previous is not None:validate(previous)
 write_status(a.output,{'format':'UIE_RENDER_STATUS1','state':a.state,'updatedAt':datetime.now(timezone.utc).isoformat(),
  'completedBatches':a.completed,'totalBatches':a.total,'etaSeconds':a.eta_seconds,
  'lastPublishedAt':a.last_published_at or (previous['lastPublishedAt'] if previous else None),'message':a.message})

if __name__=='__main__':main()
