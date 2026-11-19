# Laya review (Android)

Grade Laya's agent-rack marks from your phone. One screen, and ONE conversation on it at a time, as on the rack's web
review page: its title, your first message, then its turns under the prompt each one answered. Swipe left or right
(or ◀ ▶) for the next or the previous conversation; scrolling up and down never leaves the one you are reading. A
light says whether the Mac answers, and one bar of grades acts on the turn you are reading (DONE, READ, RUNNING,
NEEDS YOU, SKIP; UNDO takes the last one back). Every grade is a tap. The app talks to nothing but `127.0.0.1:7683`,
the `phone-link` plugin's listener on your Mac, tunnelled over adb. No account, no cloud.

Package `world.voidcraft.tldrack`. The link `tldrack://review` opens it; the bridge's daily push uses it.

## Two limits

- **The link needs a debug build.** The Mac pushes the link's token into the app's private storage with
  `adb shell run-as`, and Android refuses `run-as` on a release build. Install with `./gradlew installDebug`.
- **The Wi-Fi impersonation gap is open.** Over Wireless debugging, a device on the same network that copies your
  phone's advertised name could receive the token. Over USB there is no such gap.

The why of both, and the whole contract between the app and the Mac:
[`plugins/phone-link/README.md`](../plugins/phone-link/README.md).

## Build and install

You need JDK 21 (Temurin) and the Android SDK (platform 36). Point Gradle at them, then, with the phone on USB and
USB debugging on:

```bash
export JAVA_HOME=/Library/Java/JavaVirtualMachines/temurin-21.jdk/Contents/Home
export ANDROID_HOME=~/Library/Android/sdk        # or write sdk.dir=… into local.properties (never committed)
./gradlew installDebug
```

`./gradlew assembleDebug` builds the APK without a phone: `app/build/outputs/apk/debug/app-debug.apk`.

## Link

1. Add `plugins/phone-link` to the bridge's plugins and restart it. Its keeper finds the phone over adb, arms
   `adb reverse tcp:7683 tcp:7683`, and pushes the token.
2. Open the app. The light turns mint and reads LINKED, then the list arrives.

Amber means it is not linked, and the screen names the fix: UNLINKED (plug in, or pair Wireless debugging once),
BRIDGE DOWN (start the bridge), or REFUSED (the bridge said no, and why).

## Layout

| Path | Holds |
|---|---|
| `app/src/main/java/world/voidcraft/tldrack/MainActivity.kt` | The one activity: the screen, and the `tldrack://review` link |
| `…/laya/LayaScreen.kt` | The screen. Its rules live in `LayaRules.kt`, its fetches in `LayaViewModel.kt` |
| `…/laya/LayaRules.kt` | Pure rules (no Android), ported from the web page's `ui/src/laya-review/` |
| `…/laya/LayaThreads.kt` | Which turns are one conversation, and the pager over them: a port of `thread-model.ts` |
| `…/laya/LayaClient.kt`, `PhoneLinkProof.kt` | The calls, each one signed by the phone-link handshake |
| `…/ui/Theme.kt` | The palette and the few shared controls |
| `app/src/main/res/xml/` | Cleartext to `127.0.0.1` only; no backup of the token |

## Tests

```bash
./gradlew :app:testDebugUnitTest
```

JVM unit tests of the rules, the client against a fake link, and the handshake's test vector, which the Python
side checks too (`plugins/phone-link/tests/test_proof.py`).
