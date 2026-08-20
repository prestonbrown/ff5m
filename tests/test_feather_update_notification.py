## Behavioral tests for the Feather Forge-X update notification.
##
## Copyright (C) 2026, Alexander K <https://github.com/drA1ex>
##
## This file may be distributed under the terms of the GNU GPLv3 license

import pathlib
import sys
import unittest


PLUGINS = (pathlib.Path(__file__).parents[1] / ".py" / "klipper" /
           "plugins")
sys.path.insert(0, str(PLUGINS))

from feather_update_notification import (  # noqa: E402
    CHECK_TIMEOUT, ForgeXUpdateNotification, STARTUP_DELAY)
from ff5m_ui.print_state import PrintState  # noqa: E402
from ff5m_ui.screen import ScreenPage  # noqa: E402
from ui import FeatherRenderer  # noqa: E402


class Reactor:
    def __init__(self, now=100.0):
        self.now = float(now)
        self.callbacks = []
        self.sequence = 0

    def monotonic(self):
        return self.now

    def register_callback(self, callback, when=None):
        self.sequence += 1
        self.callbacks.append((
            self.now if when is None else float(when),
            self.sequence, callback))

    def run_until(self, deadline):
        deadline = float(deadline)
        while self.callbacks:
            scheduled, sequence, callback = min(
                self.callbacks, key=lambda item: (item[0], item[1]))
            if scheduled > deadline:
                break
            self.callbacks.remove((scheduled, sequence, callback))
            self.now = max(self.now, scheduled)
            callback(self.now)
        self.now = max(self.now, deadline)


class Webhooks:
    def __init__(self):
        self.endpoints = {}
        self.remote_calls = []

    def register_endpoint(self, path, callback):
        self.endpoints[path] = callback

    def call_remote_method(self, method, **kwargs):
        self.remote_calls.append((method, kwargs))


class WebRequest:
    def __init__(self, **values):
        self.values = values

    def get_int(self, name, default=None):
        return int(self.values.get(name, default))

    def get_boolean(self, name, default=None):
        return bool(self.values.get(name, default))

    def get(self, name, default=None):
        return self.values.get(name, default)

    def get_str(self, name, default=None):
        value = self.values.get(name, default)
        return default if value is None else str(value)


class Host:
    def __init__(self):
        self.reactor = Reactor()
        self.webhooks = Webhooks()
        self.renderer = FeatherRenderer()
        self.draw_batches = []
        self.renderer.send = self.draw_batches.append
        self.page = ScreenPage.IDLE_HOME
        self.previous_page = ScreenPage.IDLE_HOME
        self.print_state = PrintState.IDLE
        self.print_active = False
        self.operation_active = False
        self.busy_message = None
        self.messages = []
        self.update_notification = ForgeXUpdateNotification(
            self, self.webhooks)

    def _safety_print_active(self, _eventtime):
        return self.print_active

    def _update_notification_safe(self, _eventtime):
        return not (self.print_active or self.operation_active)

    def _show_page(self, page):
        old_page = self.page
        self.previous_page = old_page
        self.page = page
        self.update_notification.on_page_changed(old_page, page)
        if page == ScreenPage.UPDATE_NOTIFICATION:
            self.update_notification.render()

    def _show_message(self, message, return_page):
        self.messages.append((message, return_page))
        self._show_page(ScreenPage.MESSAGE)

    def page_for_print_state(self):
        return (ScreenPage.PRINTING if self.print_active
                else ScreenPage.IDLE_HOME)


def status_response(notification, version="1.4.3", installed="1.4.2",
                    changes=None, available=True, token=None, error=""):
    if token is None:
        token = notification.check_token
    if changes is None:
        changes = ["Fix Wi-Fi", "Improve boot splash"]
    return WebRequest(
        token=token, available=available,
        installed_version=installed, available_version=version,
        changes=changes, changes_total=len(changes), error=error)


class ForgeXUpdateNotificationTest(unittest.TestCase):
    def setUp(self):
        self.host = Host()
        self.notification = self.host.update_notification

    def request_and_respond(self, **kwargs):
        self.notification.request_check()
        request = status_response(self.notification, **kwargs)
        self.notification.handle_status_response(request)

    def test_startup_check_waits_five_seconds(self):
        self.notification.start()

        self.host.reactor.run_until(100.0 + STARTUP_DELAY - 0.01)
        self.assertEqual(self.host.webhooks.remote_calls, [])

        self.host.reactor.run_until(100.0 + STARTUP_DELAY)
        self.assertEqual(
            self.host.webhooks.remote_calls[0][0],
            "feather_request_forge_x_update_status")

    def test_startup_check_defers_until_printer_returns_idle(self):
        self.host.print_active = True
        self.host.print_state = PrintState.PRINTING
        self.notification.start()

        self.host.reactor.run_until(100.0 + STARTUP_DELAY)
        self.assertEqual(self.host.webhooks.remote_calls, [])
        self.assertEqual(self.host.reactor.callbacks, [])

        self.host.print_active = False
        self.host.print_state = PrintState.IDLE
        self.notification.on_print_state_changed(
            PrintState.PRINTING, PrintState.IDLE, "standby")
        self.assertEqual(len(self.host.webhooks.remote_calls), 1)

    def test_non_printing_printer_activity_defers_check_without_polling(self):
        self.host.operation_active = True
        self.notification.start()

        self.host.reactor.run_until(100.0 + STARTUP_DELAY)
        self.assertEqual(self.host.webhooks.remote_calls, [])
        self.assertTrue(self.notification.check_pending)

        self.host.operation_active = False
        self.host.reactor.run_until(
            100.0 + STARTUP_DELAY + 300.0)
        self.assertEqual(len(self.host.webhooks.remote_calls), 1)

    def test_no_update_available_does_not_open_dialog(self):
        self.request_and_respond(available=False, version="1.4.2", changes=[])

        self.assertEqual(self.host.page, ScreenPage.IDLE_HOME)
        self.assertFalse(self.notification.dialog_visible)

    def test_available_update_is_shown_only_from_safe_idle_home(self):
        self.host.page = ScreenPage.MESSAGE
        self.request_and_respond()
        self.assertEqual(self.host.page, ScreenPage.MESSAGE)

        self.host._show_page(ScreenPage.IDLE_HOME)
        self.assertEqual(self.host.page, ScreenPage.UPDATE_NOTIFICATION)
        self.assertTrue(self.notification.dialog_visible)
        self.assertEqual(self.notification.available_version, "1.4.3")

    def test_print_start_before_deferred_dialog_keeps_update_postponed(self):
        self.host.page = ScreenPage.MESSAGE
        self.request_and_respond()

        self.host.print_active = True
        self.host.print_state = PrintState.PRINTING
        self.notification.on_print_state_changed(
            PrintState.IDLE, PrintState.PRINTING, "printing")
        self.host._show_page(ScreenPage.IDLE_HOME)

        self.assertNotEqual(self.host.page, ScreenPage.UPDATE_NOTIFICATION)
        self.assertFalse(self.notification.dialog_visible)

        self.host.print_active = False
        self.host.print_state = PrintState.IDLE
        self.notification.on_print_state_changed(
            PrintState.PRINTING, PrintState.IDLE, "standby")
        self.host._show_page(ScreenPage.IDLE_HOME)
        self.assertEqual(self.host.page, ScreenPage.UPDATE_NOTIFICATION)

    def test_print_start_closes_dialog_without_dismissing_update(self):
        self.request_and_respond()
        self.assertTrue(self.notification.dialog_visible)

        self.host.print_active = True
        self.host.print_state = PrintState.PRINTING
        self.notification.on_print_state_changed(
            PrintState.IDLE, PrintState.PRINTING, "printing")

        self.assertEqual(self.host.page, ScreenPage.PRINTING)
        self.assertFalse(self.notification.dialog_visible)
        self.assertIsNone(self.notification.dismissed_version)

        self.host.print_active = False
        self.host.print_state = PrintState.IDLE
        self.notification.on_print_state_changed(
            PrintState.PRINTING, PrintState.IDLE, "standby")
        self.host._show_page(ScreenPage.IDLE_HOME)
        self.assertEqual(self.host.page, ScreenPage.UPDATE_NOTIFICATION)

    def test_later_suppresses_only_the_displayed_version(self):
        self.request_and_respond()
        self.notification.handle_action("update.later")

        self.assertEqual(self.notification.dismissed_version, "1.4.3")
        self.assertEqual(self.host.page, ScreenPage.IDLE_HOME)
        self.notification.maybe_present()
        self.assertEqual(self.host.page, ScreenPage.IDLE_HOME)

        self.notification.request_check()
        self.notification.handle_status_response(status_response(
            self.notification, version="1.4.4", changes=["New fix"]))
        self.assertEqual(self.host.page, ScreenPage.UPDATE_NOTIFICATION)
        self.assertEqual(self.notification.available_version, "1.4.4")

    def test_new_runtime_clears_in_memory_dismissal(self):
        self.request_and_respond()
        self.notification.handle_action("update.later")

        restarted = Host().update_notification
        self.assertIsNone(restarted.dismissed_version)

    def test_duplicate_checks_and_responses_do_not_duplicate_dialogs(self):
        self.notification.request_check()
        token = self.notification.check_token
        self.notification.request_check()
        self.assertEqual(len(self.host.webhooks.remote_calls), 1)

        response = status_response(self.notification, token=token)
        self.notification.handle_status_response(response)
        draw_count = len(self.host.draw_batches)
        self.notification.handle_status_response(response)

        self.assertEqual(len(self.host.draw_batches), draw_count)
        self.assertTrue(self.notification.dialog_visible)

    def test_timeout_and_malformed_response_leave_printer_ui_usable(self):
        self.notification.request_check()
        self.host.reactor.run_until(100.0 + CHECK_TIMEOUT)
        self.assertFalse(self.notification.check_in_flight)
        self.assertEqual(self.host.page, ScreenPage.IDLE_HOME)

        self.notification.request_check()
        self.notification.handle_status_response(WebRequest(
            token=self.notification.check_token,
            available=True, installed_version=None,
            available_version=None, changes="not-a-list"))
        self.assertFalse(self.notification.dialog_visible)
        self.assertEqual(self.host.page, ScreenPage.IDLE_HOME)

    def test_long_changelog_is_bounded_scrollable_and_explicitly_truncated(self):
        changes = ["Change %02d" % index for index in range(80)]
        self.request_and_respond(changes=changes)

        self.assertIn("update.next", self.host.renderer._buttons)
        self.notification.handle_action("update.next")
        self.assertEqual(self.notification.change_page, 1)
        self.notification.handle_action("update.next")
        self.notification.handle_action("update.next")
        drawing = "\n".join(self.host.draw_batches[-1])

        self.assertIn("MORE CHANGES NOT SHOWN", drawing)
        self.assertNotIn("Change 79", drawing)

    def test_short_changelog_has_no_scroll_controls(self):
        self.request_and_respond(changes=["One", "Two"])

        self.assertNotIn("update.prev", self.host.renderer._buttons)
        self.assertNotIn("update.next", self.host.renderer._buttons)

    def test_missing_changelog_falls_back_to_available_version_message(self):
        self.request_and_respond(changes=[])
        drawing = "\n".join(self.host.draw_batches[-1])

        self.assertIn("CHANGELOG UNAVAILABLE", drawing)
        self.assertEqual(self.host.page, ScreenPage.UPDATE_NOTIFICATION)

    def test_update_rechecks_idle_before_delegating_to_moonraker(self):
        self.request_and_respond()
        self.host.print_active = True
        self.host.print_state = PrintState.PRINTING

        self.notification.handle_action("update.install")

        methods = [method for method, _params in self.host.webhooks.remote_calls]
        self.assertNotIn("feather_start_forge_x_update", methods)
        self.assertEqual(self.host.page, ScreenPage.PRINTING)
        self.assertIsNone(self.notification.dismissed_version)

    def test_update_uses_existing_moonraker_ota_path(self):
        self.request_and_respond()

        self.notification.handle_action("update.install")

        method, params = self.host.webhooks.remote_calls[-1]
        self.assertEqual(method, "feather_start_forge_x_update")
        self.assertEqual(params["expected_version"], "1.4.3")
        self.assertEqual(self.host.page, ScreenPage.MESSAGE)


if __name__ == "__main__":
    unittest.main()
