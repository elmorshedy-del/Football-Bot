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
    def test_original_baseline_is_included_even_outside_custom_search(self):
        data=self.dataset()
        r=opt.run_search(data,[.2,.5])
        original=r["baseline_comparison"]["repository_original_log"]
        self.assertEqual(original["threshold"],.8)
        self.assertEqual(original["training_metrics"]["tp"],3)
        self.assertEqual(original["holdout_metrics"]["tp"],1)
        self.assertFalse(original["sibling_confirmation"])
        self.assertFalse(original["entry_price_gates"])
        self.assertEqual({row["threshold"] for row in r["training_grid"]},{.2,.5,.8})

    def test_cache_floor_cannot_silently_exclude_original_baseline(self):
        data=self.dataset();data["metadata"]["feature_floor_log"]=.9
        with self.assertRaises(ValueError):opt.run_search(data,[1])

    def test_feature_shape_matches_actual_detector_on_late_exchange_print(self):
        from app.detector import Detector
        from app import config
        from unittest.mock import patch
        raw=[]
        # Last received print has an older exchange stamp. Original detector
        # includes already-received newer prints in its burst, still causal.
        for ms,p in [(1000,20),(2900,22),(2920,24),(2940,26),(2960,28),(2980,30),(2950,27)]:
            raw.append(dict(market="G-A",group="G",exchange_ts=ms/1000,
                            received_ts=3+len(raw)/100,yes_price_c=p,size=100,taker_side="yes"))
        with patch.object(config,"DL_MIN",0),patch.object(config,"LEVELS_MIN",0), \
             patch.object(config,"SIZE_MIN",0),patch.object(config,"EPISODE_COOLDOWN_S",-1):
            detector=Detector();actual=[]
            for t in raw:
                c=detector.on_trade(t["market"],round(t["exchange_ts"]*1000),
                                    t["yes_price_c"],t["size"],t["taker_side"])
                if c:actual.append(c)
        fs=list(opt.trade_features(raw))
        self.assertEqual(len(fs),len(actual))
        for f,c in zip(fs,actual):
            self.assertAlmostEqual(f["log_move"],c["dl"],places=3)
            self.assertEqual((f["levels"],f["volume"]),(c["levels"],c["size"]))

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

    def test_future_clock_cannot_qualify_a_past_alert(self):
        stamps=[dict(id=1,group="G",observed_ts=100,minute=90,added_time=8,period=2,status="live")]
        r=list(opt.attach_clocks([alert(99),alert(101)],stamps))
        self.assertEqual([x["eligible"] for x in r],[False,True])

    def test_control_exposure_is_union_not_duplicate_rows(self):
        cs=coverage()+[dict(coverage()[0],start_ts=500,end_ts=1500)]
        self.assertAlmostEqual(opt.certified_control_minutes(cs,[truth()]),(1500-31)/60)

    def test_raw_nonfinite_values_are_rejected(self):
        raw=[dict(market="G-A",group="G",exchange_ts=1,received_ts=1,
                  yes_price_c=float("nan"),size=1,taker_side="yes")]
        with self.assertRaises(ValueError):
            list(opt.trade_features(raw))

    @staticmethod
    def dataset():
        groups=[];fs=[];ts=[];cs=[]
        for i in range(4):
            group="match-"+str(i);base=i*86400;market=group+"-A"
            groups.append(dict(id=group,start_ts=base))
            fs.extend([alert(base+50,log=.3,market=market,group=group,eligible=True),
                       alert(base+100,log=.8,market=market,group=group,eligible=True)])
            ts.append(truth(base+100,market=market,group=group,ident=group))
            cs.append(dict(start_ts=base,end_ts=base+1000,market=market,group=group,
                           negative_labels_complete=True))
        return dict(metadata=dict(feature_floor_log=0,synthetic=True),
                    groups=groups,features=fs,truths=ts,coverage=cs)

    def test_holdout_results_cannot_reselect_training_choice(self):
        data=self.dataset()
        first=opt.run_search(data,[.2,.5,.8])
        self.assertEqual(first["frozen_training_candidate"]["threshold"],.5)
        held=set(first["split"]["holdout_groups"])
        data["features"]=[f for f in data["features"] if f["group"] not in held]
        second=opt.run_search(data,[.2,.5,.8])
        self.assertEqual(second["frozen_training_candidate"]["threshold"],.5)
        self.assertEqual(second["holdout_metrics"]["tp"],0)
        self.assertIsNone(second["recommended_threshold"])

    def test_command_line_writes_complete_outputs(self):
        import contextlib
        import io
        import json
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);source=root/"data.json";out=root/"result"
            source.write_text(json.dumps(self.dataset()))
            with contextlib.redirect_stdout(io.StringIO()):
                status=opt.main(["--input",str(source),"--out",str(out),
                                 "--start",".2","--stop",".8","--step",".3"])
            self.assertEqual(status,0)
            result=json.loads((out/"result.json").read_text())
            self.assertEqual(result["frozen_training_candidate"]["threshold"],.5)
            self.assertNotIn("holdout_grid",result)
            self.assertIn("synthetic_data_only",result["blockers"])
            self.assertTrue((out/"training_grid.csv").exists())
            self.assertTrue((out/"training_frontier.json").exists())

    def test_cache_adapter_does_not_certify_negative_periods(self):
        import csv
        import gzip
        import json
        import tempfile
        from pathlib import Path
        from research.prepare_threshold_data import prepare
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);p=root/"football-research";p.mkdir()
            legs=[]
            for market,side,role in [("G-A","yes","scoring_team_yes"),("G-TIE","no","draw_no")]:
                legs.append(dict(record_id="1",event="G",ticker=market,held_side=side,
                    role=role,anchor_ts="100",forward_end_ts="220",
                    price_window_complete="True",large_move="True",stable_completion="True",
                    reversal_record_id="",baseline_bid="20",peak_bid="80"))
            with (p/"two_leg_event_paths.csv").open("w",newline="") as f:
                writer=csv.DictWriter(f,fieldnames=list(legs[0]));writer.writeheader();writer.writerows(legs)
            (p/"late_candidates.json").write_text(json.dumps([
                dict(record_id="1",score_before="0-0",score_after="1-0")]))
            qs={}
            for market,side in [("G-A","yes"),("G-TIE","no")]:
                points=[]
                for n in range(300,1301):
                    t=n/10;held_bid=20 if t<90 else 79
                    y=held_bid if side=="yes" else 99-held_bid
                    points.append(dict(time=t,exchange_age_ms=0,yes_bid=y,yes_ask=y+1,
                        no_bid=99-y,no_ask=100-y,yes_bid_size=1000,yes_ask_size=1000,
                        no_bid_size=1000,no_ask_size=1000))
                qs[market]=points
            cs={market:[dict(time=90.1,exchange_time=90,dl=.6,levels=5,volume=250,
                            direction=1 if side=="yes" else -1)]
                for market,side in [("G-A","yes"),("G-TIE","no")]}
            with gzip.open(p/"two_leg_replay_cache.json.gz","wt") as f:
                json.dump(dict(quotes=qs,candidates=cs),f)
            result=prepare(root)
            self.assertEqual(len(result["truths"]),2)
            self.assertFalse(result["metadata"]["negative_labels_complete"])
            self.assertTrue(all(c["negative_labels_complete"] is False for c in result["coverage"]))


if __name__=="__main__":
    unittest.main()
