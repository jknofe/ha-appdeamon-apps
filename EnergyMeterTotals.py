"""Aggregate OpenDTU inverter kWh into a single sensor.

Sums live yield sensors and a fixed legacy_kwh_offset (accumulated kWh from
decommissioned inverters that no longer report). Skips the tick silently when
any live sensor is unavailable.
"""
import appdaemon.plugins.hass.hassapi as hass

from app_helpers import parse_interval


# Bump before each deploy and grep the AppDaemon log for it to confirm the
# new file actually landed on the host (deploys are manual file copies).
VERSION = "2026-05-26-1"


class EnergyMeterTotals(hass.Hass):

    def initialize(self):
        a = self.args
        self.update_interval = parse_interval(a.get("update_interval", "5m"))
        self.sensors         = a.get("sensors", [])
        self.legacy_kwh      = float(a.get("legacy_kwh_offset", 0.0))
        self.run_every(self._tick, "now", self.update_interval)
        self.log(f"EnergyMeterTotals started (version: {VERSION})")

    def _tick(self, kwargs):
        total = self.legacy_kwh
        for entity_id in self.sensors:
            val = self.get_state(entity_id)
            if val in (None, "unknown", "unavailable"):
                return
            try:
                total += float(val)
            except (ValueError, TypeError):
                return
        state_str = repr(round(total, 1))
        if self.get_state("sensor.power_meter_solar_total") == state_str:
            return
        self.set_state("sensor.power_meter_solar_total", state=state_str, attributes={
            "state_class": "total_increasing",
            "unit_of_measurement": "kWh",
            "device_class": "energy",
            "friendly_name": "Power Meter Solar Total",
        })
