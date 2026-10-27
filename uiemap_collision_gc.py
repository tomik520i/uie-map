"""Enable bounded maintenance only for a verified, configuration-owned collision output."""
from pathlib import Path
import os
import dimension_binding as binding
import collision_export as export
import collision_bundle as bundle
import collision_world_store as store_api
import collision_world_gc as gc
import collision_world_gc_init as initializer
import collision_world_gc_cli as maintenance


def collect(work):
    out=Path(work['out'])
    if out.resolve()!=out:raise ValueError('Linked collision output ancestors are not allowed')
    binding.ensure(out,work['binding'])
    lock=export._lock(out)
    if lock is None:raise ValueError('Collision output is busy; maintenance not initialized')
    try:
        binding.ensure(out,work['binding'])
        marker=out/gc.legacy.OWNED_MARKER
        if marker.exists() or marker.is_symlink():
            gc.legacy.check_owned_out(out)
        else:
            # Adoption is limited to our configuration-bound published output.
            # Check every referenced payload before marking it owned for GC.
            sha=store_api._read_pointer(out/'store')
            if sha is None:raise ValueError('No collision publication to protect')
            pairs=store_api._read_index(out/'store',sha)
            if not pairs:raise ValueError('Empty collision publication')
            for key,digest in pairs.items():bundle._verify_pair(out/'store',key,digest)
            with marker.open('xb') as file:
                file.write(b'UIE configured collision output\n');file.flush();os.fsync(file.fileno())
            bundle._fsync_directory(out)
    finally:lock.close()
    initialized=initializer.initialize(out,apply=True)
    if initialized['state'] not in ('initialized','already-initialized'):
        raise ValueError('Collision maintenance initialization: '+initialized['state'])
    report=maintenance.run_maintenance(out,apply=True,max_ticks=50,deadline_seconds=120,
        max_delete_files=64,max_delete_bytes=16*1024**2)
    if maintenance.exit_code(report)!=0:
        raise ValueError('Collision maintenance incomplete: '+str(report))
    return dict(initialization=initialized,collection=report)
