# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

This is a CircuitPython-based IoT fan controller(vornado-633dc) designed for Raspberry Pi Pico W. The system controls a fan's speed via PWM and communicates with external systems through MQTT over WiFi. It monitors a potentiometer for local input and publishes values to MQTT topics.

## Architecture

### Core Components
- **FanController**: Manages PWM-based fan speed control with async event-driven updates
- **MQTT Client**: Handles bidirectional communication for remote control and status reporting
- **WiFi Manager**: Maintains connection with automatic reconnection on failures
- **Potentiometer Monitor**: Tracks analog input changes and publishes to MQTT(optional)

### Async Task Structure
The application runs 4 concurrent async tasks:
- MQTT client loop for message processing
- WiFi connection monitoring and auto-reconnection
- Potentiometer value monitoring and publishing
- Fan speed control with PWM duty cycle updates

### Hardware Configuration
- Fan control: GPIO 22 (PWM output)
- Potentiometer: GPIO 28 (ADC input)
- WiFi: Built-in Raspberry Pi Pico W radio

## Environment Configuration

Configuration is handled through `settings.toml`:
```toml
BOARD_NAME = "device-name"
CIRCUITPY_WIFI_SSID = "wifi-network"
CIRCUITPY_WIFI_PASSWORD = "wifi-password"
MQTT_BROKER_IP = "broker-ip"
MQTT_BROKER_PORT = 1883
```

## MQTT Topics
- `{BOARD_NAME}/feeds/speed` - Receives fan speed commands (0-100%)
- `{BOARD_NAME}/feeds/knob` - Publishes potentiometer readings
- `{BOARD_NAME}/feeds/onoff` - Receives on/off commands

## Development and Deployment

### Running the Code
1. Copy `main.py` and `settings.toml` to the Raspberry Pi Pico W root directory
2. Configure `settings.toml` with your WiFi and MQTT broker details
3. The code runs automatically on device startup

### No Build Process
This is a CircuitPython project that runs directly on the microcontroller without compilation. Code changes take effect immediately when files are saved to the device.

### Dependencies
All required libraries are part of the CircuitPython bundle:
- `adafruit_minimqtt` - MQTT client
- `asyncio` - Async task management
- Hardware interfaces: `pwmio`, `analogio`, `wifi`, `socketpool`

### VS Code Configuration
The project includes CircuitPython-specific VS Code settings with Pylance language server configured for microcontroller development, including appropriate stub paths for CircuitPython libraries.