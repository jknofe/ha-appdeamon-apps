import requests
import appdaemon.plugins.hass.hassapi as hass

class PowerMeter(hass.Hass):
    def initialize(self):
        """Initialize the app and set up periodic polling."""
        self.log("PowerMeter App Started!")
        self.run_every(self.query_power_meters, "now", 2)  # Runs every 3 second

        self.entidy_id_garage = "sensor.fritz_dect_200_1_power"
        self.log(f"{self.get_entity(self.entidy_id_garage)}")

        self.power_ph_sum = 0.0
        self.power_garage = 0.0
        self.power_solar = 0.0
    
    def _small_change_ema_filter(self, cur_value, prev_value, alpha, threshold=25):
        """Simple Exponential Moving Average filter only on small changes."""
        change = abs(cur_value - prev_value)
        if change < threshold:
            return alpha * cur_value + (1 - alpha) * prev_value
        else:
            return cur_value
        
    def query_power_meters(self, kwargs):
        """Fetch data from both power meters and update sensors."""
        
        url_3em = "http://10.0.0.210/rpc/EM.GetStatus?id=0"
        url_1pm = "http://10.0.0.211/rpc/Switch.GetStatus?id=0"

        try:
            # Read data from Home Assistant sensor
            self.power_garage = float(self.get_state(self.entidy_id_garage))
            # self.log(f"Garage G={self.power_garage}W")
        except (ValueError, Exception) as e:
            # self.log(f"Error fetching sensor.fritz_dect_200_1_power: {e}")
            self.power_garage = 0.0

        # Query first URL (EM.GetStatus)
        try:
            response1 = requests.get(url_3em, timeout=1.25)
            data1 = response1.json()
            power_ph_a = float(data1.get("a_act_power", 0))
            power_ph_b = float(data1.get("b_act_power", 0))
            power_ph_c = float(data1.get("c_act_power", 0))

            # Update Home Assistant sensors
            #self.set_state("sensor.a_act_power", state=a_act_power, unit_of_measurement="W")
            #self.set_state("sensor.b_act_power", state=b_act_power, unit_of_measurement="W")
            #self.set_state("sensor.c_act_power", state=c_act_power, unit_of_measurement="W")

            #self.log(f"3EM: A={power_ph_a}W, B={power_ph_b}W, C={power_ph_c}W", G={power_garage}W")

        except (ValueError, Exception) as e:
            self.log(f"Error fetching 3EM.GetStatus: {e}")
            power_ph_a = 0.0
            power_ph_b = 0.0
            power_ph_c = 0.0

        # Query second URL (Switch.GetStatus)
        try:
            response2 = requests.get(url_1pm, timeout=1.25)
            data2 = response2.json()
            self.power_solar = float(data2.get("apower", 0))
            #self.log(f"1PM: S={power_solar}W")
        except (ValueError, Exception) as e:
            self.log(f"Error fetching 1PM.GetStatus: {e}")
            self.power_solar = 0.0

        # calculate phase sum
        ph_sum_act = power_ph_a + power_ph_b + power_ph_c + self.power_garage
        self.power_ph_sum = self._small_change_ema_filter(ph_sum_act, self.power_ph_sum, 0.6)
        
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
        # set state of new sensor
        self.set_state("sensor.power_consumption_new", state=self.power_con,
                        state_class="measurement",
                        unit_of_measurement="W",
                        device_class="power",
                        friendly_name="Power Consumption New")
        # self.log(f"P={round(self.power_ph_sum,1)}W, I={power_imp}W, E={power_exp}W, S={self.power_solar} C={self.power_con}W")