from types import SimpleNamespace
import unittest
from unittest.mock import patch

from app.engine import market_game_title
from app import main


class DisplayNameTests(unittest.TestCase):
    def test_provider_match_question_and_contract_subtitle_are_human_readable(self):
        fake_engine = SimpleNamespace(meta={
            "KXMLSGAME-26AUG29SDLAG-SD": {
                "title": "San Diego FC vs Los Angeles G Winner?",
                "event": "KXMLSGAME-26AUG29SDLAG",
                "game_title": "San Diego FC vs Los Angeles G",
                "leg_title": "San Diego FC",
            },
        })
        with patch.object(main, "engine", fake_engine):
            names = main._display_names("KXMLSGAME-26AUG29SDLAG-SD")

        self.assertEqual(names["display_game"], "San Diego FC vs Los Angeles G")
        self.assertEqual(names["display_leg"], "San Diego FC")
        self.assertEqual(names["display_contract"], "San Diego FC wins")
        self.assertNotIn("SDLAG-SD", names["display_contract"])

    def test_draw_subtitle_is_normalized_without_changing_raw_title(self):
        names = main._display_names(
            "KXMLSGAME-26SEP13VANATX-TIE",
            "KXMLSGAME-26SEP13VANATX",
            "Tie is the result",
            "Tie",
            "Vancouver vs Austin",
        )

        self.assertEqual(names, {
            "display_game": "Vancouver vs Austin",
            "display_leg": "Draw",
            "display_contract": "Draw",
        })

    def test_matchup_can_be_derived_from_provider_rules_when_title_is_leg_only(self):
        market = {
            "title": "Vancouver wins",
            "rules_secondary": (
                "The following market refers to the Vancouver vs Austin professional "
                "MLS soccer game originally scheduled for Sep 13, 2026."
            ),
        }

        self.assertEqual(market_game_title(market), "Vancouver vs Austin")


if __name__ == "__main__":
    unittest.main()
