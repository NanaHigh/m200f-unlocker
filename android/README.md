# Android port

This is a native Android USB-host implementation of the M200F fingerprint
verification flow. It uses the same documented initialization and `90 2F`
state machine as `finger_tool_gui.py`; fingerprint matching and the encrypted
partition unlock remain inside the drive firmware.

## Requirements

- An Android device running Android 8.0 or newer with USB OTG / host support.
- An OTG adapter that can power the M200F.
- Android SDK Platform 35, Build-Tools 35.x, and JDK 17.

## Build

From this directory, use Android Studio's Build APK command, or run:

```powershell
./gradlew.bat assembleDebug
```

The debug APK is written to
`app/build/outputs/apk/debug/m200f-unlocker-<version>-debug.apk`.

`0.0.2` and later also produce a release APK at
`app/build/outputs/apk/release/m200f-unlocker-<version>-release.apk`. The release build is signed
with the stable `NanaHigh.jks` keystore in this directory. Preserve that file:
replacing it prevents Android from accepting future updates over an installed
release APK.

For CI, do not commit the keystore or `signing.properties`. Configure the
repository secrets `ANDROID_KEYSTORE_BASE64`, `ANDROID_KEYSTORE_PASSWORD`,
`ANDROID_KEY_ALIAS`, and `ANDROID_KEY_PASSWORD`; the workflow reconstructs the
same signing configuration during the build. Without these secrets, CI still
builds the debug APK but does not publish a release APK.

## Device behavior

Android may mount the public LUN before the application starts. The app asks
for USB permission and force-claims the mass-storage interface so it can send
the vendor SCSI CDBs encapsulated in USB Bulk-Only Transport. OEM USB stacks
may refuse that claim or expose no usable bulk interface; this is a device/ROM
restriction, not something an APK can bypass. Test on a physical Android
device before relying on it for access to the encrypted partition.
