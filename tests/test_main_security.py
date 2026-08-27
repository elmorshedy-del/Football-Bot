import unittest
from unittest.mock import patch

from fastapi import HTTPException

from app.main import require_admin


class AdminControlTests(unittest.TestCase):
    def test_controls_fail_closed_when_token_is_missing(self):
        with patch("app.main.config.ADMIN_TOKEN", ""):
            with self.assertRaises(HTTPException) as caught:
                require_admin(None)

        self.assertEqual(caught.exception.status_code, 503)

    def test_controls_reject_wrong_token(self):
        with patch("app.main.config.ADMIN_TOKEN", "correct"):
            with self.assertRaises(HTTPException) as caught:
                require_admin("wrong")

        self.assertEqual(caught.exception.status_code, 401)

    def test_controls_accept_matching_token(self):
        with patch("app.main.config.ADMIN_TOKEN", "correct"):
            self.assertIsNone(require_admin("correct"))


if __name__ == "__main__":
    unittest.main()
