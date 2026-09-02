## Per-channel filament presence for the AD5X IFS.
##
##     [ifs_channel_sensor lane1]
##     channel: 1
##
## Reads the IFS board's silk bitmask through the [ifs] object rather than any
## wire of its own, so four of these cost one F13 poll between them. The AD5M
## has no counterpart - it has no IFS.
##
## Copyright (C) 2026, Preston Brown
##
## This file may be distributed under the terms of the GNU GPLv3 license

from . import ifs_sensor_base
from . import ifs_sensor_logic


class IfsChannelSensor(ifs_sensor_base.IfsSensorBase):
    def __init__(self, config):
        ifs_sensor_base.IfsSensorBase.__init__(self, config)
        self.channel = config.getint("channel", minval=1, maxval=8)
        self.sensor = ifs_sensor_logic.ChannelFilamentSensor(self.channel)
        self._ifs = None

    def _lookup(self):
        if self._ifs is None:
            ## Deferred rather than looked up in __init__: config section order
            ## decides construction order, and [ifs] may not exist yet.
            self._ifs = self.printer.lookup_object("ifs", None)
        return self._ifs

    def read_present(self):
        ifs = self._lookup()
        if ifs is None:
            return None
        return self.sensor.has_filament(ifs.latest_status())


def load_config_prefix(config):
    return IfsChannelSensor(config)
