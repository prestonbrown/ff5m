## The AD5X toolhead filament sensor.
##
##     [ifs_toolhead_sensor toolhead]
##     adc_name: temperature_sensor filamentValue
##     pause_on_runout: True
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


class IfsToolheadSensor(ifs_sensor_base.IfsSensorBase):
    def __init__(self, config):
        ifs_sensor_base.IfsSensorBase.__init__(self, config)
        self.adc_name = config.get("adc_name", DEFAULT_ADC)

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

    def _adc(self):
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
        ## klipper returns (value, timestamp) in that order. zmod unpacks it
        ## the other way round and compensates further down; do not copy that.
        value, _timestamp = channel.get_last_value()
        return value

    def read_present(self):
        value = self._adc()
        self.last_value = value
        self.last_state = self.classifier.classify(value)
        if value is None:
            return None
        return self.classifier.has_filament(value)

    def cmd_IFS_SENSOR_VALUE(self, gcmd):
        value = self._adc()
        if value is None:
            gcmd.respond_info("%s: no reading from ADC %r"
                              % (self.name, self.adc_name))
            return
        gcmd.respond_info("%s: %s" % (self.name, self.classifier.describe(value)))


def load_config_prefix(config):
    return IfsToolheadSensor(config)
