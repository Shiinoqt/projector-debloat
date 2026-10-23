# Connecting to the projector with ADB

ADB (Android Debug Bridge) is the command-line tool this project uses to talk to the projector. It comes with [Android platform-tools](https://developer.android.com/tools/releases/platform-tools).

## 1. Enable Developer options

1. Open **Settings > About** (sometimes **Settings > Device Preferences > About**).
2. Select **Build** (or **Build number**) about 7 times until it says you are a developer.
3. A new **Developer options** menu appears, usually under Settings or Device Preferences.

## 2. Turn on debugging and connect

Find the projector's IP address under **Settings > Network** (Wi-Fi details). Pick the option your projector offers.

### Network debugging (port 5555)

Many projector firmwares have a **Network debugging** or **ADB over network** switch in Developer options. Turn it on, then:

```
adb connect 192.168.1.50:5555
```

### Wireless debugging (Android 11+)

1. In Developer options, turn on **Wireless debugging** and open it.
2. Choose **Pair device with pairing code**. It shows an IP:PORT and a 6-digit code.
3. Pair with that port:
   ```
   adb pair 192.168.1.50:37123
   ```
   Enter the code when asked.
4. Connect to the **other** port, the one shown on the main Wireless debugging screen (not the pairing port):
   ```
   adb connect 192.168.1.50:41234
   ```

### USB fallback (A-to-A cable)

Some projectors only allow ADB over USB, through a USB-A port that is meant for USB sticks. Connect it to your computer with a **USB A-to-A** cable and turn on **USB debugging**. Then switch to network mode so you can unplug the cable:

```
adb devices
adb tcpip 5555
adb connect 192.168.1.50:5555
```

## 3. Accept the authorization prompt

The first time you connect, the projector shows **"Allow USB debugging?"** with your computer's key fingerprint. Accept it, and tick "Always allow" if you want. Until you do, `adb devices` lists the device as `unauthorized`.

Check the connection:

```
adb devices
```

You should see a line ending in `device`.

## Warning: never expose port 5555 to the internet

ADB over the network has **no password**. Anyone who reaches port 5555 gets full control of the device. Only use it on your home network, never forward the port on your router, and turn network debugging off when you're done.
