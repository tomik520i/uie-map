import json,os,subprocess,sys,tempfile,time,unittest
from pathlib import Path
from unittest import mock
import owned_process as op

@unittest.skipUnless(sys.platform.startswith('linux'),'cgroup v2 needs Linux')
class CgroupOwnershipTests(unittest.TestCase):
    def test_membership_filters_before_environment_and_scope_survives_roundtrip(self):
        with tempfile.TemporaryDirectory() as temp, open(os.devnull,'wb') as unused:
            root=Path(temp);scope=op.create_scope(isolate_cgroup=True)
            self.assertEqual(json.loads(json.dumps(scope)),scope)
            self.assertNotEqual(scope['cgroup'],'/')
            with (root/'log').open('wb') as log:
                process=op.launch_owned([sys.executable,'-c','import pathlib,sys,time;pathlib.Path(sys.argv[1]).touch();time.sleep(30)',str(root/'ready')],cwd=root,log=log,scope=scope)
                try:
                    until=time.monotonic()+5
                    while not (root/'ready').exists() and time.monotonic()<until:time.sleep(.01)
                    self.assertTrue((root/'ready').exists())
                    self.assertEqual(op.inspect_owned(scope),[{'pid':process.pid}])
                    read=op._read_environ;group=op._cgroup_path
                    def unreadable(pid):
                        if pid==process.pid:raise PermissionError('fixture')
                        return read(pid)
                    def elsewhere(pid):return '/different-test-scope' if pid==process.pid else group(pid)
                    with mock.patch.object(op,'_read_environ',unreadable),mock.patch.object(op,'_cgroup_path',elsewhere):
                        self.assertEqual(op.inspect_owned(scope),[])
                    with mock.patch.object(op,'_read_environ',unreadable):
                        with self.assertRaises(op.ScanError):op.inspect_owned(scope)
                    changed=dict(scope,notBeforeTicks=max(0,scope['notBeforeTicks']-1))
                    with self.assertRaises(op.OwnedProcessError):op.stop_owned(changed,process=process)
                    self.assertIsNone(process.poll())
                finally:op.stop_owned(scope,process=process,term_timeout=1,kill_timeout=3)
                self.assertEqual(op.inspect_owned(scope),[])

    def test_cross_cgroup_launch_is_refused(self):
        scope=op.create_scope(isolate_cgroup=True);scope['cgroup']='/nonexistent-unrelated-test'
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaisesRegex(op.OwnedProcessError,'outside'):
                op.launch_owned([sys.executable,'-c','pass'],cwd=temp,log=subprocess.DEVNULL,scope=scope)

def cgroup_file(raw_for):
    """Patch /proc/<pid>/cgroup contents: raw_for(pid) returns bytes, or None for the real file."""
    real = op._cgroup_bytes
    return mock.patch.object(op, '_cgroup_bytes', lambda pid: raw_for(pid) if raw_for(pid) is not None else real(pid))


@unittest.skipUnless(sys.platform.startswith('linux'), 'cgroup v2 needs Linux')
class CgroupPathTests(unittest.TestCase):
    def test_non_ascii_paths_are_exact_and_non_utf8_never_raises_or_matches(self):
        me = os.getpid()
        utf8 = b'0::/user.slice/uie-w\xc3\xa4rm.service\n'
        with cgroup_file(lambda pid: utf8 if pid == me else None):
            self.assertEqual(op._cgroup_path(me), '/user.slice/uie-wärm.service')
            self.assertEqual(op.check_cgroup_isolation(), '/user.slice/uie-wärm.service')
            scope = op.create_scope(isolate_cgroup=True)
        stored = json.loads(json.dumps(scope))   # ensure_ascii escaping round-trips exactly
        self.assertEqual(stored, scope)
        op._check_scope_schema(stored)
        # Raw byte 0xff is not the UTF-8 encoding of U+00FF: decoded losslessly, it never equals the text path.
        with cgroup_file(lambda pid: b'0::/user.slice/bad\xff.service\n' if pid == me else None):
            path = op._cgroup_path(me)
            self.assertFalse(op._in_cgroup(path, '/user.slice/badÿ.service'))
            with self.assertRaisesRegex(op.UnsupportedPlatformError, 'UTF-8'):
                op.check_cgroup_isolation()
            with self.assertRaises(op.UnsupportedPlatformError):
                op.create_scope(isolate_cgroup=True)

    def test_unusable_isolation_is_one_documented_error_and_plain_scopes_still_work(self):
        me = os.getpid()
        cases = {'cgroup v1 only': b'1:name=systemd:/user.slice\n', 'root cgroup': b'0::/\n',
                 'two unified rows': b'0::/a.service\n0::/b.service\n', 'relative': b'0::a.service\n',
                 'escaped unit name': b'0::/app.slice/uie\\x2dw.service\n', 'empty': b''}
        for label, raw in cases.items():
            with self.subTest(label), cgroup_file(lambda pid, raw=raw: raw if pid == me else None):
                with self.assertRaises(op.UnsupportedPlatformError):
                    op.check_cgroup_isolation()
                with self.assertRaises(op.UnsupportedPlatformError):
                    op.create_scope(isolate_cgroup=True)
                self.assertNotIn('cgroup', op.create_scope())   # isolation is optional
        for bad in ('/', 'relative', '/a//b', '/a/../b', '/a\\b', '/a\nb', '/bad\udcff'):
            with self.subTest(scope_cgroup=bad), self.assertRaises(op.OwnedProcessError):
                op._check_scope_schema(dict(op.create_scope(), cgroup=bad))

    def test_scan_handles_foreign_non_utf8_members_without_decoding_errors(self):
        try:
            op.check_cgroup_isolation()
        except op.UnsupportedPlatformError as exc:
            self.skipTest(f'no usable cgroup v2 isolation here: {exc}')
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            scope = op.create_scope(isolate_cgroup=True)
            with (root / 'log').open('wb') as log:
                process = op.launch_owned([sys.executable, '-c', 'import time;time.sleep(30)'], cwd=root, log=log,
                                          scope=scope)
                try:
                    inside = b'0::' + scope['cgroup'].encode() + b'/sub\xff\n'
                    outside = b'0::/elsewhere\xff.service\n'
                    until = time.monotonic() + 5
                    while op.inspect_owned(scope) != [{'pid': process.pid}] and time.monotonic() < until:
                        time.sleep(.02)
                    with cgroup_file(lambda pid: outside if pid == process.pid else None):
                        self.assertEqual(op.inspect_owned(scope), [], 'a non-UTF-8 path elsewhere is simply outside')
                    with cgroup_file(lambda pid: inside if pid == process.pid else None):
                        self.assertEqual(op.inspect_owned(scope), [{'pid': process.pid}],
                                         'a non-UTF-8 child cgroup below the recorded one is inside')
                finally:
                    op.stop_owned(scope, process=process, term_timeout=1, kill_timeout=3)
            self.assertEqual(op.inspect_owned(scope), [])


if __name__=='__main__':unittest.main()
