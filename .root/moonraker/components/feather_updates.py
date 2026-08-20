## Forge-X update adapter for Feather
##
## Copyright (C) 2026, Alexander K <https://github.com/drA1ex>
##
## This file may be distributed under the terms of the GNU GPLv3 license

from __future__ import annotations

import logging

from ..common import WebRequest

from typing import TYPE_CHECKING, Any, Dict, Optional

if TYPE_CHECKING:
    from ..confighelper import ConfigHelper
    from .application import InternalTransport
    from .job_state import JobState
    from .klippy_connection import KlippyConnection


MAX_COMMIT_SUBJECTS = 24
MAX_UPDATE_KEY_LENGTH = 64
MAX_VERSION_LENGTH = 64
MAX_SUBJECT_LENGTH = 160
MAX_ERROR_LENGTH = 160


def _version(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value or value == "?" or len(value) > MAX_VERSION_LENGTH:
        return None
    return value


def forge_x_update_snapshot(status: Any) -> Dict[str, Any]:
    """Return the bounded subset of cached status consumed by Feather."""
    if not isinstance(status, dict):
        raise ValueError("Forge-X update status is not an object")
    installed = _version(status.get("version"))
    remote = _version(status.get("remote_version"))
    if installed is None or remote is None:
        raise ValueError("Forge-X version information is unavailable")

    try:
        behind = max(0, int(status.get("commits_behind_count", 0)))
    except (TypeError, ValueError):
        raise ValueError("Forge-X commit count is invalid")
    current_hash = status.get("current_hash")
    remote_hash = status.get("remote_hash")
    available = bool(
        status.get("is_valid", False)
        and not status.get("corrupt", False)
        and not status.get("is_dirty", False)
        and installed != remote
        and behind > 0
        and isinstance(current_hash, str)
        and isinstance(remote_hash, str)
        and current_hash not in ("", "?")
        and remote_hash not in ("", "?")
        and current_hash != remote_hash)

    subjects = []
    commits = status.get("commits_behind", ())
    if isinstance(commits, (tuple, list)):
        for commit in commits[:MAX_COMMIT_SUBJECTS]:
            if not isinstance(commit, dict):
                continue
            subject = commit.get("subject")
            if not isinstance(subject, str):
                continue
            subject = " ".join(subject.split())
            if subject:
                subjects.append(subject[:MAX_SUBJECT_LENGTH])

    return {
        "available": available,
        "installed_version": installed,
        "available_version": remote,
        "changes": subjects,
        "changes_total": behind,
    }


class FeatherUpdates:
    """Adapt Moonraker's public update API to Klipper remote methods."""

    def __init__(self, config: ConfigHelper) -> None:
        self.server = config.get_server()
        self.update_key = config.get("update_key", "forge-x").strip()
        if (not self.update_key
                or len(self.update_key) > MAX_UPDATE_KEY_LENGTH):
            raise config.error(
                "[feather_updates]: update_key must contain 1 to %d characters"
                % MAX_UPDATE_KEY_LENGTH)
        self.internal_transport: InternalTransport
        self.internal_transport = self.server.lookup_component(
            "internal_transport")
        self.klippy_connection: KlippyConnection
        self.klippy_connection = self.server.lookup_component(
            "klippy_connection")
        self.job_state: JobState
        self.job_state = self.server.lookup_component("job_state")

        self.server.register_remote_method(
            "feather_request_forge_x_update_status",
            self._handle_status_request)
        self.server.register_remote_method(
            "feather_start_forge_x_update",
            self._handle_update_request)

    def _printer_busy(self) -> bool:
        try:
            state = self.job_state.get_last_stats().get("state", "")
            return not self.klippy_connection.is_ready() or state in (
                "printing", "paused")
        except Exception:
            return True

    async def _cached_forge_x_status(self) -> Dict[str, Any]:
        status = await self.internal_transport.call_method(
            "machine.update.status", refresh=False)
        if not isinstance(status, dict):
            raise ValueError("Moonraker update status is unavailable")
        versions = status.get("version_info")
        if not isinstance(versions, dict) or self.update_key not in versions:
            raise ValueError(
                "Configured update source is unavailable: %s"
                % self.update_key)
        return forge_x_update_snapshot(versions[self.update_key])

    async def _handle_status_request(self, token: int) -> None:
        response: Dict[str, Any] = {"token": int(token), "error": ""}
        try:
            response.update(await self._cached_forge_x_status())
        except Exception as exc:
            logging.info("Feather update status unavailable: %s", exc)
            response["error"] = str(exc)[:MAX_ERROR_LENGTH]

        try:
            await self.klippy_connection.request(WebRequest(
                "feather/update_status", response))
        except Exception:
            logging.exception(
                "Unable to return Forge-X update status to Feather")

    async def _handle_update_request(self, expected_version: str) -> None:
        if self._printer_busy():
            logging.info(
                "Feather Forge-X update refused: Klippy is not safely idle")
            return

        try:
            snapshot = await self._cached_forge_x_status()
        except Exception:
            logging.exception("Unable to verify Forge-X update status")
            return
        if (not snapshot["available"]
                or snapshot["available_version"] != expected_version):
            logging.info(
                "Feather Forge-X update refused: available version changed")
            return
        if self._printer_busy():
            logging.info(
                "Feather Forge-X update refused: Klippy is not safely idle")
            return

        try:
            await self.internal_transport.call_method(
                "machine.update.client", name=self.update_key)
        except Exception:
            logging.exception("Unable to start Forge-X update")


def load_component(config: ConfigHelper) -> FeatherUpdates:
    return FeatherUpdates(config)
