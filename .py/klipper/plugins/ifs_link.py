## Serial transport for the AD5X IFS (4-channel filament system) board.
##
## The IFS is a separate STM32 on the host UART /dev/ttyS4. This module owns
## that link and nothing else: opening the port, framing a request, matching a
## reply to it, and probing the board's capabilities. Interpreting the payloads
## is the job of the layers above.
##
## The protocol is documented in docs/AD5X_IFS_PROTOCOL.md, recovered from the
## board's own firmware image rather than from a driver. The framing (the 0xFF
## commit byte, the ~200 ms gap before it, the port parameters) is the derived
## portion the attribution below covers, taken from a driver proven to work.
##
## Copyright (C) 2026, Preston Brown
## Portions derived from zmod (C) 2025-2026 ghzserg <https://github.com/ghzserg/zmod/>
##
## This file may be distributed under the terms of the GNU GPLv3 license

import logging
import re
import time


PORT = "/dev/ttyS4"
BAUDRATE = 115200
BYTESIZE = 8
PARITY = "N"
STOPBITS = 1

## Per-read timeout. Short, because a healthy board answers well inside it and
## a silent board has to be noticed quickly.
READ_TIMEOUT = 0.2

## Measured on an IFS board running firmware 3.0.6: the board answers identically
## with and without the trailing 0xFF, and with and without a space before the
## CRLF. All four combinations returned the same 127-byte F13 reply. FlashForge's
## own firmware sends the space and never the commit byte. Neither extra is
## required, so the default sends neither and skips the delay -
## that is a hard 200 ms saved on every single command.
##
## SEND_COMMIT_BYTE stays as an escape hatch. One board at one firmware revision
## is not every board, and if a different revision ever goes silent this is the
## first knob to turn.
SEND_COMMIT_BYTE = False
COMMIT_DELAY = 0.2
COMMIT_BYTE = b"\xff"

## FlashForge's own driver puts a space before the CRLF on every command. The
## board does not care, but matching the OEM costs nothing.
COMMAND_SUFFIX = " \r\n"

DEFAULT_CHANNEL_COUNT = 4

## Only F21 answers with more than one line: "F21 ok." then a silk line then a
## stall line, CRLF-separated. Reading one line for it leaves two in the buffer,
## which then answer the following two requests - every later poll silently off
## by one. Draining costs a full read timeout, so it is done only where the
## firmware actually needs it.
MULTILINE_OPCODES = frozenset([21])

## F21 sends two continuation lines. The cap is deliberately just above that.
MAX_EXTRA_LINES = 4

## Payloads that mean the command did not happen. "ok." in the prefix is the
## board echoing the opcode, not reporting success: "F10 ok. FFS not ready." is
## a failure. Nothing above this layer should have to know that.
ERROR_PAYLOADS = frozenset([
    "FFS channel not exist.",
    "FFS not ready.",
    "No channel selected.",
    "no chan.",
])

## "F19 ok. four color. version: 3.0.6" - a literal in the firmware, with no
## format specifiers, so the count word and the version are baked into each
## build and a different board answers with its own.
PROBE_COMMAND = "F19"
_PROBE_RE = re.compile(r"(\w+)\s+color\.\s*version:\s*(\S+)")
_COUNT_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4,
    "five": 5, "six": 6, "seven": 7, "eight": 8,
}

## F<n> for the IFS board itself; Z<n> for pass-through devices that jack the
## link (the IFS Jacker sits between host and board and answers Z opcodes the
## bare board cannot - which is exactly how it is detected).
_PREFIX_RE = re.compile(r"^([FZ]\d{1,3})\s+ok\.?\s*")


class IfsLinkError(Exception):
    """The link failed in a way the caller cannot retry past."""


class IfsTimeout(IfsLinkError):
    """The board did not answer within the allotted retries."""


class IfsResponse(object):
    """One reply, split into the echoed opcode and everything after it."""

    def __init__(self, opcode, payload, extra=None, raw=""):
        self.opcode = opcode
        self.payload = payload
        self.extra = extra or []
        self.raw = raw

    @property
    def is_error(self):
        return self.payload in ERROR_PAYLOADS

    def __repr__(self):
        return "IfsResponse(%s, %r%s)" % (
            self.opcode, self.payload,
            ", +%d line(s)" % len(self.extra) if self.extra else "")


class IfsCapabilities(object):
    """What the board said about itself when asked.

    `probed` is False when F19 went unanswered or unparsed, in which case the
    channel count is the assumed default and callers should treat the version
    as unknown rather than trusting a guess.
    """

    def __init__(self, channel_count, version, probed, raw=""):
        self.channel_count = channel_count
        self.version = version
        self.probed = probed
        self.raw = raw

    def __repr__(self):
        return "IfsCapabilities(channels=%d, version=%s, probed=%s)" % (
            self.channel_count, self.version, self.probed)


def parse_capabilities(text):
    """Parse an F19 payload. Returns None when it does not look like one."""
    match = _PROBE_RE.search(text or "")
    if match is None:
        return None
    word, version = match.group(1).lower(), match.group(2)
    count = _COUNT_WORDS.get(word)
    if count is None:
        ## A numeric count would be a newer firmware being helpful.
        try:
            count = int(word)
        except ValueError:
            return None
    return IfsCapabilities(count, version, True, text)


def split_response(line):
    """Split "F13 ok. rest" into ("F13", "rest"); None without the prefix.

    The opcode keeps its letter, so F and Z replies of the same number stay
    distinct. The separator after "ok." is inconsistent in the firmware -
    F10-F24 and F39 put a space there, F40-F64 do not - so the prefix is
    matched as a unit rather than by splitting on "ok. ".
    """
    match = _PREFIX_RE.match(line or "")
    if match is None:
        return None
    return match.group(1), line[match.end():].strip()


def _default_serial_factory():
    ## Imported here rather than at module scope so this module stays importable
    ## without pyserial. Every test drives it through an injected factory, and
    ## the klipper object is the only caller that reaches a real port.
    import serial

    return serial.Serial(
        port=PORT, baudrate=BAUDRATE, parity=PARITY,
        stopbits=STOPBITS, bytesize=BYTESIZE, timeout=READ_TIMEOUT)


class IfsLink(object):
    """Owns the serial link to the IFS board.

    Not thread-safe on its own: one thread should drive it. The caller supplies
    the serial factory so this can be exercised against a fake endpoint without
    pyserial or hardware.
    """

    ## Stale prefixed lines to discard before giving up on finding our reply.
    ## A desynced stream is bounded, not infinite; if this many lines all belong
    ## to other opcodes the link is broken, not merely behind.
    MAX_STALE_LINES = 8

    def __init__(self, serial_factory=None, sleep=None, retries=2,
                 send_commit_byte=SEND_COMMIT_BYTE):
        self._serial_factory = serial_factory or _default_serial_factory
        self._sleep = sleep or time.sleep
        self._retries = max(0, retries)
        self._send_commit_byte = send_commit_byte
        self._serial = None
        self.capabilities = None
        self.stale_lines = 0

    ## -- lifecycle ----------------------------------------------------------

    @property
    def is_open(self):
        return self._serial is not None

    def open(self):
        """Open the port and probe the board. Returns the capabilities.

        A board that will not answer F19 is not treated as absent - older
        firmware may not implement it - but the returned capabilities say so.
        """
        if self._serial is not None:
            return self.capabilities
        try:
            self._serial = self._serial_factory()
        except Exception as exc:
            self._serial = None
            raise IfsLinkError("cannot open %s: %s" % (PORT, exc))
        self.capabilities = self._probe()
        return self.capabilities

    def close(self):
        port, self._serial = self._serial, None
        if port is None:
            return
        try:
            port.close()
        except Exception as exc:
            logging.warning("IFS: error closing %s: %s", PORT, exc)

    def _probe(self):
        ## Only silence is tolerated here: firmware predating F19 simply will
        ## not answer, and that is not a reason to refuse the board. A desynced
        ## stream is a different failure and must not be reported as "no F19".
        try:
            response = self.request(PROBE_COMMAND)
        except IfsTimeout as exc:
            logging.info("IFS: F19 probe unanswered (%s); assuming %d channels",
                         exc, DEFAULT_CHANNEL_COUNT)
            return IfsCapabilities(DEFAULT_CHANNEL_COUNT, None, False)
        capabilities = parse_capabilities(response.payload)
        if capabilities is None:
            logging.info("IFS: F19 answered %r, which does not parse; "
                         "assuming %d channels",
                         response.payload, DEFAULT_CHANNEL_COUNT)
            return IfsCapabilities(DEFAULT_CHANNEL_COUNT, None, False,
                                   response.payload)
        return capabilities

    ## -- request/response ---------------------------------------------------

    def request(self, command):
        """Send a command and return its IfsResponse.

        Raises IfsTimeout when the board stays silent through every retry, and
        IfsLinkError when the port itself fails.
        """
        if self._serial is None:
            raise IfsLinkError("link is not open")
        opcode = self._opcode_of(command)
        last = None
        for _ in range(self._retries + 1):
            try:
                self._write_frame(command)
                response = self._read_response(opcode)
            except IfsTimeout as exc:
                last = exc
                continue
            except IfsLinkError:
                raise
            except Exception as exc:
                raise IfsLinkError("IFS transport error on %r: %s"
                                   % (command, exc))
            if response is not None:
                return response
            last = IfsTimeout("no reply to %r" % command)
        raise last

    @staticmethod
    def _opcode_of(command):
        ## The opcode is carried as its "F13"/"Z2" string so an F and a Z of
        ## the same number can never be correlated with each other.
        match = re.match(r"\s*([FZ]\d{1,3})\b", command or "")
        if match is None:
            raise IfsLinkError("not an IFS command: %r" % command)
        return match.group(1)

    def _write_frame(self, command):
        self._serial.write((command + COMMAND_SUFFIX).encode())
        if not self._send_commit_byte:
            return
        self._sleep(COMMIT_DELAY)
        self._serial.write(COMMIT_BYTE)

    def _read_line(self):
        raw = self._serial.readline()
        if not raw:
            return None
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="ignore")
        return raw.strip()

    def drain_pending(self, limit=MAX_EXTRA_LINES):
        """Throw away anything the board is still sending. Returns the count.

        Some opcodes answer with continuation lines we do not model, and a line
        left in the buffer becomes the answer to the NEXT command. Measured on
        the printer: the F13 straight after IFS_DIAGNOSTICS failed to read, and
        the one after it succeeded, with the diagnostics output itself varying
        between calls - stall counts on one, raw silk on the next. Call this
        after a batch of queries so the poller starts from a clean stream.
        """
        dropped = 0
        for _ in range(limit):
            line = self._read_line()
            if not line:
                break
            logging.debug("IFS: dropping unclaimed line %r", line)
            dropped += 1
        return dropped

    def _read_response(self, opcode):
        """Find the line that echoes our opcode ("F13"/"Z2"), dropping stale ones."""
        for _ in range(self.MAX_STALE_LINES + 1):
            line = self._read_line()
            if line is None:
                raise IfsTimeout("silent after %s" % opcode)
            split = split_response(line)
            if split is None:
                ## An unprefixed line here belongs to a previous multi-line
                ## reply we did not drain. Skip it and keep looking.
                self.stale_lines += 1
                logging.debug("IFS: dropping unprefixed line %r", line)
                continue
            seen, payload = split
            if seen != opcode:   # seen is the "F<n>"/"Z<n>" echo tag
                self.stale_lines += 1
                logging.debug("IFS: dropping F%d reply while awaiting F%d",
                              seen, opcode)
                continue
            extra = self._drain_extra() if opcode.startswith("F") and \
                int(opcode[1:]) in MULTILINE_OPCODES else []
            return IfsResponse(opcode, payload, extra, line)
        raise IfsLinkError(
            "link out of sync: %d lines without an %s reply"
            % (self.MAX_STALE_LINES + 1, opcode))

    def _drain_extra(self):
        """Collect the continuation lines of a multi-line reply.

        They carry no opcode prefix, so the end of the reply is a read timeout.
        Bounded so a chatty board cannot wedge the caller.
        """
        extra = []
        for _ in range(MAX_EXTRA_LINES):
            line = self._read_line()
            if not line:
                break
            if split_response(line) is not None:
                ## A prefixed line is the next reply, not our continuation.
                ## Nothing can push it back, so count it and stop.
                self.stale_lines += 1
                logging.debug("IFS: multi-line drain hit a new reply %r", line)
                break
            extra.append(line)
        return extra

