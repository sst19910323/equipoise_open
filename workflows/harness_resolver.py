"""Neutral codex-harness mapping for the shared /opt/equipoise tree.

The numbered Contabo wrappers live under /usr/local/bin. Older deployments
use /root/.local/bin and separate .codex-N homes.

Resolution order: explicit --harness > EQUIPOISE_HARNESS env > codex1.  CODEX_HOME
alone is never used to guess the identity (interactive shells may carry either
value).  Claude runs through its own CC runner and is deliberately not
resolvable here.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

_CODEX_VARIANTS = {
    "codex1": ("/root/.local/bin/codex1", "/root/.codex"),
    "codex2": ("/root/.local/bin/codex2", "/root/.codex-2"),
    "codex3": ("/root/.local/bin/codex3", "/root/.codex-3"),
    "codex4": ("/root/.local/bin/codex4", "/root/.codex-4"),
}
DEFAULT_HARNESS = "codex1"
LOCAL_BIN = "/root/.local/bin"
CHOICES = sorted(_CODEX_VARIANTS)


@dataclass(frozen=True)
class CodexHarness:
    name: str
    executable: str
    codex_home: str

    def child_env(self, base: dict | None = None) -> dict:
        """Environment for a subprocess that must run under this identity.

        Sets EQUIPOISE_HARNESS + CODEX_HOME explicitly (the wrapper also exports
        CODEX_HOME, this keeps python-side children consistent) and makes sure
        PATH contains the wrapper directory (systemd's default PATH lacks it).
        """
        env = dict(os.environ if base is None else base)
        env["EQUIPOISE_HARNESS"] = self.name
        env["CODEX_HOME"] = self.codex_home
        path = env.get("PATH", "")
        bin_dir = os.path.dirname(self.executable)
        if bin_dir not in path.split(":"):
            env["PATH"] = f"{bin_dir}:{path}" if path else bin_dir
        return env

    def describe(self) -> str:
        return f"{self.name} ({self.executable}, CODEX_HOME={self.codex_home})"


def resolve_codex(name: str | None = None) -> CodexHarness:
    value = (name or os.getenv("EQUIPOISE_HARNESS") or DEFAULT_HARNESS).strip().lower()
    if value == "claude":
        raise ValueError(
            "harness 'claude' uses the separate CC runner; the codex resolver never falls back to it"
        )
    if value not in _CODEX_VARIANTS:
        raise ValueError(f"unknown codex harness {value!r}; expected one of {CHOICES}")
    executable, home = _CODEX_VARIANTS[value]
    if os.path.isdir("/root/.codex-accounts") and os.path.isfile("/usr/local/bin/codex1"):
        executable = f"/usr/local/bin/{value}"
        if value != "codex1":
            home = f"/root/.codex-accounts/{value.removeprefix('codex')}"
    return CodexHarness(value, executable, home)
