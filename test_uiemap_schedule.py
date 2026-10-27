import tempfile,unittest,os
from pathlib import Path
from unittest.mock import patch
import test_uiemap_render as fixtures
import uiemap_schedule as schedule
import uiemap_render as render


class ScheduleTests(unittest.TestCase):
    setUp=fixtures.RenderConfigTests.setUp
    save=fixtures.RenderConfigTests.save

    def test_plan_is_readonly_and_waits_after_completion(self):
        before=set(self.root.rglob('*'));work=schedule.plan(self.file,600)
        self.assertEqual(before,set(self.root.rglob('*')))
        timer=work['files'][work['name']+'.timer'];service=work['files'][work['name']+'.service']
        self.assertIn('OnUnitInactiveSec=600',timer);self.assertNotIn('OnUnitActiveSec=',timer)
        self.assertIn('KillSignal=SIGINT',service);self.assertIn('Type=oneshot',service)
        self.assertEqual(work['name'],schedule.plan(self.file,900)['name'])

    def test_exact_repeat_allowed_but_changed_units_not_overwritten(self):
        work=schedule.plan(self.file);directory=self.root/'units'
        schedule.install_files(work,directory);before={p.name:p.read_bytes() for p in directory.iterdir()}
        schedule.install_files(work,directory)
        with self.assertRaisesRegex(ValueError,'differs'):schedule.install_files(schedule.plan(self.file,600),directory)
        self.assertEqual(before,{p.name:p.read_bytes() for p in directory.iterdir()})

    def test_paths_escaped_and_invalid_interval_refused(self):
        self.assertEqual(schedule.quote('/tmp/a %n $VAR "q"'),'"/tmp/a %%n $$VAR \\"q\\""')
        with self.assertRaises(ValueError):schedule.quote('a\nb')
        for value in (True,0,59,86401):
            with self.assertRaises(ValueError):schedule.plan(self.file,value)

    def test_collection_only_in_opted_in_service(self):
        plain=schedule.plan(self.file);cleanup=schedule.plan(self.file,cleanup=True)
        self.assertNotIn('--gc',plain['files'][plain['name']+'.service'])
        self.assertIn('"--gc"',cleanup['files'][cleanup['name']+'.service'])

    def test_collision_schedule_is_explicit_and_surface_only(self):
        plain=schedule.plan(self.file);paired=schedule.plan(self.file,collisions=True)
        self.assertNotIn('--collisions',plain['files'][plain['name']+'.service'])
        self.assertIn('"--collisions"',paired['files'][paired['name']+'.service'])
        self.doc['dimensions'][0]['mode']='caves';self.save()
        with self.assertRaisesRegex(ValueError,'surface'):schedule.plan(self.file,collisions=True)

    def test_collision_cleanup_requires_export_and_is_explicit(self):
        with self.assertRaisesRegex(ValueError,'requires'):schedule.plan(self.file,collision_gc=True)
        plan=schedule.plan(self.file,collisions=True,collision_gc=True)
        self.assertIn('"--collision-gc"',plan['files'][plan['name']+'.service'])

    def test_cave_schedule_refuses_surface_gc_option(self):
        self.doc['dimensions'][0]['mode']='caves';self.save()
        self.assertTrue(schedule.plan(self.file)['readOnly'])
        with self.assertRaisesRegex(ValueError,'surface'):schedule.plan(self.file,cleanup=True)
        plan=schedule.plan(self.file,cave_cleanup=True)
        self.assertIn('"--cave-gc"',plan['files'][plan['name']+'.service'])

    @unittest.skipUnless(os.name=='posix','Linux render service')
    def test_render_interrupt_stops_its_owned_service(self):
        with patch.object(render.shutil,'which',return_value='/bin/systemd-run'),\
             patch.object(render,'free_space',return_value=100*1024**3),\
             patch.object(render.subprocess,'run',side_effect=[KeyboardInterrupt(),None]) as run:
            with self.assertRaises(KeyboardInterrupt):render.main([str(self.file),'--dimension','42','--apply'])
            stop=run.call_args_list[-1].args[0]
            self.assertEqual(stop[:3],['systemctl','--user','stop'])
            self.assertTrue(stop[3].startswith('uiemap-render-'))


if __name__=='__main__':unittest.main()
