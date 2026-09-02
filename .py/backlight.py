#!/usr/bin/env python

## Configuration management, backup and restore
##
## Copyright (C) 2025-2026, Alexander K <https://github.com/drA1ex>
## Copyright (C) 2024, Tristan <https://github.com/consp>
##
## This file may be distributed under the terms of the GNU GPLv3 license


from fcntl import ioctl
from struct import pack
import errno
import sys

# Display ioctls that mean "this board has no controllable LCD backlight": the
# node is not the Allwinner display device (the AD5X exposes /dev/disp as a plain
# file), so open() succeeds but the ioctl is rejected. Treated as a no-op below.
_NO_BACKLIGHT_ERRNO = (errno.ENOTTY, errno.ENODEV, errno.ENXIO)

DISP_LCD_SET_BRIGHTNESS = 0x102
DISP_LCD_GET_BRIGHTNESS = 0x103
DISP_LCD_BACKLIGHT_ENABLE = 0x104
DISP_LCD_BACKLIGHT_DISABLE = 0x105


def backlight_enable(enable=True):
    if enable:
        ctl = DISP_LCD_BACKLIGHT_ENABLE
    else:
        ctl = DISP_LCD_BACKLIGHT_DISABLE
    with open("/dev/disp", "wb") as f:
        try:
            ioctl(f, ctl, b"")
        except OSError as exc:
            # Re-enabling an already enabled backlight returns EPERM on this
            # display driver. It is harmless when a brightness update follows.
            if not enable or exc.errno != errno.EPERM:
                raise

def backlight_get():
    with open("/dev/disp", "wb") as f:
        buffer = bytearray(4)
        value = ioctl(f, DISP_LCD_GET_BRIGHTNESS, buffer)
    return value

def backlight_set(value):
    with open("/dev/disp", "wb") as f:
        raw_value = int(1 + (value * (255 / 100.0)))
        ioctl(f, DISP_LCD_SET_BRIGHTNESS,
              pack("=L", 0) + pack("=L", raw_value))


def parse_brightness(value):
    try:
        value = float(value)
    except ValueError:
        raise ValueError("input must be int or float")

    if not (value is None or (value >= 0 and value <= 100.0)):
        raise ValueError("value must be within 0-100")
    return value

def parse_args(argv):
    if not argv:
        return None
    if len(argv) == 1 and argv[0] not in ("-h", "--help"):
        try:
            return parse_brightness(argv[0])
        except ValueError:
            pass

    import argparse

    def inputrange(value):
        try:
            return parse_brightness(value)
        except ValueError as exc:
            raise argparse.ArgumentTypeError(str(exc))

    parser = argparse.ArgumentParser(
        prog="FF AD5M Backlight control",
        description=(
            "Control the backlight: 0 disables it, 1-100 sets brightness. "
            "Credits to @xblax for finding the IOCTLs."),
        epilog=(
            "See https://github.com/consp/flashforge_ad5m_backlight and "
            "https://github.com/xblax/flashforge_adm5_klipper_mod"))

    parser.add_argument(
            'brightness',
            type=inputrange,
            const=None,
            nargs='?',
            help=(
                "brightness: 0 disables, >0 to 100 sets a percentage; "
                "omitting it returns the current raw value (0-255)"),
            )

    return parser.parse_args(argv).brightness


def main(argv=None):
    value = parse_args(sys.argv[1:] if argv is None else argv)

    try:
        if value is None:
            print("%d" % backlight_get())
        elif value == 0 or value == 0.0:
            backlight_enable(False)
        else:
            backlight_enable()
            ## `value`, not `args.brightness` - this branch referenced a name
            ## that does not exist in main(), so any non-zero brightness raised
            ## NameError. Upstream 1.4.2 fixed it; the fix comes in with the
            ## merge and the graceful-degrade wrapper around it stays ours.
            backlight_set(value)
    except OSError as exc:
        # No controllable backlight on this board (see _NO_BACKLIGHT_ERRNO).
        # Degrade to a no-op rather than failing the caller: screen.sh runs this
        # from the print macros (START_PRINT etc.), and an uncaught error there
        # surfaces as "Error running command {screen}". Boards that do have the
        # backlight never enter this branch - the ioctl succeeds for them.
        if exc.errno not in _NO_BACKLIGHT_ERRNO:
            raise
        sys.stderr.write(
            "backlight: no controllable backlight on this board "
            "(errno %d); skipping\n" % exc.errno)
        if value is None:
            print("0")

if __name__ == "__main__":
    main()
