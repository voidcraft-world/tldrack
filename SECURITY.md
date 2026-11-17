# Security

tldrack is a local tool for one person on their own machine. It has no login. Read this before you run
it anywhere else.

## What it trusts

- **The machine.** The bridge listens on loopback only: the terminals on 7681, its API on 7682, the phone
  link on 7683. While it runs, any process on the machine, under any account, can reach a writable shell
  in your tmux sessions. Do not run it on a host you share.
- **Pages on loopback.** A page served from `localhost` or `127.0.0.1`, on any port, may call the bridge,
  and that includes starting an agent. Do not serve content you do not trust from loopback while the
  rack runs.
- **No other site.** Every route checks the `Host` header and then the `Origin`. A hosted origin is
  refused unless you list it under `allowed_origins` in the settings file. The terminal port checks
  `Host` too, so a page on another hostname that re-resolves to `127.0.0.1` (DNS rebinding) is refused
  before it reaches a terminal.
- **No other site can frame a page.** Every page the bridge serves, the terminal page included, sends
  a `frame-ancestors` policy that names loopback and your `allowed_origins`, so a site cannot put the
  rack or a terminal under a decoy to borrow a click. The terminal takes its on-screen keys only from
  the frame that holds it, on one of those origins: a frame beside it on the same page cannot press
  them.
- **Remote use is `tailscale serve` only**, to a host you pin by name. Every device your tailnet lets
  reach that node then has the same shell. Never `tailscale funnel`, and never a `0.0.0.0` bind.
- **The phone link** signs every request with a token the bridge pushes over USB. A caller without the
  token is refused.

An agent runs with your account's permissions, inside the permission mode of its own CLI. That boundary
is the agent CLI's. tldrack treats what an agent writes as untrusted when it draws it: canvas output runs
in a sandbox with no network, and agent text is sanitized before it is shown.

## Known gaps in 0.1

- **`--no-status` runs ttyd alone**, with no bridge in front of the terminal port: no `Host` check and
  no `frame-ancestors`. The on-screen keys still need a trusted parent. Use it only to debug ttyd.

`bridge/CLAUDE.md` § Security posture has the reasoning behind each guard.

## Reporting a vulnerability

Use GitHub's private report: the repository's **Security** tab, then **Report a vulnerability**. Please
do not open a public issue for a problem that is not fixed yet.
