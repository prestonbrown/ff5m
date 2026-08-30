## Diagnostics for the AD5X IFS board.
##
## The board answers 32 opcodes; the operations layer uses nine of them. This
## module is most of the difference: firmware version, cumulative stall counts, raw per-channel sensor
## values, and both TMC stepper drivers' registers. None of it reaches any UI
## today on any firmware.
##
## Pure parsing plus one reader over IfsLink. See docs/AD5X_IFS_PROTOCOL.md;
## every response shape here was read off a live board at firmware 3.0.6.
##
## Copyright (C) 2026, Preston Brown
##
## This file may be distributed under the terms of the GNU GPLv3 license

import re


## -- TMC registers ----------------------------------------------------------
##
## GSTAT's low three bits and DRV_STATUS bit 31 mean the same thing on every TMC
## part in this family (2130/2208/2209/5160), so they are decoded unconditionally.
## The fault bits do NOT agree between parts, and decoding them under the wrong
## family would turn "overtemperature" into "short to ground" silently. So they
## are decoded per family, and the family is named in the result.

GSTAT_FLAGS = (
    (0, "reset", "driver was reset"),
    (1, "driver_error", "driver shut down from a fault"),
    (2, "undervoltage_charge_pump", "charge pump undervoltage"),
)

## Bit 31 is `stst` on every part in the family.
DRV_STATUS_UNIVERSAL = ((31, "standstill", "no motion for 2^20 clocks"),)

## TMC2209 layout. Evidence this is the part: both drivers report GCONF 0x1dc,
## which sets pdn_disable and mstep_reg_select together - the canonical
## configuration for driving a 2209 over UART, which is how this board does it.
## Reading the same value as a 2130 would require enc_commutation on a filament
## feeder, which makes no sense. Strong, but circumstantial: override
## DRIVER_FAMILY if a board ever disagrees.
TMC2209_DRV_STATUS = (
    (0, "overtemp_prewarning", "approaching thermal limit"),
    (1, "overtemp", "thermal shutdown"),
    (2, "short_to_ground_a", "phase A shorted to ground"),
    (3, "short_to_ground_b", "phase B shorted to ground"),
    (4, "low_side_short_a", "phase A low-side short"),
    (5, "low_side_short_b", "phase B low-side short"),
    (6, "open_load_a", "phase A open - check wiring"),
    (7, "open_load_b", "phase B open - check wiring"),
    (8, "temp_over_120c", "above 120 C"),
    (9, "temp_over_143c", "above 143 C"),
    (10, "temp_over_150c", "above 150 C"),
    (11, "temp_over_157c", "above 157 C"),
)

DRIVER_FAMILIES = {"tmc2209": TMC2209_DRV_STATUS}
DRIVER_FAMILY = "tmc2209"

## Faults worth interrupting a print for, as opposed to informational bits.
SERIOUS_FLAGS = frozenset([
    "driver_error", "undervoltage_charge_pump", "overtemp",
    "short_to_ground_a", "short_to_ground_b",
    "low_side_short_a", "low_side_short_b",
])


def _decode(value, table):
    return [name for bit, name, _ in table if (value >> bit) & 1]


def decode_gstat(value):
    """Flags set in a GSTAT read. Reliable on every part in the family."""
    return _decode(value, GSTAT_FLAGS)


def decode_drv_status(value, family=None):
    """(flags, family_used). Universal bits are always included.

    An unknown family still decodes bit 31 and still reports whether any other
    bit is set, because "something is wrong and I cannot say what" is a far
    better answer than silence.
    """
    family = family or DRIVER_FAMILY
    table = DRIVER_FAMILIES.get(family)
    flags = _decode(value, DRV_STATUS_UNIVERSAL)
    if table is None:
        if value & ~(1 << 31):
            flags.append("unknown_fault_bits")
        return flags, None
    return flags + _decode(value, table), family


def describe(flag):
    for table in (GSTAT_FLAGS, DRV_STATUS_UNIVERSAL, TMC2209_DRV_STATUS):
        for _, name, text in table:
            if name == flag:
                return text
    return flag


## -- response parsers -------------------------------------------------------

_HEX = re.compile(r"([0-9a-fA-F]{8})\b")
_QUAD = re.compile(r"(-?\d+)\s+(-?\d+)\s+(-?\d+)\s+(-?\d+)")


def parse_register(payload):
    """`GCONF: 000001dc` -> 0x1dc. None when the payload has no register."""
    match = _HEX.search(payload or "")
    return int(match.group(1), 16) if match else None


def parse_stall_counts(payload):
    """`stall count: C1: 1 C2: 462 C3: 1 C4: 1` -> [1, 462, 1, 1]."""
    values = re.findall(r"C\d+:\s*(\d+)", payload or "")
    return [int(v) for v in values] if values else None


def parse_quad(payload, label=None):
    """Four whitespace-separated numbers, optionally after `<label>:`."""
    text = payload or ""
    if label:
        index = text.find(label + ":")
        if index < 0:
            return None
        text = text[index + len(label) + 1:]
    match = _QUAD.search(text)
    return [int(g) for g in match.groups()] if match else None


def parse_stepper(payload):
    """`stepper_motor: 0 stepper_motor_irun: 0` -> (0, 0)."""
    motor = re.search(r"stepper_motor:\s*(-?\d+)", payload or "")
    irun = re.search(r"stepper_motor_irun:\s*(-?\d+)", payload or "")
    if motor is None or irun is None:
        return None
    return int(motor.group(1)), int(irun.group(1))


## -- aggregates -------------------------------------------------------------

## Bits 16-20 of DRV_STATUS are CS_ACTUAL, the current scale the driver is
## actually applying. Observed as 9 on both drivers while moving and absent at
## standstill, which is its own corroboration of the register layout.
CS_ACTUAL_SHIFT, CS_ACTUAL_MASK = 16, 0x1F


class DriverSnapshot(object):
    """One TMC driver's registers.

    Which bank is which was settled by watching the standstill bit while each
    motor was made to move: `F24` (select a channel) dropped standstill on the
    F60 bank, and `F11` (move filament) dropped it on the F50 bank. That matches
    the two GCONF values `F13` reports, `jinsi` (进丝, feed) and `qiehuan`
    (切换, switch).
    """

    def __init__(self, label, gconf=None, gstat=None, chopconf=None,
                 drv_status=None, pwmconf=None):
        self.label = label
        self.gconf = gconf
        self.gstat = gstat
        self.chopconf = chopconf
        self.drv_status = drv_status
        self.pwmconf = pwmconf

    @property
    def gstat_flags(self):
        return decode_gstat(self.gstat) if self.gstat is not None else []

    @property
    def drv_status_flags(self):
        if self.drv_status is None:
            return []
        return decode_drv_status(self.drv_status)[0]

    @property
    def current_scale(self):
        """CS_ACTUAL - what the driver is applying right now, or None."""
        if self.drv_status is None:
            return None
        return (self.drv_status >> CS_ACTUAL_SHIFT) & CS_ACTUAL_MASK

    @property
    def is_moving(self):
        """A driver that is not at standstill. Inverse of the stst bit."""
        if self.drv_status is None:
            return None
        return not (self.drv_status >> 31) & 1

    @property
    def flags(self):
        return self.gstat_flags + self.drv_status_flags

    @property
    def faults(self):
        """Only the flags that mean something is wrong."""
        return [f for f in self.flags
                if f in SERIOUS_FLAGS or f == "unknown_fault_bits"]

    @property
    def is_healthy(self):
        return not self.faults

    def __repr__(self):
        return "DriverSnapshot(%s, %s)" % (
            self.label, ", ".join(self.flags) if self.flags else "no flags")


class IfsDiagnostics(object):
    """Everything the board will tell us about itself."""

    def __init__(self, version=None, channel_count=None, stall_counts=None,
                 silk_raw=None, stall_raw=None, stall_flags=None,
                 stepper=None, drivers=None, errors=None):
        self.version = version
        self.channel_count = channel_count
        self.stall_counts = stall_counts
        self.silk_raw = silk_raw
        self.stall_raw = stall_raw
        self.stall_flags = stall_flags
        self.stepper = stepper
        self.drivers = drivers or []
        self.errors = errors or {}

    @property
    def faults(self):
        return [(d.label, f) for d in self.drivers for f in d.faults]

    def driver(self, label):
        """The feeder or the selector by name, or None."""
        for d in self.drivers:
            if d.label == label:
                return d
        return None

    @property
    def feeder(self):
        return self.driver(FEEDER)

    @property
    def selector(self):
        return self.driver(SELECTOR)

    @property
    def is_healthy(self):
        return not self.faults

    ## Observed with silk_state 11 (channels 1, 2 and 4 loaded, 3 empty):
    ##     silk: 200 328 1689 275
    ## The three loaded channels read 200-330 and the empty one 1689, so a LOW
    ## raw value means filament PRESENT. Only one empty channel has been seen,
    ## so treat the polarity as strongly indicated rather than proven - but do
    ## not assume the intuitive direction, because it is backwards.
    SILK_LOADED_TYPICAL = 330
    SILK_EMPTY_TYPICAL = 1689

    def marginal_channels(self, low, high):
        """Channels whose raw silk reading sits between two thresholds.

        The reason F21 is worth reading at all: F13 and F14 report filament
        presence as one bit per channel, so a channel that is barely triggering
        looks identical to a solid one right up until it fails mid-print. The
        raw value shows it coming - a loaded channel drifting UP toward the
        empty value is the failure about to happen.
        """
        if not self.silk_raw:
            return []
        return [i + 1 for i, v in enumerate(self.silk_raw) if low <= v <= high]

    def loaded_by_raw(self, threshold=None):
        """Channels the RAW silk reading says are loaded. Low means present.

        A cross-check on `F13`'s bitmask: the two disagreeing means a channel is
        sitting near whatever threshold the board applies internally, which is
        the interesting case and is invisible in `F13` alone.
        """
        if not self.silk_raw:
            return []
        if threshold is None:
            threshold = (self.SILK_LOADED_TYPICAL + self.SILK_EMPTY_TYPICAL) // 2
        return [i + 1 for i, v in enumerate(self.silk_raw) if v <= threshold]

    def as_dict(self):
        return {
            "version": self.version,
            "channel_count": self.channel_count,
            "stall_counts": self.stall_counts,
            "silk_raw": self.silk_raw,
            "stall_raw": self.stall_raw,
            "stall_flags": self.stall_flags,
            "stepper": self.stepper,
            "drivers": [
                {"label": d.label, "gconf": d.gconf, "gstat": d.gstat,
                 "chopconf": d.chopconf, "drv_status": d.drv_status,
                 "pwmconf": d.pwmconf, "flags": d.flags, "faults": d.faults}
                for d in self.drivers],
            "healthy": self.is_healthy,
            "errors": self.errors,
        }


## Register banks: F50-F54 is one driver, F60-F64 the other, same five
## registers in the same order. F41/F44/F45 return values matching the F50 bank
## and look like aliases of it, so they are not read separately.
FEEDER, SELECTOR = "feeder", "selector"

DRIVER_BANKS = (
    (FEEDER, {"gconf": "F50", "gstat": "F51", "chopconf": "F52",
              "drv_status": "F53", "pwmconf": "F54"}),
    (SELECTOR, {"gconf": "F60", "gstat": "F61", "chopconf": "F62",
                "drv_status": "F63", "pwmconf": "F64"}),
)


def read_diagnostics(link, capabilities=None):
    """Read everything diagnostic. Every opcode used here is a query.

    A failing opcode is recorded in `errors` rather than aborting - a board that
    will not answer F42 should still report its driver faults.
    """
    errors = {}

    def ask(opcode):
        try:
            return link.request(opcode).payload
        except Exception as exc:
            errors[opcode] = str(exc)
            return None

    capabilities = capabilities or getattr(link, "capabilities", None)

    ## F21's own payload is empty - the numbers arrive on its two continuation
    ## lines, which is why the transport drains them.
    silk_raw = stall_raw = None
    try:
        f21 = link.request("F21")
        joined = " ".join([f21.payload] + list(f21.extra))
        silk_raw = parse_quad(joined, "silk")
        stall_raw = parse_quad(joined, "stall")
    except Exception as exc:
        errors["F21"] = str(exc)

    drivers = []
    for label, bank in DRIVER_BANKS:
        values = {}
        for field, opcode in bank.items():
            payload = ask(opcode)
            values[field] = parse_register(payload) if payload else None
        drivers.append(DriverSnapshot(label, **values))

    f40 = ask("F40")
    f14 = ask("F14")
    f42 = ask("F42")

    ## Whatever any of the above left unread would otherwise be handed to the
    ## next command as its reply. The poller's F13 is always the next command.
    drain = getattr(link, "drain_pending", None)
    if drain is not None:
        try:
            drain()
        except Exception as exc:
            errors["drain"] = str(exc)

    return IfsDiagnostics(
        version=getattr(capabilities, "version", None),
        channel_count=getattr(capabilities, "channel_count", None),
        stall_counts=parse_stall_counts(f40) if f40 else None,
        silk_raw=silk_raw,
        stall_raw=stall_raw,
        stall_flags=parse_quad(f14, "stall") if f14 else None,
        stepper=parse_stepper(f42) if f42 else None,
        drivers=drivers,
        errors=errors)
