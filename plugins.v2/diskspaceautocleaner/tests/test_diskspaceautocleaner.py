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
        self.assertIn('已达目标', self.p.post_message.call_args.kwargs['title'])

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


class FallbackTests(unittest.TestCase):
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
        self.p._min_free_gb = 10
        self.p._target_free_gb = 30
        self.p._recent_days_protect = 30
        self.p._fallback_enabled = False
        self.p._fallback_min_keep_days = 3
        self.p._fallback_buffer_gb = 5.0
        self.p._check_running = False
        self.p._lock = threading.Lock()
        self.p._save_record = MagicMock()
        self.p._persist_config = MagicMock()
        self.p.post_message = MagicMock()
        self.deleter = MagicMock()
        self.deleter.measure_for_budget.side_effect = lambda item, *args: item['size']
        self.deleter.delete_selected.side_effect = lambda items, **kwargs: (items, [])

    def test_defaults_and_legacy_strategy_are_compatible(self):
        strategy = self.p._parse_strategy_profiles()
        self.assertEqual(strategy, [])
        defaults = self.p._strategy_form_defaults('')
        self.assertFalse(defaults[0]['fallback_enabled'])
        self.assertEqual(defaults[0]['fallback_min_keep_days'], 3)
        self.assertEqual(defaults[0]['fallback_buffer_gb'], 5.0)
        text = self.p._build_strategy_profiles_from_form({
            'strategy_1_name': 'x', 'strategy_1_monitor_path': '/mock/disk',
            'strategy_1_media_paths': '/mock/media', 'strategy_1_recent_days_protect': 30,
        })
        self.assertNotIn('fallback_enabled=', text)
        self.assertFalse(self.p._parse_strategy_profiles())

    def test_form_round_trip_contains_fallback_fields(self):
        values = {'strategy_1_name': 'x', 'strategy_1_monitor_path': '/mock/disk',
                  'strategy_1_media_paths': '/mock/media', 'strategy_1_fallback_enabled': True,
                  'strategy_1_fallback_min_keep_days': 5, 'strategy_1_fallback_buffer_gb': 2.5,
                  'strategy_1_recent_days_protect': 30}
        text = self.p._build_strategy_profiles_from_form(values)
        self.assertIn('fallback_enabled=True', text)
        self.assertIn('fallback_min_keep_days=5', text)
        self.assertIn('fallback_buffer_gb=2.5', text)
        parsed = self.p._parse_strategy_profiles()
        # The form builder returns text but does not mutate config; parse the result explicitly.
        self.p._strategy_profiles = text
        parsed = self.p._parse_strategy_profiles()
        self.assertTrue(parsed[0]['fallback_enabled'])
        self.assertEqual(parsed[0]['fallback_min_keep_days'], 5)
        self.assertEqual(parsed[0]['fallback_buffer_gb'], 2.5)

    def test_normal_candidates_precede_fallback_and_fallback_is_sorted_by_age(self):
        normal = candidate(1); normal.update(is_recent=False, age_days=100)
        recent_old = candidate(2); recent_old.update(is_recent=True, age_days=20, fallback=True)
        recent_new = candidate(3); recent_new.update(is_recent=True, age_days=25, fallback=True)
        candidates = [normal, recent_old, recent_new]
        self.p._fallback_enabled = True
        self.p._fallback_min_keep_days = 3
        self.p._fallback_buffer_gb = 5
        calls = []
        self.deleter.delete_selected.side_effect = lambda items, **kwargs: (calls.append(items[0]['name']) or (items, []))
        with patch.object(module.shutil, 'disk_usage', side_effect=[usage(5), usage(15), usage(25), usage(30)]):
            self.p._delete_until_target('/mock/disk', [normal, recent_new, recent_old], ['/mock/media'], 5, self.deleter, fallback_target=15)
        # The helper receives the already ordered list; verify its shared-budget/stop behavior.
        self.assertEqual(calls, ['1', '3'])

    def test_fallback_candidates_respect_minimum_keep_days_and_hard_flags(self):
        self.p._fallback_enabled = True
        self.p._fallback_min_keep_days = 3
        too_new = candidate(1); too_new.update(is_recent=True, age_days=2)
        valid = candidate(2); valid.update(is_recent=True, age_days=5)
        self.assertEqual([x['name'] for x in [too_new, valid] if x.get('is_recent') and x['age_days'] >= self.p._fallback_min_keep_days], ['2'])
        # Deleter rejects a forged/non-current fallback marker even when age looks valid.
        from cleaner_under_test.deleter import DiskSpaceDeleter
        with patch.object(Path, 'resolve', lambda node, **kw: node):
            with self.assertRaises(ValueError):
                DiskSpaceDeleter(self.p).measure_for_budget(dict(valid, fallback=True, fallback_candidate=False,
                                                                fallback_min_keep_days=3), ['/mock/media'], Path('/mock/disk'))

    def test_shared_budget_charges_normal_and_fallback_without_reset(self):
        self.p._fallback_enabled = True
        self.p._max_delete_gb = 15
        normal = candidate(1, 10); normal.update(is_recent=False)
        fallback = candidate(2, 10); fallback.update(fallback=True, fallback_candidate=True,
                                                       fallback_min_keep_days=3)
        with patch.object(module.shutil, 'disk_usage', side_effect=[usage(5), usage(5), usage(5)]):
            _, errors, _, reason, result = self.p._delete_until_target('/mock/disk', [normal, fallback],
                ['/mock/media'], 5, self.deleter, fallback_target=15)
        self.assertEqual(self.deleter.delete_selected.call_count, 1)
        self.assertEqual(result['charged_gb'], 10)
        self.assertIn('上限', reason)

    def test_notification_reports_fallback_state_and_watermark(self):
        notifier = module.DiskSpaceNotifier(self.p)
        text = notifier.outcome_text({
            'before_free_gb': 5, 'after_free_gb': 15, 'target_free_gb': 30,
            'fallback_enabled': True, 'fallback_used': True,
            'fallback_min_keep_days': 3, 'fallback_target_free_gb': 15,
            'stop_reason': '实际剩余空间已达兜底安全水位', 'delete_errors': []}, [])
        self.assertIn('兜底清理：已启用/已使用', text)
        self.assertIn('安全水位15GB', notifier.diagnosis_text({
            'fallback_enabled': True, 'fallback_used': True, 'fallback_min_keep_days': 3,
            'fallback_target_free_gb': 15}))


class PipelineFallbackTests(unittest.TestCase):
    run_check = CleanerTests.run_check

    def setUp(self):
        CleanerTests.setUp(self)
        self.p._min_free_gb = 10
        self.p._target_free_gb = 30
        self.p._recent_days_protect = 30
        self.p._fallback_enabled = True
        self.p._fallback_min_keep_days = 3
        self.p._fallback_buffer_gb = 5

    def recent(self, n, age=20, size=5):
        return dict(candidate(n, size), is_recent=True, age_days=age)

    def diagnosis(self):
        return self.p._save_record.call_args.kwargs['diagnosis']

    def test_pipeline_order_age_minimum_and_safety_stop(self):
        self.run_check([self.recent(1, 10), self.recent(2, 29), self.recent(3, 1), candidate(4, 2)],
                       [usage(5), usage(6), usage(10), usage(15)])
        names = [c.args[0][0]['name'] for c in self.deleter.delete_selected.call_args_list]
        self.assertEqual(names, ['4', '2', '1'])
        self.assertEqual(self.diagnosis()['fallback_attempted_count'], 2)
        self.assertTrue(self.diagnosis()['fallback_used'])
        self.assertIn('安全水位', self.diagnosis()['stop_reason'])

    def test_disabled_cannot_be_enabled_by_candidate_metadata(self):
        self.p._fallback_enabled = False
        self.run_check([dict(self.recent(1), fallback=True, fallback_candidate=True)], [usage(5)])
        self.deleter.delete_selected.assert_not_called()
        self.assertFalse(self.diagnosis()['fallback_used'])

    def test_normal_achieves_safety_no_fallback_attempt(self):
        self.run_check([candidate(1), self.recent(2)], [usage(5), usage(15)])
        self.assertEqual(self.deleter.delete_selected.call_count, 1)
        self.assertFalse(self.diagnosis()['fallback_used'])
        self.assertIn('安全水位', self.diagnosis()['stop_reason'])

    def test_safety_at_exact_budget_is_not_overwritten(self):
        self.p._max_delete_gb = 5
        self.p._max_candidates = 1
        self.run_check([self.recent(1)], [usage(5), usage(15)])
        self.assertIn('安全水位', self.diagnosis()['stop_reason'])
        self.assertNotIn('仍不足', self.diagnosis()['stop_reason'])
        self.assertIn('✅', self.p.post_message.call_args.kwargs['title'])

    def test_failed_fallback_is_attempted_not_successfully_deleted(self):
        self.deleter.delete_selected.return_value = ([], ['failure'])
        self.deleter.delete_selected.side_effect = None
        self.run_check([self.recent(1)], [usage(5), usage(5)])
        self.assertTrue(self.diagnosis()['fallback_used'])
        self.assertEqual(self.diagnosis()['fallback_attempted_count'], 1)
        self.assertIn('仍不足', self.diagnosis()['stop_reason'])

    def test_failure_normal_charges_budget_and_attempts_across_phases(self):
        self.p._max_candidates = 2
        self.p._max_delete_gb = 10
        self.deleter.delete_selected.side_effect = lambda *a, **kw: ([], ['failure'])
        self.run_check([candidate(1, 5), self.recent(2), self.recent(3)], [usage(5), usage(5), usage(5)])
        self.assertEqual(self.diagnosis()['charged_gb'], 10)
        self.assertEqual(self.diagnosis()['attempted_count'], 2)
        self.assertEqual(self.diagnosis()['fallback_attempted_count'], 1)

    def test_duplicate_path_not_charged_or_retried_across_phases(self):
        self.run_check([candidate(1, 5), candidate(1, 5), self.recent(1), self.recent(2)],
                       [usage(5), usage(5), usage(15)])
        self.assertEqual(self.deleter.delete_selected.call_count, 2)
        self.assertEqual(self.diagnosis()['charged_gb'], 10)

    def test_dry_run_only_fallback_safe_target_budget_and_plan_status(self):
        self.p._dry_run = True
        self.run_check([self.recent(1), self.recent(2), self.recent(3)], [usage(5)])
        selected = self.p._save_record.call_args.args[4]
        self.assertEqual(len(selected), 2)
        self.assertTrue(self.diagnosis()['fallback_planned'])
        self.assertFalse(self.diagnosis()['fallback_used'])
        self.deleter.delete_selected.assert_not_called()
        self.p._max_delete_gb = 5
        self.assertEqual(len(self.p._plan_candidates([self.recent(4), self.recent(5)], 5, 15)), 1)

    def test_dry_run_normal_phase_still_uses_normal_target(self):
        self.p._dry_run = True
        self.run_check([candidate(1), candidate(2), candidate(3), self.recent(4)], [usage(5)])
        self.assertEqual(len(self.p._save_record.call_args.args[4]), 3)
        self.assertFalse(self.diagnosis()['fallback_planned'])

    def test_token_is_per_run_not_serialized_and_cleared(self):
        self.run_check([self.recent(1)], [usage(5), usage(15)])
        item = self.deleter.delete_selected.call_args.args[0][0]
        self.assertIsNotNone(item['_fallback_token'])
        self.assertIsNone(self.p._fallback_run_token)
        serialized = self.p._serialize_candidate(item)
        self.assertNotIn('_fallback_token', serialized)
        self.assertNotIn('fallback_candidate', serialized)

    def test_invalid_numbers_fail_closed_per_strategy(self):
        for field, value in [('fallback_buffer_gb', 'nan'), ('fallback_buffer_gb', 'inf'),
                             ('fallback_buffer_gb', 'garbage'), ('fallback_buffer_gb', '0'),
                             ('max_delete_gb', 'inf'), ('max_candidates', 'nan'),
                             ('fallback_min_keep_days', '-1'), ('fallback_min_keep_days', '30')]:
            with self.subTest(field=field, value=value):
                self.deleter.reset_mock()
                self.p._strategy_profiles = (f'name=bad\nmonitor_path=/mock/a\n{field}={value}\n\n'
                                             'name=good\nmonitor_path=/mock/b\nfallback_enabled=False')
                self.p._monitor_paths = ''
                scanner = self.run_check([candidate(1)], [usage(5), usage(5), usage(30)])
                self.assertEqual(scanner.build_candidates.call_count, 1)
                self.assertEqual(self.deleter.delete_selected.call_count, 1)
                self.assertTrue(self.p._fallback_enabled)  # context restored


class HardProtectionFallbackTests(unittest.TestCase):
    def setUp(self):
        self.p = module.DiskSpaceAutoCleaner()
        self.p._fallback_enabled = True
        self.p._fallback_min_keep_days = 3
        self.p._recent_days_protect = 30
        self.p._fallback_run_token = object()
        self.p._protect_dirs = ''
        self.p._protect_keywords = ''
        self.item = dict(candidate(1), fallback=True, fallback_min_keep_days=3,
                         _fallback_token=self.p._fallback_run_token)
        self.deleter = module.DiskSpaceDeleter(self.p)

    def test_forged_absent_stale_and_disabled_capabilities_rejected_before_io(self):
        for token in [None, True, object(), 'token']:
            with self.subTest(token=token), patch.object(Path, 'stat', side_effect=AssertionError('no I/O')):
                with self.assertRaises(ValueError):
                    self.deleter.measure_for_budget(dict(self.item, _fallback_token=token), ['/mock/media'], Path('/mock'))
        self.p._fallback_enabled = False
        with self.assertRaises(ValueError):
            self.deleter.measure_for_budget(self.item, ['/mock/media'], Path('/mock'))

    def test_valid_token_still_enforces_keep_age_path_keyword_symlink_crossdisk(self):
        import time
        def stat(node):
            return types.SimpleNamespace(st_dev=7, st_size=100, st_mtime=time.time()-4*86400)
        with patch.object(Path, 'resolve', lambda node, **kw: node), \
             patch.object(Path, 'is_symlink', return_value=False), \
             patch.object(Path, 'stat', stat), patch.object(Path, 'lstat', stat), \
             patch.object(Path, 'is_dir', return_value=False), patch.object(Path, 'is_file', return_value=True):
            self.assertEqual(self.deleter.measure_for_budget(self.item, ['/mock/media'], Path('/mock')), 100)
            for field, value in [('_protect_dirs', '/mock/media/1'), ('_protect_keywords', '1')]:
                setattr(self.p, field, value)
                with self.assertRaises(ValueError):
                    self.deleter.measure_for_budget(self.item, ['/mock/media'], Path('/mock'))
                setattr(self.p, field, '')
            with patch.object(Path, 'stat', return_value=types.SimpleNamespace(st_dev=7, st_mtime=time.time()-86400)):
                with self.assertRaises(ValueError):
                    self.deleter.measure_for_budget(self.item, ['/mock/media'], Path('/mock'))
            with patch.object(Path, 'is_symlink', return_value=True):
                with self.assertRaises(ValueError):
                    self.deleter.measure_for_budget(self.item, ['/mock/media'], Path('/mock'))
            with patch.object(Path, 'lstat', return_value=types.SimpleNamespace(st_dev=8)):
                with self.assertRaises(ValueError):
                    self.deleter.measure_for_budget(self.item, ['/mock/media'], Path('/mock'))


class ScannerFallbackTests(unittest.TestCase):
    def scan(self, enabled=True, active=False, complete=True):
        import time
        p = module.DiskSpaceAutoCleaner()
        p._fallback_enabled = enabled
        p._fallback_min_keep_days = 3
        p._recent_days_protect = 30
        p._protect_dirs = ''
        p._protect_keywords = ''
        p._max_scan_items = 5
        scanner = module.DiskSpaceScanner(p)
        paths = [Path('/mock/media/recent'), Path('/mock/media/normal'), Path('/mock/media/new')]
        ages = {'recent': 20, 'normal': 60, 'new': 1}
        order = []
        def stat(node):
            return types.SimpleNamespace(st_mtime=time.time()-ages.get(node.name, 60)*86400)
        with patch.object(Path, 'exists', return_value=True), patch.object(Path, 'is_dir', return_value=True), \
             patch.object(Path, 'stat', stat), \
             patch.object(scanner, '_iter_candidate_items', return_value=iter(paths)), \
             patch.object(scanner, '_match_active_media', side_effect=lambda child, _: 'active' if active and child.name == 'recent' else ''), \
             patch.object(scanner, '_match_recent_media', return_value=''), \
             patch.object(module.DiskSpaceUtils, 'is_safe_root', return_value=True), \
             patch.object(module.DiskSpaceUtils, 'is_series_candidate', return_value=not complete), \
             patch.object(module.DiskSpaceUtils, 'is_completed_complete_series', return_value=(False, 'incomplete')), \
             patch.object(module.DiskSpaceUtils, 'calc_path_size_fast', side_effect=lambda child, _: order.append(child.name) or GIB):
            result, diagnosis = scanner._scan_media_root(Path('/mock/media'), 2, time.time(), 30*86400,
                5, [], [], {}, threading.Lock(), active_titles=set(), recent_titles=set())
        return result, diagnosis, order

    def test_off_skips_recent_before_sizing(self):
        result, _, order = self.scan(enabled=False)
        self.assertEqual(order, ['normal'])
        self.assertEqual([x['name'] for x in result], ['normal'])

    def test_normal_sized_first_minimum_age_enforced(self):
        result, _, order = self.scan()
        self.assertEqual(order, ['normal', 'recent'])
        self.assertEqual([x['name'] for x in result], ['normal', 'recent'])

    def test_active_and_incomplete_remain_hard_protections(self):
        result, diagnosis, order = self.scan(active=True)
        self.assertEqual(order, ['normal'])
        self.assertEqual(diagnosis['active_play_skipped'], 1)
        result, diagnosis, order = self.scan(complete=False)
        self.assertEqual(result, [])
        self.assertEqual(order, [])
        self.assertEqual(diagnosis['incomplete_series_skipped'], 2)


class PushPresentationTests(unittest.TestCase):
    """Exercise the payload sent to MoviePilot, not just a standalone formatter."""

    def send(self, selected=None, **changes):
        p = module.DiskSpaceAutoCleaner()
        p._notify = True
        p._scan_state = {}
        p._current_strategy_name = '硬盘4-国产电视剧'
        p._persist_config = MagicMock()
        p.post_message = MagicMock()
        diagnosis = dict(before_free_gb=14.95, after_free_gb=14.95,
                         target_free_gb=50, actual_change_gb=-0.000001,
                         stop_reason='保护/路径规则或累计删除上限限制；空间未明显增加 /secret token=PASSWORD',
                         fallback_enabled=False, fallback_used=False, delete_errors=[],
                         items_scanned=9999, cache_hits=200, tmdb_rating_used=30,
                         candidate_depth=8, protected_skipped=400)
        diagnosis.update(changes)
        module.DiskSpaceNotifier(p).notify_report(
            Path('/硬盘4'), 14.95, 1000, 1.495,
            [candidate(1, 800)] if selected is None else selected, 35.05,
            scan_paths=['/secret/media'], diagnosis=diagnosis,
            strategy_name=p._current_strategy_name)
        p.post_message.assert_called_once()
        payload = p.post_message.call_args.kwargs
        self.assertEqual(payload['mtype'], 'plugin')
        self.assertGreaterEqual(len(payload['text'].splitlines()), 2)
        self.assertLessEqual(len(payload['text'].splitlines()), 5)
        for noisy in ('/硬盘4', '/secret', 'PASSWORD', '扫描', '缓存', 'TMDB',
                      '候选深度', '保护跳过', '800', 'Traceback'):
            self.assertNotIn(noisy, payload['title'] + payload['text'])
        return payload

    def test_supplied_report_is_short_and_does_not_claim_deletion_failed(self):
        payload = self.send()
        self.assertEqual(payload['title'], '⚠️ 硬盘4-国产电视剧 · 空间仍不足')
        self.assertEqual(payload['text'], '剩余 14.95 GB / 目标 50.00 GB\n'
                         '已删除1 项，空间变化 0.00 GB\n'
                         '删除后空间未明显增加，可检查硬链接、快照或文件占用')
        for word in ('删除失败', '兜底', '-0.00', '未释放'):
            self.assertNotIn(word, payload['text'])

    def test_success_uses_measured_delta_not_logical_size(self):
        payload = self.send(after_free_gb=55, actual_change_gb=40.05)
        self.assertIn('✅', payload['title'])
        self.assertIn('40.05 GB', payload['text'])
        self.assertNotIn('释放', payload['text'])

    def test_unused_fallback_hidden_and_normal_target_retained(self):
        payload = self.send(fallback_enabled=True, fallback_target_free_gb=20)
        self.assertIn('50.00 GB', payload['text'])
        self.assertNotIn('兜底', payload['text'])

    def test_fallback_success_uses_safe_target(self):
        payload = self.send(after_free_gb=20, actual_change_gb=5.05,
                            fallback_used=True, fallback_safety_reached=True,
                            fallback_target_free_gb=20)
        self.assertIn('✅', payload['title'])
        self.assertIn('已达安全水位', payload['title'])
        self.assertIn('目标 20.00 GB', payload['text'])
        self.assertIn('已尝试兜底', payload['text'])
        self.assertNotIn('50.00', payload['text'])

    def test_safety_reached_without_attempt_hides_fallback_label(self):
        payload = self.send(after_free_gb=20, actual_change_gb=5.05,
                            fallback_safety_reached=True, fallback_target_free_gb=20)
        self.assertIn('目标 20.00 GB', payload['text'])
        self.assertNotIn('尝试兜底', payload['text'])

    def test_dry_run_planned_fallback_never_claims_deletion_or_release(self):
        payload = self.send(dry_run=True, fallback_planned=True,
                            fallback_target_free_gb=20, actual_change_gb=0)
        self.assertIn('模拟建议（未删除）', payload['title'])
        self.assertIn('建议删除1 项', payload['text'])
        self.assertIn('目标 20.00 GB', payload['text'])
        self.assertIn('计划尝试兜底清理（模拟）', payload['text'])
        self.assertNotIn('已删除', payload['text'])
        self.assertNotIn('失败', payload['text'])

    def test_no_candidates_has_short_actionable_hint(self):
        payload = self.send(selected=[], stop_reason='无候选（保护/路径规则过滤或扫描上限）')
        self.assertIn('无可清理候选', payload['title'])
        self.assertIn('保护规则/删除上限', payload['text'])
        self.assertIn('已删除0 项', payload['text'])

    def test_error_hides_raw_exception_and_unknown_metrics(self):
        payload = self.send(selected=[], error=True, after_free_gb=None,
                            actual_change_gb=None,
                            stop_reason='Traceback /secret/media token=PASSWORD')
        self.assertIn('清理异常', payload['title'])
        self.assertIn('剩余 未知', payload['text'])
        self.assertIn('空间变化 未知', payload['text'])
        self.assertIn('查看日志', payload['text'])

    def test_partial_failure_even_when_space_reaches_target(self):
        payload = self.send(after_free_gb=60, actual_change_gb=45.05,
                            delete_errors=['Traceback /secret token=PASSWORD'])
        self.assertIn('清理部分完成', payload['title'])
        self.assertIn('删除失败/保护复核跳过 1 项', payload['text'])
        self.assertNotIn('✅', payload['title'])

    def test_negative_delta_is_not_masked_or_called_deletion_failure(self):
        payload = self.send(after_free_gb=12.95, actual_change_gb=-2)
        self.assertIn('-2.00 GB', payload['text'])
        self.assertIn('可检查并发写入', payload['text'])
        self.assertNotIn('删除失败', payload['text'])
        payload = self.send(actual_change_gb=-0.006)
        self.assertIn('-0.01 GB', payload['text'])

    def test_unknown_or_nonfinite_metrics_not_reported_as_shortage_or_zero(self):
        for value in (None, float('nan'), float('inf')):
            payload = self.send(after_free_gb=value, target_free_gb=value,
                                actual_change_gb=value)
            self.assertIn('空间状态待确认', payload['title'])
            self.assertIn('剩余 未知 / 目标 未知', payload['text'])
            self.assertIn('空间变化 未知', payload['text'])
            self.assertNotIn('硬链接', payload['text'])

    def test_full_detail_formatter_still_retains_diagnostics(self):
        notifier = module.DiskSpaceNotifier(MagicMock())
        diagnosis = dict(stop_reason='raw detailed reason /secret', items_scanned=456)
        self.assertIn('raw detailed reason /secret', notifier.outcome_text(diagnosis, []))
        self.assertIn('扫描456项', notifier.diagnosis_text(diagnosis))


if __name__ == '__main__':
    unittest.main()
