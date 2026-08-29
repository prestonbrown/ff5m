## Tests for the klipper-facing half of the AD5X IFS support: the [ifs] object
## and the two filament sensors that read through it.
##
## Copyright (C) 2026, Preston Brown
##
## This file may be distributed under the terms of the GNU GPLv3 license

import unittest

import ifs_modules
import ifs_klipper_fakes as fakes

fakes.install_filament_switch_sensor()

IFS = ifs_modules.load("ifs")
STATUS = ifs_modules.load("ifs_status")
LOGIC = ifs_modules.load("ifs_sensor_logic")
TOOLHEAD = ifs_modules.load("ifs_toolhead_sensor")
CHANNEL = ifs_modules.load("ifs_channel_sensor")


class FakeCapabilities:
    def __init__(self, channel_count=4, version="3.0.6", probed=True):
        self.channel_count = channel_count
        self.version = version
        self.probed = probed


class FakeLink:
    """A board that answers F13 with scripted lines.

    Deliberately a *link*, not an operations layer: the real IfsOperations and
    the real F13 parser run on top of it, so these tests exercise the whole
    stack below klipper rather than a stand-in for it.
    """

    def __init__(self, capabilities=None, replies=None, fail=None):
        self.capabilities = capabilities or FakeCapabilities()
        self.replies = list(replies or [])
        self.fail = fail
        self.closed = False
        self.asked = []

    def request(self, command):
        self.asked.append(command)
        if self.fail:
            raise RuntimeError(self.fail)
        if not self.replies:
            raise RuntimeError("no more scripted replies")
        return FakeResponse(self.replies.pop(0))

    def close(self):
        self.closed = True


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload
        self.extra = []
        self.is_error = False


def f13(state=STATUS.READY, silk=0b1011, chan=0, insert=0, stall=0):
    """An F13 payload as the board formats it."""
    return ("FFS_state: %d silk_state: %d chan: %d ffs_channels_insert: %d "
            "stall_state: %d" % (state, silk, chan, insert, stall))


def status(state=STATUS.READY, silk=0b1011, insert=0, stall=0, chan=0):
    """A parsed snapshot, for the sensors that read one rather than the wire."""
    return STATUS.IfsStatus(state=state, silk_mask=silk, active_channel=chan,
                            insert_mask=insert, stall_mask=stall)


def make_ifs(values=None, link=None, replies=None):
    printer = fakes.FakePrinter()
    config = fakes.FakeConfig("ifs", values, printer)
    if link is None:
        link = FakeLink(replies=replies)
    obj = IFS.IFS(config, open_link=lambda port, commit: link)
    printer.add_object("ifs", obj)
    return obj, printer, link


class TestIfsObject(unittest.TestCase):
    def test_before_any_poll_nothing_is_claimed(self):
        obj, _, _ = make_ifs()
        info = obj.get_status()
        self.assertFalse(info["connected"])
        self.assertIsNone(info["activity"])
        self.assertEqual(info["loaded_channels"], [])
        ## None, not False - "no reading yet" must not look like a runout.
        self.assertIsNone(obj.has_filament(1))
        self.assertIsNone(obj.latest_status())

    def test_a_poll_publishes_the_snapshot(self):
        obj, _, link = make_ifs(replies=[f13(silk=0b1011)])
        self.assertTrue(obj._connect())
        self.assertTrue(obj._poll_once())
        info = obj.get_status()
        self.assertTrue(info["connected"])
        self.assertEqual(info["loaded_channels"], [1, 2, 4])
        self.assertEqual(info["activity"], "ready")
        self.assertTrue(obj.has_filament(2))
        self.assertFalse(obj.has_filament(3))

    def test_status_keys_are_the_same_shape_either_way(self):
        ## Moonraker consumers should not have to test for missing keys.
        obj, _, link = make_ifs(replies=[f13()])
        empty = set(obj.get_status())
        obj._connect(); obj._poll_once()
        self.assertEqual(empty, set(obj.get_status()))

    def test_probed_capabilities_win_over_config(self):
        obj, _, _ = make_ifs({"channel_count": 8},
                             link=FakeLink(FakeCapabilities(channel_count=4)))
        obj._connect()
        self.assertEqual(obj.channel_count, 4)

    def test_config_is_the_fallback_when_f19_went_unanswered(self):
        link = FakeLink(FakeCapabilities(channel_count=4, version=None,
                                         probed=False))
        obj, _, _ = make_ifs({"channel_count": 2}, link=link)
        obj._connect()
        self.assertEqual(obj.channel_count, 2)

    def test_a_failed_open_is_recorded_not_raised(self):
        def explode(port, commit):
            raise OSError("no such device")
        printer = fakes.FakePrinter()
        obj = IFS.IFS(fakes.FakeConfig("ifs", {}, printer), open_link=explode)
        self.assertFalse(obj._connect())
        info = obj.get_status()
        self.assertFalse(info["connected"])
        self.assertIn("no such device", info["error"])

    def test_a_failed_poll_is_recorded_not_raised(self):
        obj, _, link = make_ifs()
        obj._connect()
        link.fail = "board silent"
        self.assertFalse(obj._poll_once())
        self.assertIn("board silent", obj.get_status()["error"])

    def test_repeated_failures_drop_the_link(self):
        ## One bad read is noise; a run of them means the port is gone.
        obj, _, link = make_ifs()
        obj._connect()
        link.fail = "gone"
        for _ in range(IFS.MAX_POLL_FAILURES):
            obj._poll_once()
            obj._failures += 1
            if obj._failures >= IFS.MAX_POLL_FAILURES:
                obj._drop_link("test")
        self.assertTrue(link.closed)
        self.assertIsNone(obj._link)

    def test_an_insert_is_marshalled_to_the_reactor(self):
        ## Serial work happens off-thread; anything touching klipper has to
        ## come back through the reactor.
        obj, printer, link = make_ifs(replies=[f13(insert=0b10)])
        obj._connect()
        obj._poll_once()
        self.assertEqual(len(printer.reactor.async_callbacks), 1)
        self.assertEqual(printer.sent, [])
        printer.reactor.run_async()
        self.assertEqual(printer.sent, [("ifs:filament_inserted", ([2],))])

    def test_an_insert_fires_once(self):
        obj, printer, link = make_ifs(
            replies=[f13(insert=0b10), f13(insert=0b10)])
        obj._connect()
        obj._poll_once(); obj._poll_once()
        printer.reactor.run_async()
        self.assertEqual(len(printer.sent), 1)

    def test_dropping_a_link_twice_is_harmless(self):
        obj, _, _ = make_ifs()
        obj._connect()
        obj._drop_link()
        obj._drop_link()


class AliveThread:
    """Stands in for the poll thread, which these tests drive by hand."""

    def is_alive(self):
        return True

    def join(self, timeout=None):
        return None


class TestCommandQueue(unittest.TestCase):
    """Commands from klipper must not block the reactor.

    IfsLink is not thread-safe and an exchange takes ~165 ms on hardware, so a
    mutex would stall klipper repeatedly during exactly the sequences that
    matter. Requests are queued for the poll thread and the caller yields.
    """

    def setUp(self):
        self.obj, self.printer, self.link = make_ifs(replies=[f13()])
        self.obj._connect()
        self.obj._thread = AliveThread()
        self.reactor = self.printer.reactor
        ## Stand in for the poll thread running between reactor pauses. The real
        ## one services queued commands AND keeps polling F13, and anything that
        ## waits for the board to settle needs those polls to arrive.
        def tick():
            if not self.obj._run_queued():
                self.obj._poll_once()
        self.reactor.on_pause = tick

    def test_a_command_is_answered(self):
        self.link.replies = ["chan 1."]
        response = self.obj.execute("F24 C1")
        self.assertEqual(response.payload, "chan 1.")
        self.assertIn("F24 C1", self.link.asked)

    def test_it_yields_the_reactor_rather_than_blocking(self):
        ## The whole point. If this ever stops pausing, klipper stalls for the
        ## length of every IFS exchange during a load.
        self.link.replies = ["chan 1."]
        self.obj.execute("F24 C1")
        self.assertGreater(self.reactor.pauses, 0)

    def test_a_board_error_surfaces(self):
        self.link.fail = "FFS not ready."
        with self.assertRaises(RuntimeError) as caught:
            self.obj.execute("F24 C1")
        self.assertIn("FFS not ready.", str(caught.exception))

    def test_a_silent_board_times_out_and_the_request_is_dropped(self):
        ## Nothing runs the queue, so the wait must end by itself and leave
        ## nothing behind for a later poll to answer into the void.
        self.reactor.on_pause = None
        with self.assertRaises(IFS.IfsBusy):
            self.obj.execute("F24 C1", timeout=0.2)
        self.assertEqual(self.obj._queue, [])

    def test_no_poller_means_no_command(self):
        self.obj._thread = None
        with self.assertRaises(IFS.IfsBusy):
            self.obj.execute("F13")

    def test_clamp_completes_on_the_acknowledgement(self):
        """The regression that cost a real load attempt.

        F24 answers "F24 ok. chan N." and the board then sits in READY - it
        never reports CLAMPED for the channel. Waiting on that state transition
        timed out after 15s on hardware while the clamp had already happened,
        and IFS_LOAD died at its first IFS command. zmod's cmd_IFS_F24 waits on
        the same acknowledgement this now waits on.
        """
        self.link.replies = ["chan 1.", f13()]
        gcmd = fakes.FakeGcmd({"CHANNEL": 1})
        self.obj.cmd_IFS_CLAMP(gcmd)
        self.assertIn("F24 C1", self.link.asked)
        self.assertTrue(any("clamped channel 1" in r for r in gcmd.responses),
                        gcmd.responses)

    def test_clamp_never_waits_on_a_state_transition(self):
        ## The board stays in READY throughout; a poll that keeps saying so must
        ## not turn into a timeout. Only the acknowledgement may end the wait.
        self.link.replies = ["chan 1."] + [f13(state=STATUS.READY)] * 40
        gcmd = fakes.FakeGcmd({"CHANNEL": 1})
        self.obj.cmd_IFS_CLAMP(gcmd)  # must not raise
        self.assertEqual(self.link.asked.count("F24 C1"), 1)

    def test_clamp_waits_for_the_board_to_settle_before_returning(self):
        """zmod's cmd_IFS_F24 follows the ack with wait_for_state().

        The ack only says the opcode was accepted. A feed sent while the board
        was still clamping came back "F10 ... refused: FFS not ready." - the
        clamp has to wait for F13 to report READY before the next command.
        """
        self.link.replies = (["chan 1."]
                             + [f13(state=STATUS.CLAMPED)] * 3
                             + [f13(state=STATUS.READY)])
        gcmd = fakes.FakeGcmd({"CHANNEL": 1})
        self.obj.cmd_IFS_CLAMP(gcmd)
        ## It must have kept polling F13 until ready, not returned on the ack.
        self.assertGreater(self.link.asked.count("F13"), 1, self.link.asked)

    def test_a_refused_clamp_is_not_reported_as_success(self):
        ## The board prefixes refusals with "ok." exactly as it does successes,
        ## so the payload is the only thing that distinguishes them. Sending the
        ## raw command without checking it reported every clamp as fine.
        self.link.replies = ["FFS not ready."]
        gcmd = fakes.FakeGcmd({"CHANNEL": 1})
        with self.assertRaises(Exception):
            self.obj.cmd_IFS_CLAMP(gcmd)
        self.assertFalse(any("clamped" in r for r in gcmd.responses),
                         gcmd.responses)

    def test_a_refused_release_is_not_reported_as_success(self):
        self.link.replies = ["FFS not ready."]
        gcmd = fakes.FakeGcmd({"CHANNEL": 1})
        with self.assertRaises(Exception):
            self.obj.cmd_IFS_RELEASE(gcmd)
        self.assertFalse(any("released" in r for r in gcmd.responses),
                         gcmd.responses)

    def test_release_accepts_the_payload_the_board_actually_sends(self):
        self.link.replies = ["FFS channel 1 release.", f13()]
        gcmd = fakes.FakeGcmd({"CHANNEL": 1})
        self.obj.cmd_IFS_RELEASE(gcmd)
        self.assertIn("F39 C1", self.link.asked)

    def test_a_refused_feed_fails_fast_instead_of_waiting_it_out(self):
        """The second hardware failure, and the same root cause as the clamp.

        F10 answers "F10 ok. FFS channel N feeding." on success and
        "F10 ok. FFS not ready." on refusal - both prefixed "ok.". Writing the
        opcode straight to the link accepted either, then waited 120s for a
        LOADING state the board was never going to report, so a refusal read as
        a timeout with no clue in it.
        """
        self.link.replies = ["FFS not ready."]
        gcmd = fakes.FakeGcmd({"CHANNEL": 1, "UNTIL": "done"})
        with self.assertRaises(gcmd.error) as caught:
            self.obj.cmd_IFS_FEED(gcmd)
        self.assertIn("not ready", str(caught.exception).lower())

    def test_a_refusal_is_a_command_error_not_an_internal_one(self):
        """A refused opcode must fail the command, never the printer.

        Klipper turns any non-gcode exception into "Internal error on command",
        which puts klippy into SHUTDOWN and takes the MCUs with it. A refused
        F10 did exactly that on the printer and needed a FIRMWARE_RESTART; a
        board declining an opcode is an ordinary answer, not a fault.
        """
        for command, params in (
                (self.obj.cmd_IFS_FEED, {"CHANNEL": 1, "UNTIL": "done"}),
                (self.obj.cmd_IFS_CLAMP, {"CHANNEL": 1}),
                (self.obj.cmd_IFS_RELEASE, {"CHANNEL": 1}),
                (self.obj.cmd_IFS_RELEASE_ALL, {}),
                (self.obj.cmd_IFS_STOP, {}),
                (self.obj.cmd_IFS_RESET_DRIVER, {})):
            self.link.replies = ["FFS not ready."]
            gcmd = fakes.FakeGcmd(params)
            with self.assertRaises(gcmd.error):
                command(gcmd)

    def test_an_accepted_feed_sends_the_length_and_speed_asked_for(self):
        self.link.replies = ["FFS channel 1 feeding."] + [f13(state=STATUS.READY)] * 4
        gcmd = fakes.FakeGcmd({"CHANNEL": 1, "UNTIL": "done",
                               "LENGTH": 600, "SPEED": 1200, "TIMEOUT": 0.3})
        try:
            self.obj.cmd_IFS_FEED(gcmd)
        except Exception:
            pass  # the state wait may still time out; the send is what matters
        self.assertIn("F10 C1 L600 S1200", self.link.asked)

    def test_the_poller_speeds_up_while_a_move_is_watched(self):
        """zmod polls F13 every 0.2s inside wait_for_state; idle cadence is 1s.

        The motion bit toggles, so three 1s samples can all land in gaps while
        the motor is running and read as a stall that never happened. A feed
        that was moving was failed as "stalled" on exactly that.
        """
        self.assertEqual(self.obj._poll_delay(), self.obj.poll_interval)
        self.obj._watch_moves(1)
        try:
            self.assertEqual(self.obj._poll_delay(), IFS.MOVE_POLL_INTERVAL)
            self.assertLess(IFS.MOVE_POLL_INTERVAL, self.obj.poll_interval)
        finally:
            self.obj._watch_moves(-1)
        self.assertEqual(self.obj._poll_delay(), self.obj.poll_interval)

    def test_the_watch_count_is_released_even_when_the_move_fails(self):
        ## A leaked watcher would pin the board at the fast cadence forever.
        self.link.replies = ["FFS not ready."]
        gcmd = fakes.FakeGcmd({"CHANNEL": 1, "UNTIL": "done"})
        with self.assertRaises(gcmd.error):
            self.obj.cmd_IFS_FEED(gcmd)
        self.assertEqual(self.obj._poll_delay(), self.obj.poll_interval)

    def test_sleep_fires_the_opcode_without_watching_state(self):
        """zmod's SLEEP=1, used where the extruder drives the same filament.

        There the lane's motion bit is not a stall signal - something else is
        pulling - and at the extruder's 300 mm/min it reads as stopped within
        seconds. Watching it failed a co-push that was physically working.
        """
        ## Only LOADING is ever reported, never READY. A state-watching feed
        ## could not finish against this; SLEEP does not look, so it returns.
        self.link.replies = (["FFS channel 1 feeding."]
                             + [f13(state=STATUS.LOADING)] * 60)
        gcmd = fakes.FakeGcmd({"CHANNEL": 1, "UNTIL": "done", "SLEEP": 1,
                               "LENGTH": 100, "SPEED": 300})
        self.obj.cmd_IFS_FEED(gcmd)          # must not raise
        self.assertIn("F10 C1 L100 S300", self.link.asked)

    def test_without_sleep_that_same_board_fails_the_move(self):
        ## The contrast that makes the test above mean something.
        self.link.replies = (["FFS channel 1 feeding."]
                             + [f13(state=STATUS.LOADING)] * 60)
        gcmd = fakes.FakeGcmd({"CHANNEL": 1, "UNTIL": "done",
                               "LENGTH": 100, "SPEED": 300, "TIMEOUT": 0.3})
        with self.assertRaises(gcmd.error):
            self.obj.cmd_IFS_FEED(gcmd)

    def test_sleep_waits_in_proportion_to_the_move(self):
        ## zmod: (leng * 20) // speed + 1. A move must not return instantly.
        waited = []
        self.reactor.on_pause = lambda: waited.append(self.reactor.monotonic())
        self.link.replies = ["FFS channel 1 feeding."]
        before = self.reactor.monotonic()
        gcmd = fakes.FakeGcmd({"CHANNEL": 1, "UNTIL": "done", "SLEEP": 1,
                               "LENGTH": 100, "SPEED": 300})
        ## _run needs the queue serviced, so service it then let the sleep run.
        self.reactor.on_pause = self.obj._run_queued
        self.obj.cmd_IFS_FEED(gcmd)
        self.assertGreaterEqual(self.reactor.monotonic() - before,
                                (100 * 20) // 300 + 1)

    def test_a_retract_is_not_watched_for_stalls(self):
        """zmod's unload is a plain IFS_F11 with no CHECK.

        Only its LOAD feed passes CHECK=1. Motion stopping is how a retract
        ENDS - the filament is home - so judging it as a stall failed a retract
        that had worked, with the toolhead sensor clear to prove it.
        """
        self.link.replies = (["FFS channel 4 exiting."]
                             + [f13(state=STATUS.UNLOADING, chan=4,
                                    stall=0)] * 3
                             + [f13(state=STATUS.READY)])
        gcmd = fakes.FakeGcmd({"CHANNEL": 4, "UNTIL": "done",
                               "LENGTH": 1000, "SPEED": 1200})
        self.obj.cmd_IFS_RETRACT(gcmd)          # must not raise

    def test_check_1_does_watch_for_stalls(self):
        ## The contrast: the load feed asks for CHECK=1 and still gets the
        ## silk/stall judgement zmod applies there.
        self.link.replies = (["FFS channel 1 feeding."]
                             + [f13(state=STATUS.LOADING, chan=1, stall=0)] * 8)
        gcmd = fakes.FakeGcmd({"CHANNEL": 1, "UNTIL": "done", "CHECK": 1,
                               "LENGTH": 1000, "SPEED": 1200, "TIMEOUT": 1.0})
        with self.assertRaises(gcmd.error) as caught:
            self.obj.cmd_IFS_FEED(gcmd)
        self.assertIn("stall", str(caught.exception).lower())

    def test_a_driver_fault_resets_the_driver_and_re_issues_the_move(self):
        """zmod's recovery, which we did not have.

        Its wait_for_state sends F15 the moment F13 reads DRV_ERROR and returns
        RET_RETRY; cmd_IFS_F10 then re-sends the same opcode, up to retry_count
        times. Ours failed the command outright, so a driver that dropped out
        ended the run - which is exactly how one evening on the AD5X ended.
        """
        self.link.replies = (["FFS channel 1 feeding."]
                             + [f13(state=STATUS.DRIVER_ERROR)]
                             + [""]                       # F15 C
                             + ["FFS channel 1 feeding."]  # the re-issue
                             + [f13(state=STATUS.READY)] * 4)
        gcmd = fakes.FakeGcmd({"CHANNEL": 1, "UNTIL": "done", "CHECK": 1,
                               "LENGTH": 600, "SPEED": 1200, "TIMEOUT": 2.0})
        self.obj.cmd_IFS_FEED(gcmd)             # must not raise
        self.assertIn("F15 C", self.link.asked)
        self.assertEqual(self.link.asked.count("F10 C1 L600 S1200"), 2)

    def test_the_driver_retry_gives_up_after_retry_count(self):
        ## Three attempts, and a reset after each so the board is not left
        ## faulted even on the one we give up on.
        self.link.replies = ([]
                             + (["FFS channel 1 feeding."]
                                + [f13(state=STATUS.DRIVER_ERROR)]
                                + [""]) * 3
                             + [f13(state=STATUS.READY)] * 4)
        gcmd = fakes.FakeGcmd({"CHANNEL": 1, "UNTIL": "done", "CHECK": 1,
                               "LENGTH": 600, "SPEED": 1200, "TIMEOUT": 2.0})
        with self.assertRaises(gcmd.error) as caught:
            self.obj.cmd_IFS_FEED(gcmd)
        self.assertIn("driver", str(caught.exception).lower())
        self.assertEqual(self.link.asked.count("F10 C1 L600 S1200"), 3)
        self.assertEqual(self.link.asked.count("F15 C"), 3)

    def test_a_stall_is_not_retried(self):
        ## zmod retries RET_RETRY only. RET_STALL breaks out of the loop: the
        ## filament is jammed, and driving into it again grinds a flat on it.
        self.link.replies = (["FFS channel 1 feeding."]
                             + [f13(state=STATUS.LOADING, chan=1, stall=0)] * 8)
        gcmd = fakes.FakeGcmd({"CHANNEL": 1, "UNTIL": "done", "CHECK": 1,
                               "LENGTH": 600, "SPEED": 1200, "TIMEOUT": 1.0})
        with self.assertRaises(gcmd.error):
            self.obj.cmd_IFS_FEED(gcmd)
        self.assertNotIn("F15 C", self.link.asked)
        self.assertEqual(self.link.asked.count("F10 C1 L600 S1200"), 1)

    def test_an_empty_lane_reads_as_a_runout_not_a_jam(self):
        """zmod passes silk alongside stall on every CHECK=1 move.

        A spool that ran out also stops moving, so without the silk check the
        board's answer is "stalled" and the operator goes looking for a jam
        that is not there.
        """
        self.link.replies = (["FFS channel 1 feeding."]
                             + [f13(state=STATUS.LOADING, chan=1, silk=0b1010,
                                    stall=0b1)] * 8)
        gcmd = fakes.FakeGcmd({"CHANNEL": 1, "UNTIL": "done", "CHECK": 1,
                               "LENGTH": 600, "SPEED": 1200, "TIMEOUT": 1.0})
        with self.assertRaises(gcmd.error) as caught:
            self.obj.cmd_IFS_FEED(gcmd)
        self.assertIn("no filament", str(caught.exception).lower())

    def test_an_unchecked_move_ignores_an_empty_lane(self):
        ## The contrast, and zmod's shape: no CHECK, no judging. Its unload is
        ## a plain F11 that waits for READY and nothing else.
        self.link.replies = (["FFS channel 4 exiting."]
                             + [f13(state=STATUS.UNLOADING, chan=4, silk=0)] * 2
                             + [f13(state=STATUS.READY, silk=0)])
        gcmd = fakes.FakeGcmd({"CHANNEL": 4, "UNTIL": "done",
                               "LENGTH": 600, "SPEED": 1200})
        self.obj.cmd_IFS_RETRACT(gcmd)          # must not raise

    def test_a_timeout_releases_every_channel(self):
        """zmod's timeout path is F112 then F18.

        A timeout means we no longer know what the board is doing, and a lane
        left clamped holds its filament until a human notices - two of them sat
        gripped for hours after one failed run.
        """
        self.link.replies = (["FFS channel 1 feeding."]
                             + [f13(state=STATUS.CLAMPED, chan=1)] * 12
                             + ["", ""])
        gcmd = fakes.FakeGcmd({"CHANNEL": 1, "UNTIL": "done", "CHECK": 1,
                               "LENGTH": 600, "SPEED": 1200, "TIMEOUT": 0.3})
        with self.assertRaises(gcmd.error):
            self.obj.cmd_IFS_FEED(gcmd)
        self.assertIn("F112", self.link.asked)
        self.assertIn("F18", self.link.asked)

    def test_a_stall_stops_the_board_but_leaves_the_clamp_alone(self):
        ## zmod releases everything only on a timeout. A stall is a known state
        ## with a known channel, and the operator is about to clear it - taking
        ## the clamp off would drop the filament back down the tube.
        self.link.replies = (["FFS channel 1 feeding."]
                             + [f13(state=STATUS.LOADING, chan=1, stall=0)] * 8
                             + [""])
        gcmd = fakes.FakeGcmd({"CHANNEL": 1, "UNTIL": "done", "CHECK": 1,
                               "LENGTH": 600, "SPEED": 1200, "TIMEOUT": 1.0})
        with self.assertRaises(gcmd.error):
            self.obj.cmd_IFS_FEED(gcmd)
        self.assertIn("F112", self.link.asked)
        self.assertNotIn("F18", self.link.asked)

    def test_shutdown_releases_a_waiting_caller(self):
        ## A caller blocked on a command when klippy goes down must not hang.
        request = IFS._Request("F13")
        self.obj._queue.append(request)
        self.obj._stop()
        self.assertTrue(request.done.is_set())
        self.assertIn("shutting down", request.error)

    def test_a_disconnected_link_fails_queued_commands(self):
        obj, printer, _ = make_ifs()
        obj._open_link = lambda port, commit: (_ for _ in ()).throw(
            OSError("gone"))
        request = IFS._Request("F13")
        obj._queue.append(request)
        ## One pass of the loop body with no link: connect fails, queue drains.
        self.assertFalse(obj._connect())
        obj._fail_queued("the IFS is not connected")
        self.assertTrue(request.done.is_set())
        self.assertIn("not connected", request.error)

    def test_queued_commands_run_before_status_polls(self):
        ## A sequence waiting on a command should not pay for a status poll.
        self.link.replies = ["chan 1.", f13()]
        self.assertTrue(self.obj._run_queued() is False)
        request = IFS._Request("F24 C1")
        self.obj._queue.append(request)
        self.assertTrue(self.obj._run_queued())
        self.assertEqual(self.link.asked[-1], "F24 C1")


class TestGcode(unittest.TestCase):
    def test_status_command_when_disconnected(self):
        obj, printer, _ = make_ifs()
        gcode = printer.lookup_object("gcode")
        gcode.commands["IFS_STATUS"](gcode)
        self.assertIn("not connected", gcode.responses[-1])

    def test_status_command_reports_the_board(self):
        obj, printer, link = make_ifs(replies=[f13(silk=0b1011, chan=2)])
        obj._connect(); obj._poll_once()
        gcode = printer.lookup_object("gcode")
        gcode.commands["IFS_STATUS"](gcode)
        message = gcode.responses[-1]
        self.assertIn("3.0.6", message)
        self.assertIn("ready", message)
        self.assertIn("[1, 2, 4]", message)

    def test_both_commands_are_registered(self):
        _, printer, _ = make_ifs()
        gcode = printer.lookup_object("gcode")
        self.assertIn("IFS_STATUS", gcode.commands)
        self.assertIn("IFS_DIAGNOSTICS", gcode.commands)


class FakeAdcChannel:
    def __init__(self, value):
        self.value = value

    def get_last_value(self):
        ## klipper returns (value, timestamp) in that order. zmod unpacks it
        ## the other way and compensates; this pins the correct order.
        return self.value, 1234.5


class FakeQueryAdc:
    def __init__(self, values):
        self.adc = {name: FakeAdcChannel(v) for name, v in values.items()}


def make_toolhead(values=None, adc=None):
    printer = fakes.FakePrinter()
    if adc is not None:
        printer.add_object("query_adc", FakeQueryAdc(adc))
    config = fakes.FakeConfig("ifs_toolhead_sensor toolhead", values, printer)
    return TOOLHEAD.IfsToolheadSensor(config), printer


class TestToolheadSensor(unittest.TestCase):
    ADC = TOOLHEAD.DEFAULT_ADC

    def test_engaged_reads_present(self):
        sensor, _ = make_toolhead(adc={self.ADC: 0.0081})
        self.assertTrue(sensor.read_present())
        self.assertEqual(sensor.last_state, LOGIC.PRESENT)

    def test_empty_reads_absent(self):
        sensor, _ = make_toolhead(adc={self.ADC: 0.0432})
        self.assertFalse(sensor.read_present())
        self.assertEqual(sensor.last_state, LOGIC.ABSENT)

    def test_no_query_adc_is_no_reading_not_a_runout(self):
        sensor, _ = make_toolhead()
        self.assertIsNone(sensor.read_present())

    def test_a_missing_adc_name_is_no_reading(self):
        sensor, _ = make_toolhead(adc={"something else": 0.5})
        self.assertIsNone(sensor.read_present())

    def test_thresholds_are_configurable(self):
        ## The bands are one printer's hardware, not a universal constant.
        sensor, _ = make_toolhead({"present_max": 0.5, "absent_min": 0.6},
                                  adc={self.ADC: 0.4})
        self.assertTrue(sensor.read_present())

    def test_it_registers_as_a_stock_filament_switch_sensor(self):
        ## This is the surface Moonraker and HelixScreen already watch.
        sensor, printer = make_toolhead(adc={self.ADC: 0.008})
        self.assertIs(printer.lookup_object("filament_switch_sensor toolhead"),
                      sensor)

    def test_the_timer_notes_the_reading(self):
        sensor, printer = make_toolhead(adc={self.ADC: 0.008})
        printer.fire("klippy:ready")
        self.assertEqual(len(printer.reactor.timers), 1)
        printer.reactor.timers[0](100.0)
        self.assertEqual(sensor.runout_helper.notes, [True])

    def test_a_reading_of_none_leaves_the_helper_alone(self):
        ## Absence of a reading must not be reported as absence of filament.
        sensor, printer = make_toolhead()
        printer.fire("klippy:ready")
        printer.reactor.timers[0](100.0)
        self.assertEqual(sensor.runout_helper.notes, [])


def make_channel(channel=1, ifs_obj=None):
    printer = fakes.FakePrinter()
    if ifs_obj is not None:
        printer.add_object("ifs", ifs_obj)
    config = fakes.FakeConfig("ifs_channel_sensor lane%d" % channel,
                              {"channel": channel}, printer)
    return CHANNEL.IfsChannelSensor(config), printer


class StubIfs:
    def __init__(self, status_value):
        self._status = status_value

    def latest_status(self):
        return self._status


class TestChannelSensor(unittest.TestCase):
    def test_reads_the_silk_bit_for_its_channel(self):
        loaded = StubIfs(status(silk=0b1011))
        self.assertTrue(make_channel(2, loaded)[0].read_present())
        self.assertFalse(make_channel(3, loaded)[0].read_present())

    def test_no_ifs_object_is_no_reading(self):
        ## Config section order decides construction order, so [ifs] may not
        ## exist yet. That is not a runout.
        sensor, _ = make_channel(1)
        self.assertIsNone(sensor.read_present())

    def test_no_status_yet_is_no_reading(self):
        sensor, _ = make_channel(1, StubIfs(None))
        self.assertIsNone(sensor.read_present())

    def test_it_registers_as_a_stock_filament_switch_sensor(self):
        sensor, printer = make_channel(1, StubIfs(status()))
        self.assertIs(printer.lookup_object("filament_switch_sensor lane1"),
                      sensor)

    def test_four_channels_share_one_poll(self):
        ## The whole reason these read a snapshot instead of the wire.
        shared = StubIfs(status(silk=0b1011))
        results = [make_channel(ch, shared)[0].read_present()
                   for ch in (1, 2, 3, 4)]
        self.assertEqual(results, [True, True, False, True])


if __name__ == "__main__":
    unittest.main()
