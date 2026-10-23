# Gaimoo GM200: recommended setup

Tested on a retail unit. The result is ad-free, uses only free apps, and every step can be undone. Nothing is flashed or rooted.

## About the device

- The hardware is an **Allwinner H713** (`ro.hardware=sun50iw12p1`, `ro.board.platform=ares`), with Android 11, a 32-bit userspace (`armeabi-v7a`), about 1 GB of RAM and a 1280x720 panel.
- The firmware's read-only build properties use a generic `adt3` identity (`ro.product.model=adt3`, brand `ADT-3`, manufacturer `askey`), not "GM200". The device name you set in Settings is separate (`device_name`): it is what casting and Bluetooth show, and changing it does not affect profile detection.
- It runs the HTC/Hotack firmware also found on the Portronics Beem 470 and Magcubic HY3xx projectors.
- The settings app is `com.htc.htcsettings`; there is no `com.android.settings`.

## 1. Debloat

```
python projector_debloat.py plan
python projector_debloat.py apply
```

This disables telemetry (`com.htc.eventuploadservice`), the HTC app store, the firmware updaters, factory test apps and unused Android services. Screen mirroring, the Play Store, Google services, HDMI input and the remote keep working.

Optional extras, one package at a time:

```
python projector_debloat.py apply --include com.softwinner.dragonbox --include com.softwinner.agingdragonbox \
  --include com.softwinner.dragonatt --include com.hotack.writesn --include com.android.nfx \
  --include com.softwinner.theme.color --include com.softwinner.theme.icon --include com.htc.screensaver
```

Add `--include com.softwinner.TvdVideo --include com.softwinner.TvdFileManager --include com.softwinner.ImageParser` if you play USB media with VLC instead of the stock apps.

## 2. Replace the ad-supported apps

| Need | Stock app | Replacement (free, no ads) |
|---|---|---|
| Home screen | HTC launcher (shows ads) | [Projectivy Launcher](https://play.google.com/store/apps/details?id=com.spocky.projengmenu) (Play Store) |
| YouTube | YouTube for TV (`com.google.android.youtube.tv`) | [SmartTube](https://github.com/yuliskov/SmartTube/releases), `armeabi-v7a` APK |
| iPhone/Mac mirroring | `com.mphotool.airplay` (fails with iOS 27) | [AirPlay Receiver](https://github.com/jqssun/android-airplay-server/releases), the GitHub APK (the Play Store says "not compatible") |
| USB media | Allwinner video player and file manager | VLC (Play Store) |

Order matters for the launcher: install Projectivy, set it as the default home screen, check that the remote's Home key opens it, and only then:

```
python projector_debloat.py apply --include com.htc.htclauncherhighenglishd08 --include com.htc.expandsdk
```

The tool refuses to disable the current launcher while no other one is installed.

For AirPlay, disable the stock receiver first. It holds TCP port 7000, which stops other receivers from starting:

```
python projector_debloat.py apply --include com.mphotool.airplay
```

Stock YouTube is a regular Play Store app here, so it can be uninstalled with `adb uninstall com.google.android.youtube.tv` and reinstalled from the Play Store later.

**SmartTube settings:** this chip has no hardware VP9 decoder, but decodes H.264 (AVC) in hardware up to 1080p60. Set the video codec to **AVC** and the maximum resolution to **720p** (the panel's native resolution) for smooth playback.

To keep sideloaded apps up to date, track their GitHub releases with [Obtainium](https://github.com/ImranR98/Obtainium). For SmartTube, filter APKs by `armeabi-v7a`.

## 3. Casting

Keep:
- **AirPlay:** the receiver above, for iPhone, iPad and Mac.
- **Miracast:** `com.htc.samescreen` + `com.softwinner.miracastReceiver`, for Windows (Win+K) and most Android phones.

You can disable these if you don't use them:

```
python projector_debloat.py apply --include com.mphotool.dlna --include com.mphotool.usbcastserver \
  --include com.toofifi.lineserver --include com.android.toofifi
```

There is no Google Cast: the device isn't Google-certified.

## 4. Optional system settings

These are not package changes, so `restore` does not undo them. Each line shows the command, then how to undo it.

| Setting | Command | Undo |
|---|---|---|
| System-wide ad blocking (DNS) | `adb shell settings put global private_dns_specifier dns.adguard-dns.com` then `adb shell settings put global private_dns_mode hostname` | `adb shell settings put global private_dns_mode off` |
| Faster animations | `adb shell settings put global window_animation_scale 0.5` (also `transition_animation_scale`, `animator_duration_scale`) | set back to `1.0` |
| Location off (the projector doesn't move) | `adb shell cmd location set-location-enabled false` | `... true` |
| Sleep after 2 h idle (default: never) | `adb shell settings put system screen_off_timeout 7200000` and `adb shell settings put secure sleep_timeout 7200000` | `2147483647` and `-1` |

## Known firmware quirks

- **No Accessibility settings screen.** Projectivy's "Override current launcher" needs its accessibility service, and the firmware clears `enabled_accessibility_services` on every boot. You don't need it: set Projectivy as the default home screen and turn "Override current launcher" off.
- **The Home key needs the setup flags.** If Home does nothing after swapping launchers, run `adb shell settings put secure user_setup_complete 1` and `adb shell settings put global device_provisioned 1`.
- **Firmware updates:** the updaters are disabled by `apply`. Run `restore` before checking for an update, then `apply` again.
- **TikTok over AirPlay** shows a grey placeholder instead of the video. iOS does this on the sender side, so no receiver can fix it.

## Undo everything

```
python projector_debloat.py restore
```

This re-enables every package the tool disabled. Then revert the settings in step 4 if you changed them, and uninstall the replacement apps if you want the stock experience back.
