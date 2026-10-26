
# =====================================================
# FORGEWATCH AI - BOARD 1
# RFID + ZONE 1 + BUZZER + SENSOR MONITORING
# =====================================================

import network
import socket
import time
import dht
import ujson

from machine import Pin, ADC, SoftSPI, PWM
#python -m uvicorn server.app:app --host 0.0.0.0 --port 8000
# ---------------- WIFI / SERVER ----------------

SSID = "Pixel"
PASSWORD = "11111111"

SERVER_IP = "10.230.141.149"
SERVER_PORT = 8000
DEVICE_KEY = "demo-change-me"

# ---------------- SETTINGS ----------------

TAG_ZONE1 = "F35B6EE2"
TAG_ZONE2 = "7D69E13F"

BASE_CAPACITY = 10
CAPACITY = 10
capacities = [8, 8]       # Dynamic environmental capacities for [zone1, zone2] (5-10 range)
server_alerts = None      # Live server alerts [alert_z1, alert_z2]
DARK_THRESHOLD = 1500

MIN_TOUCH_MS = 80

# ---------------- PINS ----------------

red = Pin(13, Pin.OUT)
green = Pin(12, Pin.OUT)

touch = Pin(34, Pin.IN)

led = Pin(2, Pin.OUT)

dht_sensor = dht.DHT11(Pin(14))

ldr = ADC(Pin(39))
ldr.atten(ADC.ATTN_11DB)

buzzer = PWM(
    Pin(27),
    freq=2000,
    duty=0
)

# ---------------- BUZZER SETTINGS ----------------

SHORT_BEEP_MS = 120

# Police siren frequencies
SIREN_HIGH = 2400
SIREN_LOW = 1300

SIREN_INTERVAL_MS = 350

beep_until = None

def short_beep():
    global beep_until

    # One quick confirmation beep
    beep_until = time.ticks_add(
        time.ticks_ms(),
        SHORT_BEEP_MS
    )


def update_buzzer(now, alarm):

    # Safety siren gets priority
    if alarm:

        phase = (
            (now // SIREN_INTERVAL_MS) % 2
        )

        if phase == 0:
            buzzer.freq(SIREN_HIGH)
        else:
            buzzer.freq(SIREN_LOW)

        buzzer.duty(512)
        return

    # Short beep
    if beep_until is not None:

        if time.ticks_diff(
            beep_until, now
        ) > 0:

            buzzer.freq(2000)
            buzzer.duty(512)
            return

    buzzer.duty(0)


# ---------------- RFID RC522 ----------------

class RC522:

    def __init__(self):

        self.spi = SoftSPI(
            baudrate=1000000,
            polarity=0,
            phase=0,
            sck=Pin(33),
            mosi=Pin(32),
            miso=Pin(35)
        )

        self.cs = Pin(
            26,
            Pin.OUT,
            value=1
        )

        self.rst = Pin(
            25,
            Pin.OUT,
            value=1
        )

        self.rst.off()
        time.sleep_ms(50)

        self.rst.on()
        time.sleep_ms(50)

        registers = (
            (0x01, 0x0F),
            (0x2A, 0x8D),
            (0x2B, 0x3E),
            (0x2D, 30),
            (0x2C, 0),
            (0x15, 0x40),
            (0x11, 0x3D)
        )

        for r, v in registers:
            self.w(r, v)

        self.bits(0x14, 0x03)

    def w(self, r, v):

        self.cs.off()

        self.spi.write(
            bytes([
                (r << 1) & 0x7E,
                v
            ])
        )

        self.cs.on()

    def r(self, r):

        self.cs.off()

        self.spi.write(
            bytes([
                ((r << 1) & 0x7E) | 0x80
            ])
        )

        value = self.spi.read(1)[0]

        self.cs.on()

        return value

    def bits(self, r, mask):
        self.w(
            r,
            self.r(r) | mask
        )

    def clear(self, r, mask):
        self.w(
            r,
            self.r(r) & ~mask
        )

    def command(self, cmd, data):

        self.w(0x02, 0xF7)
        self.clear(0x04, 0x80)
        self.bits(0x0A, 0x80)

        self.w(0x01, 0)

        for x in data:
            self.w(0x09, x)

        self.w(0x01, cmd)

        if cmd == 0x0C:
            self.bits(0x0D, 0x80)

        for _ in range(200):

            if self.r(0x04) & 0x31:
                break

            time.sleep_us(100)

        else:
            return None, 0

        self.clear(0x0D, 0x80)

        if self.r(0x06) & 0x1B:
            return None, 0

        n = self.r(0x0A)
        last = self.r(0x0C) & 7

        bits = (
            (n - 1) * 8 + last
            if last
            else n * 8
        )

        return [
            self.r(0x09)
            for _ in range(min(n, 16))
        ], bits

    def scan(self):

        self.w(0x0D, 7)

        data, bits = self.command(
            0x0C,
            [0x26]
        )

        if data is None or bits != 16:
            return None

        self.w(0x0D, 0)

        uid, bits = self.command(
            0x0C,
            [0x93, 0x20]
        )

        if uid is None or len(uid) != 5:
            return None

        checksum = (
            uid[0] ^ uid[1] ^
            uid[2] ^ uid[3]
        )

        if checksum != uid[4]:
            return None

        return ''.join(
            '%02X' % x
            for x in uid[:4]
        )


reader = RC522()

print(
    "RC522 version:",
    hex(reader.r(0x37))
)

# ---------------- WIFI ----------------

wifi = network.WLAN(network.STA_IF)
wifi.active(True)

last_wifi_attempt = 0

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
        print("WiFi:", e)

    return wifi.isconnected()


# ---------------- STATE ----------------

boot = str(time.ticks_us())

seq = 0
pending = []

counts = [0, 0]

last_card = None
missing_since = None

last_touch = touch.value()
touch_start = None

last_dht = time.ticks_ms() - 3000
last_ldr = time.ticks_ms() - 1000
last_scan = time.ticks_ms() - 500
last_send = time.ticks_ms() - 2000

temp = None
hum = None
light = None

last_remote_exit2 = "init"

# ---------------- HTTP ----------------

def post(action=None, event_seq=0):

    body = ujson.dumps({
        "node": 1,
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
                "HTTP status not OK"
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


# ---------------- EVENT QUEUE ----------------

def queue(action):

    global seq

    seq += 1

    pending.append(
        (action, seq)
    )

    print("EVENT:", action)

    # RFID and Zone 1 touch feedback
    short_beep()


# ---------------- TOUCH ----------------

def update_touch(now):

    global last_touch
    global touch_start

    value = touch.value()

    if value and not last_touch:
        touch_start = now

    elif (
        not value
        and last_touch
        and touch_start is not None
    ):

        duration = time.ticks_diff(
            now,
            touch_start
        )

        if duration >= MIN_TOUCH_MS:
            queue("exit1")

        touch_start = None

    last_touch = value


# ---------------- RFID ----------------

def update_rfid(now):

    global last_scan
    global last_card
    global missing_since

    if time.ticks_diff(
        now, last_scan
    ) < 140:
        return

    last_scan = now

    uid = reader.scan()

    if uid is not None:

        missing_since = None

        if uid != last_card:

            if uid == TAG_ZONE1:
                queue("entry1")

            elif uid == TAG_ZONE2:
                queue("entry2")

            else:
                print("Unknown UID:", uid)

        last_card = uid

    else:

        if missing_since is None:
            missing_since = now

        elif time.ticks_diff(
            now,
            missing_since
        ) >= 1200:

            last_card = None


# ---------------- SENSORS ----------------

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
            dht_sensor.measure()

            temp = dht_sensor.temperature()
            hum = dht_sensor.humidity()

        except OSError as e:
            print("DHT:", e)

    if time.ticks_diff(
        now, last_ldr
    ) >= 500:

        last_ldr = now

        light = ldr.read()

        # ON when LDR below 1500
        led.value(
            1 if light < DARK_THRESHOLD else 0
        )


# ---------------- LED + SIREN ----------------

def update_outputs(now):

    if server_alerts is not None:
        alarm1 = server_alerts[0]
        alarm_any = server_alerts[0] or server_alerts[1]
    else:
        alarm1 = counts[0] > capacities[0]
        alarm_any = counts[0] > capacities[0] or counts[1] > capacities[1]

    red.value(
        int(
            alarm1
            and (now // 300) % 2 == 0
        )
    )

    green.value(
        int(not alarm1)
    )

    update_buzzer(
        now,
        alarm_any
    )


# ---------------- SERVER SYNC ----------------

def sync_server(now):

    global last_send
    global counts
    global last_remote_exit2
    global capacities
    global server_alerts

    if (
        not pending
        and time.ticks_diff(
            now, last_send
        ) < 400
    ):
        return

    last_send = now

    if not wifi_ok():
        return

    item = pending[0] if pending else None

    result = post(
        item[0] if item else None,
        item[1] if item else 0
    )

    if result is None:
        return

    counts = result["counts"]
    if "capacities" in result:
        capacities = result["capacities"]
    elif "capacity" in result:
        capacities[0] = result["capacity"]

    if "alerts" in result:
        server_alerts = result["alerts"]

    if item:

        print(
            "Processed:",
            item[0],
            "| Counts:",
            counts,
            "| Capacities:",
            capacities
        )

        pending.pop(0)

    # Remote Zone 2 Exit confirmation beep via server
    remote_id = result.get(
        "last_exit2_id"
    )

    if remote_id is not None:

        if (
            last_remote_exit2 is not None
            and remote_id != last_remote_exit2
        ):
            print("Remote Zone 2 Exit detected! Beeping buzzer.")
            short_beep()

        last_remote_exit2 = remote_id


# ---------------- START ----------------

print()
print("FORGEWATCH AI - BOARD 1")
print("RFID + BUZZER READY")
print("LDR THRESHOLD:", DARK_THRESHOLD)
print("SERVER:", SERVER_IP)

try:
    wifi.connect(SSID, PASSWORD)
except Exception as e:
    print("Initial Wi-Fi:", e)

try:

    while True:

        now = time.ticks_ms()

        update_touch(now)
        update_rfid(now)
        update_sensors(now)

        update_outputs(now)
        sync_server(now)

        time.sleep_ms(25)

finally:

    buzzer.duty(0)
    buzzer.deinit()
