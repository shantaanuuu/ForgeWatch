
# =====================================================
# FORGEWATCH AI - BOARD 2
# ZONE 2 EXIT + DHT11 + LDR + LED MONITORING
# =====================================================

import network
import socket
import time
import dht
import ujson

from machine import Pin, ADC

# ---------------- CONFIG ----------------

SSID = "TP-Link_4728"
PASSWORD = "sanskar@3515"

SERVER_IP = "192.168.1.104"
SERVER_PORT = 8000
DEVICE_KEY = "demo-change-me"

BASE_CAPACITY = 10
CAPACITY = 10
capacity = 8             # Dynamic environmental capacity for Zone 2 (5-10 range)
server_alert = None       # Live server alert
DARK_THRESHOLD = 1500

MIN_TOUCH_MS = 80

# ---------------- GPIO ----------------

red = Pin(13, Pin.OUT)
green = Pin(12, Pin.OUT)

touch = Pin(26, Pin.IN)

led = Pin(2, Pin.OUT)

sensor = dht.DHT11(
    Pin(27)
)

# Analog LDR must use GPIO34
ldr = ADC(Pin(34))
ldr.atten(ADC.ATTN_11DB)

# ---------------- INITIAL STATE ----------------

red.off()
green.on()
led.off()

count = 0
connected = False

temp = None
hum = None
light = None

boot = str(time.ticks_us())

seq = 0
pending = []

last_touch = touch.value()
touch_start = None

last_dht = time.ticks_ms() - 3000
last_ldr = time.ticks_ms() - 1000
last_send = time.ticks_ms() - 2000

last_wifi_attempt = 0

# ---------------- WIFI ----------------

wifi = network.WLAN(
    network.STA_IF
)

wifi.active(True)

def wifi_ok():

    global last_wifi_attempt

    if wifi.isconnected():
        return True

    now = time.ticks_ms()

    if time.ticks_diff(
        now, last_wifi_attempt
    ) < 5000:
        return False

    last_wifi_attempt = now

    try:
        wifi.connect(
            SSID,
            PASSWORD
        )
    except Exception as e:
        print("Wi-Fi:", e)

    return wifi.isconnected()


# ---------------- HTTP ----------------

def post(action=None, event_seq=0):

    body = ujson.dumps({
        "node": 2,
        "boot": boot,
        "seq": event_seq,
        "action": action,
        "temp": temp,
        "hum": hum,
        "ldr": light
    }).encode()

    s = None

    try:

        addr = socket.getaddrinfo(
            SERVER_IP,
            SERVER_PORT
        )[0][-1]

        s = socket.socket()

        s.settimeout(1)
        s.connect(addr)

        header = (
            "POST /api/ingest HTTP/1.0\r\n"
            "Host: %s\r\n"
            "X-Device-Key: %s\r\n"
            "Content-Type: application/json\r\n"
            "Content-Length: %d\r\n"
            "Connection: close\r\n\r\n"
            % (
                SERVER_IP,
                DEVICE_KEY,
                len(body)
            )
        ).encode()

        s.write(header)
        s.write(body)

        response = b""

        while True:

            part = s.recv(512)

            if not part:
                break

            response += part

            if len(response) > 5000:
                raise ValueError(
                    "Response too large"
                )

        status = response.split(
            b"\r\n", 1
        )[0]

        if b"200 OK" not in status:
            raise ValueError(
                "HTTP error"
            )

        return ujson.loads(
            response.split(
                b"\r\n\r\n", 1
            )[1]
        )

    except Exception as e:

        print("Server:", e)
        return None

    finally:

        if s:
            try:
                s.close()
            except Exception:
                pass


# ---------------- TOUCH ----------------

def update_touch(now):

    global last_touch
    global touch_start
    global seq

    current = touch.value()

    if current and not last_touch:
        touch_start = now

    elif (
        not current
        and last_touch
        and touch_start is not None
    ):

        duration = time.ticks_diff(
            now,
            touch_start
        )

        if duration >= MIN_TOUCH_MS:

            seq += 1

            pending.append(
                ("exit2", seq)
            )

            print(
                "ZONE 2 EXIT:",
                seq
            )

        touch_start = None

    last_touch = current


# ---------------- DHT + LDR ----------------

def update_sensors(now):

    global last_dht
    global last_ldr

    global temp
    global hum
    global light

    if time.ticks_diff(
        now, last_dht
    ) >= 2500:

        last_dht = now

        try:

            sensor.measure()

            temp = sensor.temperature()
            hum = sensor.humidity()

        except OSError as e:
            print("DHT:", e)

    if time.ticks_diff(
        now, last_ldr
    ) >= 500:

        last_ldr = now

        light = ldr.read()

        led.value(
            1 if light < DARK_THRESHOLD else 0
        )


# ---------------- LED CONTROL ----------------

def update_leds(now):

    alarm = server_alert if server_alert is not None else (count > capacity)

    if alarm:

        red.value(
            int(
                (now // 300) % 2 == 0
            )
        )

        green.off()

    else:

        red.off()
        green.on()


# ---------------- SERVER ----------------

def sync_server(now):

    global last_send
    global connected
    global count
    global capacity
    global server_alert

    if (
        not pending
        and time.ticks_diff(
            now, last_send
        ) < 1000
    ):
        return

    last_send = now

    if not wifi_ok():
        connected = False
        return

    item = (
        pending[0]
        if pending
        else None
    )

    result = post(
        item[0] if item else None,
        item[1] if item else 0
    )

    if result is None:
        connected = False
        return

    connected = True

    count = result["counts"][1]
    if "capacities" in result:
        capacity = result["capacities"][1]
    elif "capacity" in result:
        capacity = result["capacity"]

    if "alerts" in result:
        server_alert = result["alerts"][1]

    if item:

        print(
            "Exit processed:",
            result.get("accepted"),
            "| Count:",
            count,
            "| Capacity:",
            capacity
        )

        pending.pop(0)


# ---------------- MAIN ----------------

print()
print("FORGEWATCH AI - BOARD 2")
print("LDR THRESHOLD:", DARK_THRESHOLD)
print("SERVER:", SERVER_IP)

try:
    wifi.connect(
        SSID,
        PASSWORD
    )
except Exception as e:
    print("Wi-Fi:", e)

while True:

    now = time.ticks_ms()

    update_touch(now)
    update_sensors(now)

    update_leds(now)
    sync_server(now)

    time.sleep_ms(25)
