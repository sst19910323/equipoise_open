"""Opt-in next-session execution: unknown is not zero payoff; old snapshots keep their ruler."""
import copy
import unittest

from test_odds_model import CFG, leaf_events, om, reflect_scenario, ruler_scenario
import ruling

STRICT = dict(CFG, conditional_fill_policy="strict_next_open_v1", cost_bps=0)
BUY = {"id": "confirm", "strategy": "overweight_on_condition", "direction": "overweight",
       "kind": "close", "op": ">", "level": 103, "fill": "next_open", "stop": None,
       "observe": "close > 103", "provenance": []}


def scenario(events):
    return ruler_scenario([("U", .6, 115, events), ("D", .4, 90, leaf_events((15, "close", 90)))])


class ConditionalExecutionTest(unittest.TestCase):
    def simulate(self, sc, cfg=STRICT, cand=BUY):
        return om.simulate_leaf(om.expand_leaves(sc)[0], cand['strategy'], cand, sc, cfg)

    def test_exact_next_open_includes_gap_and_same_day_is_not_next(self):
        sc=scenario(leaf_events((3,"open",104),(3,"close",105),(4,"gap_open",108),(15,"close",115)))
        sim=self.simulate(sc)
        self.assertEqual(sim['entry_status'],'filled')
        self.assertEqual(sim['fill_price'],108)
        self.assertAlmostEqual(sim['r_path'],.07)

    def test_later_open_is_unknown_legacy_keeps_later_fill(self):
        sc=scenario(leaf_events((3,"close",105),(6,"open",108),(15,"close",115)))
        strict=self.simulate(sc);legacy=self.simulate(sc,CFG)
        self.assertEqual(strict['entry_status'],'unknown_next_open')
        self.assertEqual(strict['expected_open_day'],4)
        self.assertEqual(strict['next_written_open_day'],6)
        self.assertIsNone(strict['r_path'])
        self.assertEqual(legacy['fill_price'],108)
        self.assertNotIn('entry_status',legacy)
        with self.assertRaisesRegex(ValueError,'unknown next-session open'):
            om.leaf_payoff(.1,strict,0,.08,.08)

    def test_next_close_does_not_replace_missing_open(self):
        sc=scenario(leaf_events((3,"close",105),(4,"close",108),(15,"close",115)))
        self.assertEqual(self.simulate(sc)['entry_status'],'unknown_next_open')
        self.assertIsNone(self.simulate(sc)['next_written_open_day'])
        self.assertEqual(self.simulate(sc,CFG)['r_path'],0)

    def test_pending_at_horizon_is_not_unknown_or_excluded(self):
        sc=scenario(leaf_events((15,"close",115)))
        sim=self.simulate(sc)
        self.assertEqual(sim['entry_status'],'pending_after_horizon')
        self.assertEqual(om.leaf_payoff(.1,sim,0,.08,.08),0)
        cmp=om.compare_strategies(sc,[BUY],STRICT)
        cov=cmp['conditional_execution']['candidates']['confirm']
        self.assertTrue(cov['eligible'])
        self.assertAlmostEqual(cov['mass']['pending_after_horizon'],.6)
        self.assertAlmostEqual(cov['mass']['not_triggered'],.4)
        self.assertIn('confirm',cmp['strategies'])

    def test_incomplete_candidate_excluded_not_assigned_a_zero_ce(self):
        sc=scenario(leaf_events((3,"close",105),(6,"open",108),(15,"close",115)))
        cmp=om.compare_strategies(sc,[BUY],STRICT)
        self.assertNotIn('confirm',cmp['strategies'])
        self.assertNotEqual(cmp['best'],'confirm')
        self.assertIn('confirm',[c['id'] for c in cmp['candidates']])
        self.assertFalse(cmp['conditional_execution']['candidates']['confirm']['eligible'])
        self.assertAlmostEqual(cmp['conditional_execution']['candidates']['confirm']['mass']['unknown_next_open'],.6)
        self.assertIn('unknown next-session open',' '.join(cmp['warn']))

    def test_both_directions_have_the_same_unknown_status(self):
        sc=scenario(leaf_events((3,"close",105),(6,"open",108),(15,"close",115)))
        sell=dict(BUY,id='sell',strategy='underweight_on_condition',direction='underweight',op='<',level=97)
        sim=self.simulate(reflect_scenario(sc),cand=sell)
        self.assertEqual(sim['entry_status'],'unknown_next_open')
        cmp=om.compare_strategies(reflect_scenario(sc),[sell],STRICT)
        self.assertNotIn('sell',cmp['strategies'])
        self.assertAlmostEqual(cmp['conditional_execution']['candidates']['sell']['mass']['unknown_next_open'],.6)

    def test_zero_probability_unknown_leaf_does_not_disable_candidate(self):
        sc=scenario(leaf_events((3,"close",105),(6,"open",108),(15,"close",115)))
        sc['nodes'][0]['p']=0;sc['nodes'][1]['p']=1
        cmp=om.compare_strategies(sc,[BUY],STRICT)
        self.assertTrue(cmp['conditional_execution']['candidates']['confirm']['eligible'])
        self.assertIn('confirm',cmp['strategies'])

    def test_simple_card_unchanged_and_sensitivity_skips_exclusions(self):
        sc=scenario(leaf_events((3,"close",105),(6,"open",108),(15,"close",115)))
        self.assertEqual(om.derive_simple(sc,[BUY],STRICT),om.derive_simple(sc,[BUY],dict(CFG,cost_bps=0)))
        cmp=om.compare_strategies(sc,[BUY],dict(STRICT,gamma_sensitivity=[5,20]))
        for values in cmp['gamma_sensitivity'].values():self.assertNotIn('confirm',values)

    def test_snapshot_version_roundtrip_default_off_and_typo_fails(self):
        snap=om.snapshot_from_config(STRICT)
        self.assertEqual(om.conditional_fill_policy(om.config_from_snapshot(snap)),'strict_next_open_v1')
        self.assertEqual(om.conditional_fill_policy(om.config_from_snapshot({})),'legacy_next_written_open')
        self.assertNotIn('conditional_fill_policy',om.load_config())
        with self.assertRaisesRegex(ValueError,'conditional_fill_policy'):
            om.compare_strategies(scenario(leaf_events((15,'close',115))),[],dict(CFG,conditional_fill_policy='typo'))

    def test_release_recomputation_rejects_removed_or_tampered_coverage(self):
        sc=scenario(leaf_events((3,"close",105),(6,"open",108),(15,"close",115)))
        d=om.derive_for_report(sc,[BUY],STRICT)
        self.assertTrue(d['ok'],d)
        d['s_ref']=d['score']['S_ref'];d['unit_basis']=STRICT.get('unit_basis')
        report_tree=dict(sc,derived=d)
        self.assertEqual(ruling.recompute_check(report_tree,STRICT,candidates=[BUY]),[])
        for mode in ('missing','tampered','non_mapping'):
            bad=copy.deepcopy(report_tree)
            if mode=='missing':bad['derived']['comparison'].pop('conditional_execution')
            elif mode=='non_mapping':bad['derived']['comparison']=[]
            else:bad['derived']['comparison']['conditional_execution']['candidates']['confirm']['eligible']=True
            errors=ruling.recompute_check(bad,STRICT,candidates=[BUY])
            self.assertTrue(any('conditional_execution' in e for e in errors),errors)


if __name__=='__main__':unittest.main()
