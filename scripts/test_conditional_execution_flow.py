"""Scripted offline strict-policy sessions through assembly and release; no model, network or PG calls."""
import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import odds_model as om
import test_lens_fixtures as lenses
import test_v34_flow as single
import validate_debate as vd


class StrictExecutionFlowTest(unittest.TestCase):
    def run_flow(self, multi):
        original=om.snapshot_from_config
        def strict_snapshot(cfg,*args,**kwargs):
            return original(dict(cfg,conditional_fill_policy='strict_next_open_v1'),*args,**kwargs)
        # Only the mock session opener receives the opt-in key; no repository config is edited.
        with tempfile.TemporaryDirectory(prefix='strict_execution_') as tmp:
            root=Path(tmp);cwd=Path.cwd()
            try:
                os.chdir(root)
                with mock.patch.object(om,'snapshot_from_config',side_effect=strict_snapshot):
                    if multi:
                        out=lenses.drive_lens_session(root,until='signoff')
                        self.assertEqual(out['errors'],{},out)
                        rc=out['results']['signoff']
                    else:
                        rc,_,_=single.run_ruler_session(root,single.ruler_script())
                self.assertTrue(rc['gate']['ok'],rc)
                doc=json.loads((root/rc['analysis_path']).read_text(encoding='utf8'))
                self.assertEqual(doc['odds']['conditional_fill_policy'],'strict_next_open_v1')
                self.assertEqual(vd.validate(doc)[0],[])
                targets=list(doc['by_lens'].values()) if multi else [doc]
                for target in targets:
                    meta=target['scenario']['derived']['comparison']['conditional_execution']
                    self.assertEqual(meta['policy'],'strict_next_open_v1')
                forged=copy.deepcopy(doc)
                target=next(iter(forged['by_lens'].values())) if multi else forged
                target['scenario']['derived']['comparison'].pop('conditional_execution')
                self.assertTrue(any('conditional_execution' in e for e in vd.validate(forged)[0]))
            finally:
                os.chdir(cwd)

    def test_single_symbol_strict_session_and_release_gate(self):self.run_flow(False)

    def test_multi_lens_strict_session_and_release_gate(self):self.run_flow(True)


if __name__=='__main__':unittest.main()
