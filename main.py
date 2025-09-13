import os
import asyncio
import ssl
import socketpool
import wifi
import adafruit_minimqtt.adafruit_minimqtt as MQTT
import board
import pwmio
from analogio import AnalogIn
import time
import random

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

class BaseConnectionManager:
    def __init__(self, connection_type, max_retries=5, base_delay=1.0, max_delay=60.0):
        self.connection_type = connection_type
        self.max_retries = max_retries
        self.base_delay = base_delay
        self.max_delay = max_delay
        self.retry_count = 0
        self.last_retry_time = 0

    def reset_retries(self):
        self.retry_count = 0

    def calculate_delay(self, retry_count):
        delay = min(self.base_delay * (2 ** retry_count), self.max_delay)
        jitter = random.uniform(0.8, 1.2)
        return delay * jitter

    def can_retry(self):
        if self.retry_count >= self.max_retries:
            current_time = time.monotonic()
            if current_time - self.last_retry_time >= self.max_delay:
                self.retry_count = 0
                return True
            return False
        return True

    async def wait_before_retry(self):
        if not self.can_retry():
            print(f"{self.connection_type} max retries ({self.max_retries}) reached. Waiting {self.max_delay}s...")
            await asyncio.sleep(self.max_delay)
            return

        if self.retry_count > 0:
            delay = self.calculate_delay(self.retry_count)
            print(f"{self.connection_type} retry {self.retry_count}/{self.max_retries} in {delay:.1f}s...")
            await asyncio.sleep(delay)

        self.retry_count += 1
        self.last_retry_time = time.monotonic()

    async def connect_with_retry(self, connect_func, *args, **kwargs):
        while True:
            try:
                result = await connect_func(*args, **kwargs)
                if result:
                    print(f"{self.connection_type} connected successfully")
                    self.reset_retries()
                    return True
            except Exception as e:
                print(f"{self.connection_type} connection failed: {e}")

            await self.wait_before_retry()

class WiFiConnectionManager(BaseConnectionManager):
    def __init__(self, max_retries=5, base_delay=1.0, max_delay=60.0):
        super().__init__("WiFi", max_retries, base_delay, max_delay)

    async def connect_to_wifi(self):
        try:
            if not wifi.radio.connected:
                wifi.radio.connect(WIFI_SSID, WIFI_PASSWORD)
                while not wifi.radio.connected:
                    await asyncio.sleep(1)
            print("Connected to WiFi")
            print("My IP address is", wifi.radio.ipv4_address)
            return True
        except Exception as e:
            print(f"Failed to connect to WiFi: {e}")
            return False

    async def ensure_connection(self):
        if not wifi.radio.connected:
            print("WiFi disconnected. Reconnecting...")
            return await self.connect_with_retry(self.connect_to_wifi)
        return True

class MqttConnectionManager(BaseConnectionManager):
    def __init__(self, mqtt_client, max_retries=3, base_delay=2.0, max_delay=30.0):
        super().__init__("MQTT", max_retries, base_delay, max_delay)
        self.mqtt_client = mqtt_client

    async def connect_to_mqtt(self):
        try:
            print(f"Connecting to {self.mqtt_client.broker}:{self.mqtt_client.port}...")
            self.mqtt_client.connect()
            print("Connected to MQTT broker")
            return True
        except Exception as e:
            print(f"Failed to connect to MQTT broker: {e}")
            return False

    async def reconnect_mqtt(self):
        try:
            self.mqtt_client.reconnect()
            print("MQTT reconnected successfully")
            return True
        except Exception as e:
            print(f"Failed to reconnect MQTT: {e}")
            return False

    async def ensure_connection(self):
        return await self.connect_with_retry(self.connect_to_mqtt)

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


async def run_mqtt_client_loop(mqtt_client):
    while True:
        try:
            mqtt_client.loop()
        except Exception as e:
            print(f"MQTT loop error: {e}")
        await asyncio.sleep(0)

async def check_connections(wifi_manager, mqtt_manager):
    while True:
        await asyncio.sleep(WIFI_CHECK_INTERVAL_SEC)

        # Check WiFi connection
        wifi_ok = await wifi_manager.ensure_connection()

        # If WiFi is working, ensure MQTT connection
        if wifi_ok:
            try:
                # Check if MQTT client needs reconnection
                if not hasattr(mqtt_manager.mqtt_client, '_sock') or mqtt_manager.mqtt_client._sock is None:
                    await mqtt_manager.connect_with_retry(mqtt_manager.reconnect_mqtt)
            except Exception as e:
                print(f"MQTT connection check failed: {e}")
                await mqtt_manager.connect_with_retry(mqtt_manager.reconnect_mqtt)

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
    # Create connection managers
    wifi_manager = WiFiConnectionManager()

    # Initial WiFi connection
    await wifi_manager.ensure_connection()

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

    # Create MQTT manager and initial connection
    mqtt_manager = MqttConnectionManager(mqtt_client)
    await mqtt_manager.ensure_connection()

    tasks = [
        asyncio.create_task(run_mqtt_client_loop(mqtt_client)),
        asyncio.create_task(check_connections(wifi_manager, mqtt_manager)),
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