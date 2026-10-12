"""Read-only bounded inventory of cave companion files, including hardlink accounting.

Allocated bytes count unique inodes inside this inventory, not space that deleting
these paths would reclaim: hardlinks outside cave-data may keep them alive.
No deletion, no world access; caller may hold the build lock for a stable snapshot.
"""
import os,re,stat
import hires_bundle as hb

KINDS={'objects':r'[0-9a-f]{64}\.uih','caps':r'[0-9a-f]{64}\.uic\.gz','pairs':r'[0-9a-f]{64}\.json','indexes':r'[0-9a-f]{64}\.json'}

def inspect_storage(out,*,max_entries=300000):
    if type(max_entries) is not int or not 1<=max_entries<=1000000:raise ValueError('Invalid inventory bound')
    out=hb._owned(out);root=out/'cave-data';hb._real_dir(root)
    kinds={};seen=set();total=logical=allocated=0;allocation_known=True
    for kind,pattern in KINDS.items():
        folder=root/kind;hb._real_dir(folder)
        count=unknown=size=links=0
        with os.scandir(folder) as entries:
            for entry in entries:
                total+=1
                if total>max_entries:raise ValueError('Cave inventory entry limit exceeded')
                # Windows DirEntry metadata may omit hardlink count; request full stat.
                info=os.stat(entry.path,follow_symlinks=False)
                if not stat.S_ISREG(info.st_mode):raise ValueError('Cave inventory contains symlink or nonregular entry')
                if not re.fullmatch(pattern,entry.name):unknown+=1;continue
                count+=1;size+=info.st_size;logical+=info.st_size
                if info.st_nlink>1:links+=1
                inode=(info.st_dev,info.st_ino)
                if inode not in seen:
                    seen.add(inode)
                    if hasattr(info,'st_blocks'):allocated+=info.st_blocks*512
                    else:allocation_known=False
        kinds[kind]={'files':count,'logicalBytes':size,'multiplyLinkedFiles':links,'unknownFiles':unknown}
    return {'inspectionOnly':True,'deleted':False,'scanned':total,'kinds':kinds,
            'logicalBytes':logical,'allocatedUniqueBytes':allocated if allocation_known else None,'uniqueInodes':len(seen),
            'reclaimableBytes':None,'note':'Allocation is not reclaimable space; references outside this inventory are not counted.'}
