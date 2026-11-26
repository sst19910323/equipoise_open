import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from workflows.codex_models import resolve_model
from workflows.harness_resolver import CodexHarness


class ModelPolicyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.harness = CodexHarness('codex1', 'unused', str(self.home))
        (self.home / 'equipoise_models.json').write_text(json.dumps({
            'debate': 'latest:sol', 'postcheck': 'latest:astra'}))

    def cache(self, slugs, age=0):
        (self.home / 'models_cache.json').write_text(json.dumps({
            'fetched_at': (datetime.now(timezone.utc) - timedelta(hours=age)).isoformat(),
            'models': [{'slug': s, 'visibility': v} for s, v in slugs]}))

    def test_numeric_order_and_family(self):
        self.cache([(s, 'list') for s in ['gpt-6.2-sol', 'gpt-6.10-sol',
                   'gpt-7-astra', 'gpt-8-sol-preview']] + [('gpt-9-sol', 'hide')])
        self.assertEqual(resolve_model(self.harness, 'debate'), 'gpt-6.10-sol')
        self.assertEqual(resolve_model(self.harness, 'postcheck'), 'gpt-7-astra')

    def test_stale_stops(self):
        self.cache([('gpt-6.2-sol', 'list')], age=25)
        with self.assertRaises(ValueError):
            resolve_model(self.harness, 'debate')

    def test_missing_family_stops(self):
        self.cache([('gpt-6.2-astra', 'list')])
        with self.assertRaises(ValueError):
            resolve_model(self.harness, 'debate')

    def test_explicit_pin_needs_no_cache(self):
        self.assertEqual(resolve_model(self.harness, 'debate', 'gpt-6.2-sol'), 'gpt-6.2-sol')

    def test_all_identities_use_their_own_policy(self):
        self.cache([('gpt-6.2-sol', 'list'), ('gpt-6.2-astra', 'list')])
        for name in ['codex1', 'codex2', 'codex3']:
            harness = CodexHarness(name, 'unused', str(self.home))
            self.assertEqual(resolve_model(harness, 'debate'), 'gpt-6.2-sol')
            self.assertEqual(resolve_model(harness, 'postcheck'), 'gpt-6.2-astra')

    def test_missing_identity_policy_does_not_fallback(self):
        harness = CodexHarness('codex2', 'unused', str(self.home / 'missing'))
        with self.assertRaises(FileNotFoundError):
            resolve_model(harness, 'debate', fallback='gpt-6.1-sol')
