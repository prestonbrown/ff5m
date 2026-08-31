## Audio player for Flashforge AD5M
##
## Copyright (C) 2025, Alexander K <https://github.com/drA1ex>
##
## This file may be distributed under the terms of the GNU GPLv3 license
##
################################################################
## PwmAudio implementation:
##
## link: https://github.com/consp/flashforge_adm5_audio
##
## Copyright (C) 2025, Tristan <https://github.com/consp>


import logging
from pathlib import Path


class PWMAudio:
    chip = 0
    device = 0
    PWMEXPORT = "/sys/class/pwm/pwmchip%d/export"
    PWMCLASS = "/sys/class/pwm/pwmchip%d/pwm%d/%s"
    ENABLE = "enable"
    PERIOD = "period"
    DUTY_CYCLE = "duty_cycle"

    DC = 0.5  # fixed
    enabled = False

    def __init__(self, chip, device):
        self.chip = chip
        self.device = device
        self.export()
        self.disable()

    def pwmdevice(self, end):
        return self.PWMCLASS % (self.chip, self.device, end)

    def export(self):
        # check if exists
        pwmpath = Path(self.PWMEXPORT[:-6] % (self.chip) + "/pwm%d" % (self.device))
        if pwmpath.is_dir():
            return
        with open(self.PWMEXPORT % self.chip, 'wb') as f:
            f.write(b"%d" % self.device)
            f.flush()

    def enable(self, enable=True):
        self.enabled = enable

        if self.period == 0:  # period needs to be set otherwise errors will be thrown
            self.set(1000)
        with open(self.pwmdevice(self.ENABLE), "wb") as f:
            f.write(b"1" if enable else b"0")
            f.flush()

    def disable(self):
        self.enable(enable=False)

    @property
    def period(self):
        with open(self.pwmdevice(self.PERIOD), "rb") as f:
            return int(f.read())

    @period.setter
    def period(self, period):
        with open(self.pwmdevice(self.PERIOD), "wb") as f:
            f.write(b"%d" % period)
            f.flush()

    @property
    def duty_cycle(self):
        with open(self.pwmdevice(self.DUTY_CYCLE), "rb") as f:
            return int(f.read())

    @duty_cycle.setter
    def duty_cycle(self, dc):
        with open(self.pwmdevice(self.DUTY_CYCLE), "wb") as f:
            f.write(b"%d" % dc)
            f.flush()

    def set(self, frequency):
        period = 1000000000 / frequency
        dc = int(period * self.DC)
        if period < self.duty_cycle:
            self.duty_cycle = dc
            self.period = period
        else:
            self.period = period
            self.duty_cycle = dc


## Where the buzzer lives, per board. Not every board has one.
##
## AD5M: sysfs PWM at /sys/class/pwm/pwmchip0/pwm6.
## AD5X: the buzzer (pc12) hangs off Ingenic's PWM2 block behind the stock
##       soc_pwm.ko driver, which exposes a misc device (/dev/jz_pwm) rather
##       than sysfs; the fx-pwm tool in our rootfs speaks its ioctl ABI. The
##       whole tune is one subprocess, so playback never blocks klippy's
##       reactor per note.
PWM_CHIP = 0
PWM_DEVICE = 6

## fx-pwm lives in the mod's rootfs chroot, while klippy itself runs on the
## host (root=/, /usr/prog Python). Check the binary at its host-visible
## path and exec it through the chroot; empty CHROOT on a non-chrooted
## install runs the tool in place.
FX_CHROOT = "/usr/data/.mod/.forge-x"
FX_PWM = "/usr/bin/fx-pwm"
BUZZER_GPIO = "pc12"
JZ_PWM_DEVICE = "/dev/jz_pwm"
## The soc_pwm parent clock feeding the channel divider. The tool's
## assumed clock must be the raw parent: the DMA waveform runs at the
## full ~500 MHz parent (count-coded ear tests on the rig: commanded x7.8).
FX_PWM_BASE_HZ = 500000000


def fx_pwm_available():
    """Whether the fx-pwm tool and its device are both present."""
    prefix = FX_CHROOT if FX_CHROOT else ""
    return Path(prefix + FX_PWM).is_file() and Path(JZ_PWM_DEVICE).exists()


def buzzer_available(chip=PWM_CHIP):
    """Whether this board exposes a PWM buzzer to userspace in any form."""
    return fx_pwm_available() or Path("/sys/class/pwm/pwmchip%d" % chip).is_dir()


class TonePlayer:
    def __init__(self, config):
        self.config = config
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.gcode = self.printer.lookup_object("gcode")

        self.verbose = config.getboolean("verbose", False)
        ## Probed once, not per note. A board with no buzzer answers TONE
        ## silently rather than failing every caller that wants to chirp.
        self.available = buzzer_available()
        if not self.available:
            logging.info("TONE: no PWM buzzer on this board; tones are silent")

        self.gcode.register_command("TONE", self.cmd_TONE)

    def cmd_TONE(self, gcmd):
        """Play a tune, and NEVER take the printer down over a buzzer.

        Klipper turns any non-gcode exception into "Internal error on command",
        which shuts klippy down and takes the MCUs with it. That is a wildly
        disproportionate outcome for a beep, and it happened: the AD5X has no
        /sys/class/pwm, so PWMAudio raised FileNotFoundError and a completed
        filament change ended in a shutdown and a FIRMWARE_RESTART.
        """
        notes = self._parse_notes(gcmd)

        if not self.available:
            return

        if self.verbose:
            duration = sum(d / 1000 for (_, d) in notes)
            notes_cnt = len([freq for (freq, _) in notes if freq > 0])
            gcmd.respond_raw(f"Playing tune: duration: {duration:.2f}s, notes: {notes_cnt}")

        try:
            self._play(notes)
        except Exception as exc:
            ## Stop trying: a buzzer that failed once fails every time, and a
            ## warning per note during a print is its own kind of broken.
            self.available = False
            logging.warning("TONE: silencing the buzzer after %s", exc)
            return

        if self.verbose:
            gcmd.respond_raw("Done")

    def _play(self, notes):
        if fx_pwm_available():
            self._play_fx_pwm(notes)
            return

        pwm = PWMAudio(PWM_CHIP, PWM_DEVICE)
        try:
            for tone, duration in notes:
                if tone > 0:
                    pwm.set(tone)
                    pwm.enable()
                else:
                    pwm.disable()

                now = self.reactor.monotonic()
                self.reactor.pause(now + duration / 1000)
        finally:
            pwm.disable()

    def _play_fx_pwm(self, notes):
        """Play the tune via fx-pwm subprocesses; the reactor never waits.

        The prelude mirrors how stock drives the buzzer (firmwareExe shells
        out to cmd_pwm, one process per command): probe, config, prescale
        each as their own invocation before the tone process. Doing it all
        inside one tone process is driver-clean but SILENT on the hardware
        (measured by ear twice - separate invocations audible, single
        process not; mechanism not bisected, recipe kept verbatim). The
        whole chain runs inside one sh -c so klippy's reactor is never
        blocked; the sleeps reproduce the gaps the separate stock commands
        got for free from process-spawn overhead.
        """
        import subprocess
        tune = " ".join(
            "%s:%s" % (tone, duration) for (tone, duration) in notes)
        inner = "%s probe %s; sleep 0.3; " \
                "%s config %s freq=50000000 max_level=300 " \
                "active_level=1 accuracy_priority=freq; sleep 0.3; " \
                "%s set_prescale %s 6; sleep 0.3; " \
                "%s tone %s '%s' --base=%d" % (
                    FX_PWM, BUZZER_GPIO,
                    FX_PWM, BUZZER_GPIO,
                    FX_PWM, BUZZER_GPIO,
                    FX_PWM, BUZZER_GPIO, tune, FX_PWM_BASE_HZ)
        argv = ["/bin/sh", "-c", inner]
        if FX_CHROOT:
            argv = ["chroot", FX_CHROOT] + argv
        subprocess.Popen(argv,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def _parse_notes(self, gcmd):
        notes_str = gcmd.get("NOTES")

        try:
            return [
                (float(pair[0]), float(pair[1])) if len(pair) == 2 else (0.0, float(pair[0]))
                for note in notes_str.strip().split(" ")
                for pair in [note.split(":", maxsplit=1)]
            ]
        except:
            msg = "Unable to parse notes."
            logging.exception(f"[tone_player] {msg}\n{notes_str}")
            raise gcmd.error(msg)


def load_config(config):
    return TonePlayer(config)
