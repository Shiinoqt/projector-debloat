"""Tests for profile validation and auto-detection."""

import contextlib
import copy
import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import projector_debloat as pd

GOOD = {
    "schema_version": 1,
    "name": "Test Projector",
    "match": {"ro.product.model": ["T1"], "ro.board.platform": []},
    "android_version": "9",
    "tested_by": ["someone"],
    "notes": "",
    "packages": [
        {"id": "com.example.bloat", "tier": "safe", "note": "Vendor store, never used."},
        {"id": "com.example.focus", "tier": "keep", "note": "Autofocus motor service."},
    ],
}


def broken(change):
    """A copy of GOOD with one change applied."""
    data = copy.deepcopy(GOOD)
    change(data)
    return data


class ValidateTest(unittest.TestCase):
    def assert_error(self, data, text):
        errors, _ = pd.validate_profile(data)
        self.assertTrue(any(text in e for e in errors), errors)

    def test_good_profile(self):
        self.assertEqual(pd.validate_profile(GOOD), ([], []))

    def test_missing_note(self):
        self.assert_error(broken(lambda d: d["packages"][0].pop("note")), "note is required")

    def test_blank_note(self):
        self.assert_error(broken(lambda d: d["packages"][0].update(note="  ")), "note is required")

    def test_bad_tier(self):
        self.assert_error(broken(lambda d: d["packages"][0].update(tier="maybe")), "tier must be one of")

    def test_duplicate_id(self):
        self.assert_error(broken(lambda d: d["packages"].append(dict(d["packages"][0]))), "duplicate id")

    def test_wrong_schema_version(self):
        self.assert_error(broken(lambda d: d.update(schema_version=2)), "schema_version")

    def test_match_must_be_lists(self):
        self.assert_error(broken(lambda d: d.update(match={"ro.product.model": "T1"})), "list of strings")

    def test_not_an_object(self):
        self.assert_error([], "JSON object")

    def test_empty_match_is_warning_not_error(self):
        errors, warnings = pd.validate_profile(broken(lambda d: d.update(match={"ro.product.model": []})))
        self.assertEqual(errors, [])
        self.assertEqual(len(warnings), 1)

    def test_shipped_profiles_are_valid(self):
        paths = sorted(pd.PROFILES_DIR.glob("*.json"))
        self.assertTrue(paths)
        for path in paths:
            _, errors, _ = pd.read_profile(path)
            self.assertEqual(errors, [], path)

    def test_gm200_skeleton_warns(self):
        _, errors, warnings = pd.read_profile(pd.PROFILES_DIR / "gaimoo-gm200.json")
        self.assertEqual(errors, [])
        self.assertTrue(warnings)

    def test_validate_command_exit_codes(self):
        with tempfile.TemporaryDirectory() as tmp:
            good, bad, not_json = Path(tmp) / "good.json", Path(tmp) / "bad.json", Path(tmp) / "x.json"
            pd.write_json(good, GOOD)
            pd.write_json(bad, broken(lambda d: d["packages"][0].pop("note")))
            not_json.write_text("{", encoding="utf-8")
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(pd.main(["validate", str(good)]), 0)
                self.assertEqual(pd.main(["validate", str(good), str(bad)]), 1)
                self.assertEqual(pd.main(["validate", str(not_json)]), 1)


class DetectTest(unittest.TestCase):
    PROPS = {"ro.product.model": "T1", "ro.board.platform": "mt9669"}

    def profile(self, **match):
        return broken(lambda d: d.update(match=match))

    def test_match_rules(self):
        self.assertTrue(pd.profile_matches(self.profile(**{"ro.product.model": ["T1", "T1 Pro"]}), self.PROPS))
        self.assertFalse(pd.profile_matches(self.profile(**{"ro.product.model": ["T2"]}), self.PROPS))
        # every non-empty list must match; empty lists are ignored
        self.assertFalse(pd.profile_matches(
            self.profile(**{"ro.product.model": ["T1"], "ro.board.platform": ["other"]}), self.PROPS))
        self.assertTrue(pd.profile_matches(
            self.profile(**{"ro.product.model": ["T1"], "ro.hardware": []}), self.PROPS))
        # an all-empty match (like the GM200 skeleton) must never match everything
        self.assertFalse(pd.profile_matches(self.profile(**{"ro.product.model": []}), self.PROPS))

    def test_zero_one_two_matches(self):
        a = ("a.json", self.profile(**{"ro.product.model": ["T1"]}))
        b = ("b.json", self.profile(**{"ro.board.platform": ["mt9669"]}))
        c = ("c.json", self.profile(**{"ro.product.model": ["Other"]}))
        self.assertEqual(pd.detect_profiles([c], self.PROPS), [])
        self.assertEqual(pd.detect_profiles([a, c], self.PROPS), [a])
        self.assertEqual(pd.detect_profiles([a, b, c], self.PROPS), [a, b])

    def test_choose_profile_messages(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            with mock.patch.object(pd, "PROFILES_DIR", tmp):
                with self.assertRaises(SystemExit) as ctx:
                    pd.choose_profile(None, self.PROPS)
                self.assertIn("dump", str(ctx.exception))

                pd.write_json(tmp / "a.json", self.profile(**{"ro.product.model": ["T1"]}))
                path, _ = pd.choose_profile(None, self.PROPS)
                self.assertEqual(path, tmp / "a.json")

                pd.write_json(tmp / "b.json", self.profile(**{"ro.board.platform": ["mt9669"]}))
                with self.assertRaises(SystemExit) as ctx:
                    pd.choose_profile(None, self.PROPS)
                self.assertIn("--profile", str(ctx.exception))
                self.assertIn("b.json", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
