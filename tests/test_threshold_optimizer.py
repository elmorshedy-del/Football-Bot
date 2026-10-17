"""Behavioral tests for the offline log-threshold optimizer."""
import unittest
from research import threshold_optimizer as opt


def alert(ts, log=.5, market="G-A", side="yes", group="G", **extra):
    return dict(received_ts=ts, exchange_ts=ts, log_move=log, levels=5,
                volume=200, market=market, side=side, group=group, **extra)


def truth(ts=100, market="G-A", side="yes", group="G", ident="goal"):
    return dict(id=ident, event_id=ident, market=market, side=side, group=group,
                onset_ts=ts, start_ts=ts-1, end_ts=ts+30,
                baseline_c=20, terminal_c=80)


def coverage(certified=True):
    return [dict(start_ts=0, end_ts=1000, market="G-A", group="G",
                 negative_labels_complete=certified)]


class ThresholdOptimizerTests(unittest.TestCase):
    def test_unknown_is_not_false(self):
        r=opt.score_alerts([alert(50)], [truth()], [])
        self.assertEqual((r.get("tp"),r.get("fp"),r.get("unknown"),r.get("fn")),(0,0,1,1))

    def test_duplicate_does_not_create_second_true_detection(self):
        r=opt.score_alerts([alert(100),alert(102)], [truth()], coverage())
        self.assertEqual((r.get("tp"),r.get("fp"),r.get("duplicates")),(1,1,1))

    def test_wrong_direction_is_false_only_in_certified_coverage(self):
        r=opt.score_alerts([alert(100,side="no")], [truth()], coverage())
        self.assertEqual((r.get("tp"),r.get("fp"),r.get("fn")),(0,1,1))
        r=opt.score_alerts([alert(100,side="no")], [truth()], coverage(False))
        self.assertEqual((r.get("fp"),r.get("unknown")),(0,1))

    def test_silent_detector_has_no_zero_false_per_true_reward(self):
        r=opt.score_alerts([], [truth()], coverage())
        self.assertIsNone(r.get("false_per_true"))
        self.assertEqual(r.get("penalized_delay_s"),30)
        self.assertEqual(r.get("recall"),0)

    def test_two_legs_remain_independent_but_one_match(self):
        ts=[truth(),truth(market="G-B",side="no",ident="second-leg")]
        r=opt.score_alerts([alert(100),alert(101,market="G-B",side="no")],ts,coverage())
        self.assertEqual((r.get("tp"),r.get("truth_matches")),(2,1))

    def test_weak_early_alarm_can_preempt_true_shock(self):
        fs=[alert(50,log=.3),alert(100,log=.8)]
        low=opt.score_alerts(opt.make_alerts(fs,.2),[truth()],coverage())
        high=opt.score_alerts(opt.make_alerts(fs,.5),[truth()],coverage())
        self.assertEqual((low.get("tp"),low.get("fp"),low.get("fn")),(0,1,1))
        self.assertEqual((high.get("tp"),high.get("fp"),high.get("fn")),(1,0,0))

    def test_suppressed_candidates_do_not_extend_lockout(self):
        r=opt.make_alerts([alert(0),alert(119),alert(120)],.5)
        self.assertEqual([a["received_ts"] for a in r],[0,120])

    def test_explicitly_ineligible_clock_drops_alert(self):
        self.assertEqual(opt.make_alerts([alert(100,eligible=False)],.5),[])

    def test_stoppage_is_eligible_until_explicit_final(self):
        s=dict(observed_ts=100,minute=90,added_time=8,period=2,status="live")
        self.assertTrue(opt.clock_eligible(s,101))
        self.assertFalse(opt.clock_eligible(dict(s,status="ended"),101))
        self.assertFalse(opt.clock_eligible(s,111))
        self.assertFalse(opt.clock_eligible(dict(s,minute=79),101))
        self.assertFalse(opt.clock_eligible(dict(s,period=1),101))

    def test_quote_cost_is_not_log_threshold(self):
        r=opt.score_alerts([alert(100,entry_ask_c=72)],[truth()],coverage())
        self.assertEqual(r.get("median_entry_c"),72)

    def test_split_keeps_same_day_and_match_together(self):
        groups=[dict(id="A",start_ts=0),dict(id="B",start_ts=100),
                dict(id="C",start_ts=86400),dict(id="D",start_ts=172800)]
        train,test=opt.split_groups(groups,.3)
        self.assertEqual(train&test,set())
        self.assertEqual(train|test,{"A","B","C","D"})
        self.assertEqual("A" in train,"B" in train)
        self.assertIn("D",test)

    def test_pick_earliest_subject_to_quality(self):
        b=dict(recall=1,median_entry_c=50)
        rows=[dict(b,threshold=.2,precision=.7,false_per_true=.4,penalized_delay_s=.1),
              dict(b,threshold=.5,precision=.95,false_per_true=.05,penalized_delay_s=.5),
              dict(b,threshold=.8,precision=1,false_per_true=0,penalized_delay_s=2)]
        self.assertEqual((opt.choose_threshold(rows) or {}).get("threshold"),.5)

    def test_low_recall_is_not_an_optimum(self):
        self.assertIsNone(opt.choose_threshold([dict(threshold=2,precision=1,recall=.1,
            false_per_true=0,penalized_delay_s=27,median_entry_c=90)]))

    def test_tradeoff_frontier(self):
        rows=[dict(threshold=.2,false_per_true=.5,penalized_delay_s=.1,recall=1),
              dict(threshold=.5,false_per_true=.1,penalized_delay_s=1,recall=.9),
              dict(threshold=.8,false_per_true=.2,penalized_delay_s=2,recall=.8)]
        self.assertEqual({x["threshold"] for x in opt.pareto_frontier(rows)},{.2,.5})

    def test_cache_floor_is_enforced(self):
        with self.assertRaises(ValueError):opt.threshold_grid(.1,1,.05,feature_floor=.2)

    def test_raw_features_are_causal_and_volume_is_own_market(self):
        def tr(t,p,size=100,market="G-A"):
            return dict(exchange_ts=t,received_ts=t,yes_price_c=p,size=size,
                        taker_side="yes",market=market,group="G")
        before=[tr(1,20),tr(2.90,22),tr(2.92,24),tr(2.94,26),tr(2.96,28),tr(2.98,30)]
        prefix=list(opt.trade_features(before,min_log=0))
        full=list(opt.trade_features(before+[tr(3,90,99999,market="G-B")],min_log=0))
        self.assertTrue(prefix)
        self.assertEqual(prefix,full[:len(prefix)])
        if prefix:
            self.assertEqual((prefix[-1]["volume"],prefix[-1]["levels"]),(500,5))

    def test_incomplete_coverage_blocks_recommendation(self):
        reasons=opt.recommendation_blockers({},dict(precision=1,recall=1,truth_matches=30),
                                           dict(precision=1,recall=1,truth_matches=20))
        self.assertIn("continuous_ordinary_periods_incomplete",reasons)
        self.assertIn("negative_labels_incomplete",reasons)


if __name__=="__main__":
    unittest.main()
