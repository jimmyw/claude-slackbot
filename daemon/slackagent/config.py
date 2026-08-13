"""Configuration, all of it from the environment.

Secrets are never committed: systemd loads them from an EnvironmentFile with
mode 0600. See .env.example for the shape.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


class ConfigError(RuntimeError):
    pass


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ConfigError(f"{name} is not set")
    return value


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc


@dataclass(frozen=True)
class Config:
    # Slack
    bot_token: str
    app_token: str
    authorized_user: str

    # VM bridge
    vm_host: str
    vm_user: str
    vm_ssh_key: Path
    vm_domain: str
    vm_workdir: str
    libvirt_uri: str

    # Approval gate
    approval_host: str
    approval_port: int
    approval_timeout_s: int
    tunnel_port_low: int
    tunnel_port_high: int

    # Local state
    db_path: Path

    # Rendering
    update_interval_s: float

    extra_system_prompt: str = field(default="")

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            bot_token=_required("SLACK_BOT_TOKEN"),
            app_token=_required("SLACK_APP_TOKEN"),
            authorized_user=_required("AUTHORIZED_USER_ID"),
            vm_host=_required("VM_HOST"),
            vm_user=os.environ.get("VM_USER", "agent"),
            vm_ssh_key=Path(
                os.environ.get("VM_SSH_KEY", "~/.ssh/agent_vm_ed25519")
            ).expanduser(),
            vm_domain=os.environ.get("VM_DOMAIN", "agent-vm"),
            vm_workdir=os.environ.get("VM_WORKDIR", "/home/agent/work"),
            # NOT the default URI: for an unprivileged user that resolves to
            # qemu:///session, a separate and empty hypervisor instance.
            libvirt_uri=os.environ.get("LIBVIRT_URI", "qemu:///system"),
            approval_host=os.environ.get("APPROVAL_HOST", "127.0.0.1"),
            approval_port=_int("APPROVAL_PORT", 9100),
            approval_timeout_s=_int("APPROVAL_TIMEOUT_S", 600),
            tunnel_port_low=_int("TUNNEL_PORT_LOW", 9101),
            tunnel_port_high=_int("TUNNEL_PORT_HIGH", 9199),
            db_path=Path(
                os.environ.get(
                    "DB_PATH", "~/.local/share/slack-claude/state.sqlite3"
                )
            ).expanduser(),
            update_interval_s=float(os.environ.get("UPDATE_INTERVAL_S", "1.2")),
            extra_system_prompt=os.environ.get("EXTRA_SYSTEM_PROMPT", ""),
        )

    @property
    def known_hosts(self) -> Path:
        """Where ssh should keep host keys.

        Not ~/.ssh/known_hosts: the systemd unit mounts $HOME read-only, so ssh
        could not write there. This sits beside the sqlite state, which is the
        one directory the unit grants write access to.
        """
        return self.db_path.parent / "known_hosts"

    def validate(self) -> None:
        # Catch an unedited .env before it becomes a confusing Slack API error.
        # The example file's values are all non-empty and the right shape, so the
        # generic checks below pass and the first symptom is otherwise an
        # `invalid_auth` traceback from apps.connections.open.
        placeholders = {
            "SLACK_BOT_TOKEN": (self.bot_token, "xoxb-..."),
            "SLACK_APP_TOKEN": (self.app_token, "xapp-..."),
            "AUTHORIZED_USER_ID": (self.authorized_user, "U000000000"),
        }
        unset = [
            name
            for name, (value, example) in placeholders.items()
            if value == example
        ]
        if unset:
            raise ConfigError(
                f"{', '.join(unset)} still holds the .env.example placeholder — "
                "fill in the real values in daemon/.env"
            )

        if not self.vm_ssh_key.is_file():
            raise ConfigError(f"VM_SSH_KEY does not exist: {self.vm_ssh_key}")
        if not self.authorized_user.startswith("U"):
            raise ConfigError(
                f"AUTHORIZED_USER_ID should be a Slack user ID like U012ABCDEF, "
                f"got {self.authorized_user!r}"
            )
        if self.approval_timeout_s >= 850:
            # settings.json pins the hook timeout at 900s. The approval window
            # has to close first, or the harness kills the hook before Slack can
            # answer and the deny reason becomes a hook crash.
            raise ConfigError(
                "APPROVAL_TIMEOUT_S must stay below the 900s hook timeout "
                "(leave headroom; 600 is the default)"
            )
        if self.tunnel_port_low >= self.tunnel_port_high:
            raise ConfigError("TUNNEL_PORT_LOW must be below TUNNEL_PORT_HIGH")
