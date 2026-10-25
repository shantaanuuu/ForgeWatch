# ForgeWatch AI

> **Autonomous Industrial Safety, Adaptive Capacity SCADA & 3D Digital Twin Platform**  
> Powered by Dual ESP32 MicroPython Nodes, Scikit-Learn Predictive Analytics, and WebGL 3D Visualization.

---

## Overview

**ForgeWatch AI** is an enterprise-grade industrial monitoring and safety automation system designed for smart manufacturing plants, metal forging bays, and high-precision CNC facilities.

Unlike traditional static headcount counters, ForgeWatch AI dynamically modulates permissible workforce occupancy per zone between **5 and 10 workers** in real-time, factoring in:
- **Thermal Ergonomics** (NOAA / OSHA Heat Index calculated from ambient Temperature and Relative Humidity).
- **Sub-Standard Illumination Hazards** (optical lux monitoring via LDR ADC).
- **Physical Access Control** (high-frequency RFID entry turnstiles and capacitive touch egress points).
- **Edge Computing Fallbacks** (MicroPython nodes maintain local environmental calculations even during network disconnection).

The system features a **Three.js WebGL 3D Digital Twin**, an **AI Predictive Ridge Regression Engine**, **Web Speech voice synthesis**, and hardware-synchronized multi-zone alarms.

---

## Key Features

- **Interactive 3D WebGL Digital Twin**:
  - Real-time 3D factory visualization powered by Three.js.
  - Realistic industrial assets: Heated Blast Furnace with core thermal glow, active Articulated Robotic Arm, CNC Milling station, and RFID Turnstile portals.
  - Dynamic 3D Personnel Avatars that spawn, position, and bob in 3D space matching live occupancy counts.
  - Multi-angle camera controls (Isometric 3D, Top-Down Ortho Blueprint, Zone 1 Focus, Zone 2 Focus, and Reset View).

- **Adaptive Environmental Capacity Engine (5 to 10 Scale)**:
  - Dynamically recalculates allowed workforce density per zone to prevent heat exhaustion and overcrowding.
  - Independent per-zone limits: Zone 1 (Furnace Bay) and Zone 2 (Machining Bay) scale their limits independently based on dedicated sensor pods.
  - Transparent penalty audit reporting (e.g. `Severe Thermal Load: -2 Workers`).

- **Scikit-Learn ML Occupancy Forecasting**:
  - Weighted Ridge Regression trained on chronological arrival and egress pulses.
  - Multi-horizon forecasts for **+5 min, +10 min, +15 min, and +30 min**.
  - Dynamic Crowd Surge & Anomaly Detection radar with confidence scoring.

- **Dual ESP32 Distributed Edge Hardware**:
  - **Board 1 (Node 1)**: RC522 RFID reader (SPI), capacitive touch exit sensor, DHT11 temp/humidity, analog LDR, 2000 Hz passive buzzer siren, status LEDs.
  - **Board 2 (Node 2)**: Capacitive touch exit sensor, DHT11 temp/humidity, analog LDR, status LEDs.
  - **Remote Exit Beep Synchronization**: Touching Board 2 exit instantly triggers the audio confirmation buzzer on Board 1 via server event relay.
  - Offline event queueing with unique cryptographic event IDs (`node:boot:seq`) preventing double-counting and loss of data during network drops.

- **Emergency Evacuation Drill (Master SOS Mode)**:
  - One-click factory-wide emergency siren mode.
  - Flashing visual strobe, live evacuation headcount muster roll, and hardware alarm activation.

- **Enterprise Auditing & Compliance**:
  - One-click OSHA-compliant Safety Audit Trail export in CSV format.
  - Personnel registry mapping RFID UIDs to employee badges, names, and operational stations.

---

## System Architecture

```
                      +---------------------------------------+
                      |       FORGEWATCH AI FASTAPI SERVER    |
                      |   - Adaptive Dynamic Capacity Engine  |
                      |   - Scikit-Learn Ridge Regressor      |
                      |   - SQLite WAL Telemetry Storage      |
                      +-------------------+-------------------+
                                          |
                +-------------------------+-------------------------+
                |                                                   |
      HTTP / LAN Telemetry                                HTTP / LAN Telemetry
  (Idempotent Retry Queues)                           (Idempotent Retry Queues)
                |                                                   |
                v                                                   v
     +--------------------+                              +--------------------+
     |   ESP32 NODE 1     |                              |   ESP32 NODE 2     |
     | - RFID Entry Turnstile                            | - Touch Exit 2     |
     | - Touch Exit 1     |                              | - DHT11 (Temp/Hum) |
     | - DHT11 (Temp/Hum) |                              | - LDR (Optical)    |
     | - LDR (Optical)    |                              | - Status LEDs      |
     | - PWM Siren Buzzer |                              +--------------------+
     | - Status LEDs      |
     +--------------------+
```

---

## Hardware Pinout Configuration

### Board 1 (Node 1 - Heavy Forge Bay)
| Component | ESP32 GPIO Pin | Description / Notes |
|---|---|---|
| **RC522 SCK** | GPIO 33 | SoftSPI Clock |
| **RC522 MOSI** | GPIO 32 | SoftSPI Master Out Slave In |
| **RC522 MISO** | GPIO 35 | SoftSPI Master In Slave Out (Input only) |
| **RC522 SDA/SS** | GPIO 26 | SoftSPI Chip Select |
| **RC522 RST** | GPIO 25 | Hardware Reset |
| **Touch Sensor** | GPIO 34 | Active HIGH capacitive touch (Exit 1) |
| **DHT11 Sensor** | GPIO 14 | Ambient Temperature & Humidity |
| **LDR Sensor** | GPIO 39 (VN) | Analog optical light sensor (ADC 11dB) |
| **Passive Buzzer** | GPIO 27 | PWM Siren & Confirmation Beep |
| **Red Alarm LED** | GPIO 13 | Overcrowding / Hazard Flasher |
| **Green Normal LED**| GPIO 12 | Normal System Status Indicator |
| **Onboard LED** | GPIO 2 | Night-light / LDR Indicator |

### Board 2 (Node 2 - Precision CNC Bay)
| Component | ESP32 GPIO Pin | Description / Notes |
|---|---|---|
| **Touch Sensor** | GPIO 26 | Active HIGH capacitive touch (Exit 2) |
| **DHT11 Sensor** | GPIO 27 | Ambient Temperature & Humidity |
| **LDR Sensor** | GPIO 34 | Analog optical light sensor (ADC 11dB) |
| **Red Alarm LED** | GPIO 13 | Overcrowding / Hazard Flasher |
| **Green Normal LED**| GPIO 12 | Normal System Status Indicator |
| **Onboard LED** | GPIO 2 | Night-light / LDR Indicator |

---

## Installation & Setup

### 1. Prerequisites
- Python 3.10 or higher
- Git
- Two ESP32 Development Boards flashed with MicroPython
- Dedicated Wi-Fi Network or Local Hotspot

### 2. Clone Repository
```bash
git clone https://github.com/shantaanuuu/ForgeWatch.git
cd ForgeWatch
```

### 3. Setup Python Virtual Environment
**Windows (PowerShell):**
```powershell
py -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
$env:FORGEWATCH_KEY="demo-change-me"
python -m uvicorn server.app:app --host 0.0.0.0 --port 8000
```

**Linux / macOS:**
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export FORGEWATCH_KEY='demo-change-me'
python -m uvicorn server.app:app --host 0.0.0.0 --port 8000
```

Access the live dashboard at `http://localhost:8000` (or `http://<SERVER_LAN_IP>:8000` from mobile devices on the same network).

### 4. Flash ESP32 Firmware
1. Open Thonny IDE.
2. Configure **Interpreter → MicroPython (ESP32)**.
3. Edit `SERVER_IP` and Wi-Fi credentials in `esp32/board1_main.py` and `esp32/board2_main.py` to match your local laptop IP.
4. Save `esp32/board1_main.py` as `main.py` on Board 1.
5. Save `esp32/board2_main.py` as `main.py` on Board 2.
6. Reset both boards.

---

## REST API Reference

| Endpoint | Method | Description |
|---|---|---|
| `/` | `GET` | Serves the interactive 3D WebGL Dashboard. |
| `/api/ingest` | `POST` | Hardware telemetry ingestion endpoint with event deduplication. |
| `/api/state` | `GET` | Real-time plant metrics, dynamic capacities, and safety scores. |
| `/api/history` | `GET` | 60-minute historical sensor readings for live charts. |
| `/api/analytics` | `GET` | Scikit-Learn ML forecasts (+5m, +10m, +15m, +30m) & anomaly scores. |
| `/api/capacity` | `POST` | Adjusts base design capacity threshold dynamically. |
| `/api/emergency/toggle` | `POST` | Triggers / clears the Master Emergency Evacuation Drill. |
| `/api/export/audit.csv` | `GET` | Streams downloadable OSHA Safety & Attendance Audit Trail. |

---

## Demonstration Walkthrough

1. **Nominal State**: Open dashboard on laptop or phone. Verify both Node 1 and Node 2 report `ONLINE`.
2. **Access Ingress**: Scan RFID tag `F35B6EE2` at Turnstile Gate 1. Zone 1 count increments by +1; Board 1 buzzer confirms with a crisp beep; 3D worker model spawns in the 3D Forge room; voice synthesizer announces the worker name.
3. **Zone 2 Ingress**: Scan RFID tag `7D69E13F`. Zone 2 count increments by +1; 3D worker spawns in the Robotic Machining room.
4. **Touch Egress**: Touch and release capacitive sensor on Board 1 (Zone 1 -1) or Board 2 (Zone 2 -1). Notice Board 1 buzzer sounds confirmation even for remote Zone 2 exits!
5. **Adaptive Capacity**: Observe the live **Adaptive Environmental Capacity Engine** card. If heat index rises (or upon applying warm air to DHT11), permissible capacity automatically drops, alerting operators before hazardous crowd accumulation occurs.
6. **Evacuation Drill**: Click `EMERGENCY SOS` on the dashboard. Immediate audio-visual sirens fire plant-wide, activating revolving beacons and muster roll tracking.

---

## License
MIT License. Built for industrial IoT safety demonstrations and smart factory research.
