# Adding a device

## Option A: send us a dump

1. Connect to the projector ([connect-adb.md](connect-adb.md)).
2. Run:
   ```
   python projector_debloat.py dump
   ```
3. Open a **New device** issue and attach the `dump-<model>-<date>.json` file. The file holds build properties and the package list, but no serial number or other unique identifier.

## Option B: write the profile yourself

Copy an existing file in `profiles/` to `profiles/<brand>-<model>.json` (lowercase, dashes) and fill it from your dump.

```json
{
  "schema_version": 1,
  "name": "Brand Model",
  "match": {
    "ro.product.model": ["MODEL"],
    "ro.board.platform": ["platform"]
  },
  "android_version": "9",
  "tested_by": ["your-github-name"],
  "notes": "Anything others should know about this device.",
  "packages": [
    { "id": "com.vendor.store", "tier": "safe", "note": "Vendor app store, Play Store works fine instead." }
  ]
}
```

- **`match`**: copy values from the `properties` section of your dump. A profile matches when every non-empty list contains the device's value. `ro.product.model` plus `ro.board.platform` is usually enough. Add more values to a list if the same projector ships under several names.
- **`note`** is required for every package. It says what the package is and why it has that tier. The notes are the main thing this project knows, so make them useful to the next person.

### Choosing a tier

| Tier | Meaning |
|---|---|
| `safe` | Disabled by default. Use it only when you have disabled it yourself and checked that nothing broke. |
| `optional` | Disabled only with `--include-optional`, e.g. a stock app some people actually use. |
| `keep` | Never touched. Use it to **document** packages that look like bloat but aren't: keystone correction, focus motor, panel/HDMI services, remote pairing. |

**When unsure, use `keep` and write what you know in the note.** A wrong `keep` costs nothing. A wrong `safe` can break keystone or the remote for everyone with that projector.

To find out what an unclear package does:

```
adb shell dumpsys package com.vendor.thing
```

You can also pull the APK (the path is in your dump) and open it in JADX.

### Test it

```
python projector_debloat.py plan --profile profiles/<your-file>.json
python projector_debloat.py apply --profile profiles/<your-file>.json
```

Check keystone, focus, HDMI input, the remote, Wi-Fi and Bluetooth, then run `restore` once to confirm it works.

### Before opening the PR

```
python projector_debloat.py validate
python -m unittest
```

Also add your device to the table in the README.
