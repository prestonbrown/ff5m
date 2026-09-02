## The AD5X toolhead filament sensor.
##
##     [ifs_toolhead_sensor toolhead]
##     adc_name: temperature_sensor filamentValue
##     pause_on_runout: True
##
## Optionally, hand it the pin instead and it is sampled 20x more often:
##
##     [ifs_toolhead_sensor toolhead]
##     sensor_pin: eboard:PA3
##
## which requires removing `[temperature_sensor filamentValue]` from
## printer.base.cfg, because a pin can only be claimed once.
##
## This is NOT an IFS device. It is an analog input on the extruder board
## (eboard:PA3), declared in printer.cfg as a thermistor purely so klipper will
## sample the ADC. The AD5M's equivalent is a plain digital microswitch, which
## is why that printer needs no code here.
##
## LOW means filament PRESENT. The measurement itself - all three states, and
## why the empty figure was once recorded wrong - lives in ifs_sensor_logic
## beside the constants derived from it, and is deliberately not repeated here.
##
## Copyright (C) 2026, Preston Brown
##
## This file may be distributed under the terms of the GNU GPLv3 license

import logging

from . import ifs_sensor_base
from . import ifs_sensor_logic


DEFAULT_ADC = "temperature_sensor filamentValue"

## Sampling when we own the pin. klipper reports a thermistor every 0.300s
## (adc_temperature.REPORT_TIME), which is how this sensor is normally read -
## fine for temperature, coarse for a filament tip moving at 20-60 mm/s. At
## 0.300s a 1200 mm/min feed travels 6mm between samples and a 3600 one travels
## 18mm, against a transition zone measured at only 5-10mm wide. 0.015s is
## klipper's own figure for adc_button, and it takes the ADC out of the
## critical path entirely. What bounds overshoot after that is ifs.py's
## COMMAND_POLL_PAUSE - the 0.05s wait-loop that reads this sensor - so the
## figure is 1mm at 1200 mm/min and 3mm at 3600, not the 6mm and 18mm above.
## Six times better, not twenty: the remaining factor is that loop, not this.
REPORT_TIME = 0.015
SAMPLE_TIME = 0.001
SAMPLE_COUNT = 6


class IfsToolheadSensor(ifs_sensor_base.IfsSensorBase):
    def __init__(self, config):
        ifs_sensor_base.IfsSensorBase.__init__(self, config)
        self.adc_name = config.get("adc_name", DEFAULT_ADC)

        ## Optional, and opt-in for a reason: a pin can only be claimed once,
        ## and a stock printer.base.cfg already declares this one as
        ## `[temperature_sensor filamentValue]`. Setting sensor_pin means you
        ## have removed that section and handed us the pin instead - in return
        ## the sensor is sampled 20x more often. Left unset, nothing changes.
        self.sensor_pin = config.get("sensor_pin", None)
        self._direct_value = None

        ## Defaults are the measured AD5X values (see ifs_sensor_logic). They
        ## are config options because they describe one printer's hardware, and
        ## a rebuilt toolhead can move them.
        bands = ifs_sensor_logic.toolhead_bands(
            config.getfloat("present_max",
                            ifs_sensor_logic.AD5X_PRESENT_MAX, above=0.),
            config.getfloat("absent_min",
                            ifs_sensor_logic.AD5X_ABSENT_MIN, above=0.))
        self.classifier = ifs_sensor_logic.AnalogFilamentSensor(
            bands, fail_safe=config.getboolean("fail_safe", True))

        self._query_adc = None
        self._warned = False
        self.last_value = None
        self.last_state = None

        self.gcode.register_mux_command(
            "IFS_SENSOR_VALUE", "SENSOR", self.name,
            self.cmd_IFS_SENSOR_VALUE,
            desc="Report the raw ADC reading behind a toolhead filament sensor")

        ## Last, so a fallback has the whole object behind it.
        if self.sensor_pin is not None:
            self._claim_pin()

    def _claim_pin(self):
        """Take the ADC ourselves, or fall back to reading it the slow way.

        A pin can only be claimed once, and a stock `printer.base.cfg` declares
        this one as `[temperature_sensor filamentValue]`. Setting `sensor_pin`
        assumes that section has been stripped - but a firmware update can
        restore the file, and an init that did not run leaves it in place. A
        raise here would be klipper refusing to start, on a printer whose only
        symptom is a config it did not know it had. Degrading to the 0.300s
        reading loses precision; failing to boot loses the machine.
        """
        try:
            ppins = self.printer.lookup_object("pins")
            self._mcu_adc = ppins.setup_pin("adc", self.sensor_pin)
            self._mcu_adc.setup_adc_sample(SAMPLE_TIME, SAMPLE_COUNT)
            self._mcu_adc.setup_adc_callback(REPORT_TIME, self._adc_callback)
        except Exception as exc:
            logging.warning(
                "IFS sensor %s: cannot claim %s (%s); falling back to %r, "
                "which klipper reports every 0.300s",
                self.name, self.sensor_pin, exc, self.adc_name)
            self.sensor_pin = None
            return
        ## So IFS_SENSOR_VALUE and anything else reading through query_adc
        ## keeps working against the pin we took.
        query_adc = self.printer.lookup_object("query_adc", None)
        if query_adc is not None:
            query_adc.register_adc(self.name, self._mcu_adc)

    def _adc_callback(self, read_time, read_value):
        """klipper hands us every sample once we own the pin."""
        self._direct_value = read_value

    def _adc(self):
        ## Ours if we claimed the pin. None until the first callback arrives -
        ## which is "no reading yet", not "no filament", and read_present keeps
        ## those distinct so fail_safe cannot turn a cold start into a runout.
        if self.sensor_pin is not None:
            return self._direct_value
        if self._query_adc is None:
            self._query_adc = self.printer.lookup_object("query_adc", None)
        if self._query_adc is None:
            return None
        channel = self._query_adc.adc.get(self.adc_name)
        if channel is None:
            if not self._warned:
                logging.warning(
                    "IFS sensor %s: no ADC named %r; available: %s",
                    self.name, self.adc_name,
                    sorted(self._query_adc.adc.keys()))
                self._warned = True
            return None
        ## klipper returns (value, timestamp) in that order.
        value, _timestamp = channel.get_last_value()
        return value

    def read_present(self):
        value = self._adc()
        self.last_value = value
        self.last_state = self.classifier.classify(value)
        if value is None:
            return None
        return self.classifier.has_filament(value)

    ## GCODE_SAFE: _adc() answers None rather than raising when there is no
    ## ADC to read, and describing a float cannot fail.
    def cmd_IFS_SENSOR_VALUE(self, gcmd):
        value = self._adc()
        if value is None:
            gcmd.respond_info("%s: no reading from ADC %r"
                              % (self.name, self.adc_name))
            return
        gcmd.respond_info("%s: %s" % (self.name, self.classifier.describe(value)))


def load_config_prefix(config):
    return IfsToolheadSensor(config)
