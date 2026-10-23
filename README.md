# projector-debloat

Clean up the Android system on cheap Android projectors over ADB. It disables bloatware, leaves alone what the projector needs (keystone, focus, HDMI, remote), and can undo everything.

> **Everything is reversible.** The tool only *disables* packages (`pm disable-user`), it never uninstalls them. It shows a dry run first, saves a snapshot before it changes anything, and `restore` turns everything back on.

Device knowledge lives in JSON profiles under [`profiles/`](profiles/), so adding a new projector model doesn't require touching code.

## Prerequisites

- Python 3.9 or newer. Standard library only, nothing to `pip install`.
- `adb` from [Android platform-tools](https://developer.android.com/tools/releases/platform-tools).
- Works the same on Windows, macOS and Linux.

## Quick start

1. **Install platform-tools.** Unzip it and put the folder on your `PATH`, or point the tool at it with `--adb PATH` or the `ADB` environment variable.
2. **Connect to the projector.** Enable debugging and connect, for example `adb connect 192.168.1.50:5555`. Step-by-step instructions are in [docs/connect-adb.md](docs/connect-adb.md). Check with `adb devices`.
3. **Dump the device:**
   ```
   python projector_debloat.py dump
   ```
   This saves `dump-<model>-<date>.json` (no serial number) and prints a summary. If your model has no profile yet, see [Contributing a device](#contributing-a-device).
4. **Plan (dry run, changes nothing):**
   ```
   python projector_debloat.py plan
   ```
   The profile is detected from the device. Pass `--profile profiles/<file>.json` to choose one yourself, and `--include-optional` to also disable the `optional` tier. To disable only some optional packages, repeat `--include PACKAGE` instead.
5. **Apply:**
   ```
   python projector_debloat.py apply
   ```
   It prints the plan, asks `y/N` (skip that with `--yes`), writes a snapshot to `snapshots/`, then disables the packages.

If more than one device is connected, add `--serial SERIAL` before the command, e.g. `python projector_debloat.py --serial 192.168.1.50:5555 plan`.

## Undo: restore

```
python projector_debloat.py restore
```

This re-enables every package that was enabled in the oldest snapshot for this model (taken before the tool's first change) and is disabled now, so it undoes every `apply` at once. Packages that were already disabled before the tool ran stay as they were. To undo only a later `apply`, pass its snapshot with `--snapshot PATH`.

## Safety

- A built-in denylist (framework, System UI, Settings, shell, package installer, providers, keyboards, Bluetooth, Wi-Fi/network stack, permission controller) is never disabled, whatever a profile says. These rows show as `blocked (denylist)`.
- `apply` refuses to disable your current home launcher unless another launcher is installed. Install one first, e.g. Projectivy Launcher.

## Supported devices

| Device | Profile | Status |
|---|---|---|
| Gaimoo GM200 (Allwinner H713) | [gaimoo-gm200.json](profiles/gaimoo-gm200.json) | Tested; see the [recommended setup](docs/gaimoo-gm200.md) |

## Contributing a device

Run `dump` and open a ["new device" issue](.github/ISSUE_TEMPLATE/new-device.md) with the file attached, or write the profile yourself. See [docs/adding-a-device.md](docs/adding-a-device.md). Before opening a PR:

```
python -m unittest
python projector_debloat.py validate
```

## Disclaimer

Use at your own risk. Disabling the wrong system package can break features until you run `restore` or factory-reset the device. This project is not affiliated with Gaimoo or any other manufacturer. MIT licensed, see [LICENSE](LICENSE).
