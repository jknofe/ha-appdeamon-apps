import requests
import appdaemon.plugins.hass.hassapi as hass

class PowerMeter(hass.Hass):
    def initialize(self):
        """Initialize the app and set up periodic polling."""
        self.log("PowerMeter App Started!")

        # Entity IDs
        self.entidy_id_garage = "sensor.fritz_dect_200_1_power"
        self.log(f"{self.get_entity(self.entidy_id_garage)}")

        # URLs for the power meters
        self.url_3em = "http://10.0.0.210/rpc/EM.GetStatus?id=0"
        self.url_1pm = "http://10.0.0.214/rpc/PM1.GetStatus?id=0"

        # request timout
        self.timeout = 1.25

        # initialize variables
        self.power_con = 0.0
        self.power_con_flt = 0.0
        self.power_garage = 0.0
        self.power_ph_a = 0.0
        self.power_ph_b = 0.0
        self.power_ph_c = 0.0
        self.power_ph_sum = 0.0
        self.power_solar = 0.0

        #
        self._is_running = False

        # after initialization, start polling
        self.run_every(self.query_power_meters, "now", 3)  # Runs every 3 second

    def _small_change_ema_filter(self, cur_value, prev_value, alpha=0.6, threshold=60):
        """Simple Exponential Moving Average filter only on small changes."""
        change = abs(cur_value - prev_value)
        if change < threshold:
            return alpha * cur_value + (1 - alpha) * prev_value
        else:
            return cur_value

    def query_power_meters(self, kwargs):
        """Fetch data from both power meters and update sensors."""

        # check if query is already running
        if self._is_running:
            self.log("Query already running, skipping this run")
            return

        # run the query and update sensors
        try:
            self._is_running = True

            try:
                # Read data from Home Assistant sensor
                self.power_garage = float(self.get_state(self.entidy_id_garage))
                # self.log(f"Garage G={self.power_garage}W")
            except (ValueError, Exception) as e:
                # self.log(f"Error fetching sensor.fritz_dect_200_1_power: {e}")
                self.power_garage = 0.0

            # Query first URL (EM.GetStatus)
            try:
                response1 = requests.get(self.url_3em, timeout=self.timeout)
                data1 = response1.json()
                self.power_ph_a = float(data1.get("a_act_power", 0))
                self.power_ph_b = float(data1.get("b_act_power", 0))
                self.power_ph_c = float(data1.get("c_act_power", 0))
                #self.log(f"3EM: A={self.power_ph_a}W, B={self.power_ph_b}W, C={self.power_ph_c}W", G={power_garage}W")
            except Exception as e:
                self.log(f"Error fetching 3EM.GetStatus: {e}")

            # Query second URL (PM1.GetStatus)
            try:
                response2 = requests.get(self.url_1pm, timeout=self.timeout)
                data2 = response2.json()
                self.power_solar = float(abs((data2.get("apower", 0))))
                #self.log(f"1PM: S={power_solar}W")
            except Exception as e:
                self.log(f"Error fetching 1PM.GetStatus: {e}")
            # lightly filter low values
            self.power_solar= self._small_change_ema_filter(self.power_solar, self.power_solar, 0.5, 50)

            # calculate phase sum
            self.power_ph_sum = (
                self.power_ph_a + self.power_ph_b + self.power_ph_c + self.power_garage
            )

            # calculate power import/export
            if self.power_ph_sum > 0:
                power_imp = round(self.power_ph_sum, 1)
                power_exp = 0.0
            else:
                power_imp = 0.0
                power_exp = round(abs(self.power_ph_sum), 1)

            # caclulate power consumption
            if power_exp > 0:
                power_con = self.power_solar - power_exp
            else:
                power_con = self.power_solar + power_imp
            # round and limit power consumption to 0.0W
            self.power_con = max(0.0, round(power_con, 1))
            self.power_con_flt = self._small_change_ema_filter(self.power_con, self.power_con_flt, 0.25, 240)

            # set states of new sensors
            ha_sensor_mapping = {
                "sensor.power_consumption": (self.power_con, "Power Consumption"),
                "sensor.power_consunption_filtered": (round(self.power_con_flt, 1), "Power Consumption Filtered"),
                "sensor.power_import": (power_imp, "Power Import"),
                "sensor.power_export": (power_exp, "Power Export"),
                "sensor.power_solargen": (self.power_solar, "Power Solar Generation"),
            }
            # Update sensors
            for sensor_id, (state, friendly_name) in ha_sensor_mapping.items():
                self.set_state(sensor_id, state=state,
                    state_class="measurement",
                    unit_of_measurement="W",
                    device_class="power",
                    friendly_name=friendly_name)

            # self.log(f"P={round(self.power_ph_sum,1)}W, I={power_imp}W, E={power_exp}W, S={self.power_solar} C={self.power_con}W")
        except Exception as e:
            self.log(f"Error in query_power_meters: {e}")
        finally:
            self._is_running = False
