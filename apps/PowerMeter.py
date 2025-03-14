import requests
import appdaemon.plugins.hass.hassapi as hass

class PowerMeter(hass.Hass):
    def initialize(self):
        """Initialize the app and set up periodic polling."""
        self.log("PowerMeter App Started!")
        self.run_every(self.query_power_meters, "now", 3)  # Runs every 3 second

    def query_power_meters(self, kwargs):
        """Fetch data from both power meters and update sensors."""
        url1 = "http://10.0.0.210/rpc/EM.GetStatus?id=0"
        url2 = "http://10.0.0.211/rpc/Switch.GetStatus?id=0"

        # Query first URL (EM.GetStatus)
        try:
            response1 = requests.get(url1, timeout=2)
            data1 = response1.json()
            a_act_power = data1.get("a_act_power", 0)
            b_act_power = data1.get("b_act_power", 0)
            c_act_power = data1.get("c_act_power", 0)

            # Update Home Assistant sensors
            self.set_state("sensor.a_act_power", state=a_act_power, unit_of_measurement="W")
            self.set_state("sensor.b_act_power", state=b_act_power, unit_of_measurement="W")
            self.set_state("sensor.c_act_power", state=c_act_power, unit_of_measurement="W")

            self.log(f"Updated Power Sensors: A={a_act_power}W, B={b_act_power}W, C={c_act_power}W")

        except Exception as e:
            self.log(f"Error fetching EM.GetStatus: {e}")

        # Query second URL (Switch.GetStatus)
        try:
            response2 = requests.get(url2, timeout=2)
            data2 = response2.json()
            apower = data2.get("apower", 0)

            # Update Home Assistant sensor
            self.set_state("sensor.apower", state=apower, unit_of_measurement="W")

            self.log(f"Updated Switch Power Sensor: apower={apower}W")

        except Exception as e:
            self.log(f"Error fetching Switch.GetStatus: {e}")
