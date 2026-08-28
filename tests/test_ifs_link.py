## Tests for the AD5X IFS serial transport.
##
## The responses replayed here are verbatim from the board's firmware image -
## see docs/AD5X_IFS_PROTOCOL.md and tools/ifs/extract_ifs_protocol.py. Where a
## test asserts on a quirk (the inconsistent separator after "ok.", F18's
## missing period, F21's three lines), the quirk is the firmware's, not ours.
##
## Copyright (C) 2026, Preston Brown
##
## This file may be distributed under the terms of the GNU GPLv3 license

import unittest


import ifs_modules

IFS = ifs_modules.load("ifs_link")


PROBE_REPLY = "F19 ok. four color. version: 3.0.6 "


class FakeSerial:
    """A scripted IFS board.

    `script` maps an opcode to the line(s) it answers with; a missing opcode
    means the board stays silent. `preloaded` seeds lines that are already in
    the buffer before the first command, which is how a desynced stream looks.
    """

    def __init__(self, script=None, preloaded=None, events=None):
        self.script = {int(k): (v if isinstance(v, list) else [v])
                       for k, v in (script or {}).items()}
        self.pending = list(preloaded or [])
        self.events = events if events is not None else []
        self.closed = False
        self._partial = b""

    ## -- pyserial surface ---------------------------------------------------

    def write(self, data):
        self.events.append(("write", data))
        if data == IFS.COMMIT_BYTE:
            ## Tolerated but not required - see docs/AD5X_IFS_PROTOCOL.md. The
            ## real board answers the command line either way.
            return len(data)
        self._partial += data
        if self._partial.endswith(b"\r\n"):
            self._commit()
        return len(data)

    def readline(self):
        if not self.pending:
            return b""
        return (self.pending.pop(0) + "\r\n").encode()

    def close(self):
        self.closed = True

    ## -- board behaviour ----------------------------------------------------

    def _commit(self):
        command, self._partial = self._partial.decode().strip(), b""
        opcode = int(command.split()[0][1:])
        self.pending.extend(self.script.get(opcode, []))


def make_link(script=None, preloaded=None, retries=2, events=None):
    events = events if events is not None else []
    fake = FakeSerial(script, preloaded, events)
    link = IFS.IfsLink(serial_factory=lambda: fake,
                       sleep=lambda d: events.append(("sleep", d)),
                       retries=retries)
    return link, fake, events


def make_open_link(script=None, preloaded=None, retries=2):
    """A link already past its F19 probe, so tests can get to the point."""
    script = dict(script or {})
    script.setdefault(19, PROBE_REPLY)
    link, fake, events = make_link(script, preloaded, retries)
    link.open()
    del events[:]
    return link, fake, events


class TestSplitResponse(unittest.TestCase):
    """Every reply echoes its opcode; that echo is the correlation handle."""

    def test_space_after_ok(self):
        self.assertEqual(IFS.split_response("F13 ok. FFS_state: 5 chan: 2"),
                         (13, "FFS_state: 5 chan: 2"))

    def test_no_space_after_ok(self):
        ## F40-F64 omit the separator. Splitting on "ok. " would lose the payload.
        self.assertEqual(
            IFS.split_response("F40 ok.stall count: C1: 0 C2: 3"),
            (40, "stall count: C1: 0 C2: 3"))

    def test_f18_has_no_period(self):
        self.assertEqual(IFS.split_response("F18 ok"), (18, ""))

    def test_three_digit_opcode(self):
        self.assertEqual(IFS.split_response("F112 ok."), (112, ""))

    def test_trailing_space_is_stripped(self):
        self.assertEqual(IFS.split_response("F23 ok. chan 2. "),
                         (23, "chan 2."))

    def test_unprefixed_line_is_rejected(self):
        ## An F21 continuation line, which must never be read as a reply head.
        self.assertIsNone(IFS.split_response(" silk: 1 0 0 0 "))
        self.assertIsNone(IFS.split_response(""))
        self.assertIsNone(IFS.split_response(None))


class TestCapabilities(unittest.TestCase):
    def test_parses_the_firmware_literal(self):
        caps = IFS.parse_capabilities("four color. version: 3.0.6")
        self.assertEqual(caps.channel_count, 4)
        self.assertEqual(caps.version, "3.0.6")
        self.assertTrue(caps.probed)

    def test_a_board_that_is_not_four_channel(self):
        ## The count is a literal word per firmware build, so a different board
        ## reports a different word. This is the whole point of probing.
        self.assertEqual(IFS.parse_capabilities(
            "two color. version: 1.2.3").channel_count, 2)

    def test_numeric_count(self):
        self.assertEqual(IFS.parse_capabilities(
            "6 color. version: 4.0").channel_count, 6)

    def test_unparseable(self):
        self.assertIsNone(IFS.parse_capabilities("wat"))
        self.assertIsNone(IFS.parse_capabilities(""))


class TestFraming(unittest.TestCase):
    def test_one_write_no_commit_byte_no_delay(self):
        ## Measured on firmware 3.0.6: the board answers with or without the
        ## 0xFF commit byte, so the default sends neither it nor the 200 ms
        ## sleep that has to precede it.
        link, fake, events = make_open_link({13: "F13 ok. FFS_state: 5"})
        link.request("F13")
        self.assertEqual(events, [("write", b"F13 \r\n")])

    def test_commit_byte_can_be_re_enabled_for_a_stubborn_board(self):
        ## One board at one firmware revision is not every board.
        events = []
        fake = FakeSerial({13: "F13 ok. FFS_state: 5", 19: PROBE_REPLY},
                          None, events)
        link = IFS.IfsLink(serial_factory=lambda: fake,
                           sleep=lambda d: events.append(("sleep", d)),
                           send_commit_byte=True)
        link.open()
        del events[:]
        link.request("F13")
        self.assertEqual(events, [
            ("write", b"F13 \r\n"),
            ("sleep", IFS.COMMIT_DELAY),
            ("write", IFS.COMMIT_BYTE),
        ])

    def test_rejects_a_non_command(self):
        link, _, _ = make_open_link()
        with self.assertRaises(IFS.IfsLinkError):
            link.request("hello")

    def test_request_before_open(self):
        link, _, _ = make_link()
        with self.assertRaises(IFS.IfsLinkError):
            link.request("F13")


class TestProbeOnOpen(unittest.TestCase):
    def test_probe_succeeds(self):
        link, _, _ = make_link({19: PROBE_REPLY})
        caps = link.open()
        self.assertEqual(caps.channel_count, 4)
        self.assertEqual(caps.version, "3.0.6")
        self.assertTrue(caps.probed)

    def test_silent_board_still_opens_with_a_flagged_default(self):
        ## Older firmware may not implement F19. That is not "no board".
        link, _, _ = make_link({}, retries=0)
        caps = link.open()
        self.assertTrue(link.is_open)
        self.assertFalse(caps.probed)
        self.assertEqual(caps.channel_count, IFS.DEFAULT_CHANNEL_COUNT)
        self.assertIsNone(caps.version)

    def test_unparseable_probe_is_flagged_not_guessed(self):
        link, _, _ = make_link({19: "F19 ok. something else entirely"})
        caps = link.open()
        self.assertFalse(caps.probed)
        self.assertIsNone(caps.version)

    def test_open_is_idempotent(self):
        link, fake, _ = make_link({19: PROBE_REPLY})
        first = link.open()
        self.assertIs(link.open(), first)

    def test_open_failure_leaves_the_link_closed(self):
        def explode():
            raise OSError("no such device")

        link = IFS.IfsLink(serial_factory=explode, sleep=lambda d: None)
        with self.assertRaises(IFS.IfsLinkError):
            link.open()
        self.assertFalse(link.is_open)


class TestErrorPayloads(unittest.TestCase):
    """"ok." is the opcode echo, not a success report."""

    def test_ffs_not_ready_is_an_error(self):
        link, _, _ = make_open_link({10: "F10 ok. FFS not ready. "})
        response = link.request("F10 C1 L100 S1200")
        self.assertEqual(response.opcode, 10)
        self.assertEqual(response.payload, "FFS not ready.")
        self.assertTrue(response.is_error)

    def test_channel_not_exist_is_an_error(self):
        link, _, _ = make_open_link({24: "F24 ok. FFS channel not exist. "})
        self.assertTrue(link.request("F24 C9").is_error)

    def test_no_channel_selected_is_an_error(self):
        link, _, _ = make_open_link({11: "F11 ok. No channel selected. "})
        self.assertTrue(link.request("F11 C1 L10 S600").is_error)

    def test_a_real_answer_is_not_an_error(self):
        link, _, _ = make_open_link({10: "F10 ok. FFS channel 2 feeding. "})
        response = link.request("F10 C2 L100 S1200")
        self.assertFalse(response.is_error)
        self.assertEqual(response.payload, "FFS channel 2 feeding.")


class TestMultiLine(unittest.TestCase):
    """F21 is the only opcode that answers with more than one line."""

    F21 = ["F21 ok. ", " silk: 1 0 1 0 ", " stall: 0 0 0 0 "]

    def test_continuation_lines_are_collected(self):
        link, _, _ = make_open_link({21: list(self.F21)})
        response = link.request("F21")
        self.assertEqual(response.payload, "")
        self.assertEqual(response.extra, ["silk: 1 0 1 0", "stall: 0 0 0 0"])

    def test_the_next_command_is_not_desynced(self):
        ## The failure this guards: a single readline() per command leaves the
        ## silk and stall lines in the buffer, and they answer the next two
        ## requests instead. Every later poll is then silently off by one.
        link, _, _ = make_open_link({21: list(self.F21),
                                     13: "F13 ok. FFS_state: 5 chan: 2"})
        link.request("F21")
        response = link.request("F13")
        self.assertEqual(response.opcode, 13)
        self.assertEqual(response.payload, "FFS_state: 5 chan: 2")

    def test_single_line_replies_do_not_pay_the_drain(self):
        ## Draining costs a full read timeout, so it must not happen for the
        ## status poll that runs continuously.
        link, fake, _ = make_open_link({13: "F13 ok. FFS_state: 5"})
        response = link.request("F13")
        self.assertEqual(response.extra, [])


class TestResync(unittest.TestCase):
    def test_a_stale_reply_is_discarded(self):
        ## A late F13 sitting in the buffer must not be handed back as the
        ## answer to F24.
        link, _, _ = make_open_link({24: "F24 ok. chan 2. "},
                                    preloaded=["F13 ok. FFS_state: 5 "])
        response = link.request("F24 C2")
        self.assertEqual(response.opcode, 24)
        self.assertEqual(response.payload, "chan 2.")
        self.assertEqual(link.stale_lines, 1)

    def test_an_orphaned_continuation_line_is_discarded(self):
        link, _, _ = make_open_link({13: "F13 ok. FFS_state: 5 "},
                                    preloaded=[" stall: 0 0 0 0 "])
        self.assertEqual(link.request("F13").opcode, 13)
        self.assertEqual(link.stale_lines, 1)

    def test_a_hopelessly_desynced_link_gives_up(self):
        link, fake, _ = make_open_link({24: "F24 ok. chan 1. "})
        fake.pending.extend(["F13 ok. FFS_state: 5 "]
                            * (IFS.IfsLink.MAX_STALE_LINES + 2))
        with self.assertRaises(IFS.IfsLinkError):
            link.request("F24 C1")

    def test_a_desync_at_open_is_not_reported_as_a_missing_probe(self):
        ## _probe tolerates silence, because firmware predating F19 answers
        ## nothing. It must not tolerate a stream that is out of sync - that
        ## would surface as a cheerful "assuming 4 channels" on a broken link.
        junk = ["F13 ok. FFS_state: 5 "] * (IFS.IfsLink.MAX_STALE_LINES + 2)
        link, _, _ = make_link({19: PROBE_REPLY}, preloaded=junk)
        with self.assertRaises(IFS.IfsLinkError) as caught:
            link.open()
        self.assertNotIsInstance(caught.exception, IFS.IfsTimeout)


class TestTimeoutAndRetry(unittest.TestCase):
    def test_silence_raises_after_the_retries(self):
        link, fake, events = make_open_link({}, retries=2)
        with self.assertRaises(IFS.IfsTimeout):
            link.request("F13")
        writes = [d for kind, d in events if kind == "write"]
        self.assertEqual(writes.count(b"F13 \r\n"), 3)

    def test_a_retry_can_succeed(self):
        link, fake, _ = make_open_link(retries=2)
        calls = {"n": 0}
        original = fake._commit

        def flaky():
            calls["n"] += 1
            original()
            if calls["n"] == 1:
                fake.pending = []       # first attempt: board says nothing

        fake._commit = flaky
        fake.script[13] = ["F13 ok. FFS_state: 5 "]
        response = link.request("F13")
        self.assertEqual(response.opcode, 13)
        self.assertEqual(calls["n"], 2)

    def test_retries_zero_means_one_attempt(self):
        link, _, events = make_open_link({}, retries=0)
        with self.assertRaises(IFS.IfsTimeout):
            link.request("F13")
        self.assertEqual([d for k, d in events if k == "write"].count(b"F13 \r\n"), 1)


class TestClose(unittest.TestCase):
    def test_close_closes_the_port(self):
        link, fake, _ = make_open_link()
        link.close()
        self.assertTrue(fake.closed)
        self.assertFalse(link.is_open)

    def test_close_is_idempotent(self):
        link, _, _ = make_open_link()
        link.close()
        link.close()

    def test_close_survives_a_throwing_port(self):
        link, fake, _ = make_open_link()

        def explode():
            raise OSError("gone")

        fake.close = explode
        link.close()
        self.assertFalse(link.is_open)


if __name__ == "__main__":
    unittest.main()
