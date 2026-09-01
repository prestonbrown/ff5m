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
        obj, printer, link = make_ifs(replies=[f13(), f13(insert=0b10)])
        obj._connect()
        obj._poll_once()                    # primes; nothing was inserted yet
        obj._poll_once()
        self.assertEqual(len(printer.reactor.async_callbacks), 1)
        self.assertEqual(printer.sent, [])
        printer.reactor.run_async()
        self.assertEqual(printer.sent, [("ifs:filament_inserted", ([2],))])

    def test_an_insert_fires_once(self):
        obj, printer, link = make_ifs(
            replies=[f13(), f13(insert=0b10), f13(insert=0b10)])
        obj._connect()
        obj._poll_once(); obj._poll_once(); obj._poll_once()
        printer.reactor.run_async()
        self.assertEqual(len(printer.sent), 1)

    def test_an_insert_threads_the_lane(self):
        """zmod runs _IFS_AUTOINSERT the moment the board reports an insert.

        It is the step that puts a lane in a KNOWN position. Without it every
        lane sits wherever a human left it and a later load has to guess how
        far the toolhead is, which is how a load ends up feeding into nothing.
        """
        obj, printer, link = make_ifs(replies=[f13(), f13(insert=0b10)])
        obj._connect()
        obj._poll_once(); obj._poll_once()
        printer.reactor.run_async()
        gcode = printer.lookup_object("gcode")
        self.assertEqual(gcode.scripts, ["IFS_AUTOINSERT CHANNEL=2"])

    def test_every_inserted_lane_is_threaded(self):
        ## Two lanes can gain filament between polls. zmod collapses the mask
        ## to its highest bit and threads only that one.
        obj, printer, link = make_ifs(replies=[f13(), f13(insert=0b1001)])
        obj._connect()
        obj._poll_once(); obj._poll_once()
        printer.reactor.run_async()
        self.assertEqual(printer.lookup_object("gcode").scripts,
                         ["IFS_AUTOINSERT CHANNEL=1",
                          "IFS_AUTOINSERT CHANNEL=4"])

    def test_autoinsert_can_be_turned_off(self):
        ## The event still fires - something else may want to know - but the
        ## printer does not move filament on its own.
        obj, printer, link = make_ifs(values={"autoinsert": False},
                                      replies=[f13(), f13(insert=0b10)])
        obj._connect()
        obj._poll_once(); obj._poll_once()
        printer.reactor.run_async()
        self.assertEqual(printer.lookup_object("gcode").scripts, [])
        self.assertEqual(printer.sent, [("ifs:filament_inserted", ([2],))])

    def test_a_lane_that_will_not_thread_does_not_take_klippy_down(self):
        """This runs on the reactor, not inside anybody's command.

        An exception escaping here is not a failed command, it is an unhandled
        error in klippy's event loop. zmod's _safe_run_script swallows the same
        thing for the same reason.
        """
        obj, printer, link = make_ifs(replies=[f13(), f13(insert=0b10)])
        obj._connect()
        printer.lookup_object("gcode").script_error = "lane 2 is empty"
        obj._poll_once(); obj._poll_once()
        printer.reactor.run_async()          # must not raise
        self.assertEqual(printer.sent, [("ifs:filament_inserted", ([2],))])

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

    def test_a_stalled_feed_raises_by_default(self):
        """The autoinsert thread still needs to fail loudly.

        Nothing follows a thread that could rescue a lane which never arrived,
        so there the stall IS the answer.
        """
        loading = STATUS.state_value(STATUS.LOADING, 1)
        self.link.replies = (["FFS channel 1 feeding."]
                             + [f13(state=loading, stall=0)] * 8)
        gcmd = fakes.FakeGcmd({"CHANNEL": 1, "UNTIL": "done", "CHECK": 1,
                               "TIMEOUT": 2.0})
        with self.assertRaises(gcmd.error) as caught:
            self.obj.cmd_IFS_FEED(gcmd)
        self.assertIn("stalled", str(caught.exception).lower())

    def test_SOFT_hands_a_stalled_feed_to_the_extruder_instead_of_raising(self):
        """zmod's only behaviour, and the load depends on it.

        A load feed ENDS by arriving at the extruder gear, and the IFS cannot
        push filament past a gear that is not turning. So the stall is how this
        feed finishes, and the co-push purge after it is what completes the
        load. zmod's cmd_IFS_F10 calls print_result(), which only prints, then
        carries on to _SBROS_TRASH_DAVIM. Raising there aborted the load before
        the one step that could have finished it.
        """
        loading = STATUS.state_value(STATUS.LOADING, 1)
        self.link.replies = (["FFS channel 1 feeding."]
                             + [f13(state=loading, stall=0)] * 8
                             + ["FFS stopped."] * 4)
        gcmd = fakes.FakeGcmd({"CHANNEL": 1, "UNTIL": "done", "CHECK": 1,
                               "SOFT": 1, "TIMEOUT": 2.0})
        self.obj.cmd_IFS_FEED(gcmd)  # must not raise
        ## Stopped, exactly as zmod does after a failed wait...
        self.assertTrue(any("F112" in a for a in self.link.asked),
                        self.link.asked)
        ## ...but NOT released: the purge that follows drives this same lane
        ## and needs the clamp the caller took.
        self.assertFalse(any("F39" in a for a in self.link.asked),
                         self.link.asked)

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

    def test_a_feed_that_never_reaches_the_toolhead_is_reported_not_failed(self):
        """zmod carries on here, and so must we.

        Its checked feed returns RET_OK when the board simply completes the
        move, and the macro goes straight to the co-push, where the EXTRUDER
        gear pulls the filament the last stretch in. Failing instead stranded a
        load whose filament was already at the toolhead entry waiting for
        exactly that step. It is still worth SAYING, because ending on the
        sensor and running out of length are different things.
        """
        self.obj._toolhead_has_filament = lambda: False
        self.link.replies = (["FFS channel 1 feeding."]
                             + [f13(state=STATUS.LOADING, chan=1)] * 2
                             + [f13(state=STATUS.READY)]
                             + ["", ""])
        gcmd = fakes.FakeGcmd({"CHANNEL": 1, "UNTIL": "toolhead", "CHECK": 1,
                               "LENGTH": 600, "SPEED": 1200, "TIMEOUT": 2.0})
        self.obj.cmd_IFS_FEED(gcmd)          # must NOT raise
        self.assertTrue(any("not_reached" in r for r in gcmd.gcode.responses),
                        gcmd.gcode.responses)
        ## And it must not have stopped the board, which is what a problem does.
        self.assertNotIn("F112", self.link.asked)

    def test_backoff_only_runs_when_the_sensor_ended_the_move(self):
        """A klipper macro is rendered ONCE, before any of it runs.

        So a template cannot read the toolhead sensor after its own feed - that
        read already happened. The conditional retract auto-insert needs has to
        be decided here, where the outcome is known. zmod decides the same thing
        off RET_EXTRUDER in python.
        """
        self.obj._toolhead_has_filament = lambda: True
        self.link.replies = (["FFS channel 1 feeding."]
                             + [f13(state=STATUS.LOADING, chan=1)]
                             + ["FFS channel 1 exiting."]
                             + [f13(state=STATUS.READY)] * 3)
        gcmd = fakes.FakeGcmd({"CHANNEL": 1, "UNTIL": "toolhead", "CHECK": 1,
                               "LENGTH": 600, "SPEED": 1200, "BACKOFF": 90,
                               "TIMEOUT": 2.0})
        self.obj.cmd_IFS_FEED(gcmd)
        self.assertIn("F11 C1 L90 S1200", self.link.asked)

    def test_no_backoff_when_the_feed_merely_ran_out_of_length(self):
        ## Nothing arrived anywhere, so there is nothing to back away from.
        self.obj._toolhead_has_filament = lambda: False
        self.link.replies = (["FFS channel 1 feeding."]
                             + [f13(state=STATUS.LOADING, chan=1)] * 2
                             + [f13(state=STATUS.READY)]
                             + ["", ""])
        gcmd = fakes.FakeGcmd({"CHANNEL": 1, "UNTIL": "toolhead", "CHECK": 1,
                               "LENGTH": 600, "SPEED": 1200, "BACKOFF": 90,
                               "TIMEOUT": 2.0})
        self.obj.cmd_IFS_FEED(gcmd)
        self.assertNotIn("F11 C1 L90 S1200", self.link.asked)

    def test_a_feed_that_does_reach_the_toolhead_succeeds(self):
        ## The contrast: same board, sensor tripped, no error.
        self.obj._toolhead_has_filament = lambda: True
        self.link.replies = (["FFS channel 1 feeding."]
                             + [f13(state=STATUS.LOADING, chan=1)] * 4)
        gcmd = fakes.FakeGcmd({"CHANNEL": 1, "UNTIL": "toolhead", "CHECK": 1,
                               "LENGTH": 600, "SPEED": 1200, "TIMEOUT": 2.0})
        self.obj.cmd_IFS_FEED(gcmd)         # must not raise

    def test_an_until_done_move_still_finishes_on_ready(self):
        ## Only a move aimed at a sensor can miss it. zmod's unchecked moves
        ## wait for READY and that IS their success.
        self.link.replies = (["FFS channel 4 exiting."]
                             + [f13(state=STATUS.READY)] * 2)
        gcmd = fakes.FakeGcmd({"CHANNEL": 4, "UNTIL": "done",
                               "LENGTH": 600, "SPEED": 1200})
        self.obj.cmd_IFS_RETRACT(gcmd)      # must not raise

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
        ## A timeout does not know which channel the board thinks it is on.
        self.assertIn("F18", self.link.asked)
        self.assertNotIn("F39 C1", self.link.asked)

    def test_a_failed_move_lets_go_of_its_own_channel(self):
        """A klipper macro has no finally.

        IFS_AUTOINSERT clamps, feeds, and releases - but once the feed raises,
        the release written after it never runs, and the lane stays gripped
        until a human notices. Two of them sat clamped for hours after one
        failed run. zmod's AUTOINSERT ends with F39 whether or not the feed
        worked, for exactly this reason.

        A stall knows its channel, so it lets go of that one and leaves the
        others alone.
        """
        self.link.replies = (["FFS channel 1 feeding."]
                             + [f13(state=STATUS.LOADING, chan=1, stall=0)] * 8
                             + ["", ""])
        gcmd = fakes.FakeGcmd({"CHANNEL": 1, "UNTIL": "done", "CHECK": 1,
                               "LENGTH": 600, "SPEED": 1200, "TIMEOUT": 1.0})
        with self.assertRaises(gcmd.error):
            self.obj.cmd_IFS_FEED(gcmd)
        self.assertIn("F112", self.link.asked)
        self.assertIn("F39 C1", self.link.asked)
        self.assertNotIn("F18", self.link.asked)

    def test_a_move_that_worked_keeps_its_clamp(self):
        ## The load holds the lane through the purge that follows, so a
        ## successful feed must not let go.
        self.link.replies = (["FFS channel 1 feeding."]
                             + [f13(state=STATUS.READY)] * 2)
        gcmd = fakes.FakeGcmd({"CHANNEL": 1, "UNTIL": "done",
                               "LENGTH": 600, "SPEED": 1200})
        self.obj.cmd_IFS_FEED(gcmd)
        self.assertNotIn("F39 C1", self.link.asked)
        self.assertNotIn("F112", self.link.asked)

    def test_diagnostics_goes_through_the_queue(self):
        """Fourteen queries must not race the poll thread for the link.

        Reading the link straight from klipper's thread let the poller and the
        diagnostics read split each other's replies: the output varied between
        calls - stall counts on one, raw silk on the next - and the status poll
        that landed mid-batch came back empty and reported the board as
        disconnected. With no poller there is nothing to run on, so this must
        report a failure rather than quietly reading the link itself.
        """
        self.obj._thread = None
        gcmd = fakes.FakeGcmd({})
        self.obj.cmd_IFS_DIAGNOSTICS(gcmd)
        self.assertTrue(any("diagnostics failed" in r
                            for r in gcmd.gcode.responses), gcmd.gcode.responses)
        self.assertEqual(self.link.asked, [])

    def test_diagnostics_reports_when_the_poller_runs_it(self):
        ## The contrast: same command, poll thread alive, real output.
        self.link.replies = [""] * 30
        gcmd = fakes.FakeGcmd({})
        self.obj.cmd_IFS_DIAGNOSTICS(gcmd)
        self.assertTrue(any("IFS firmware" in r
                            for r in gcmd.gcode.responses), gcmd.gcode.responses)

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

    def test_the_new_maintenance_commands_are_registered(self):
        _, printer, _ = make_ifs()
        gcode = printer.lookup_object("gcode")
        self.assertIn("IFS_REINIT_DRIVERS", gcode.commands)
        self.assertIn("IFS_JOG_SELECTOR", gcode.commands)


class TestReinitDrivers(unittest.TestCase):
    """F43 rewrites both TMC drivers with the values the board uses at boot.

    It is the only selector-side driver reset in the opcode set - F15 C drops
    the feeder's enable line and writes no TMC register at all.
    """

    def setUp(self):
        self.obj, self.printer, self.link = make_ifs(replies=[f13()])
        self.obj._connect()
        self.obj._thread = AliveThread()
        self.reactor = self.printer.reactor

        def tick():
            if not self.obj._run_queued():
                self.obj._poll_once()
        self.reactor.on_pause = tick
        self.gcode = self.printer.lookup_object("gcode")

    def run_command(self, params=None):
        gcmd = fakes.FakeGcmd(params or {}, self.gcode)
        self.obj.cmd_IFS_REINIT_DRIVERS(gcmd)
        return gcmd

    def test_it_sends_f43(self):
        self.obj._poll_once()
        self.link.replies = [""] + [f13()] * 4
        self.run_command()
        self.assertIn("F43", self.link.asked)

    def test_it_refuses_unless_the_board_is_ready(self):
        ## F43 reconfigures both drivers and does not stop the state machine
        ## first, so sending it mid-move reconfigures a driver that is being
        ## stepped underneath it.
        self.link.replies = [f13(state=STATUS.LOADING)]
        self.obj._poll_once()
        with self.assertRaises(fakes.FakeGcmd.error):
            self.run_command()
        self.assertNotIn("F43", self.link.asked)

    def test_it_refuses_when_the_board_has_never_answered(self):
        ## No snapshot means no evidence the board is idle, which is not the
        ## same as evidence that it is.
        obj, printer, _ = make_ifs()
        gcode = printer.lookup_object("gcode")
        with self.assertRaises(fakes.FakeGcmd.error):
            obj.cmd_IFS_REINIT_DRIVERS(fakes.FakeGcmd({}, gcode))

    def test_it_says_the_run_current_went_back_to_stock(self):
        ## F43 rewrites IHOLD_IRUN, so any F42 current set beforehand is gone.
        ## Silence about that would strand someone who had just raised it.
        self.obj._poll_once()
        self.link.replies = [""] + [f13()] * 4
        self.run_command()
        self.assertIn("current", " ".join(self.gcode.responses).lower())

    def test_a_refusal_fails_the_command_rather_than_the_printer(self):
        self.obj._poll_once()
        self.link.replies = ["FFS not ready."] + [f13()] * 4
        with self.assertRaises(fakes.FakeGcmd.error):
            self.run_command()


class TestJogSelector(unittest.TestCase):
    """F30 is the only opcode that moves the selector without homing first.

    It also parks the state machine at 129 and never leaves, so every path
    through this command has to issue the F15 C that frees it.
    """

    def setUp(self):
        self.obj, self.printer, self.link = make_ifs(replies=[f13()])
        self.obj._connect()
        self.obj._thread = AliveThread()
        self.reactor = self.printer.reactor

        def tick():
            if not self.obj._run_queued():
                self.obj._poll_once()
        self.reactor.on_pause = tick
        self.gcode = self.printer.lookup_object("gcode")
        self.obj._poll_once()

    def run_command(self, position=4096):
        gcmd = fakes.FakeGcmd({"POSITION": str(position)}, self.gcode)
        self.obj.cmd_IFS_JOG_SELECTOR(gcmd)
        return gcmd

    def arrived(self, moving_reads=1):
        """Replies for a jog: the F30 ack, some motion, then standstill."""
        return ([""]
                + ["DRV_STATUS: 00090000"] * moving_reads
                + ["DRV_STATUS: 80000000"] * 8
                + [""] + [f13()] * 20)

    def test_it_sends_the_jog_then_frees_the_state_machine(self):
        self.link.replies = self.arrived()
        self.run_command(4096)
        self.assertIn("F30 D4096", self.link.asked)
        self.assertIn("F15 C", self.link.asked)
        self.assertLess(self.link.asked.index("F30 D4096"),
                        self.link.asked.index("F15 C"))

    def test_it_waits_for_the_selector_to_stop(self):
        ## The selector's standstill bit is the only thing that reports this
        ## motor. F13's stall_state is the feeder's.
        self.link.replies = self.arrived(moving_reads=4)
        self.run_command()
        self.assertGreaterEqual(self.link.asked.count("F63"), 4)

    def test_the_state_machine_is_freed_even_when_the_jog_is_refused(self):
        ## The safety property. A refused F30 leaves the board wherever it was,
        ## but a jog that started and then failed would strand it at 129.
        self.link.replies = ["FFS not ready."] + [""] + [f13()] * 20
        with self.assertRaises(fakes.FakeGcmd.error):
            self.run_command()
        self.assertIn("F15 C", self.link.asked)

    def test_the_state_machine_is_freed_even_when_arrival_never_confirms(self):
        ## The driver answering nothing must not leave the board parked at 129.
        self.link.replies = [""] + ["DRV_STATUS: 00090000"] * 400
        self.run_command()
        self.assertIn("F15 C", self.link.asked)

    def test_a_transient_read_failure_does_not_abandon_the_move(self):
        ## One failed F63 in the middle of a five-second move is not a reason
        ## to give up on it - and giving up early would report the jog as
        ## finished when the turret was still turning.
        self.link.replies = ([""]
                             + ["DRV_STATUS: 00090000"]
                             + [None]
                             + ["DRV_STATUS: 80000000"] * 8
                             + [""] + [f13()] * 20)
        original = self.link.request

        def flaky(command):
            if self.link.replies and self.link.replies[0] is None:
                self.link.replies.pop(0)
                self.link.asked.append(command)
                raise RuntimeError("board went quiet")
            return original(command)

        self.link.request = flaky
        self.run_command()
        self.assertIn("F15 C", self.link.asked)
        self.assertIn("jogged", " ".join(self.gcode.responses))

    def test_an_out_of_range_position_never_reaches_the_board(self):
        self.link.replies = [f13()] * 4
        for position in (-1, 16385):
            with self.assertRaises(fakes.FakeGcmd.error):
                self.run_command(position)
        self.assertNotIn("F15 C", self.link.asked)
        self.assertFalse([c for c in self.link.asked if c.startswith("F30")])

    def test_position_is_required(self):
        gcmd = fakes.FakeGcmd({}, self.gcode)
        with self.assertRaises(Exception):
            self.obj.cmd_IFS_JOG_SELECTOR(gcmd)


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
        self.registered = {}

    def register_adc(self, name, channel):
        self.registered[name] = channel


class FakeMcuAdc:
    """klipper's MCU_adc, as far as claiming a pin uses it."""

    def __init__(self):
        self.sample = None
        self.report_time = None
        self.callback = None
        self._last = (0.0, 0.0)

    def setup_adc_sample(self, sample_time, sample_count, **kwargs):
        self.sample = (sample_time, sample_count)

    def setup_adc_callback(self, report_time, callback):
        self.report_time = report_time
        self.callback = callback

    def get_last_value(self):
        return self._last


class FakePins:
    def __init__(self):
        self.claimed = []
        self.adc = FakeMcuAdc()

    def setup_pin(self, pin_type, pin_desc):
        self.claimed.append((pin_type, pin_desc))
        return self.adc


def make_toolhead(values=None, adc=None, pins=None):
    printer = fakes.FakePrinter()
    if adc is not None:
        printer.add_object("query_adc", FakeQueryAdc(adc))
    if pins is not None:
        printer.add_object("pins", pins)
    config = fakes.FakeConfig("ifs_toolhead_sensor toolhead", values, printer)
    return TOOLHEAD.IfsToolheadSensor(config), printer


class TestDirectPinSampling(unittest.TestCase):
    """Claiming the ADC ourselves, for a sensor that can be watched in time.

    Read through a `temperature_sensor`, this pin updates every 0.300 s -
    klipper's REPORT_TIME for thermistors - because klipper reports temperature
    slowly and nothing about the declaration says it is a filament sensor. At a
    1200 mm/min feed that is 6 mm of travel between samples and at 3600 it is
    18 mm, against a sensor transition only 5-10 mm wide. Claiming the pin
    directly samples it at 0.015 s instead: under a millimetre at any speed the
    feeder can reach.

    It is opt-in because the pin can only be claimed once, and on a stock
    machine `printer.base.cfg` already declares it as a temperature_sensor.
    """

    def test_without_sensor_pin_nothing_is_claimed(self):
        ## The default has to keep working on an unmodified printer.
        pins = FakePins()
        obj, _ = make_toolhead(adc={"temperature_sensor filamentValue": 0.1},
                               pins=pins)
        self.assertEqual(pins.claimed, [])
        self.assertTrue(obj.read_present())

    def test_sensor_pin_claims_the_adc(self):
        pins = FakePins()
        make_toolhead({"sensor_pin": "eboard:PA3"}, pins=pins)
        self.assertEqual(pins.claimed, [("adc", "eboard:PA3")])

    def test_it_samples_twenty_times_faster_than_a_thermistor(self):
        ## klipper's adc_temperature.REPORT_TIME is 0.300; this is the point of
        ## the whole exercise, so it is pinned rather than left to a constant.
        pins = FakePins()
        make_toolhead({"sensor_pin": "eboard:PA3"}, pins=pins)
        self.assertLessEqual(pins.adc.report_time, 0.015)
        ## 0.015 * 20 is exactly klipper's 0.300, so at-least-20x is <=.
        self.assertLessEqual(pins.adc.report_time * 20, 0.300)

    def test_the_callback_is_what_the_classifier_reads(self):
        pins = FakePins()
        obj, _ = make_toolhead({"sensor_pin": "eboard:PA3"}, pins=pins)
        pins.adc.callback(1234.5, 0.09)
        self.assertTrue(obj.read_present())
        pins.adc.callback(1235.0, 0.45)
        self.assertFalse(obj.read_present())

    def test_before_any_callback_there_is_no_reading(self):
        ## Not "absent" - a sensor that has not reported yet knows nothing, and
        ## fail_safe must not read that as a runout.
        pins = FakePins()
        obj, _ = make_toolhead({"sensor_pin": "eboard:PA3"}, pins=pins)
        self.assertIsNone(obj.read_present())

    def test_a_pin_already_claimed_falls_back_instead_of_killing_klippy(self):
        ## The failure that would otherwise be a bricked boot. The pin can only
        ## be claimed once, and a stock printer.base.cfg declares it as a
        ## temperature_sensor - so if the strip that frees it ever does not
        ## happen (a firmware update restoring the file, an init that did not
        ## run), setup_pin raises and klipper refuses to start. Degrading to
        ## the slower reading is always better than not booting.
        class Hostile:
            def setup_pin(self, pin_type, pin_desc):
                raise Exception("pin eboard:PA3 is already used")
        obj, _ = make_toolhead({"sensor_pin": "eboard:PA3"},
                               adc={"temperature_sensor filamentValue": 0.1},
                               pins=Hostile())
        self.assertIsNone(obj.sensor_pin)
        self.assertTrue(obj.read_present())

    def test_it_registers_with_query_adc_so_IFS_SENSOR_VALUE_still_works(self):
        pins = FakePins()
        obj, printer = make_toolhead({"sensor_pin": "eboard:PA3"},
                                     adc={}, pins=pins)
        self.assertTrue(printer.lookup_object("query_adc").registered)


class TestToolheadSensor(unittest.TestCase):
    ADC = TOOLHEAD.DEFAULT_ADC

    def test_engaged_reads_present(self):
        sensor, _ = make_toolhead(adc={self.ADC: 0.0081})
        self.assertTrue(sensor.read_present())
        self.assertEqual(sensor.last_state, LOGIC.PRESENT)

    def test_empty_reads_absent(self):
        ## A genuinely empty toolhead - cut, purged, retracted. 0.043 is NOT
        ## this: it is a tip still in the extruder, a few centimetres off the
        ## sensor, and reading it as absent is what made a load skip the cut.
        sensor, _ = make_toolhead(adc={self.ADC: 0.3983})
        self.assertFalse(sensor.read_present())
        self.assertEqual(sensor.last_state, LOGIC.ABSENT)

    def test_a_tip_off_the_sensor_still_reads_present(self):
        sensor, _ = make_toolhead(adc={self.ADC: 0.0227})
        self.assertTrue(sensor.read_present())
        self.assertEqual(sensor.last_state, LOGIC.PRESENT)

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
