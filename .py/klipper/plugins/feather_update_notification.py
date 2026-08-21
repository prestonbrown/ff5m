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
CHECK_TIMEOUT = 60.0
INSTALL_ACK_TIMEOUT = 15.0
FAILURE_RETRY_INTERVAL = 300.0
MAX_FAILURE_RETRY_INTERVAL = 21600.0
SAFETY_RETRY_INTERVAL = 300.0
DEFAULT_UPDATE_INTERVAL_MINUTES = 360
CHANGE_PAGE_SIZE = 6
MAX_CHANGE_ITEMS = 24
MAX_VERSION_LENGTH = 64
MAX_CHANGE_LENGTH = 160
MAX_PROGRESS_LENGTH = 160

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
        self.failure_streak = 0
        self.check_enabled = True
        self.check_interval = DEFAULT_UPDATE_INTERVAL_MINUTES * 60.0
        self._schedule_generation = 0
        self._scheduled_at = None
        self.installed_version = None
        self.available_version = None
        self.dismissed_version = None
        self.changes = ()
        self.change_page = 0
        self.installing = False
        self.install_confirmed = False
        self.install_token = 0

        if self.webhooks is not None:
            self.webhooks.register_endpoint(
                "feather/update_status", self.handle_status_response)
            self.webhooks.register_endpoint(
                "feather/update_progress", self.handle_update_progress)

    def start(self):
        if self.started and self.active:
            return
        self.started = True
        self.active = True
        self.check_enabled, self.check_interval = self._read_settings()
        if not self.check_enabled:
            self._disable_availability_check()
            logging.info("[feather_screen] update checks disabled")
            return
        logging.info("[feather_screen] update check scheduled")
        self._schedule_check(STARTUP_DELAY)

    def stop(self):
        self._clear_install_progress()
        self.active = False
        self.check_in_flight = False
        self.check_pending = False
        self.check_token += 1
        self._schedule_generation += 1
        self._scheduled_at = None

    @property
    def dialog_visible(self):
        return getattr(
            self.host, "page", None) == ScreenPage.UPDATE_NOTIFICATION

    def _read_settings(self):
        params = getattr(self.host, "params", None)
        if params is None:
            printer = getattr(self.host, "printer", None)
            if printer is not None:
                params = printer.lookup_object("mod_params", None)
        variables = getattr(params, "variables", {})

        enabled = bool(variables.get("mod_check_update", True))
        minutes = variables.get(
            "mod_check_update_interval", DEFAULT_UPDATE_INTERVAL_MINUTES)
        if (not isinstance(minutes, int) or isinstance(minutes, bool)
                or minutes <= 0):
            minutes = DEFAULT_UPDATE_INTERVAL_MINUTES
        return enabled, minutes * 60.0

    def on_mod_params_changed(self):
        enabled, interval = self._read_settings()
        enabled_changed = enabled != self.check_enabled
        interval_changed = interval != self.check_interval
        self.check_enabled = enabled
        self.check_interval = interval

        if not self.started or not self.active:
            return
        if enabled_changed and not enabled:
            self._disable_availability_check()
            return
        if enabled_changed:
            self.failure_streak = 0
            self._replace_scheduled_check(0.0)
            return
        if enabled and interval_changed and not self.check_in_flight:
            self.failure_streak = 0
            self._replace_scheduled_check(interval)

    def _disable_availability_check(self):
        self._cancel_scheduled_check()
        self.check_pending = False
        self.check_in_flight = False
        self.check_token += 1
        if self.installing:
            return

        self.installed_version = None
        self.available_version = None
        self.changes = ()
        self.change_page = 0
        if self.dialog_visible:
            self.host._show_page(self.host.page_for_print_state())

    def _schedule_check(self, delay):
        if not self.active or not self.check_enabled:
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

    def _replace_scheduled_check(self, delay):
        self._cancel_scheduled_check()
        self._schedule_check(delay)

    def _schedule_failure_retry(self):
        exponent = min(self.failure_streak, 7)
        delay = min(
            FAILURE_RETRY_INTERVAL * (2 ** exponent),
            MAX_FAILURE_RETRY_INTERVAL)
        self.failure_streak += 1
        self._schedule_check(delay)

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
        if not self.active or not self.check_enabled or self.check_in_flight:
            return False
        if self._printer_busy():
            self.check_pending = True
            logging.info("[feather_screen] update check skipped: printer busy")
            if not self._print_active():
                self._schedule_check(SAFETY_RETRY_INTERVAL)
            return False
        if self.webhooks is None:
            logging.info(
                "[feather_screen] update check failed: Moonraker bridge unavailable")
            self._schedule_failure_retry()
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
            self._schedule_failure_retry()
            return False

        def check_timeout(_eventtime):
            if (self.active and self.check_enabled and self.check_in_flight
                    and self.check_token == token):
                self.check_in_flight = False
                logging.info("[feather_screen] update check failed: timeout")
                self._schedule_failure_retry()

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
        if not self._owns_check(token):
            return "ok"

        try:
            return self._handle_status_response(web_request, token)
        except Exception:
            if not self._owns_check(token):
                return "ok"
            self.check_in_flight = False
            logging.exception(
                "[feather_screen] update check failed: malformed response")
            self._schedule_failure_retry()
            return "ok"

    def _owns_check(self, token):
        return (self.active and self.check_enabled and self.check_in_flight
                and token == self.check_token)

    def _handle_status_response(self, web_request, token):
        if not self._owns_check(token):
            return "ok"

        error = web_request.get_str("error", "")
        if error:
            self.check_in_flight = False
            logging.info("[feather_screen] update check failed: %s", error)
            self._schedule_failure_retry()
            return "ok"

        update_available = web_request.get("available", None)
        if not isinstance(update_available, bool):
            raise ValueError("availability is not boolean")
        installed = self._bounded_version(
            web_request.get("installed_version", None))
        available = self._bounded_version(
            web_request.get("available_version", None))
        if installed is None or available is None:
            raise ValueError("incomplete version information")

        if not update_available:
            self.check_in_flight = False
            self.failure_streak = 0
            self.installed_version = None
            self.available_version = None
            self.changes = ()
            self._schedule_check(self.check_interval)
            return "ok"

        changes = self._bounded_changes(
            web_request.get("changes", ()),
            web_request.get("changes_total", 0))
        if installed == available:
            raise ValueError("available version matches installed version")

        self.check_in_flight = False
        self.failure_streak = 0
        self.installed_version = installed
        self.available_version = available
        self.changes = changes or ("CHANGELOG UNAVAILABLE",)
        self.change_page = 0
        logging.info("[feather_screen] update available: %s -> %s",
                     installed, available)
        if available == self.dismissed_version:
            self._schedule_check(self.check_interval)
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
        if (self.installing or self.dialog_visible
                or self.available_version is None
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
            if self.installing:
                return
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
        if self.installing:
            self.host.renderer.loader(
                getattr(self.host, "busy_message", None)
                or "UPDATING FORGE-X...",
                getattr(self.host, "busy_phase", 0))
            return
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
            72, 150, "CHANGES SINCE %s" %
            (self.installed_version or "CURRENT VERSION"),
            ThemeColor.PRIMARY, "JetBrainsMono Bold 8pt",
            max_width=570, truncate=True))
        for index, subject in enumerate(pagination.visible):
            commands.append(self.host.renderer.text(
                78, 184 + index * 30, "- " + subject,
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

    @staticmethod
    def _bounded_progress(value):
        if not isinstance(value, str):
            return ""
        return " ".join(value.split())[:MAX_PROGRESS_LENGTH]

    def _show_install_progress(self, message, detail=None):
        message = self._bounded_progress(message) or "UPDATING FORGE-X..."
        if detail:
            detail = self._bounded_progress(detail)
            if detail:
                message += "\n" + detail
        self.installing = True
        self.host.busy_message = message
        self.host.busy_phase = 0
        if self.dialog_visible:
            self.render()

    def _clear_install_progress(self):
        if not self.installing:
            return
        self.installing = False
        self.install_confirmed = False
        self.host.busy_message = None

    def handle_update_progress(self, web_request):
        try:
            return self._handle_update_progress(web_request)
        except Exception:
            logging.exception(
                "[feather_screen] update progress failed: malformed response")
            return "ok"

    def _handle_update_progress(self, web_request):
        token = web_request.get_int("token")
        if not self.installing or token != self.install_token:
            return "ok"
        state = web_request.get_str("state", "")
        message = self._bounded_progress(
            web_request.get_str("message", ""))
        if state in ("accepted", "progress"):
            self.install_confirmed = True
            self._show_install_progress(message)
            return "ok"
        if state in ("complete", "restarting"):
            self.install_confirmed = True
            self._show_install_progress(
                "PRINTER WILL RESTART NOW",
                "IF IT DOES NOT RESTART AUTOMATICALLY, RESTART IT MANUALLY")
            return "ok"
        if state != "failed":
            return "ok"

        self._clear_install_progress()
        logging.info("[feather_screen] update failed: %s", message)
        self.host._show_message(
            "Unable to update Forge-X: %s" %
            (message or "unknown error"),
            ScreenPage.UPDATE_NOTIFICATION)
        self._schedule_check(FAILURE_RETRY_INTERVAL)
        return "ok"

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
            self._schedule_check(self.check_interval)
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
        self.install_token += 1
        token = self.install_token
        self.install_confirmed = False
        self._show_install_progress("PREPARING FORGE-X UPDATE...")
        try:
            self.webhooks.call_remote_method(
                "feather_start_forge_x_update", expected_version=version,
                token=token)
        except Exception as exc:
            self._clear_install_progress()
            logging.exception(
                "[feather_screen] update request failed: dispatch")
            self.host._show_message(
                "Unable to start update: %s" % exc,
                ScreenPage.IDLE_HOME)
            self._schedule_check(FAILURE_RETRY_INTERVAL)
            return
        logging.info("[feather_screen] update requested: %s", version)

        def start_timeout(_eventtime):
            if (not self.installing or self.install_token != token
                    or self.install_confirmed):
                return
            self._clear_install_progress()
            logging.info("[feather_screen] update request failed: timeout")
            self.host._show_message(
                "Unable to start Forge-X update: Moonraker did not respond",
                ScreenPage.UPDATE_NOTIFICATION)
            self._schedule_check(FAILURE_RETRY_INTERVAL)

        self.reactor.register_callback(
            start_timeout, self.reactor.monotonic() + INSTALL_ACK_TIMEOUT)
