"""ntfy.sh sink — publish a notification with one stdlib POST.

Chosen as the first sink for exactly one property: publishing costs NOTHING to
integrate. No SDK, no IAM role, no secret in SSM, no redeploy when a token
rotates. `POST https://ntfy.sh/<topic>` with the message as the body is the
entire protocol, which is why this is ~40 lines of `urllib` and why the two
alerts it carries were never worth wiring before.

=== THE TOPIC IS A PASSWORD, AND IT IS THE ONLY ONE ===

On the hosted ntfy.sh service there is no sign-up and no ACL: a topic is created
the first time anyone posts to it, and ANYONE who knows or guesses the name can
both READ and PUBLISH. ntfy's own docs put it plainly — "the topic is essentially
a password". Three rules follow, and they are not style preferences:

 1. **The topic name must be long and random** (`tldrack-ops-<32 chars>`), and
    handled like a bearer secret. `resolve_topic()` refuses anything short enough
    to brute-force.
 2. **Messages are POINTERS, never PAYLOADS.** "3 agents need you" plus a click
    URL — never Claude's message text, never a transcript excerpt, never a file
    path, never entity or user data. The caller in `notify.py` enforces this by
    constructing the body from session names and counts only; do not "improve"
    it by pasting in what the agent actually said. That text routinely contains
    the paths and commands an agent is asking permission for.
 3. **Nothing inbound may ever trigger an action.** Anyone who guesses the topic
    can publish to it, so a message arriving on it is untrusted input from the
    open internet. This module is write-only on purpose — there is no subscribe
    half, and adding one would need a much harder think than this docstring.

Also ruled out for the same reason: ntfy's `http` ACTION buttons, which can carry
arbitrary headers. Putting an auth header in one publishes that credential to
everyone subscribed to the topic. `view` actions (a plain URL) are the only kind
this sink will ever emit, which is why `click` is a URL and there is no `actions`
parameter.

Service reality, recorded so nothing grows to depend on it: ntfy.sh is "AS IS"
with **no uptime guarantee**, broad termination rights, and ~250 messages/day for
anonymous use. Fine for a best-effort operator ping. Never a delivery guarantee.
"""

from __future__ import annotations

import re
import sys
import urllib.error
import urllib.request

from tldrack.sinks import SINK_TIMEOUT_SECONDS

DEFAULT_NTFY_SERVER = "https://ntfy.sh"

#: ntfy's own topic grammar: letters, numbers, underscore, dash, max 64.
#: The 24-char FLOOR is ours, not ntfy's — see rule 1 in the module docstring.
#: A guessable topic means strangers read your alerts and can spam your phone.
NTFY_TOPIC_RE = re.compile(r"[-_A-Za-z0-9]{24,64}")

#: ntfy caps a message at 4096 bytes. We cut well below it: a lock-screen
#: notification that needs scrolling has already failed at its job.
MAX_BODY_CHARS = 512
MAX_TITLE_CHARS = 100


def valid_topic(topic: str | None) -> bool:
    return isinstance(topic, str) and NTFY_TOPIC_RE.fullmatch(topic) is not None


def header_safe(value: str, limit: int) -> str:
    """Collapse a value to something that cannot break or inject an HTTP header.

    Belt-and-braces: every string reaching here is already shape-validated
    upstream (session names are `[A-Za-z0-9_-]+`), but a header is exactly the
    wrong place to rely on that — a stray CR/LF would let a caller append
    headers of its own choosing, and a non-latin-1 byte makes urllib raise
    where the whole point of this module is that it cannot.
    """
    collapsed = "".join(char if 32 <= ord(char) < 127 else " " for char in value)
    return " ".join(collapsed.split())[:limit]


class NtfySink:
    """Write-only ntfy publisher. Never raises; returns False on any failure."""

    name = "ntfy"

    def __init__(self, topic: str, server: str = DEFAULT_NTFY_SERVER) -> None:
        if not valid_topic(topic):
            raise ValueError(f"ntfy topic must match {NTFY_TOPIC_RE.pattern} (long enough not to be guessable)")
        self._topic = topic
        self._server = server.rstrip("/")

    @property
    def url(self) -> str:
        return f"{self._server}/{self._topic}"

    def publish(
        self,
        *,
        title: str,
        body: str,
        priority: int,
        click: str | None = None,
        tags: str | None = None,
    ) -> bool:
        """POST one message. True when ntfy accepted it.

        Every failure mode — no network, DNS failure, rate limit, 5xx, a hung
        socket — is caught and logged. A notifier that can break its caller is
        worse than no notifier, and this one is called from a thread the bridge
        depends on.
        """
        headers = {
            "Title": header_safe(title, MAX_TITLE_CHARS),
            "Priority": str(priority),
            "Content-Type": "text/plain; charset=utf-8",
        }
        if tags:
            headers["Tags"] = header_safe(tags, 64)
        if click:
            headers["Click"] = header_safe(click, 256)

        request = urllib.request.Request(
            self.url,
            data=body[:MAX_BODY_CHARS].encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=SINK_TIMEOUT_SECONDS) as response:
                return 200 <= response.status < 300
        except urllib.error.HTTPError as err:
            # 429 here is ntfy's daily cap, which our own budget should have
            # prevented reaching — say so rather than logging a bare number.
            hint = " (ntfy rate limit — check the daily budget)" if err.code == 429 else ""
            print(f"tldrack: ntfy refused the notification: {err.code}{hint}", file=sys.stderr)
        except (urllib.error.URLError, OSError, ValueError) as err:
            print(f"tldrack: could not reach ntfy ({err}) — notification dropped", file=sys.stderr)
        return False
