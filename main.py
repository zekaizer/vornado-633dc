import os
import asyncio
import ssl
import socketpool
import wifi
import adafruit_minimqtt.adafruit_minimqtt as MQTT
import board
import pwmio
from analogio import AnalogIn

# 상수 정의
BOARD_NAME = os.getenv("BOARD_NAME", "DefaultBoard")
WIFI_SSID = os.getenv('CIRCUITPY_WIFI_SSID')
WIFI_PASSWORD = os.getenv('CIRCUITPY_WIFI_PASSWORD')
MQTT_BROKER_IP = os.getenv("MQTT_BROKER_IP")
MQTT_BROKER_PORT = int(os.getenv("MQTT_BROKER_PORT", "1883"))
FAN_PIN = board.GP22
POTENTIOMETER_PIN = board.GP28_A2
PWM_FREQUENCY_HZ = 1000
WIFI_CHECK_INTERVAL_SEC = 10
POTENTIOMETER_CHECK_INTERVAL_SEC = 0.1
POTENTIOMETER_THRESHOLD = int(65535/100)

# MQTT 토픽 이름 설정
MQTT_TOPIC_SPEED = f"{BOARD_NAME}/feeds/speed"
MQTT_TOPIC_KNOB = f"{BOARD_NAME}/feeds/knob"
MQTT_TOPIC_ONOFF = f"{BOARD_NAME}/feeds/onoff"

class FanController:
    def __init__(self, pin):
        self.pin = pin
        self.speed_change_event = asyncio.Event()
        self.is_on = False
        self.current_speed_percent = 0.0
        self.target_speed_percent = 0.0
        self.pwm = pwmio.PWMOut(self.pin, frequency=PWM_FREQUENCY_HZ)
        self.set_speed(0)

    def set_speed(self, speed_percent: float):
        self.target_speed_percent = max(0, min(100, speed_percent))
        self.is_on = self.target_speed_percent > 0
        self.speed_change_event.set()

    async def run_fan_control(self):
        while True:
            await self.speed_change_event.wait()
            self.speed_change_event.clear()
            duty_cycle = int((self.target_speed_percent / 100) * 65535) if self.is_on else 0
            self.pwm.duty_cycle = duty_cycle
            print(f"{self.pin}: On {self.is_on}, speed {self.current_speed_percent:.1f}% -> {self.target_speed_percent:.1f}%, pwm.duty_cycle {duty_cycle}")
            self.current_speed_percent = self.target_speed_percent

async def connect_to_wifi():
    print("Connecting to WiFi")
    try:
        wifi.radio.connect(WIFI_SSID, WIFI_PASSWORD)
        while not wifi.radio.connected:
            await asyncio.sleep(1)
        print("Connected to WiFi")
        print("My IP address is", wifi.radio.ipv4_address)
    except Exception as e:
        print(f"Failed to connect to WiFi: {e}")

async def run_mqtt_client_loop(mqtt_client):
    while True:
        try:
            mqtt_client.loop()
        except Exception as e:
            print(f"MQTT loop error: {e}")
        await asyncio.sleep(0)

async def check_wifi_connection(mqtt_client):
    while True:
        await asyncio.sleep(WIFI_CHECK_INTERVAL_SEC)
        if not wifi.radio.connected:
            print("WiFi disconnected. Reconnecting...")
            await connect_to_wifi()
            try:
                mqtt_client.reconnect()
            except Exception as e:
                print(f"Failed to reconnect MQTT: {e}")

async def monitor_potentiometer(mqtt_client, pin):
    potentiometer = AnalogIn(pin)
    previous_potentiometer_value = potentiometer.value
    while True:
        await asyncio.sleep(POTENTIOMETER_CHECK_INTERVAL_SEC)
        current_potentiometer_value = potentiometer.value
        if abs(current_potentiometer_value - previous_potentiometer_value) >= POTENTIOMETER_THRESHOLD:
            print(f"Sending potentiometer value: {current_potentiometer_value}")
            try:
                mqtt_client.publish(MQTT_TOPIC_KNOB, str(current_potentiometer_value), retain=True)
            except Exception as e:
                print(f"Failed to publish potentiometer value: {e}")
        previous_potentiometer_value = current_potentiometer_value

def on_mqtt_message(client, topic, message):
    fan_controller: FanController = client.user_data
    print(f"New message on topic {topic}: {message}")
    if topic == MQTT_TOPIC_SPEED:
        try:
            speed = float(message)
            fan_controller.set_speed(speed)
        except ValueError:
            print(f"Invalid speed value: {message}")

def on_mqtt_connect(client, userdata, flags, rc):
    client.user_data = userdata
    print(f"Connected to MQTT Broker! Listening for topic changes on {MQTT_TOPIC_ONOFF}")
    client.subscribe(MQTT_TOPIC_ONOFF)
    client.subscribe(MQTT_TOPIC_SPEED)

def on_mqtt_disconnect(client, userdata, rc):
    print("Disconnected from MQTT Broker!")

async def main():
    await connect_to_wifi()

    fan_controller = FanController(FAN_PIN)

    socket_pool = socketpool.SocketPool(wifi.radio)
    mqtt_client = MQTT.MQTT(
        broker=MQTT_BROKER_IP,
        port=MQTT_BROKER_PORT,
        socket_pool=socket_pool,
        ssl_context=ssl.create_default_context(),
        user_data=fan_controller,
    )

    mqtt_client.on_connect = on_mqtt_connect
    mqtt_client.on_disconnect = on_mqtt_disconnect
    mqtt_client.on_message = on_mqtt_message

    print(f"Connecting to {mqtt_client.broker}:{mqtt_client.port}...")
    try:
        mqtt_client.connect()
    except Exception as e:
        print(f"Failed to connect to MQTT broker: {e}")
        return

    tasks = [
        asyncio.create_task(run_mqtt_client_loop(mqtt_client)),
        asyncio.create_task(check_wifi_connection(mqtt_client)),
        asyncio.create_task(monitor_potentiometer(mqtt_client, POTENTIOMETER_PIN)),
        asyncio.create_task(fan_controller.run_fan_control()),
    ]

    try:
        await asyncio.gather(*tasks)
    except Exception as e:
        print(f"An error occurred: {e}")
    finally:
        for task in tasks:
            task.cancel()
        mqtt_client.disconnect()
        fan_controller.pwm.deinit()

if __name__ == "__main__":
    asyncio.run(main())