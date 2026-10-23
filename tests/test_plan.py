"""Tests for adb output parsing, plan building, launcher safety, snapshots and the commands.

Everything runs against FakeAdb, a stand-in for `adb` driven by tests/fixtures/fake_device.json.
"""

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import projector_debloat as pd

FIXTURE = Path(__file__).parent / "fixtures" / "fake_device.json"

PROFILE = {
    "schema_version": 1,
    "name": "FakeBeam 1",
    "match": {"ro.product.model": ["FakeBeam 1"]},
    "packages": [
        {"id": "com.example.store", "tier": "safe", "note": "Vendor app store."},
        {"id": "com.example.ads", "tier": "safe", "note": "Ad service."},
        {"id": "com.example.already.off", "tier": "safe", "note": "Already disabled on the device."},
        {"id": "com.example.missing", "tier": "safe", "note": "Not installed on this unit."},
        {"id": "com.example.gallery", "tier": "optional", "note": "Stock gallery some people use."},
        {"id": "com.example.keystone", "tier": "keep", "note": "Keystone correction. Looks like bloat, isn't."},
        {"id": "com.android.providers.media", "tier": "safe", "note": "Wrongly marked safe; denylist must block it."},
    ],
}

EXPECTED_ACTIONS = {
    "com.example.store": "disable",
    "com.example.ads": "disable",
    "com.example.already.off": "skip (already disabled)",
    "com.example.missing": "skip (not installed)",
    "com.example.gallery": "skip (optional)",
    "com.example.keystone": "keep",
    "com.android.providers.media": "blocked (denylist)",
}


class FakeAdb:
    """Answers the adb commands the tool sends, in the format a real device prints (\\r\\n included)."""

    def __init__(self, fail=()):
        data = json.loads(FIXTURE.read_text(encoding="utf-8"))
        self.props = data["props"]
        self.packages = {p["id"]: p for p in data["packages"]}
        self.home = data["home"]
        self.home_candidates = data["home_candidates"]
        self.fail = set(fail)
        self.changes = []

    def listing(self, keep, with_path):
        lines = []
        for p in self.packages.values():
            if keep(p):
                lines.append("package:{}={}".format(p["path"], p["id"]) if with_path else "package:" + p["id"])
        return "\r\n".join(lines) + "\r\n"

    def __call__(self, args):
        cmd = " ".join(args)
        if cmd == "shell getprop":
            return 0, "".join("[{}]: [{}]\r\n".format(k, v) for k, v in self.props.items()), ""
        if cmd == "shell pm list packages -f -s":
            return 0, self.listing(lambda p: p["system"], True), ""
        if cmd == "shell pm list packages -f -3":
            return 0, self.listing(lambda p: not p["system"], True), ""
        if cmd == "shell pm list packages -d":
            return 0, self.listing(lambda p: not p["enabled"], False), ""
        if "resolve-activity" in cmd:
            return 0, "priority=0 preferredOrder=0 match=0x108000 specificIndex=-1 isDefault=true\r\n" + self.home + "\r\n", ""
        if "query-activities" in cmd:
            out = "{} activities found:\r\n".format(len(self.home_candidates))
            for i, component in enumerate(self.home_candidates):
                out += "  Activity #{}:\r\n    priority=0 preferredOrder=0 match=0x108000\r\n    {}\r\n".format(i, component)
            return 0, out, ""
        if args[:3] in (["shell", "pm", "disable-user"], ["shell", "pm", "enable"]):
            package_id, enable = args[-1], args[2] == "enable"
            self.changes.append((args[2], package_id))
            if package_id in self.fail:
                return 0, "Error: java.lang.SecurityException: Shell cannot change component state\r\n", ""
            self.packages[package_id]["enabled"] = enable
            return 0, "Package {} new state: {}\r\n".format(package_id, "enabled" if enable else "disabled-user"), ""
        return 1, "", "fake adb: unknown command " + cmd


def run_main(argv, fake):
    """Run main() and return (exit code, printed output)."""
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = pd.main(argv, run=fake)
    return code, out.getvalue()


class ParsingTest(unittest.TestCase):
    def test_pm_list_with_windows_line_endings_and_equals_in_path(self):
        text = ("package:/system/app/Foo/Foo.apk=com.foo\r\n"
                "package:/data/app/~~Q1w==/com.bar-Zx9==/base.apk=com.bar\r\n"
                "\r\n"
                "package:com.baz\r\n"
                "WARNING: linker: something unrelated\r\n")
        self.assertEqual(pd.parse_pm_list(text), {
            "com.foo": "/system/app/Foo/Foo.apk",
            "com.bar": "/data/app/~~Q1w==/com.bar-Zx9==/base.apk",
            "com.baz": None,
        })

    def test_getprop(self):
        text = "[ro.product.model]: [GM200]\r\n[ro.empty]: []\r\ngarbage\r\n"
        self.assertEqual(pd.parse_getprop(text), {"ro.product.model": "GM200", "ro.empty": ""})

    def test_home_component_is_last_line(self):
        text = "priority=0 preferredOrder=0 isDefault=true\r\ncom.foo/.Home\r\n"
        self.assertEqual(pd.parse_home_component(text), "com.foo/.Home")
        self.assertIsNone(pd.parse_home_component("No activity found\r\n"))
        self.assertIsNone(pd.parse_home_component(""))

    def test_adb_devices(self):
        text = "* daemon started successfully\r\nList of devices attached\r\n192.168.1.5:5555\tdevice\r\nABC\tunauthorized\r\n\r\n"
        self.assertEqual(pd.parse_adb_devices(text), [("192.168.1.5:5555", "device"), ("ABC", "unauthorized")])

    def test_read_device(self):
        device = pd.read_device(FakeAdb())
        self.assertEqual(device["props"]["ro.product.model"], "FakeBeam 1")
        self.assertFalse(device["packages"]["com.example.already.off"]["enabled"])
        self.assertFalse(device["packages"]["com.example.gallery"]["system"])
        self.assertEqual(device["home"], "com.example.launcher/.HomeActivity")
        self.assertEqual(device["home_candidates"], ["com.example.launcher", "com.android.settings"])


class PlanTest(unittest.TestCase):
    def setUp(self):
        self.device = pd.read_device(FakeAdb())

    def actions(self, profile, include_optional=False):
        return {row["package"]: row["action"] for row in pd.build_plan(profile, self.device, include_optional)}

    def test_every_action_type(self):
        self.assertEqual(self.actions(PROFILE), EXPECTED_ACTIONS)

    def test_include_optional(self):
        self.assertEqual(self.actions(PROFILE, include_optional=True)["com.example.gallery"], "disable")

    def test_include_single_optional(self):
        rows = pd.build_plan(PROFILE, self.device, include=["com.example.gallery"])
        self.assertEqual({row["package"]: row["action"] for row in rows}["com.example.gallery"], "disable")

    def test_include_rejects_non_optional(self):
        with self.assertRaises(SystemExit):
            pd.build_plan(PROFILE, self.device, include=["com.example.typo"])

    def test_denylist(self):
        for package_id in ("android", "com.android.settings", "com.android.providers.contacts",
                           "com.google.android.inputmethod.latin", "com.android.networkstack.tethering",
                           "com.android.wifi.resources"):
            self.assertTrue(pd.is_denylisted(package_id), package_id)
        self.assertFalse(pd.is_denylisted("com.android.providerstuff.not"))
        self.assertFalse(pd.is_denylisted("com.example.store"))

    def test_denylisted_keep_stays_keep(self):
        profile = dict(PROFILE, packages=[{"id": "com.android.settings", "tier": "keep", "note": "x"}])
        self.assertEqual(self.actions(profile), {"com.android.settings": "keep"})


class LauncherTest(unittest.TestCase):
    def plan_disabling_launcher(self, device):
        profile = dict(PROFILE, packages=[{"id": "com.example.launcher", "tier": "safe", "note": "Stock launcher."}])
        return pd.build_plan(profile, device)

    def test_refuses_when_no_other_launcher(self):
        # Settings' FallbackHome is a HOME activity but not a real launcher, so it must not count.
        device = pd.read_device(FakeAdb())
        message = pd.check_launcher(self.plan_disabling_launcher(device), device)
        self.assertIn("com.example.launcher", message)
        self.assertIn("Projectivy", message)

    def test_allows_when_another_launcher_installed(self):
        fake = FakeAdb()
        fake.home_candidates.append("com.spocky.projengmenu/.ui.home.MainActivity")
        device = pd.read_device(fake)
        self.assertIsNone(pd.check_launcher(self.plan_disabling_launcher(device), device))

    def test_ok_when_launcher_untouched(self):
        device = pd.read_device(FakeAdb())
        self.assertIsNone(pd.check_launcher(pd.build_plan(PROFILE, device), device))

    def test_no_default_set_protects_all_candidates(self):
        fake = FakeAdb()
        fake.home = "android/com.android.internal.app.ResolverActivity"
        device = pd.read_device(fake)
        self.assertIsNotNone(pd.check_launcher(self.plan_disabling_launcher(device), device))

    def test_apply_refuses_and_changes_nothing(self):
        fake = FakeAdb()
        with tempfile.TemporaryDirectory() as tmp:
            profile_path = Path(tmp) / "p.json"
            pd.write_json(profile_path, dict(PROFILE, packages=[
                {"id": "com.example.launcher", "tier": "safe", "note": "Stock launcher."}]))
            with mock.patch.object(pd, "SNAPSHOT_DIR", Path(tmp) / "snapshots"):
                code, out = run_main(["apply", "--profile", str(profile_path), "--yes"], fake)
            self.assertFalse((Path(tmp) / "snapshots").exists())
        self.assertEqual(code, 1)
        self.assertIn("Refusing", out)
        self.assertEqual(fake.changes, [])


class RestoreLogicTest(unittest.TestCase):
    def test_only_reenables_what_was_enabled_in_snapshot(self):
        snapshot = {"packages": [
            {"id": "a", "enabled": True},    # disabled by the tool  -> re-enable
            {"id": "b", "enabled": False},   # disabled before the tool ran -> leave alone
            {"id": "c", "enabled": True},    # still enabled -> nothing to do
            {"id": "d", "enabled": True},    # uninstalled since -> skip
        ]}
        device = {"packages": {
            "a": {"enabled": False}, "b": {"enabled": False}, "c": {"enabled": True},
            "e": {"enabled": False},         # not in the snapshot -> leave alone
        }}
        self.assertEqual(pd.build_restore(snapshot, device), ["a"])

    def test_find_first_snapshot_for_model(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            for name, model, created in (("old.json", "GM200", "2026-01-01T10:00:00"),
                                         ("new.json", "GM200", "2026-02-01T10:00:00"),
                                         ("other.json", "X1", "2026-03-01T10:00:00")):
                pd.write_json(tmp / name, {"created": created, "properties": {"ro.product.model": model}, "packages": []})
            (tmp / "broken.json").write_text("{not json", encoding="utf-8")
            with mock.patch.object(pd, "SNAPSHOT_DIR", tmp):
                self.assertEqual(pd.find_first_snapshot("GM200"), tmp / "old.json")
                self.assertIsNone(pd.find_first_snapshot("nope"))


class CommandsTest(unittest.TestCase):
    """End-to-end: run the real commands against the fake device in a temp directory."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.profile_path = self.dir / "fake.json"
        pd.write_json(self.profile_path, PROFILE)
        patcher = mock.patch.object(pd, "SNAPSHOT_DIR", self.dir / "snapshots")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp.cleanup)

    def test_dump_writes_file_without_serial(self):
        old_cwd = os.getcwd()
        os.chdir(self.dir)
        try:
            code, out = run_main(["dump"], FakeAdb())
        finally:
            os.chdir(old_cwd)
        self.assertEqual(code, 0)
        files = list(self.dir.glob("dump-FakeBeam_1-*.json"))
        self.assertEqual(len(files), 1)
        text = files[0].read_text(encoding="utf-8")
        self.assertNotIn("SECRET123456", text)
        data = json.loads(text)
        self.assertEqual(sorted(data["properties"]), sorted(pd.DUMP_PROPS))
        self.assertEqual(data["home"], "com.example.launcher/.HomeActivity")
        self.assertEqual(len(data["packages"]), 9)
        self.assertIn("9 total, 8 system, 1 third-party, 1 disabled", out)

    def test_plan_changes_nothing(self):
        fake = FakeAdb()
        code, out = run_main(["plan", "--profile", str(self.profile_path)], fake)
        self.assertEqual(code, 0)
        self.assertIn("blocked (denylist)", out)
        self.assertIn("2 package(s) would be disabled", out)
        self.assertEqual(fake.changes, [])

    def test_plan_auto_detects_profile(self):
        with mock.patch.object(pd, "PROFILES_DIR", self.dir):
            code, out = run_main(["plan"], FakeAdb())
        self.assertEqual(code, 0)
        self.assertIn("Profile: FakeBeam 1", out)

    def test_apply_then_restore(self):
        fake = FakeAdb()
        code, out = run_main(["apply", "--profile", str(self.profile_path), "--yes"], fake)
        self.assertEqual(code, 0, out)
        self.assertEqual(fake.changes, [("disable-user", "com.example.store"), ("disable-user", "com.example.ads")])
        self.assertIn("2 disabled, 0 failed, 5 skipped", out)

        snapshots = list((self.dir / "snapshots").glob("FakeBeam_1-*.json"))
        self.assertEqual(len(snapshots), 1)
        snapshot = json.loads(snapshots[0].read_text(encoding="utf-8"))
        before = {p["id"]: p["enabled"] for p in snapshot["packages"]}
        self.assertTrue(before["com.example.store"])  # state BEFORE the change
        self.assertNotIn("ro.serialno", snapshot["properties"])

        fake.changes.clear()
        code, out = run_main(["restore", "--yes"], fake)
        self.assertEqual(code, 0, out)
        self.assertEqual(fake.changes, [("enable", "com.example.ads"), ("enable", "com.example.store")])
        self.assertFalse(fake.packages["com.example.already.off"]["enabled"])

    def test_two_applies_in_one_second_then_restore_undoes_both(self):
        fake = FakeAdb()
        same_second = [pd.datetime(2026, 1, 1, 10, 0, 0, 1), pd.datetime(2026, 1, 1, 10, 0, 0, 2)]
        with mock.patch.object(pd, "datetime", wraps=pd.datetime) as dt:
            dt.now.side_effect = same_second
            run_main(["apply", "--profile", str(self.profile_path), "--yes"], fake)
            run_main(["apply", "--profile", str(self.profile_path), "--include", "com.example.gallery", "--yes"], fake)
        self.assertEqual(len(list((self.dir / "snapshots").glob("*.json"))), 2)
        fake.changes.clear()
        code, out = run_main(["restore", "--yes"], fake)
        self.assertEqual(code, 0, out)
        self.assertEqual(sorted(p for _, p in fake.changes),
                         ["com.example.ads", "com.example.gallery", "com.example.store"])

    def test_apply_aborted_changes_nothing(self):
        fake = FakeAdb()
        with mock.patch("builtins.input", return_value="n"):
            code, out = run_main(["apply", "--profile", str(self.profile_path)], fake)
        self.assertEqual(code, 1)
        self.assertEqual(fake.changes, [])
        self.assertFalse((self.dir / "snapshots").exists())

    def test_apply_reports_failure_and_continues(self):
        fake = FakeAdb(fail={"com.example.store"})
        code, out = run_main(["apply", "--profile", str(self.profile_path), "--yes"], fake)
        self.assertEqual(code, 1)
        self.assertIn("FAILED  com.example.store", out)
        self.assertIn("1 disabled, 1 failed", out)
        self.assertFalse(fake.packages["com.example.ads"]["enabled"])

    def test_restore_without_snapshot(self):
        with self.assertRaises(SystemExit) as ctx:
            run_main(["restore", "--yes"], FakeAdb())
        self.assertIn("no snapshot", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
