"""Standalone MoviePilot-free tests: all deletion and disk usage are mocked."""
import importlib.util
import sys
import threading
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
GIB = 1024 ** 3
for name in ['app', 'app.log', 'app.plugins', 'app.schemas', 'app.helper',
             'app.helper.mediaserver', 'app.chain', 'app.chain.media', 'app.core', 'app.core.metainfo']:
    sys.modules.setdefault(name, types.ModuleType(name))
sys.modules['app.log'].logger = MagicMock()
sys.modules['app.plugins']._PluginBase = type('PluginBase', (), {})
sys.modules['app.schemas'].NotificationType = types.SimpleNamespace(Plugin='plugin')
sys.modules['app.schemas'].MediaType = MagicMock()
sys.modules['app.helper.mediaserver'].MediaServerHelper = MagicMock()
sys.modules['app.chain.media'].MediaChain = MagicMock()
sys.modules['app.core.metainfo'].MetaInfo = MagicMock()
spec = importlib.util.spec_from_file_location('cleaner_under_test', ROOT / '__init__.py', submodule_search_locations=[str(ROOT)])
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)


def usage(free):
    return types.SimpleNamespace(free=int(free * GIB), total=1000 * GIB)


def candidate(n, size=10):
    return {'path': f'/mock/media/{n}', 'name': str(n), 'size': size * GIB, 'size_gb': size, 'score': 100-n}


class CleanerTests(unittest.TestCase):
    def setUp(self):
        self.p = module.DiskSpaceAutoCleaner()
        self.p._enabled = True
        self.p._dry_run = False
        self.p._notify = True
        self.p._monitor_paths = '/mock/disk'
        self.p._media_paths = '/mock/media'
        self.p._strategy_profiles = ''
        self.p._scan_state = {}
        self.p._strategy_scan_totals = {}
        self.p._max_candidates = 30
        self.p._max_delete_gb = 1000
        self.p._check_running = False
        self.p._lock = threading.Lock()
        self.p._save_record = MagicMock()
        self.p._persist_config = MagicMock()
        self.p.post_message = MagicMock()
        self.deleter = MagicMock()
        self.deleter.measure_for_budget.side_effect = lambda item, *args: item['size']
        self.deleter.delete_selected.side_effect = lambda items, **kwargs: (items, [])

    def run_check(self, candidates=None, spaces=None):
        with patch.object(module, 'DiskSpaceScanner') as scanner, patch.object(module, 'DiskSpaceDeleter', return_value=self.deleter), patch.object(module.shutil, 'disk_usage', side_effect=spaces or [usage(5), usage(30)]):
            scanner.return_value.build_candidates.return_value = (candidates or [], {})
            self.p._check_space_and_report()
            return scanner.return_value

    def test_equal_threshold_triggers_and_old_cooldown_ignored(self):
        self.p._scan_state = {'/mock/disk::/mock/disk': {'next_allowed_scan_at': 10**20, 'low_space_scan_streak': 999999}}
        scanner = self.run_check([candidate(1)])
        scanner.build_candidates.assert_called_once()
        self.deleter.delete_selected.assert_called_once()

    def test_healthy_does_not_scan(self):
        scanner = self.run_check(spaces=[usage(5.01)])
        scanner.build_candidates.assert_not_called()

    def test_running_skip_does_not_unlock_owner(self):
        self.p._check_running = True
        self.p._check_space_and_report = MagicMock()
        self.p._run_check(schedule_next=False)
        self.assertTrue(self.p._check_running)
        self.p._check_space_and_report.assert_not_called()

    def test_run_releases_own_lock_on_exception(self):
        self.p._check_space_and_report = MagicMock(side_effect=RuntimeError('error'))
        self.p._run_check(schedule_next=False)
        self.assertFalse(self.p._check_running)

    def test_no_time_dedupe_blocks_sequential_runs(self):
        self.p._check_space_and_report = MagicMock()
        self.p._run_check(schedule_next=False)
        self.p._run_check(schedule_next=False)
        self.assertEqual(self.p._check_space_and_report.call_count, 2)

    def test_actual_target_stops_after_first(self):
        self.run_check([candidate(1), candidate(2)])
        self.assertEqual(self.deleter.delete_selected.call_count, 1)
        self.assertIn('已达目标', self.p.post_message.call_args.kwargs['text'])

    def test_estimate_does_not_truncate_when_no_space_released(self):
        self.run_check([candidate(1, 30), candidate(2, 30), candidate(3, 30)],
                       [usage(5), usage(5), usage(10), usage(30)])
        self.assertEqual(self.deleter.delete_selected.call_count, 3)

    def test_failure_charged_full_size_and_later_small_candidate_allowed(self):
        self.p._max_delete_gb = 25
        self.deleter.delete_selected.side_effect = lambda items, **kw: ([], ['partial failure'])
        self.run_check([candidate(1, 20), candidate(2, 20), candidate(3, 5)], [usage(5), usage(5), usage(5)])
        self.assertEqual(self.deleter.delete_selected.call_count, 2)
        d = self.p._save_record.call_args.kwargs['diagnosis']
        self.assertEqual(d['charged_gb'], 25)
        self.assertIn('上限', d['stop_reason'])
        self.assertIn('未明显增加', d['stop_reason'])
        self.assertIn('删除失败', self.p.post_message.call_args.kwargs['text'])

    def test_attempt_count_charged_on_exception(self):
        self.p._max_candidates = 1
        self.deleter.delete_selected.side_effect = RuntimeError('partial')
        self.run_check([candidate(1), candidate(2)], [usage(5), usage(5)])
        self.assertEqual(self.deleter.delete_selected.call_count, 1)
        self.assertEqual(self.p._save_record.call_args.kwargs['diagnosis']['attempted_count'], 1)

    def test_fresh_budget_measurement_not_cached_estimate(self):
        self.p._max_delete_gb = 15
        self.deleter.measure_for_budget.return_value = 20 * GIB
        self.deleter.measure_for_budget.side_effect = None
        self.run_check([candidate(1, 1)], [usage(5)])
        self.deleter.delete_selected.assert_not_called()
        self.assertIn('上限', self.p.post_message.call_args.kwargs['text'])

    def test_dry_run_never_deletes_or_claims_failure(self):
        self.p._dry_run = True
        self.run_check([candidate(1)], [usage(5)])
        self.deleter.delete_selected.assert_not_called()
        self.deleter.measure_for_budget.assert_not_called()
        self.assertIn('模拟报告', self.p.post_message.call_args.kwargs['text'])
        self.assertNotIn('清理后空间仍不足', self.p.post_message.call_args.kwargs['text'])

    def test_no_candidates_alert_and_notify_switch(self):
        self.run_check([], [usage(5)])
        self.assertIn('无候选', self.p.post_message.call_args.kwargs['text'])
        self.p._notify = False
        self.p.post_message.reset_mock()
        self.run_check([], [usage(5)])
        self.p.post_message.assert_not_called()

    def test_notification_dedupe_does_not_block_cleanup(self):
        for _ in range(2):
            self.run_check([candidate(1)], [usage(5), usage(5)])
        self.assertEqual(self.deleter.delete_selected.call_count, 2)
        self.assertEqual(self.p.post_message.call_count, 1)

    def test_one_disk_exception_continues_other_disk(self):
        self.p._monitor_paths = '/mock/disk1\n/mock/disk2'
        scanner = self.run_check([candidate(1)], [OSError('unavailable'), usage(5), usage(30)])
        scanner.build_candidates.assert_called_once()
        self.deleter.delete_selected.assert_called_once()
        self.assertEqual(self.p.post_message.call_count, 2)

    def test_invalid_configuration_refuses_scan_and_delete(self):
        for attr, value in [('_target_free_gb', 5), ('_target_free_gb', 4),
                            ('_min_free_gb', -1), ('_max_delete_gb', 0), ('_max_candidates', 0)]:
            with self.subTest(attr=attr, value=value):
                old = getattr(self.p, attr)
                setattr(self.p, attr, value)
                scanner = self.run_check([candidate(1)], [usage(5)])
                scanner.build_candidates.assert_not_called()
                setattr(self.p, attr, old)
        self.deleter.delete_selected.assert_not_called()

    def test_default_interval_and_explicit_60_preserved(self):
        self.assertEqual(module.DiskSpaceAutoCleaner._scan_interval_minutes, 5)
        self.p.stop_service = MagicMock()
        self.p._schedule_next = MagicMock()
        self.p._build_deleted_history_from_logs = MagicMock(return_value=[])
        self.p.init_plugin({'scan_interval_minutes': 60, 'scan_backoff_multiplier': 'invalid', 'scan_cooldown_minutes': 10**99})
        self.assertEqual(self.p._scan_interval_minutes, 60)
        self.p.init_plugin({'notify': False})
        self.assertEqual(self.p._scan_interval_minutes, 5)
        self.assertNotIn('cooldown', str(self.p._build_form_data([])))

    def test_size_recheck_rejected_does_not_delete(self):
        self.deleter.measure_for_budget.side_effect = ValueError('protected')
        self.run_check([candidate(1)], [usage(5)])
        self.deleter.delete_selected.assert_not_called()
        self.assertIn('保护', self.p.post_message.call_args.kwargs['text'])

    def test_actual_measurement_boundaries_with_mock_paths(self):
        deleter = module.DiskSpaceDeleter(self.p)
        with patch.object(module.Path, 'resolve', return_value=Path('/mock/media')):
            with self.assertRaises(ValueError):
                deleter.measure_for_budget(candidate(1), ['/mock/media'], Path('/mock'))

    def test_uncached_measure_sums_all_files_and_rejects_stat_error(self):
        from cleaner_under_test.deleter import DiskSpaceDeleter
        deleter = DiskSpaceDeleter(self.p)
        self.p._recent_days_protect = 0
        self.p._protect_dirs = ''
        self.p._protect_keywords = ''
        directory = Path('/mock/media/title')
        files = [directory / 'a.mkv', directory / 'b.mkv']
        def stat(node):
            return types.SimpleNamespace(st_dev=7, st_size=11 if node.name == 'a.mkv' else 19)
        with patch.object(Path, 'resolve', lambda node, **kw: node), \
             patch.object(Path, 'is_symlink', return_value=False), \
             patch.object(Path, 'stat', stat), patch.object(Path, 'lstat', stat), \
             patch.object(Path, 'is_dir', lambda node: node == directory), \
             patch.object(Path, 'is_file', lambda node: node in files), \
             patch.object(Path, 'iterdir', return_value=iter(files)):
            self.assertEqual(deleter.measure_for_budget({'path': str(directory)}, ['/mock/media'], Path('/mock')), 30)
            with patch.object(Path, 'lstat', side_effect=PermissionError('blocked')):
                with self.assertRaises(PermissionError):
                    deleter.measure_for_budget({'path': str(directory)}, ['/mock/media'], Path('/mock'))

    def test_measure_rejects_symlinks_and_cross_device(self):
        from cleaner_under_test.deleter import DiskSpaceDeleter
        deleter = DiskSpaceDeleter(self.p)
        self.p._recent_days_protect = 0
        with patch.object(Path, 'resolve', lambda node, **kw: node), \
             patch.object(Path, 'is_symlink', return_value=True):
            with self.assertRaises(ValueError):
                deleter.measure_for_budget(candidate(1), ['/mock/media'], Path('/mock'))
        with patch.object(Path, 'resolve', lambda node, **kw: node), \
             patch.object(Path, 'is_symlink', return_value=False), \
             patch.object(Path, 'stat', return_value=types.SimpleNamespace(st_dev=7)), \
             patch.object(Path, 'lstat', return_value=types.SimpleNamespace(st_dev=8)):
            with self.assertRaises(ValueError):
                deleter.measure_for_budget(candidate(1), ['/mock/media'], Path('/mock'))

    def test_repeated_actual_lock_contention_keeps_owner(self):
        entered, release = threading.Event(), threading.Event()
        def blocking_check():
            entered.set()
            release.wait(2)
        self.p._check_space_and_report = MagicMock(side_effect=blocking_check)
        owner = threading.Thread(target=self.p._run_check, kwargs={'schedule_next': False})
        owner.start()
        self.assertTrue(entered.wait(1))
        try:
            self.p._run_check(schedule_next=False)
            self.p._run_check(schedule_next=False)
            self.assertTrue(self.p._check_running)
            self.assertEqual(self.p._check_space_and_report.call_count, 1)
        finally:
            release.set()
            owner.join(2)
        self.assertFalse(self.p._check_running)


if __name__ == '__main__':
    unittest.main()
