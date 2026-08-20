## Forge-X update availability notification for Feather
##
## Copyright (C) 2026, Alexander K <https://github.com/drA1ex>
##
## This file may be distributed under the terms of the GNU GPLv3 license

import logging

from feather_pagination import Pagination
from ff5m_ui.print_state import PrintState
from ff5m_ui.screen import ScreenPage
from ui import ThemeColor


STARTUP_DELAY = 5.0
CHECK_TIMEOUT = 15.0
FAILURE_RETRY_INTERVAL = 300.0
RECHECK_INTERVAL = 21600.0
CHANGE_PAGE_SIZE = 6
MAX_CHANGE_ITEMS = 24
MAX_VERSION_LENGTH = 64
MAX_CHANGE_LENGTH = 160

_ACTIVE_PRINT_STATES = frozenset((
    PrintState.PREPARING, PrintState.PRINTING, PrintState.PAUSED,
))


class ForgeXUpdateNotification:
    """Own the small, process-local lifecycle of one update notification."""

    def __init__(self, host, webhooks):
        self.host = host
        self.reactor = host.reactor
        self.webhooks = webhooks
        self.active = True
        self.started = False
        self.check_in_flight = False
        self.check_pending = False
        self.check_token = 0
        self._schedule_generation = 0
        self._scheduled_at = None
        self.installed_version = None
        self.available_version = None
        self.dismissed_version = None
        self.changes = ()
        self.change_page = 0

        if self.webhooks is not None:
            self.webhooks.register_endpoint(
                "feather/update_status", self.handle_status_response)

    def start(self):
        if self.started and self.active:
            return
        self.started = True
        self.active = True
        logging.info("[feather_screen] update check scheduled")
        self._schedule_check(STARTUP_DELAY)

    def stop(self):
        self.active = False
        self.check_in_flight = False
        self.check_pending = False
        self._schedule_generation += 1
        self._scheduled_at = None

    @property
    def dialog_visible(self):
        return getattr(
            self.host, "page", None) == ScreenPage.UPDATE_NOTIFICATION

    def _schedule_check(self, delay):
        if not self.active:
            return
        deadline = self.reactor.monotonic() + max(0.0, float(delay))
        if self._scheduled_at is not None and self._scheduled_at <= deadline:
            return
        self._schedule_generation += 1
        generation = self._schedule_generation
        self._scheduled_at = deadline

        def check_due(_eventtime):
            if not self.active or generation != self._schedule_generation:
                return
            self._scheduled_at = None
            self.request_check()

        self.reactor.register_callback(check_due, deadline)

    def _cancel_scheduled_check(self):
        if self._scheduled_at is None:
            return
        self._schedule_generation += 1
        self._scheduled_at = None

    def _print_active(self):
        if getattr(self.host, "print_state", None) in _ACTIVE_PRINT_STATES:
            return True
        try:
            return bool(self.host._safety_print_active(
                self.reactor.monotonic()))
        except Exception:
            logging.exception(
                "[feather_screen] update check failed: unable to read print state")
            return True

    def _printer_busy(self):
        if self._print_active():
            return True
        try:
            notification_safe = getattr(
                self.host, "_update_notification_safe", None)
            if notification_safe is not None:
                return not bool(notification_safe(
                    self.reactor.monotonic()))
            return False
        except Exception:
            logging.exception(
                "[feather_screen] update check failed: unable to read activity state")
            return True

    def request_check(self):
        if self.check_in_flight:
            return False
        if self._printer_busy():
            self.check_pending = True
            logging.info("[feather_screen] update check skipped: printer busy")
            if not self._print_active():
                self._schedule_check(FAILURE_RETRY_INTERVAL)
            return False
        if self.webhooks is None:
            logging.info(
                "[feather_screen] update check failed: Moonraker bridge unavailable")
            return False

        self.check_token += 1
        self._cancel_scheduled_check()
        token = self.check_token
        self.check_in_flight = True
        self.check_pending = False
        try:
            self.webhooks.call_remote_method(
                "feather_request_forge_x_update_status", token=token)
        except Exception:
            self.check_in_flight = False
            logging.exception(
                "[feather_screen] update check failed: request dispatch")
            self._schedule_check(FAILURE_RETRY_INTERVAL)
            return False

        def check_timeout(_eventtime):
            if not self.active:
                return
            if self.check_in_flight and self.check_token == token:
                self.check_in_flight = False
                logging.info("[feather_screen] update check failed: timeout")
                self._schedule_check(FAILURE_RETRY_INTERVAL)

        self.reactor.register_callback(
            check_timeout, self.reactor.monotonic() + CHECK_TIMEOUT)
        return True

    @staticmethod
    def _bounded_version(value):
        if not isinstance(value, str):
            return None
        value = value.strip()
        if not value or value == "?" or len(value) > MAX_VERSION_LENGTH:
            return None
        return value

    @staticmethod
    def _bounded_changes(raw_changes, reported_total):
        if not isinstance(raw_changes, (tuple, list)):
            raise ValueError("commit subjects are not a list")
        subjects = []
        for raw_subject in raw_changes:
            if not isinstance(raw_subject, str):
                continue
            subject = " ".join(raw_subject.split())
            if not subject:
                continue
            subjects.append(subject[:MAX_CHANGE_LENGTH])

        try:
            total = max(len(raw_changes), int(reported_total))
        except (TypeError, ValueError):
            total = len(raw_changes)
        if total > MAX_CHANGE_ITEMS:
            visible = subjects[:MAX_CHANGE_ITEMS - 1]
            visible.append("... %d MORE CHANGES NOT SHOWN" %
                           max(1, total - len(visible)))
            return tuple(visible)
        return tuple(subjects[:MAX_CHANGE_ITEMS])

    def handle_status_response(self, web_request):
        try:
            token = web_request.get_int("token")
        except Exception:
            logging.info(
                "[feather_screen] update check failed: malformed response token")
            return "ok"
        if not self.check_in_flight or token != self.check_token:
            return "ok"
        self.check_in_flight = False

        error = web_request.get_str("error", "")
        if error:
            logging.info("[feather_screen] update check failed: %s", error)
            self._schedule_check(FAILURE_RETRY_INTERVAL)
            return "ok"
        if not web_request.get_boolean("available", False):
            self.installed_version = None
            self.available_version = None
            self.changes = ()
            self._schedule_check(RECHECK_INTERVAL)
            return "ok"

        installed = self._bounded_version(
            web_request.get("installed_version", None))
        available = self._bounded_version(
            web_request.get("available_version", None))
        try:
            changes = self._bounded_changes(
                web_request.get("changes", ()),
                web_request.get("changes_total", 0))
        except (TypeError, ValueError):
            logging.info(
                "[feather_screen] update check failed: malformed update information")
            self._schedule_check(FAILURE_RETRY_INTERVAL)
            return "ok"
        if installed is None or available is None or installed == available:
            logging.info(
                "[feather_screen] update check failed: incomplete version information")
            self._schedule_check(FAILURE_RETRY_INTERVAL)
            return "ok"

        self.installed_version = installed
        self.available_version = available
        self.changes = changes or ("CHANGELOG UNAVAILABLE",)
        self.change_page = 0
        logging.info("[feather_screen] update available: %s -> %s",
                     installed, available)
        if available == self.dismissed_version:
            self._schedule_check(RECHECK_INTERVAL)
            return "ok"
        if not self.maybe_present():
            logging.info("[feather_screen] update notification deferred")
        return "ok"

    def _can_present(self):
        return (self.active
                and getattr(self.host, "page", None) == ScreenPage.IDLE_HOME
                and getattr(self.host, "print_state", None) == PrintState.IDLE
                and not self._printer_busy()
                and not getattr(self.host, "busy_message", None))

    def maybe_present(self):
        if (self.dialog_visible or self.available_version is None
                or self.available_version == self.dismissed_version
                or not self._can_present()):
            return False
        self.change_page = 0
        logging.info("[feather_screen] update notification shown")
        self.host._show_page(ScreenPage.UPDATE_NOTIFICATION)
        return True

    def on_page_changed(self, _old_page, new_page):
        if new_page == ScreenPage.IDLE_HOME:
            self.maybe_present()

    def on_print_state_changed(self, _old_state, new_state, _stats_state):
        if new_state in _ACTIVE_PRINT_STATES:
            if self.dialog_visible:
                logging.info(
                    "[feather_screen] update notification interrupted by print")
                self.host._show_page(self.host.page_for_print_state())
            return
        if new_state == PrintState.IDLE:
            if self.check_pending:
                self.request_check()
            else:
                self.maybe_present()

    def render(self):
        pagination = Pagination(
            self.changes or ("CHANGELOG UNAVAILABLE",),
            self.change_page, CHANGE_PAGE_SIZE)
        self.change_page = pagination.page
        version = self.available_version or "UNKNOWN"
        commands = self.host.renderer.begin_page("Forge-X update")
        commands += self.host.renderer.dialog(
            "Forge-X %s available" % version, (),
            (("update.later", "LATER", "enabled"),
             ("update.install", "UPDATE", "warning")),
            x=40, y=72, width=720, height=350, tone="info")
        commands.append(self.host.renderer.text(
            72, 133, "CHANGES SINCE %s" %
            (self.installed_version or "CURRENT VERSION"),
            ThemeColor.PRIMARY, "JetBrainsMono Bold 8pt",
            max_width=570, truncate=True))
        for index, subject in enumerate(pagination.visible):
            commands.append(self.host.renderer.text(
                78, 165 + index * 30, "- " + subject,
                ThemeColor.TEXT, "JetBrainsMono 8pt",
                max_width=570, truncate=True))
        if pagination.page_count > 1:
            commands += self.host.renderer.arrow_button(
                "update.prev", 678, 137, 52, 48, "up",
                active=pagination.has_previous)
            commands += self.host.renderer.arrow_button(
                "update.next", 678, 287, 52, 48, "down",
                active=pagination.has_next)
            commands.append(self.host.renderer.text(
                704, 237, "%d / %d" %
                (pagination.page + 1, pagination.page_count),
                ThemeColor.DIM, "JetBrainsMono 8pt", "center", "middle"))
        self.host.renderer.send(commands)

    def handle_action(self, action):
        if action in ("update.prev", "update.next"):
            pagination = Pagination(
                self.changes, self.change_page, CHANGE_PAGE_SIZE)
            delta = -1 if action == "update.prev" else 1
            target = max(
                0, min(pagination.page + delta, pagination.page_count - 1))
            if target != self.change_page:
                self.change_page = target
                self.render()
            return
        if not self.dialog_visible:
            return
        if action == "update.later":
            self.dismissed_version = self.available_version
            logging.info("[feather_screen] update dismissed: %s",
                         self.dismissed_version)
            self._schedule_check(RECHECK_INTERVAL)
            self.host._show_page(ScreenPage.IDLE_HOME)
            return
        if action != "update.install":
            return
        if self._printer_busy():
            logging.info(
                "[feather_screen] update request rejected: printer busy")
            self.host._show_page(self.host.page_for_print_state())
            return

        version = self.available_version
        try:
            self.webhooks.call_remote_method(
                "feather_start_forge_x_update", expected_version=version)
        except Exception as exc:
            logging.exception(
                "[feather_screen] update request failed: dispatch")
            self.host._show_message(
                "Unable to start update: %s" % exc,
                ScreenPage.IDLE_HOME)
            self._schedule_check(FAILURE_RETRY_INTERVAL)
            return
        logging.info("[feather_screen] update requested: %s", version)
        self.available_version = None
        self.changes = ()
        self._schedule_check(RECHECK_INTERVAL)
        self.host._show_message(
            "Starting Forge-X update", ScreenPage.IDLE_HOME)
