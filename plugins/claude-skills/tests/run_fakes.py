"""Fakes for POST /run: a profile registry and a recording `subprocess.run`.

Nothing here spawns anything. `FakeRun` answers each tmux verb from a table and records
every argv, so a test can assert what was — and was never — run.
"""

from __future__ import annotations

import subprocess
from pathlib import Path


class FakeProfiles:
    """The three registry calls /run makes. `spawn_argv` returns a recognisable argv."""

    def __init__(self, claude_id: str | None = "a", cli_found: bool = True) -> None:
        self.claude_id = claude_id
        self._cli_found = cli_found
        self.spawned: list[tuple] = []

    def default_of_kind(self, kind: str) -> str | None:
        return self.claude_id if kind == "claude" else None

    def cli_found(self, profile_id: object) -> bool:
        return self._cli_found

    def spawn_argv(self, profile_id, session, cwd, extra_args=(), cli_bin=None) -> list[str]:
        if profile_id != self.claude_id:
            raise KeyError(profile_id)
        self.spawned.append((profile_id, session, cwd, tuple(extra_args)))
        return ["tmux", "new-session", "-d", "-s", session, "-c", str(cwd), "FAKE-CLI", *extra_args]


class FakeRun:
    """`subprocess.run` for tmux: `live` is the set of sessions that exist."""

    def __init__(self, live: set[str] | None = None, new_session_stderr: str = "", new_session_code: int = 0) -> None:
        self.live = set(live or ())
        self.calls: list[list[str]] = []
        self._new_stderr = new_session_stderr
        self._new_code = new_session_code

    def __call__(self, argv, **kwargs) -> subprocess.CompletedProcess:
        argv = list(argv)
        self.calls.append(argv)
        verb = argv[1]
        if verb == "has-session":
            return subprocess.CompletedProcess(argv, 0 if argv[3].lstrip("=") in self.live else 1, "", "")
        if verb == "kill-session":
            self.live.discard(argv[3].lstrip("="))
            return subprocess.CompletedProcess(argv, 0, "", "")
        if verb == "new-session":
            if self._new_code == 0:
                self.live.add(argv[4])
            return subprocess.CompletedProcess(argv, self._new_code, "", self._new_stderr)
        raise AssertionError(f"unexpected tmux verb: {argv}")

    def verbs(self) -> list[str]:
        return [call[1] for call in self.calls]


def write_skill(skills_dir: Path, skill: str) -> Path:
    skill_dir = skills_dir / skill
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(f"---\nname: {skill}\n---\n", encoding="utf-8")
    return skill_dir


def project_with_skill(tmp_path: Path, skill: str = "my-skill") -> Path:
    project = tmp_path / "repo"
    write_skill(project / ".claude" / "skills", skill)
    return project


class DescribingProfiles(FakeProfiles):
    """A registry that also says which config dir the claude profile runs in."""

    def __init__(self, config_dir: Path, **kwargs) -> None:
        super().__init__(**kwargs)
        self.config_dir = config_dir

    def describe(self, profile_id: object) -> dict | None:
        return {"id": profile_id, "kind": "claude", "config_dir": str(self.config_dir)} if profile_id == self.claude_id else None


def has_tmux(name: str) -> str | None:
    return "/usr/bin/tmux" if name == "tmux" else None


def no_tmux(name: str) -> str | None:
    return None
