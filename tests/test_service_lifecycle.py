## Behavioral tests for the Forge-X service lifecycle.
##
## Copyright (C) 2026, Alexander K <https://github.com/drA1ex>
##
## This file may be distributed under the terms of the GNU GPLv3 license

import os
import pathlib
import subprocess
import tempfile
import unittest


ROOT = pathlib.Path(__file__).parents[1]
START = ROOT / ".root" / "start.sh"
STOP = ROOT / ".root" / "stop.sh"
ROOT_SERVICE = ROOT / ".shell" / "S99root"


class ServiceLifecycleTest(unittest.TestCase):
    def _start_calls(self, display_mode):
        with tempfile.TemporaryDirectory() as directory:
            directory = pathlib.Path(directory)
            service_dir = directory / "services"
            service_dir.mkdir()
            calls = directory / "calls"

            for name in ("S35tslib", "S45ntpd", "S65moonraker",
                         "S70httpd", "S80guppyscreen"):
                service = service_dir / name
                service.write_text(
                    "#!/bin/sh\n"
                    "printf '%s %s\\n' \"$(basename \"$0\")\" \"$1\" "
                    ">> \"$SERVICE_CALLS\"\n",
                    encoding="utf-8",
                )
                service.chmod(0o755)

            config = directory / "zconf.sh"
            config.write_text(
                "#!/bin/sh\n"
                "case \"$3\" in\n"
                "  disable_moonraker|disable_web) echo 1 ;;\n"
                "  display) echo \"$DISPLAY_MODE\" ;;\n"
                "  *) echo \"$4\" ;;\n"
                "esac\n",
                encoding="utf-8",
            )
            config.chmod(0o755)

            script_text = START.read_text(encoding="utf-8")
            script_text = script_text.replace(
                'CFG_SCRIPT="/opt/config/mod/.shell/commands/zconf.sh"',
                f'CFG_SCRIPT="{config}"',
            )
            script_text = script_text.replace(
                "/opt/config/mod/.root/", f"{service_dir}/")
            script = directory / "start.sh"
            script.write_text(script_text, encoding="utf-8")

            env = os.environ.copy()
            env.update({
                "DISPLAY_MODE": display_mode,
                "SERVICE_CALLS": str(calls),
            })
            result = subprocess.run(
                ["bash", str(script)],
                env=env,
                text=True,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            return calls.read_text(encoding="utf-8").splitlines()

    def test_feather_start_restores_touch_input(self):
        self.assertEqual(
            self._start_calls("FEATHER"),
            ["S35tslib start", "S45ntpd start"],
        )

    def test_guppy_starts_touch_before_the_screen_process(self):
        self.assertEqual(
            self._start_calls("GUPPY"),
            ["S35tslib start", "S45ntpd start", "S80guppyscreen start"],
        )

    def test_headless_does_not_start_touch_input(self):
        self.assertEqual(self._start_calls("HEADLESS"), ["S45ntpd start"])

    def test_touch_input_stops_after_the_other_buildroot_services(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = pathlib.Path(directory)
            service_dir = directory / "services"
            service_dir.mkdir()
            calls = directory / "calls"
            for name in ("S35tslib", "S45ntpd", "S65moonraker",
                         "S70httpd", "S80guppyscreen"):
                service = service_dir / name
                service.write_text(
                    "#!/bin/sh\n"
                    "printf '%s %s\\n' \"$(basename \"$0\")\" \"$1\" "
                    ">> \"$SERVICE_CALLS\"\n",
                    encoding="utf-8")
                service.chmod(0o755)

            script = directory / "stop.sh"
            script.write_text(
                STOP.read_text(encoding="utf-8").replace(
                    "/opt/config/mod/.root/", f"{service_dir}/"),
                encoding="utf-8")
            env = os.environ.copy()
            env["SERVICE_CALLS"] = str(calls)

            result = subprocess.run(
                ["bash", str(script)], env=env, text=True,
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, check=False)

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(calls.read_text(encoding="utf-8").splitlines(), [
                "S80guppyscreen stop",
                "S65moonraker stop",
                "S70httpd stop",
                "S45ntpd stop",
                "S35tslib stop",
            ])

    def test_init_kill_path_publishes_shutdown_before_service_stop(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = pathlib.Path(directory)
            calls = directory / "calls"
            printer_fifo = directory / "printer"
            os.mkfifo(printer_fifo)
            common = directory / "common.sh"
            common.write_text(
                "MOD=/forge-x\n"
                "NOT_FIRST_LAUNCH_F=/nonexistent/not-first\n"
                "CUSTOM_BOOT_F=/nonexistent/custom-boot\n"
                "printer_command() { echo marker >> \"$SERVICE_CALLS\"; }\n"
                "chroot() { echo chroot >> \"$SERVICE_CALLS\"; }\n"
                "ps() { echo '1 root klippy.py'; }\n"
                "logged() { cat; }\n",
                encoding="utf-8")
            source = ROOT_SERVICE.read_text(encoding="utf-8")
            source = source.replace(
                "source /opt/config/mod/.shell/common.sh",
                f'source "{common}"')
            source = source.replace("/tmp/printer", str(printer_fifo))

            def run(name):
                script = directory / name
                script.write_text(source, encoding="utf-8")
                env = os.environ.copy()
                env["SERVICE_CALLS"] = str(calls)
                return subprocess.run(
                    ["bash", str(script), "stop"], env=env, text=True,
                    stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE, check=False)

            result = run("K99root")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(
                calls.read_text(encoding="utf-8").splitlines(),
                ["marker", "chroot"])

            calls.write_text("", encoding="utf-8")
            result = run("S99root")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(
                calls.read_text(encoding="utf-8").splitlines(), ["chroot"])


if __name__ == "__main__":
    unittest.main()
