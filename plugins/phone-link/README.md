# phone-link

The Laya review app's door to this Mac. It lets the operator grade Laya's agent-rack marks (✅ / 💬 / ⚠️, the
RECENT view of `/laya-review.html`) from the phone, and once a day it says when grading there is worth it (§ The
daily grade door). A listener of its own on `127.0.0.1:7683`, tunnelled to the phone with `adb reverse`, forwards
eight allow-listed bridge routes to a caller that proves it holds a token only the app has. The token itself never
crosses the wire.

This file owns the contract. The Android half (`android/`, the app `world.voidcraft.tldrack`) and the code here
build against it.

## Two limits

- **The link needs a debug build of the app.** The Mac writes the token into the app's private storage with
  `adb shell run-as`, and Android refuses `run-as` on a release build. Install with `./gradlew installDebug`. A
  release build reads `release` in the phone's row and never links.
- **The token is written over USB only.** With Wireless debugging paired, adb names a phone by the instance name
  it advertises on mDNS, and that name is broadcast in the clear. The phone authenticates this Mac's key; as far as
  is known, adb does not pin the phone's TLS certificate in return. A device on the same Wi-Fi that copies the name
  and answers as adbd would be handed whatever adb is told to send that "phone". So over Wi-Fi the keeper only
  reads the token back and arms the tunnel; it writes the token to a phone attached by USB and to no other
  (`keeper.py` `_ensure_token`). An impersonator therefore gets a tunnel to a listener that still wants the
  token's proof: the same nothing any app on the phone gets (§ What this does not stop). The cost: a phone links
  the first time, and after a token rotation, only once it has been plugged in. Until then its row reads
  `plug in over USB once: the token is only written over USB`.

## Why the bridge port is never tunnelled

The bridge's guard admits any request with no `Origin` (a native app, `curl`). It can, because only processes on
this Mac reach its loopback. `adb reverse tcp:7682 tcp:7682` breaks that: the phone's `127.0.0.1:7682` becomes
this Mac's, and every app on the phone can open a socket to its own loopback. Every one of them would get every
bridge route: `/spawn`, `/kill-session`, `/ws` (a writable shell), the whole session ledger.

So the bridge's ports are never tunnelled. Only 7683 is. This listener answers nothing to a caller that cannot
prove the token, and forwards only the allowlist. The keeper **removes** any reverse tunnel whose Mac side could
reach the bridge on every check: 7681 (ttyd), 7682 (the status listener), whatever ports the running bridge was
given, under any host spelling, and any Mac-side socket that is not TCP. Someone armed
`adb reverse tcp:7682 tcp:7682` by hand on 2026-11-07. This rule exists for that.

## The contract

| | Mac (this plugin) | Phone (the app) |
|---|---|---|
| Port | `PHONE_LINK_PORT = 7683` (`phone_link/paths.py`) | the same constant; base URL `http://127.0.0.1:7683` |
| Tunnel | `adb -s <serial> reverse tcp:7683 tcp:7683`, armed by the keeper | none. Cleartext is permitted for `127.0.0.1` only (`res/xml/network_security_config.xml`) |
| Token | `~/.local/state/tldrack/phone-link/token`: `secrets.token_urlsafe(32)`, minted once, 0600 in a 0700 folder | `File(context.filesDir, "phone-link-token")`, trimmed; never backed up or transferred (`res/xml/data_extraction_rules.xml`) |
| Each call | | the handshake below, and no `Origin` |

## The handshake

**Why not a bearer token.** Whenever the phone is unplugged and Wireless debugging is off, which is most of the
time, adbd has dropped the reverse, and the phone's `127.0.0.1:7683` is free. Any app with the INTERNET
permission can bind it: loopback ports at or above 1024 are unprivileged on Android. That is the same threat as
the one above, on the phone's side. An app that sent `Authorization: Bearer <token>` would hand the token to that
squatter on its next call, and the squatter would use it through the tunnel the next time the operator plugs in.

So the token is only ever an HMAC key, on both sides. Each call is three steps:

1. **Nonce.** The app makes a fresh challenge `c` (32 random bytes, URL-safe base64 without padding, 22–128 of
   `[A-Za-z0-9_-]`) and sends `GET /nonce?c=<c>` with no `Authorization`. The link answers
   `{"ok":true,"v":2,"nonce":"<43 chars>","ttl_s":30}` and signs the reply over `c` (step 3). **The app checks
   that signature before it sends anything else.** A squatter cannot make it, so the app stops there and shows
   UNLINKED, and the squatter has seen only a random challenge.
2. **Request.** The app sends the real request with one header,
   `Authorization: PhoneLink n=<nonce>, mac=<hex>`, where `hex` is the lowercase HMAC-SHA256, keyed by the token's
   43 ASCII bytes, of this string (lines joined by `\n`, no trailing newline):
   ```
   phone-link/2 request
   <nonce>
   <METHOD>
   <target>
   <lowercase hex SHA-256 of the exact body bytes; of no bytes for a GET>
   ```
   `<target>` is the path and query exactly as they go on the request line. With OkHttp that is
   `url.encodedPath`, plus `?` and `url.encodedQuery` when there is a query. A nonce is good **once**, for
   30 seconds, and is spent the first time it is presented, valid MAC or not.
3. **Reply.** Every answer to a proven request, and every `/nonce` answer, carries `Phone-Link-Mac: <hex>`: the
   same HMAC over
   ```
   phone-link/2 reply              (phone-link/2 nonce on a /nonce reply)
   <the nonce>                     (c on a /nonce reply)
   <status>
   <lowercase hex SHA-256 of the exact body bytes>
   ```
   The `/nonce` reply has its own first line because `c` is the caller's to choose: signed as a `reply`, anyone who
   reached `/nonce` with a caught nonce as `c` would get the link's signature on a "proven" answer to it.
   The app checks it on every reply. A reply without a valid one is not the link's, and the app treats it as
   UNLINKED. Refusals made before the proof (403, 401, framing, 503 `busy`) are never signed, so a caller that
   cannot prove the token cannot collect the link's signatures.

**Test vector.** Both implementations check these numbers (`tests/test_proof.py` here):

| | |
|---|---|
| token | `TEST_VECTOR_TOKEN_do_not_use_0123456789abcd` |
| c | `TEST_VECTOR_CHALLENGE_0123456789abcdefghij` |
| `/nonce` reply, 200 | `{"ok":true,"v":2,"nonce":"TEST_VECTOR_NONCE_0123456789abcdefghijklmno","ttl_s":30}` |
| its `Phone-Link-Mac` | `d08023bee422685b7cb6d7002e662d159cca9fdec5d9d7e8936a849a20800675` |
| `POST /plugin/laya/review/answer`, body `{"id":"r1","verdict":"open"}` | mac `893bac9380754babbf40a9bfcfcc0baea347a15bb194ba7c9ed13e779a543d0d` |
| `GET /ledger/search?q=open+work` | mac `12157b675a0412d9e46bade13cdfd47ba30ecdd51ba034127c94fe3e6d7fb9bc` |
| reply 200 `{"ok":true}` to that nonce | `Phone-Link-Mac: e3c4bb9073d51afafa3dec53caf4a76f92e2fffde59f2008ccd6fd0a99b67ea6` |

The Python spelling is `phone_link/proof.py`, and `tests/link_client.py` is a reference client that runs all
three steps. Version 1 (`Bearer`) is gone: the link answers it 401 `unlinked`.

### The listener's checks, in order

| # | Check | Refusal |
|---|---|---|
| 1 | `Host` is absent or a loopback name (`localhost`, `127.0.0.1`, `[::1]`, any port). One `Host` only. This stops DNS rebinding | 403 `host_refused` |
| 2 | No proxy header (`Forwarded`, `Via`, `X-Forwarded-*`, `X-Real-IP`, `Tailscale-*`) | 403 `proxy_refused` |
| 3 | No `Origin` of any value, and no `Sec-Fetch-*` header, because those come from a browser | 403 `browser_refused` |
| 4 | `GET /nonce?c=<c>` is answered here, before any proof: a nonce, signed over `c` | 400 `bad_challenge` |
| 5 | Exactly one `Authorization: PhoneLink n=…, mac=…`, whose nonce this link issued, unspent and fresh | 401 `{"ok":false,"error":"unlinked"}` |
| 6 | The framing. A `GET` has no body. Anything else declares `Content-Length` (no chunked body) of at most 16 KiB | 411 `length_required` · 413 `too_large` · 400 `bad_body` |
| 7 | The MAC, compared in constant time | 401 `unlinked` |
| 8 | `GET` or `POST` (signed from here on) | 405 `method_not_allowed` |
| 9 | The allowlist, exactly (below) | 404 `{"ok":false,"error":"not_allowed"}` |
| 10 | A `POST` body is a JSON object of real text (a lone surrogate escape such as `"\ud800"` is not) | 400 `bad_body` |
| 11 | The forward | the bridge's status and JSON body, unchanged · 502 `bridge_down` (unreachable, timed out) · 502 `bad_upstream` (not JSON) |

Every error body is `{"ok": false, "error": "<code>"}`. A caller that cannot prove the token gets 401 on every
path but `/nonce`, so it learns nothing about which paths exist. At most 16 connections at once (503 `busy` past
that). Each connection gets 5 s from its accept to send its request and prove the token, and 20 s in all, by the
clock, not per read, so a caller dripping one byte at a time cannot hold a slot. HTTP/1.0, one request per
connection, so `/nonce` and the request are two connections. Owner of the bounds: `phone_link/link_server.py`.

### The allowlist (phone path == bridge path)

| Method | Path | For |
|---|---|---|
| GET | `/nonce` | step 1 of the handshake, answered here before any proof |
| GET | `/health` | the app's "linked?" probe, answered here and never forwarded: `{"ok":true,"v":2}` |
| GET | `/plugin/laya/review/recent` | the RECENT list |
| POST | `/plugin/laya/review/answer` | grade a row |
| POST | `/plugin/laya/review/undo` | take a grade back |
| GET | `/ledger/search` | find a row's conversation, as `ui/src/laya-review/turn-context.ts` does |
| GET | `/ledger/conversation` | read it |
| GET | `/ledger/threads` | the page's join: each verdict's conversation, its title and first message, so the app shows one conversation at a time |
| POST | `/ledger/ingest` | REFRESH INDEX |
| GET | `/topic-labels` | the rack's display name for a session |

The match is exact on the **raw** request path, before any decoding: lowercase letters, digits, `-` and `/`
only. No `%` escapes, no `.` segments, no `//`, no trailing `/`, no extra segment, no prefix match. A raw target
is used because `http.server` collapses a leading `//` in its parsed path, and because it is what the phone signed.
The query string is parsed and re-encoded (at most 32 fields).

What reaches the bridge, `http://127.0.0.1:<the bridge's status port>`, is the same path and the re-encoded query,
`Accept: application/json`, `Content-Type: application/json` and the re-serialised body on a `POST`, and a 10 s
timeout. None of the phone's headers cross: not its credential, not a cookie, not its user agent. No `Origin` is
sent, so the bridge reads the call as the native client it is. Owner: `phone_link/upstream.py`.

### App states

| State | When | The app says |
|---|---|---|
| LINKED | `/health` answers 200, signed | |
| UNLINKED | connection refused, no token file, 401, or a reply without a valid `Phone-Link-Mac` | "Turn on Wireless debugging (its tile), on the Mac's Wi-Fi — or plug into the Mac" |
| BRIDGE DOWN | a signed 502 `bridge_down` | the bridge is not answering on this Mac |

While it is UNLINKED or BRIDGE DOWN and on screen, the app asks `/health` again by itself: every 3 s for the first
minute, then every 10 s. The list arrives as soon as the link does, with no RETRY press. A refused connect on the
phone's own loopback costs nothing, and something else on that port only ever sees a random challenge.

## The token

- **Mac.** Minted at the plugin's first start and kept in the state folder above. It is never returned by any
  route, never logged, never placed in an argv, and never sent or accepted over the wire: it is only the HMAC key
  of the handshake. A file that does not hold a token is replaced; a symlink is refused. Owner:
  `phone_link/token_store.py`.
- **Which app.** `world.voidcraft.tldrack` unless configured (§ Enable). `<app>` below is that package. It must
  match Android's package-name grammar, because it is a word of the phone's shell command; anything else is
  refused, named in the state's `app.error` and the bridge log, and the default stands. Owner: `phone_link/config.py`.
- **Push.** It goes to each attached phone, on adb's **stdin**:
  `adb -s <serial> shell -T "run-as <app> sh -c 'umask 077 && mkdir -p files && cat > files/phone-link-token.tmp && mv -f files/phone-link-token.tmp files/phone-link-token'"`.
  The device-side command is one argument, so its meaning does not depend on how an adb release joins
  arguments. The rename means the app never reads half a token.
- **Verified.** `adb -s <serial> shell -T "run-as <app> cat files/phone-link-token"`, compared in
  constant time. A push counts only once it reads back. Never `exec-in`: adb 37 returns from it the moment stdin
  closes and kills the device side mid-write (why: `phone_link/adb.py`).
- **A debug build only** (§ Two limits). The row says so.
- **Rotate.** Delete `~/.local/state/tldrack/phone-link/token` and restart the bridge. Each phone gets the
  new token at its next check; until then its calls are UNLINKED, because its signatures no longer verify.

## The keeper

Two daemon threads in the bridge's process. The why of each rule: `phone_link/keeper.py`.

- `adb track-devices -l` is a long-lived stream of whole device lists. It ends when the adb server restarts,
  which also drops every reverse tunnel. It is restarted with backoff, from 1 s up to 60 s. A run that ends before
  its first list is a failure, and `adb_error` names its exit status and adb's last stderr line.
- On every new list, and every 30 s while a phone is attached (every 15 s while a paired phone is not attached
  over Wi-Fi: it may have just switched Wireless debugging on), each phone in adb's `device` state gets three
  steps, in order. 1. Every tunnel whose Mac side could reach the bridge is removed: a bridge port on any host
  (`tcp:7682`, `tcp:localhost:7682`, `tcp:[::1]:7682`), a TCP spec that does not parse, and any Mac side that is
  not TCP (`localfilesystem:` and friends are unix sockets, and tmux's server socket is one). 2. The phone's
  `tcp:7683` belongs to the one bridge on this Mac that holds the link's lock (`link.lock` in the state folder;
  owner: `phone_link/link_server.py`). While that bridge also holds the port, `reverse tcp:7683 tcp:7683` is
  armed. While a stranger holds the port, the tunnel is removed. A bridge without the lock, a second one or one
  still starting, leaves the tunnel alone. 3. The lock's holder reads the token back, and pushes it when it is
  missing or stale, to a phone on USB only (§ Two limits).
- No app installed: the token field reads `no_app`, and the next check tries again. A phone that is
  `unauthorized` or `offline` gets a row that says what to do.
- No `adb`: the `adb` field reads `missing`, and nothing crashes. The binary is looked for every 60 s on `PATH`,
  `$ANDROID_HOME`, `~/Library/Android/sdk`, and Homebrew. A bridge started outside a login shell has no Homebrew
  on its `PATH`.
- Every adb call has a timeout. A failure is a row's `error`, never an exception.

## Wireless debugging: the phone unplugged

This is how the app is used away from the cable. The tunnel is the same `adb reverse`; adb carries it over Wi-Fi.

**Once:**

1. Plug the phone in once, with the app installed, so it holds the token (§ Two limits).
2. On the phone: Settings › Developer options › Wireless debugging → on, tick **Always allow on this network**,
   then **Pair device with pairing code**. It shows an `ip:port` and a six-digit code.
3. On this Mac: `adb pair <ip:port> <code>`. `wireless.paired` in § State turns 1.
4. Add the tile: Developer options › Quick settings developer tiles › Wireless debugging.

**Each time:** make sure the Wireless debugging tile is on, and open the app. Android may switch it off when the
phone leaves that Wi-Fi (through Android 16 it always does, and it stays off after a restart), which is why the
tile is worth a place in the shade.

What happens then: adb connects a paired phone it sees advertised on mDNS (`_adb-tls-connect._tcp`), the keeper
arms the tunnel on the device list that follows, and the app, which has been asking every 3 s, links. As a
backstop for when adb's own auto-connect does not fire, the keeper looks for the advertisement every 15 s and
connects it: 5 s after a first try that did not attach, then 15 s, a minute, and every 5 minutes, starting over
whenever the phone attaches or stops advertising (a newly switched-on Wireless debugging has a new port, so it is
always tried at once). It connects an advertiser only when its instance name is **exactly** a GUID in adb's
pairing record (`~/.android/adb_known_hosts.pb`, read as the protobuf it is), the check adb's own auto-connect
makes. A substring would not do: every GUID starts with `adb-`.

## What this does not stop

- **A device that impersonates a paired phone on the same Wi-Fi** still gets a tunnel, and with it what any app
  on the phone has: the `/nonce` step, 401s, and a share of the 16 slots. It does not get the token (§ Two limits),
  so it gets no route and no signature on anything it did not choose.
- **Root on the phone** reads the app's private storage, and the token with it.
- **A local app denying service.** Any app on the phone, or any process on this Mac, can keep the 16 slots
  busy by reconnecting, or churn `/nonce` until the app's nonce is evicted. The deadlines make it keep working at
  it; they do not stop it. It gets nothing: no token, no route, no signature.
- **Local processes.** The token file is 0600 to the operator's account, like every secret under
  `~/.local/state`. Any process on this Mac can already reach the bridge directly. The token protects against the
  phone, not against this machine.

## State

`GET http://127.0.0.1:7682/plugin/phone-link/state`. Its caller policy (`phone_link/guard.py`): a loopback `Host`,
no proxy header, no browser request that hid its Origin. Which pages may read it is the bridge's own origin
setting, checked before any plugin runs: loopback pages, plus the origins the operator configured. A host too old to
pass request headers gets 403 `host_too_old`. The shape only gains keys, and it never contains the token.

```json
{"v": 1, "adb": "ok", "adb_error": null, "port": 7683,
 "app": {"package": "world.voidcraft.tldrack", "source": "default", "error": null},
 "listener": {"up": true, "error": null, "owner": true},
 "devices": [{"serial": "0123456789A", "model": "AB-C123D", "transport": "usb", "state": "device",
              "reverse": true, "token": "pushed", "error": null,
              "checked_at": "2026-11-07T21:04:10Z", "bridge_reverse_removed_at": null}],
 "requests": {"n": 12, "last_at": "2026-11-07T21:04:31Z", "refused": 0},
 "door": {"date": "2026-11-09", "at": "2026-11-09T12:10:00-06:00", "waiting": 7, "sent": true, "reason": "sent"},
 "wireless": {"paired": 1}}
```

| field | meaning |
|---|---|
| `adb` | `ok` or `missing`. `adb_error` is set while `track-devices` is failing, and names why |
| `app` | the package the token goes to; `source` is `default`, `env` or `file`; `error` names a configured value that was refused |
| `listener` | `owner`: this bridge holds the link's lock. `up`: it also holds 7683. `error` is `port 7683 is in use` while a stranger holds the port, and `another bridge holds the link` while another bridge holds the lock; both are retried |
| `devices[].transport` | `usb` or `wifi` |
| `devices[].state` | adb's word: `device`, `unauthorized`, `offline`, … |
| `devices[].reverse` | `tcp:7683 tcp:7683` is armed |
| `devices[].token` | `pushed` (it read back), `no_app`, `error`. `error` says why, or `checking` before the first check. A phone on Wi-Fi that does not hold the token yet reads `error`, and says to plug in once |
| `devices[].bridge_reverse_removed_at` | the last time a tunnel that could reach the bridge was found on this phone and removed |
| `requests` | `n`, the requests that proved the token; `last_at`; `refused`, the requests turned away by the checks above |
| `wireless.paired` | phones in adb's Wireless debugging pairing record on this Mac; 0 means none was ever paired |
| `door` | the daily grade door's last attempt (below), or `null` before the first: its local `date`, how many cards waited, whether the push went out, and the bridge's `reason` when it did not |

## The daily grade door

At most once a local day this plugin asks the bridge for ONE push that opens the app's LAYA screen
(`tldrack://review`). It asks only between 09:00 and 21:00, only while a phone is linked (adb `device`, the
7683 tunnel armed, the token read back, on the bridge that holds the link's lock), and only when the laya plugin
counts enough cards waiting that RECENT can grade (`door: {waiting, min}` on `/plugin/laya/review/queue`, 5 today;
the count and the threshold are the laya plugin's). The text is a count: `Laya: 7 to grade`.

The push is `POST /notify/send` with `open: "laya_recent"`, so the on/off switch, the 20-a-day budget and the tap
target stay the bridge's (`tldrack/notify.py`). One attempt a day, whatever came of it, kept in `door.json`
beside the token; a day with no phone, too few cards or a silent bridge is not an attempt. Turning the bridge's phone
pushes off (its `enabled` switch, `POST /notify`) turns this off with them. Both calls are this plugin's own, on this Mac's loopback: the
phone's allowlist is unchanged. Owner of the rules: `phone_link/door.py`.

## Enable

Add this directory to `~/.config/tldrack/plugins.json`, then restart the bridge. The plugin uses the stdlib and the
public core, `voidcraft-world-bridge>=0.1.4,<0.2`. The phone needs a debug build of the app that speaks the
handshake above: `cd android && ./gradlew installDebug`, with the phone on USB.

To link a different app, name its package. The first that is set wins:

```bash
export TLDRACK_PHONE_LINK_APP=org.example.review                              # the bridge's environment
echo '{"app_package": "org.example.review"}' > ~/.config/tldrack/phone-link.json
```

Restart the bridge after either. `/state`'s `app` reads what took effect.

## Tests

`uv run pytest` here. Fakes stand in for adb and the bridge, and the listener runs on loopback. One fake `adb`
executable runs through real subprocesses, to prove that the token travels on stdin and never in argv, and a
second proves that closing the keeper kills a live `track-devices`. `tests/link_client.py` drives the handshake
end to end, and a squatter on a loopback port proves the client never shows it the token or the request.
