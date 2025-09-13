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

# MQTT topics are now managed inside MqttManager class

class CircuitBreaker:
    """
    Circuit Breaker pattern implementation for fault tolerance.
    States: CLOSED (normal) -> OPEN (fault) -> HALF_OPEN (testing)
    """
    def __init__(self, failure_threshold=3, recovery_timeout=60):
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.failure_count = 0
        self.last_failure_time = 0
        self.state = "closed"  # closed, open, half_open

    async def call(self, func, *args, **kwargs):
        """Execute function through circuit breaker"""
        if self.state == "open":
            if self._should_attempt_reset():
                self.state = "half_open"
                print(f"Circuit breaker transitioning to HALF_OPEN")
            else:
                raise Exception("Circuit breaker is OPEN")

        try:
            result = await func(*args, **kwargs)
            self._on_success()
            return result
        except Exception as e:
            self._on_failure()
            raise e

    def _on_success(self):
        """Handle successful execution"""
        if self.state == "half_open":
            print(f"Circuit breaker recovered, transitioning to CLOSED")
        self.failure_count = 0
        self.state = "closed"

    def _on_failure(self):
        """Handle failed execution"""
        self.failure_count += 1
        self.last_failure_time = time.monotonic()

        if self.failure_count >= self.failure_threshold:
            if self.state != "open":
                print(f"Circuit breaker OPENED after {self.failure_count} failures")
            self.state = "open"

    def _should_attempt_reset(self):
        """Check if enough time has passed to attempt reset"""
        return (time.monotonic() - self.last_failure_time) >= self.recovery_timeout

    def get_state(self):
        """Get current circuit breaker state"""
        return {
            "state": self.state,
            "failure_count": self.failure_count,
            "time_since_failure": time.monotonic() - self.last_failure_time if self.last_failure_time > 0 else 0
        }

class BaseConnectionManager:
    def __init__(self, connection_type, max_retries=5, base_delay=1.0, max_delay=60.0):
        self.connection_type = connection_type
        self.max_retries = max_retries
        self.base_delay = base_delay
        self.max_delay = max_delay
        self.retry_count = 0
        self.last_retry_time = 0

        # Circuit breaker for connection protection
        self.circuit_breaker = CircuitBreaker(
            failure_threshold=3,
            recovery_timeout=60
        )

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
                # Check circuit breaker state before attempting connection
                cb_state = self.circuit_breaker.get_state()
                if cb_state["state"] == "open":
                    print(f"{self.connection_type} circuit breaker is OPEN, skipping connection attempt")
                    await asyncio.sleep(min(cb_state["time_since_failure"], self.max_delay))
                    continue

                # Attempt connection through circuit breaker
                result = await self.circuit_breaker.call(connect_func, *args, **kwargs)

                if result:
                    print(f"{self.connection_type} connected successfully")
                    self.reset_retries()
                    return True
                else:
                    # Connection function returned False
                    raise Exception(f"{self.connection_type} connection returned False")

            except Exception as e:
                print(f"{self.connection_type} connection failed: {e}")

            await self.wait_before_retry()

    def get_circuit_breaker_info(self):
        """Get circuit breaker status information"""
        cb_state = self.circuit_breaker.get_state()
        return f"{self.connection_type} Circuit Breaker: {cb_state['state'].upper()} " \
               f"(failures: {cb_state['failure_count']}, " \
               f"time since failure: {cb_state['time_since_failure']:.1f}s)"

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

class MqttManager(BaseConnectionManager):
    def __init__(self, broker_ip, port, fan_controller, board_name,
                 max_retries=3, base_delay=2.0, max_delay=30.0):
        super().__init__("MQTT", max_retries, base_delay, max_delay)

        self.broker_ip = broker_ip
        self.port = port
        self.fan_controller = fan_controller
        self.board_name = board_name

        # MQTT topics
        self.topics = {
            'speed': f"{board_name}/feeds/speed",
            'knob': f"{board_name}/feeds/knob",
            'onoff': f"{board_name}/feeds/onoff"
        }

        self.mqtt_client = None
        self._is_running = False

        # Adaptive loop variables
        self.consecutive_idle = 0
        self.max_consecutive = 100
        self.min_sleep = 0.001
        self.max_sleep = 0.01

    def setup_client(self, socket_pool):
        """Initialize MQTT client with SSL and callbacks"""
        self.mqtt_client = MQTT.MQTT(
            broker=self.broker_ip,
            port=self.port,
            socket_pool=socket_pool,
            ssl_context=ssl.create_default_context(),
            user_data=self.fan_controller,
        )

        # Set up callbacks
        self.mqtt_client.on_connect = self._on_connect
        self.mqtt_client.on_disconnect = self._on_disconnect
        self.mqtt_client.on_message = self._on_message

        # Add activity tracking
        self.mqtt_client._last_message_time = 0

    def _on_connect(self, client, userdata, flags, rc):
        """Handle MQTT connection"""
        client.user_data = userdata
        print(f"Connected to MQTT Broker! Subscribing to topics...")
        client.subscribe(self.topics['onoff'])
        client.subscribe(self.topics['speed'])

    def _on_disconnect(self, client, userdata, rc):
        """Handle MQTT disconnection"""
        print("Disconnected from MQTT Broker!")

    def _on_message(self, client, topic, message):
        """Handle incoming MQTT messages with activity tracking"""
        client._last_message_time = time.monotonic()

        print(f"New message on topic {topic}: {message}")

        if topic == self.topics['speed']:
            try:
                speed = float(message)
                self.fan_controller.set_speed(speed)
            except ValueError:
                print(f"Invalid speed value: {message}")

    async def connect_to_mqtt(self):
        """Connect to MQTT broker"""
        try:
            print(f"Connecting to {self.mqtt_client.broker}:{self.mqtt_client.port}...")
            self.mqtt_client.connect()
            print("Connected to MQTT broker")
            return True
        except Exception as e:
            print(f"Failed to connect to MQTT broker: {e}")
            return False

    async def reconnect_mqtt(self):
        """Reconnect to MQTT broker"""
        try:
            self.mqtt_client.reconnect()
            print("MQTT reconnected successfully")
            return True
        except Exception as e:
            print(f"Failed to reconnect MQTT: {e}")
            return False

    async def ensure_connection(self):
        """Ensure MQTT connection with retry"""
        return await self.connect_with_retry(self.connect_to_mqtt)

    async def publish_potentiometer(self, value):
        """Publish potentiometer value"""
        try:
            self.mqtt_client.publish(self.topics['knob'], str(value), retain=True)
        except Exception as e:
            print(f"Failed to publish potentiometer value: {e}")

    async def run_adaptive_loop(self):
        """Run adaptive MQTT client loop"""
        if not self.mqtt_client:
            print("MQTT client not initialized")
            return

        self._is_running = True

        while self._is_running:
            try:
                loop_start_time = time.monotonic()
                self.mqtt_client.loop()

                # Check if there was recent message activity (within last 100ms)
                time_since_last_message = loop_start_time - self.mqtt_client._last_message_time

                if time_since_last_message < 0.1:  # Recent activity within 100ms
                    # Active period - use minimal sleep for low latency
                    self.consecutive_idle = 0
                    sleep_time = self.min_sleep
                else:
                    # Idle period - gradually increase sleep time
                    self.consecutive_idle += 1
                    # Progressive sleep: 1ms -> 2ms -> 3ms ... -> 10ms
                    sleep_factor = min(self.consecutive_idle, self.max_consecutive) / self.max_consecutive
                    sleep_time = self.min_sleep + (self.max_sleep - self.min_sleep) * sleep_factor

                await asyncio.sleep(sleep_time)

            except Exception as e:
                print(f"MQTT loop error: {e}")
                self.consecutive_idle = 0
                await asyncio.sleep(1.0)  # Longer sleep on error

    def stop(self):
        """Stop the MQTT manager"""
        self._is_running = False
        if self.mqtt_client:
            self.mqtt_client.disconnect()

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


# MQTT loop logic is now handled inside MqttManager.run_adaptive_loop()

async def check_connections(wifi_manager, mqtt_manager):
    while True:
        await asyncio.sleep(WIFI_CHECK_INTERVAL_SEC)

        # Log circuit breaker states periodically
        print(f"Connection Status Check:")
        print(f"  {wifi_manager.get_circuit_breaker_info()}")
        print(f"  {mqtt_manager.get_circuit_breaker_info()}")

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
        else:
            print(f"WiFi connection failed, skipping MQTT check")

async def monitor_potentiometer(mqtt_manager, pin):
    potentiometer = AnalogIn(pin)
    previous_potentiometer_value = potentiometer.value
    while True:
        await asyncio.sleep(POTENTIOMETER_CHECK_INTERVAL_SEC)
        current_potentiometer_value = potentiometer.value
        if abs(current_potentiometer_value - previous_potentiometer_value) >= POTENTIOMETER_THRESHOLD:
            print(f"Sending potentiometer value: {current_potentiometer_value}")
            await mqtt_manager.publish_potentiometer(current_potentiometer_value)
        previous_potentiometer_value = current_potentiometer_value

# MQTT callback functions are now handled inside MqttManager class

async def main():
    # Create connection managers
    wifi_manager = WiFiConnectionManager()

    # Initial WiFi connection
    await wifi_manager.ensure_connection()

    fan_controller = FanController(FAN_PIN)

    # Create MQTT manager
    mqtt_manager = MqttManager(
        broker_ip=MQTT_BROKER_IP,
        port=MQTT_BROKER_PORT,
        fan_controller=fan_controller,
        board_name=BOARD_NAME
    )

    # Setup MQTT client
    socket_pool = socketpool.SocketPool(wifi.radio)
    mqtt_manager.setup_client(socket_pool)

    # Initial MQTT connection
    await mqtt_manager.ensure_connection()

    tasks = [
        asyncio.create_task(mqtt_manager.run_adaptive_loop()),
        asyncio.create_task(check_connections(wifi_manager, mqtt_manager)),
        asyncio.create_task(monitor_potentiometer(mqtt_manager, POTENTIOMETER_PIN)),
        asyncio.create_task(fan_controller.run_fan_control()),
    ]

    try:
        await asyncio.gather(*tasks)
    except Exception as e:
        print(f"An error occurred: {e}")
    finally:
        for task in tasks:
            task.cancel()
        mqtt_manager.stop()
        fan_controller.pwm.deinit()

if __name__ == "__main__":
    asyncio.run(main())