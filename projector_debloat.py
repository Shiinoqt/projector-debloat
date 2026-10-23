#!/usr/bin/env python3
"""projector-debloat: disable bloatware on Android projectors over ADB, reversibly.

Standard library only. Run `python projector_debloat.py --help` for usage.
Device knowledge lives in profiles/*.json; this script only knows the global
safety denylist below.
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PROFILES_DIR = ROOT / "profiles"
SNAPSHOT_DIR = ROOT / "snapshots"
PLATFORM_TOOLS_URL = "https://developer.android.com/tools/releases/platform-tools"

TIERS = ("safe", "optional", "keep")

# Global safety denylist: never disabled, whatever a profile says. Keep it short.
# These are packages whose loss can leave the device unusable or impossible to
# fix over adb without a factory reset.
DENYLIST_EXACT = {
    "android",                                  # the framework itself
    "com.android.systemui",                     # status bar, dialogs, volume UI
    "com.android.settings",                     # needed to fix things by hand
    "com.android.shell",                        # `adb shell` runs as this app
    "com.android.packageinstaller",             # installs and updates apps
    "com.google.android.packageinstaller",
    "com.android.bluetooth",                    # most remotes pair over Bluetooth
    "com.android.permissioncontroller",         # permission dialogs; apps break without it
    "com.google.android.permissioncontroller",
}
DENYLIST_PREFIXES = (
    "com.android.providers.",                   # settings/media/download storage other apps rely on
    "com.android.inputmethod.",                 # on-screen keyboards
    "com.google.android.inputmethod.",
    "com.android.networkstack",                 # network plumbing (also .tethering etc.)
    "com.android.wifi",
)

# Properties saved by `dump` and in snapshots. Deliberately no ro.serialno or
# other unique IDs: dump files are meant to be attached to public GitHub issues.
DUMP_PROPS = [
    "ro.product.model",
    "ro.product.brand",
    "ro.product.manufacturer",
    "ro.product.device",
    "ro.board.platform",
    "ro.hardware",
    "ro.build.version.release",
    "ro.build.version.sdk",
    "ro.build.fingerprint",
    "ro.product.cpu.abi",
]

HOME_INTENT = ["-a", "android.intent.action.MAIN", "-c", "android.intent.category.HOME"]
# HOME activities that are not usable launchers: Settings' FallbackHome only shows
# "Android is starting..." during boot, and "android" is the app chooser dialog.
NOT_LAUNCHERS = {"android", "com.android.settings"}


def die(message):
    """Stop with an error message (printed to stderr, exit code 1)."""
    raise SystemExit("Error: " + message)


def is_denylisted(package_id):
    return package_id in DENYLIST_EXACT or package_id.startswith(DENYLIST_PREFIXES)


# --- adb --------------------------------------------------------------------

def find_adb(adb_arg=None):
    """Return the adb executable: --adb, then the ADB env var, then adb on PATH."""
    candidate = adb_arg or os.environ.get("ADB") or "adb"
    if os.path.isfile(candidate):
        return candidate
    return shutil.which(candidate)


def make_runner(adb_path, serial=None):
    """Return run_adb(args) -> (returncode, stdout, stderr). The only place adb is called."""
    base = [adb_path] + (["-s", serial] if serial else [])

    def run_adb(args):
        try:
            result = subprocess.run(base + list(args), capture_output=True, text=True,
                                    encoding="utf-8", errors="replace", timeout=120)
        except subprocess.TimeoutExpired:
            return 1, "", "adb timed out: " + " ".join(args)
        return result.returncode, result.stdout, result.stderr

    return run_adb


def parse_adb_devices(text):
    """Parse `adb devices` into a list of (serial, state)."""
    devices = []
    for line in text.replace("\r", "").splitlines():
        parts = line.split()
        if len(parts) >= 2 and not line.startswith(("List of", "*")):
            devices.append((parts[0], parts[1]))
    return devices


def connect(adb_arg, serial):
    """Find adb, pick the device, and return a runner bound to it."""
    adb = find_adb(adb_arg)
    if not adb:
        die("adb not found. Install Android platform-tools from " + PLATFORM_TOOLS_URL
            + "\nthen put adb on your PATH, set the ADB environment variable, or pass --adb PATH.")
    if serial:
        return make_runner(adb, serial)

    _, out, err = make_runner(adb)(["devices"])
    devices = parse_adb_devices(out)
    if not devices:
        die("no device connected. See docs/connect-adb.md. " + err.strip())
    if len(devices) > 1:
        listing = "\n".join("  {}  ({})".format(s, state) for s, state in devices)
        die("more than one device connected, pick one with --serial:\n" + listing)
    serial, state = devices[0]
    if state != "device":
        die("device {} is '{}'. If it says unauthorized, accept the prompt on the projector screen.".format(serial, state))
    return make_runner(adb, serial)


def adb_shell(run, *args):
    """Run `adb shell ARGS` and return stdout; stop if adb itself reports a failure."""
    rc, out, err = run(["shell"] + list(args))
    if rc != 0:
        die("adb shell {} failed: {}".format(" ".join(args), (err or out).strip()))
    return out


# --- parsing adb output (pure functions, tested without a device) ------------

def clean_lines(text):
    """Split output into stripped, non-empty lines. Windows adb adds \\r to every line."""
    return [line.strip() for line in text.replace("\r", "").splitlines() if line.strip()]


def parse_getprop(text):
    """Parse `getprop` lines like `[ro.product.model]: [GM200]` into a dict."""
    props = {}
    for line in clean_lines(text):
        match = re.match(r"^\[(.+?)\]: \[(.*)\]$", line)
        if match:
            props[match.group(1)] = match.group(2)
    return props


def parse_pm_list(text):
    """Parse `pm list packages [-f]` into {package_id: apk_path or None}.

    Lines look like `package:/system/app/Foo/Foo.apk=com.foo` (with -f) or
    `package:com.foo`. Split on the LAST '=': Android 11+ paths contain '=='.
    """
    packages = {}
    for line in clean_lines(text):
        if not line.startswith("package:"):
            continue
        rest = line[len("package:"):]
        path, sep, package_id = rest.rpartition("=")
        if not sep:
            path, package_id = None, rest
        if package_id:
            packages[package_id] = path
    return packages


def parse_home_component(text):
    """The last line of `cmd package resolve-activity --brief` is the component, e.g. com.foo/.Home."""
    lines = clean_lines(text)
    if lines and re.match(r"^[\w.]+/[\w.$]+$", lines[-1]):
        return lines[-1]
    return None


def parse_home_candidates(text):
    """Package ids of all HOME activities listed by `cmd package query-activities --brief`."""
    found = []
    for line in clean_lines(text):
        match = re.match(r"^([\w.]+)/[\w.$]+$", line)
        if match and match.group(1) not in found:
            found.append(match.group(1))
    return found


def read_device(run):
    """Collect props, packages and launcher info. The only gatherer that talks to adb."""
    props = parse_getprop(adb_shell(run, "getprop"))
    system = parse_pm_list(adb_shell(run, "pm", "list", "packages", "-f", "-s"))
    third_party = parse_pm_list(adb_shell(run, "pm", "list", "packages", "-f", "-3"))
    disabled = set(parse_pm_list(adb_shell(run, "pm", "list", "packages", "-d")))

    packages = {}
    for listing, is_system in ((system, True), (third_party, False)):
        for package_id, path in listing.items():
            packages[package_id] = {"system": is_system, "enabled": package_id not in disabled, "path": path}

    # `cmd package` needs Android 7+. On failure these return nothing, which is fine:
    # the launcher check then has nothing to protect (see check_launcher).
    _, home_out, _ = run(["shell", "cmd", "package", "resolve-activity", "--brief"] + HOME_INTENT)
    _, candidates_out, _ = run(["shell", "cmd", "package", "query-activities", "--brief"] + HOME_INTENT)
    return {
        "props": props,
        "packages": packages,
        "home": parse_home_component(home_out),
        "home_candidates": parse_home_candidates(candidates_out),
    }


def package_list(device):
    """Packages as a sorted list of dicts, the form written to dump and snapshot files."""
    return [dict(id=package_id, **info) for package_id, info in sorted(device["packages"].items())]


def public_props(device):
    return {key: device["props"].get(key, "") for key in DUMP_PROPS}


def safe_filename(text):
    return re.sub(r"[^A-Za-z0-9._-]+", "_", text).strip("_") or "unknown"


# --- profiles -----------------------------------------------------------------

def validate_profile(data):
    """Check a profile against schema v1. Returns (errors, warnings), both lists of strings."""
    if not isinstance(data, dict):
        return ["top level must be a JSON object"], []
    errors, warnings = [], []

    if data.get("schema_version") != 1:
        errors.append("schema_version must be 1")
    if not isinstance(data.get("name"), str) or not data.get("name"):
        errors.append("name must be a non-empty string")
    for key in ("android_version", "notes"):
        if key in data and not isinstance(data[key], str):
            errors.append(key + " must be a string")
    if "tested_by" in data and not is_string_list(data["tested_by"]):
        errors.append("tested_by must be a list of strings")

    match = data.get("match")
    if not isinstance(match, dict):
        errors.append("match must be an object")
    else:
        for key, values in match.items():
            if not is_string_list(values):
                errors.append("match.{} must be a list of strings".format(key))
        if not any(match.values()):
            warnings.append("match has no values yet, so this profile never auto-detects (use --profile)")

    packages = data.get("packages")
    if not isinstance(packages, list):
        errors.append("packages must be a list")
        return errors, warnings
    seen = set()
    for index, entry in enumerate(packages):
        where = "packages[{}]".format(index)
        if not isinstance(entry, dict):
            errors.append(where + " must be an object")
            continue
        package_id = entry.get("id")
        if not isinstance(package_id, str) or not package_id:
            errors.append(where + ".id must be a non-empty string")
        elif package_id in seen:
            errors.append(where + ": duplicate id " + package_id)
        else:
            seen.add(package_id)
            where += " (" + package_id + ")"
        if entry.get("tier") not in TIERS:
            errors.append(where + ".tier must be one of " + ", ".join(TIERS))
        if not isinstance(entry.get("note"), str) or not entry.get("note").strip():
            errors.append(where + ".note is required: say what the package is and why this tier")
    return errors, warnings


def is_string_list(value):
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


def read_profile(path):
    """Load and validate one profile file. Returns (data or None, errors, warnings)."""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError) as e:
        return None, ["cannot read JSON: {}".format(e)], []
    errors, warnings = validate_profile(data)
    return data, errors, warnings


def profile_matches(profile, props):
    """True when every non-empty match list contains the device's value. All-empty never matches."""
    checks = {key: values for key, values in profile["match"].items() if values}
    return bool(checks) and all(props.get(key) in values for key, values in checks.items())


def detect_profiles(profiles, props):
    """Return the (path, profile) pairs that match the device."""
    return [(path, profile) for path, profile in profiles if profile_matches(profile, props)]


def choose_profile(profile_arg, props):
    """Return (path, profile): the --profile file, or the single auto-detected match."""
    if profile_arg:
        data, errors, _ = read_profile(profile_arg)
        if errors:
            die("{} is not a valid profile:\n  ".format(profile_arg) + "\n  ".join(errors))
        return Path(profile_arg), data

    profiles = []
    for path in sorted(PROFILES_DIR.glob("*.json")):
        data, errors, _ = read_profile(path)
        if errors:
            print("Warning: skipping invalid profile {} (run validate)".format(path.name), file=sys.stderr)
        else:
            profiles.append((path, data))

    matches = detect_profiles(profiles, props)
    if not matches:
        die("no profile matches this device (model '{}', platform '{}').\n"
            "Run `python projector_debloat.py dump` and open a 'new device' issue with the file attached\n"
            "(see docs/adding-a-device.md), or pass --profile PATH.".format(
                props.get("ro.product.model", "?"), props.get("ro.board.platform", "?")))
    if len(matches) > 1:
        names = "\n".join("  {}  ({})".format(path, profile["name"]) for path, profile in matches)
        die("several profiles match this device, pick one with --profile:\n" + names)
    return matches[0]


# --- plan -----------------------------------------------------------------------

def build_plan(profile, device, include_optional=False, include=()):
    """One row per profile package: {package, tier, action, note}.
    `include` lists single 'optional' packages to disable without the whole tier."""
    optional_ids = {entry["id"] for entry in profile["packages"] if entry["tier"] == "optional"}
    unknown = sorted(set(include) - optional_ids)
    if unknown:
        die("--include only takes 'optional' packages from the profile, not: " + ", ".join(unknown))
    rows = []
    for entry in profile["packages"]:
        package_id, tier = entry["id"], entry["tier"]
        installed = device["packages"].get(package_id)
        if tier == "keep":
            action = "keep"
        elif tier == "optional" and not include_optional and package_id not in include:
            action = "skip (optional)"
        elif is_denylisted(package_id):
            action = "blocked (denylist)"
        elif installed is None:
            action = "skip (not installed)"
        elif not installed["enabled"]:
            action = "skip (already disabled)"
        else:
            action = "disable"
        rows.append({"package": package_id, "tier": tier, "action": action, "note": entry["note"]})
    return rows


def check_launcher(rows, device):
    """Return an error message if the plan would leave the projector without a launcher, else None."""
    disabling = {row["package"] for row in rows if row["action"] == "disable"}
    home = device["home"].split("/")[0] if device["home"] else None
    candidates = device["home_candidates"]
    # If no default launcher is set (home is the "android" chooser), protect all candidates.
    # If we know nothing (Android < 7), there is nothing to check.
    protected = {home} if home and home not in NOT_LAUNCHERS else set(candidates)
    at_risk = protected & disabling
    if not at_risk:
        return None
    remaining = [p for p in candidates if p not in disabling and p not in NOT_LAUNCHERS]
    if remaining:
        return None
    return (
        "Refusing: this plan would disable your home launcher ({}) and no other launcher is installed.\n"
        "Without a launcher the projector boots to a blank screen.\n"
        "Install another launcher first, for example Projectivy Launcher, then make it the default:\n"
        "  - press Home and pick it when Android asks, or\n"
        "  - Settings > Apps > Default apps > Home app (menu names vary), or\n"
        "  - adb shell cmd package set-home-activity <package>/<activity>\n"
        "Then run apply again."
    ).format(", ".join(sorted(at_risk)))


def print_table(rows, columns):
    """Print rows (dicts) as plain aligned columns."""
    widths = [max([len(c)] + [len(str(row[c])) for row in rows]) for c in columns]

    def line(values):
        return "  ".join(str(v).ljust(w) for v, w in zip(values, widths)).rstrip()

    print(line(columns))
    print(line(["-" * w for w in widths]))
    for row in rows:
        print(line([row[c] for c in columns]))


def show_plan(profile_path, profile, rows):
    print("Profile: {} ({})\n".format(profile["name"], profile_path))
    if rows:
        print_table(rows, ["package", "tier", "action", "note"])
    else:
        print("(profile lists no packages)")
    count = sum(1 for row in rows if row["action"] == "disable")
    print("\n{} package(s) would be disabled.".format(count))


def confirm(question):
    try:
        return input(question + " [y/N] ").strip().lower() in ("y", "yes")
    except EOFError:
        return False


def change_packages(run, package_ids, enable):
    """Enable or disable each package. Returns (succeeded, failed); failed holds (package, output)."""
    # pm prints "Package <id> new state: <state>" on success. We check that text instead of
    # the exit code because `adb shell` always returned 0 before Android 7.
    if enable:
        verb, expected = "enable", "new state: enabled"
    else:
        verb, expected = "disable-user", "new state: disabled-user"
    succeeded, failed = [], []
    for package_id in package_ids:
        _, out, err = run(["shell", "pm", verb, "--user", "0", package_id])
        output = (out + err).strip()
        if expected in output:
            succeeded.append(package_id)
            print("  ok      " + package_id)
        else:
            failed.append((package_id, output))
            print("  FAILED  {}: {}".format(package_id, output or "no output"))
    return succeeded, failed


# --- snapshots ------------------------------------------------------------------

def make_snapshot(device, profile_path):
    return {
        # microseconds: two applies in the same second must still sort, or restore picks the wrong one
        "created": datetime.now().isoformat(timespec="microseconds"),
        "properties": public_props(device),
        "profile": str(profile_path),
        "packages": package_list(device),
    }


def write_snapshot(snapshot):
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.fromisoformat(snapshot["created"]).strftime("%Y%m%d-%H%M%S")
    base = "{}-{}".format(safe_filename(snapshot["properties"]["ro.product.model"]), stamp)
    path, n = SNAPSHOT_DIR / (base + ".json"), 2
    while path.exists():  # never overwrite: an older snapshot is what restore needs
        path, n = SNAPSHOT_DIR / "{}-{}.json".format(base, n), n + 1
    write_json(path, snapshot)
    return path


def load_snapshot(path):
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError) as e:
        die("cannot read snapshot {}: {}".format(path, e))
    if not isinstance(data, dict) or not isinstance(data.get("packages"), list):
        die("{} is not a snapshot file".format(path))
    return data


def find_first_snapshot(model):
    """Oldest snapshot (by its 'created' field) whose model equals this device's, or None.
    It holds the state before the tool's first change, so restoring it undoes every apply."""
    best_path, best_created = None, None
    for path in SNAPSHOT_DIR.glob("*.json"):
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            created = data["created"]
            matches = data["properties"]["ro.product.model"] == model
        except (OSError, ValueError, KeyError, TypeError):
            continue
        if matches and (best_created is None or created < best_created):
            best_path, best_created = path, created
    return best_path


def build_restore(snapshot, device):
    """Packages that were enabled in the snapshot and are disabled now. Nothing else is touched."""
    to_enable = []
    for entry in snapshot["packages"]:
        now = device["packages"].get(entry["id"])
        if entry["enabled"] and now is not None and not now["enabled"]:
            to_enable.append(entry["id"])
    return to_enable


def write_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.write("\n")


# --- commands -----------------------------------------------------------------

def cmd_dump(args, run):
    device = read_device(run)
    props = public_props(device)
    packages = package_list(device)
    data = {
        "created": datetime.now().isoformat(timespec="seconds"),
        "properties": props,
        "home": device["home"],
        "packages": packages,
    }
    path = Path("dump-{}-{}.json".format(safe_filename(props["ro.product.model"]),
                                         datetime.now().strftime("%Y%m%d-%H%M")))
    write_json(path, data)

    system = sum(1 for p in packages if p["system"])
    disabled = sum(1 for p in packages if not p["enabled"])
    print("Model:     {} {}".format(props["ro.product.brand"], props["ro.product.model"]))
    print("Platform:  {} ({})".format(props["ro.board.platform"], props["ro.hardware"]))
    print("Android:   {} (SDK {})".format(props["ro.build.version.release"], props["ro.build.version.sdk"]))
    print("Launcher:  {}".format(device["home"] or "unknown"))
    print("Packages:  {} total, {} system, {} third-party, {} disabled".format(
        len(packages), system, len(packages) - system, disabled))
    print("\nSaved to {}".format(path.resolve()))
    print("It contains no serial number, so you can attach it to a 'new device' issue.")
    return 0


def cmd_plan(args, run):
    device = read_device(run)
    profile_path, profile = choose_profile(args.profile, device["props"])
    rows = build_plan(profile, device, args.include_optional, args.include)
    show_plan(profile_path, profile, rows)
    problem = check_launcher(rows, device)
    if problem:
        print("\n" + problem)
    print("\nDry run: nothing was changed. Use the apply command to make these changes.")
    return 0


def cmd_apply(args, run):
    device = read_device(run)
    profile_path, profile = choose_profile(args.profile, device["props"])
    rows = build_plan(profile, device, args.include_optional, args.include)
    show_plan(profile_path, profile, rows)

    problem = check_launcher(rows, device)
    if problem:
        print("\n" + problem)
        return 1
    to_disable = [row["package"] for row in rows if row["action"] == "disable"]
    if not to_disable:
        print("Nothing to do.")
        return 0
    if not args.yes and not confirm("\nDisable {} package(s)?".format(len(to_disable))):
        print("Aborted, nothing changed.")
        return 1

    # The snapshot records the state BEFORE any change; restore depends on it.
    print("\nSnapshot saved: {}".format(write_snapshot(make_snapshot(device, profile_path))))
    succeeded, failed = change_packages(run, to_disable, enable=False)
    skipped = len(rows) - len(to_disable)
    print("\nDone: {} disabled, {} failed, {} skipped.".format(len(succeeded), len(failed), skipped))
    if succeeded:
        print("Undo any time with: python projector_debloat.py restore")
    return 1 if failed else 0


def cmd_restore(args, run):
    device = read_device(run)
    if args.snapshot:
        snapshot_path = Path(args.snapshot)
    else:
        model = device["props"].get("ro.product.model", "")
        snapshot_path = find_first_snapshot(model)
        if snapshot_path is None:
            die("no snapshot for model '{}' in {}. Pass --snapshot PATH.".format(model, SNAPSHOT_DIR))
    snapshot = load_snapshot(snapshot_path)
    to_enable = build_restore(snapshot, device)

    print("Snapshot: {} (taken {})\n".format(snapshot_path, snapshot.get("created", "?")))
    if not to_enable:
        print("Nothing to restore: every package enabled in the snapshot is still enabled.")
        return 0
    print_table([{"package": p, "action": "enable"} for p in to_enable], ["package", "action"])
    if not args.yes and not confirm("\nRe-enable {} package(s)?".format(len(to_enable))):
        print("Aborted, nothing changed.")
        return 1
    succeeded, failed = change_packages(run, to_enable, enable=True)
    print("\nDone: {} enabled, {} failed.".format(len(succeeded), len(failed)))
    return 1 if failed else 0


def cmd_validate(args):
    paths = [Path(p) for p in args.paths] or sorted(PROFILES_DIR.glob("*.json"))
    if not paths:
        print("No profile files found in " + str(PROFILES_DIR))
        return 1
    failed = False
    for path in paths:
        _, errors, warnings = read_profile(path)
        for message in warnings:
            print("WARN   {}: {}".format(path, message))
        for message in errors:
            print("ERROR  {}: {}".format(path, message))
        if errors:
            failed = True
        else:
            print("OK     {}".format(path))
    return 1 if failed else 0


DEVICE_COMMANDS = {"dump": cmd_dump, "plan": cmd_plan, "apply": cmd_apply, "restore": cmd_restore}


def build_parser():
    parser = argparse.ArgumentParser(
        prog="projector_debloat.py",
        description="Disable bloatware on Android projectors over ADB. Reversible; dry-run by default.")
    parser.add_argument("--serial", help="device serial (as in `adb devices`), passed as adb -s SERIAL")
    parser.add_argument("--adb", help="path to adb (default: ADB env var, then adb on PATH)")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("dump", help="save device info and package list to a JSON file for a new profile")

    profile_options = argparse.ArgumentParser(add_help=False)
    profile_options.add_argument("--profile", help="profile JSON file (default: auto-detect from profiles/)")
    profile_options.add_argument("--include-optional", action="store_true",
                                 help="also disable packages in the 'optional' tier")
    profile_options.add_argument("--include", action="append", default=[], metavar="PACKAGE",
                                 help="also disable this one 'optional' package (repeatable)")
    sub.add_parser("plan", parents=[profile_options], help="show what apply would do (changes nothing)")
    apply_parser = sub.add_parser("apply", parents=[profile_options], help="disable packages, after a snapshot")
    apply_parser.add_argument("--yes", action="store_true", help="do not ask for confirmation")

    restore_parser = sub.add_parser("restore", help="re-enable packages disabled by apply")
    restore_parser.add_argument("--snapshot", help="snapshot file (default: oldest for this model, which undoes every apply)")
    restore_parser.add_argument("--yes", action="store_true", help="do not ask for confirmation")

    validate_parser = sub.add_parser("validate", help="check profile files against the schema")
    validate_parser.add_argument("paths", nargs="*", help="profile files (default: profiles/*.json)")
    return parser


def main(argv=None, run=None):
    """Entry point. Tests pass a fake `run` so no adb or device is needed."""
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")  # profile notes may hold characters the console can't print
    args = build_parser().parse_args(argv)
    if args.command == "validate":
        return cmd_validate(args)
    if run is None:
        run = connect(args.adb, args.serial)
    return DEVICE_COMMANDS[args.command](args, run)


if __name__ == "__main__":
    sys.exit(main())
