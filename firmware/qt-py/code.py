"""
OPT4048 + MAX17048 -> MQTT publisher (own broker, not Adafruit IO)

Publishes CCT, lux, CIE x/y, and battery state as a single JSON payload
over MQTT to a self-hosted Mosquitto broker on DigitalOcean.

Battery temperature safety (Stage 1): a 10k NTC thermistor is wired to the
BQ24074 THERM pin. The charger uses it autonomously to throttle/cut charging
on a hot battery -- the MCU never reads it, so no temperature value appears in
the payload. Loggable temperature is a future Stage 3 job (a *second*,
independent NTC divider into GPIO2/A2); it does not touch the BQ24074.

Hardened for unattended operation: a single network/sensor error never
kills the loop, the connection is re-established automatically, and a
hardware watchdog reboots the board if it ever truly hangs.

Store-and-forward (2026-09-24): capture never waits on the network. A reading
that can't be published live is kept (RAM first, then a CSV on the flash so it
survives a reset or a night's deep sleep) and sent later in batches on
cct-backlog/<SENSOR_ID>, stamped with the device's own NTP-set clock so the
server files it at the moment it was taken, not the moment it arrived. Wi-Fi
joins the strongest known network in range, not the first one in the list,
and roams to a clearly stronger one if the link it is on turns weak.
"""

import time
import json
import os
import wifi
import socketpool
import board
import digitalio
import microcontroller
import rtc
import alarm
from watchdog import WatchDogMode
import adafruit_opt4048
from adafruit_opt4048 import Mode, Range
from adafruit_max1704x import MAX17048
import adafruit_minimqtt.adafruit_minimqtt as MQTT

# Remote override written by the MQTT config handler below (cct-config/<id>).
# Lives on the flash, not in settings.toml, because OTA never touches settings.toml
# and a unit on the roof can only be reached over MQTT/HTTPS. Wins over settings.toml.
INTERVAL_FILE = "publish_interval"


def _load_interval():
    try:
        with open(INTERVAL_FILE) as f:
            v = int(f.read().strip())
        if 10 <= v <= 3600:
            return v
    except Exception:
        pass
    return int(os.getenv("PUBLISH_INTERVAL", "120"))  # per-unit override in settings.toml, defaults to 120 for units without one


PUBLISH_INTERVAL = _load_interval()  # seconds between daytime readings
WATCHDOG_TIMEOUT = 90       # board resets if the loop stalls longer than this
WATCHDOG_SLEEP_CHUNK = 10   # feed the watchdog this often while idling between readings
MAX_FAILURES = 5            # consecutive broker failures (with Wi-Fi up) before a clean reboot;
                            # Wi-Fi simply being out of range never reboots -- readings buffer instead

# --- Synchronised capture ---
# Two units left free-running drift apart by up to a full PUBLISH_INTERVAL,
# depending only on when each happened to boot, which makes side-by-side
# calibration readings hard to pair up. Instead each unit sleeps until the next
# PUBLISH_INTERVAL boundary on the *wall clock*, so every unit captures on the
# same absolute grid (e.g. at :00, :02, :04 past the hour at 120 s) regardless
# of boot time, and re-aligns itself after any reboot or network stall.
# Requires adafruit_ntp.mpy in lib/; without it the board free-runs as before.
SYNC_CAPTURES = True
NTP_HOST = "pool.ntp.org"
NTP_CACHE_SECONDS = 3600    # re-query NTP at most hourly; interpolate in between

# Night power saving. Below NIGHT_LUX_FLOOR the light sits at the sensor's noise
# floor and the colour data is useless (the chromaticity is pure noise), so the
# board deep-sleeps with the radio off. That is the only change that gives an
# order-of-magnitude battery saving; trimming idle draw while connected does
# not, because the always-on radio dominates. The board still wakes every
# NIGHT_INTERVAL for a battery/lux heartbeat (which keeps the overnight
# discharge curve and a grey night band on the grid), and stays connected for
# NIGHT_AWAKE_GRACE seconds around that heartbeat so wireless flashing remains
# catchable at night. Detection is purely from measured lux, so it needs no
# clock, location, or NTP, and it self-adjusts to season and weather.
NIGHT_LUX_FLOOR = 0.1       # lux; below this a reading is treated as true night
NIGHT_INTERVAL = 900        # seconds of deep sleep between night heartbeats (15 min, to catch dawn)
NIGHT_AWAKE_GRACE = 20      # seconds to stay connected around a night heartbeat (for flashing)
SENSOR_ID = os.getenv("SENSOR_ID", "sensor-01")
TOPIC = f"cct-sensor/{SENSOR_ID}/readings"
# Deliberately NOT under cct-sensor/: the colour display and backend subscribe to
# cct-sensor/# and would treat a config message as a reading.
CONFIG_TOPIC = f"cct-config/{SENSOR_ID}"

# --- Over-the-air (OTA) firmware ---
# The board PULLS new code.py from the Droplet, so flashing works from anywhere
# (not just the home LAN): the board makes the outbound HTTPS call, so no port
# forward, VPN, or inbound access to the home network is needed. On each check
# it reads a tiny manifest and downloads only when the server's version is
# newer than the one it last applied. Set OTA_BASE_URL in settings.toml to
# enable it (e.g. "https://your-server.example.com/firmware"); leave it unset to
# disable OTA entirely. OTA_TOKEN is optional and only needed if the server
# sets one. Wireless LAN flashing still works as before, and stays the fast
# path when you are at home.
OTA_BASE_URL = os.getenv("OTA_BASE_URL")
OTA_TOKEN = os.getenv("OTA_TOKEN")
OTA_CHECK_INTERVAL = 900     # seconds between OTA checks while awake (day path)
OTA_VERSION_FILE = "ota_version"  # last-applied integer version, on the flash

# --- Wi-Fi choice and reconnection ---
# Reconnects back off (5 s doubling to 2 min) so a unit out of range keeps
# capturing on schedule instead of spending every cycle failing to join. While
# connected, a link weaker than ROAM_RSSI is re-checked every ROAM_CHECK_INTERVAL
# and moved to another known network (or access point) at least ROAM_MARGIN dB
# stronger.
RECONNECT_MIN = 5            # seconds before the first retry after a failed join
RECONNECT_MAX = 120          # ceiling on the reconnect back-off
ROAM_RSSI = -70              # dBm; only look for something better below this
ROAM_MARGIN = 8              # dB a candidate must beat the current link by
ROAM_CHECK_INTERVAL = 180    # seconds between roam scans while the link is weak

# --- Store-and-forward backlog ---
# Readings that miss the live publish sit in RAM, are appended to BACKLOG_FILE
# every FLUSH_RECORDS readings or FLUSH_SECONDS (whichever first; batching
# keeps flash wear down), and are drained BATCH_ROWS at a time with QoS 1 once
# the broker is reachable. BACKLOG_POS_FILE holds the byte offset already sent,
# so draining never rewrites the file. When the file would outgrow its cap the
# oldest unsent rows are thinned (every other row dropped) rather than the
# newest being refused, so a long outage keeps coarser coverage of its start.
# FS_RESERVE_BYTES keeps room free for an OTA download (code.py.new).
BACKLOG_TOPIC = f"cct-backlog/{SENSOR_ID}"
BACKLOG_FILE = "backlog.csv"
BACKLOG_POS_FILE = "backlog.pos"
BACKLOG_FIELDS = ("ts", "cct", "lux", "cie_x", "cie_y", "battery_v", "battery_pct")
BACKLOG_MAX_BYTES = int(os.getenv("BACKLOG_MAX_BYTES", "300000"))
FS_RESERVE_BYTES = 96000
FLUSH_RECORDS = 300
FLUSH_SECONDS = 600
PENDING_MAX = 1200           # RAM ceiling if the flash can't take them (oldest dropped)
BATCH_ROWS = 60
CLOCK_VALID_YEAR = 2025      # RTC reads 2000-01-01 until NTP has set it at least once

# --- Watchdog ---
# If the loop ever blocks longer than WATCHDOG_TIMEOUT (e.g. a network call
# hangs forever), the chip resets itself instead of sitting dead. We "feed"
# it once per loop to say "still alive".
wdt = microcontroller.watchdog
wdt.mode = None  # stop if already running from a previous crash/auto-reload
wdt.timeout = WATCHDOG_TIMEOUT
wdt.mode = WatchDogMode.RESET

# --- Power saving (radio stays always-on; these trim idle draw) ---
# Both are wrapped so a wrong pin/attr name on a firmware update can never crash
# the boot and cost us the wireless-flash path.
#
# 1) Turn off the onboard RGB NeoPixel's power rail. It draws ~1-2 mA even when
#    idle and we never light it.
try:
    _neopixel_power = digitalio.DigitalInOut(board.NEOPIXEL_POWER)
    _neopixel_power.switch_to_output(value=False)
    print("NeoPixel power rail off.")
except Exception as e:
    print("NeoPixel power-off skipped:", e)

# 2) Most aggressive Wi-Fi modem sleep. MIN (modem sleep between the AP's DTIM
#    beacons) is the default; MAX wakes the radio less often for a bit more
#    saving. Fine here: we only publish every few minutes and never rely on
#    low-latency downlink, and the keep-alive is far longer than the interval.
try:
    wifi.radio.power_management = wifi.PowerManagement.MAX
    print("Wi-Fi power management: MAX.")
except Exception as e:
    print("Wi-Fi power_management set skipped:", e)

# --- Sensor setup ---
i2c = board.STEMMA_I2C()
opt4048 = adafruit_opt4048.OPT4048(i2c)
opt4048.mode = Mode.CONTINUOUS
opt4048.range = Range.AUTO
try:
    max17048 = MAX17048(i2c)
    # No quick_start on boot: the MAX17048 is powered from the cell, so it keeps
    # its learned ModelGauge state across MCU reboots. quick_start would discard
    # that and re-guess from instantaneous voltage, which reads high when USB/
    # charger is present. Only quick_start once, manually, on fresh battery swap.
    print("MAX17048 found.")
except Exception as e:
    max17048 = None
    print("MAX17048 not found (battery hardware not connected):", e)
print(f"Sensors configured. Sensor ID: {SENSOR_ID}")
time.sleep(0.5)  # let the first OPT4048 conversion complete

# --- Networking ---
pool = socketpool.SocketPool(wifi.radio)

# HTTPS session for OTA. Built defensively: if adafruit_requests/ssl are missing
# from lib/ (they are separate from the MQTT stack), OTA simply stays disabled
# rather than crashing the boot and costing us the wireless-flash path. Requires
# adafruit_requests.mpy in lib/ (adafruit_connection_manager is already there).
_ota_requests = None
if OTA_BASE_URL:
    try:
        import ssl
        import adafruit_requests
        _ota_requests = adafruit_requests.Session(pool, ssl.create_default_context())
        print("OTA enabled:", OTA_BASE_URL)
    except Exception as e:
        print("OTA disabled (requests/ssl unavailable):", e)

# Wall clock for synchronised capture. Built defensively, exactly like the OTA
# session above: if adafruit_ntp.mpy is missing from lib/ the board simply
# free-runs instead of crashing the boot and costing us the flash path. No
# network call happens here -- adafruit_ntp queries lazily on first use.
_ntp = None
if SYNC_CAPTURES:
    try:
        import adafruit_ntp
        _ntp = adafruit_ntp.NTP(pool, server=NTP_HOST, tz_offset=0,
                                cache_seconds=NTP_CACHE_SECONDS)
        print("Capture sync enabled via", NTP_HOST)
    except Exception as e:
        print("Capture sync disabled (adafruit_ntp unavailable):", e)

mqtt_client = MQTT.MQTT(
    broker=os.getenv("MQTT_BROKER"),       # e.g. "broker.example.com"
    port=1883,
    username=os.getenv("MQTT_USERNAME"),   # e.g. "feather"
    password=os.getenv("MQTT_PASSWORD"),
    socket_pool=pool,
    keep_alive=max(60, PUBLISH_INTERVAL * 3),  # floor at 60s so a short interval cannot thrash MQTT
)


def _wifi_networks():
    """Every configured network, in the order they should be tried.

    CIRCUITPY_WIFI_SSID stays the primary because CircuitPython's own web
    workflow auto-connects to that one at boot, before code.py runs. The
    numbered pairs (CIRCUITPY_WIFI_SSID_2 .. _4) are fallbacks used only by
    this loop, so a unit can follow a venue network or a phone hotspot without
    losing the home network. Order them most- to least-likely: the hotspot
    belongs last, since it is the slowest to fail when out of range. Unset
    entries are skipped, so one network alone behaves as before.
    """
    nets = []
    for suffix in ("", "_2", "_3", "_4"):
        ssid = os.getenv("CIRCUITPY_WIFI_SSID" + suffix)
        if ssid:
            nets.append((ssid, os.getenv("CIRCUITPY_WIFI_PASSWORD" + suffix)))
    return nets


def _scan_known():
    """Known networks in range, strongest first.

    One entry per access point (BSSID), so a network name served by several
    APs -- or two venues sharing a name -- is ranked by the AP actually
    heard best. Returns (rssi, ssid, password, channel, bssid) tuples.
    """
    known = dict(_wifi_networks())
    found = []
    try:
        for n in wifi.radio.start_scanning_networks():
            if n.ssid in known:
                found.append((n.rssi, n.ssid, known[n.ssid], n.channel, bytes(n.bssid)))
    finally:
        wifi.radio.stop_scanning_networks()
    found.sort(key=lambda c: c[0], reverse=True)
    return found


def _join(candidates):
    """Try each (rssi, ssid, password, channel, bssid) in turn. Raises on total failure."""
    last_error = None
    for rssi, ssid, password, channel, bssid in candidates:
        try:
            if rssi is None:
                print("Connecting to Wi-Fi:", ssid)
                wifi.radio.connect(ssid, password)
            else:
                print(f"Connecting to Wi-Fi: {ssid} ({rssi} dBm, ch {channel})")
                try:
                    wifi.radio.connect(ssid, password, channel=channel, bssid=bssid)
                except TypeError:
                    # A build without channel/bssid support must still get
                    # online (a unit that can't join can't be fixed over the air).
                    wifi.radio.connect(ssid, password)
            if wifi.radio.connected:
                print("Wi-Fi connected:", wifi.radio.ipv4_address)
                return
        except Exception as e:
            print("Wi-Fi failed on", ssid, "--", e)
            last_error = e
    raise last_error or RuntimeError("Wi-Fi join failed")


def connect_wifi():
    """Join the strongest known network in range. Raises if none can be joined.

    Scanning first means a unit only ever tries networks that are actually
    there (a failed join on an absent SSID costs ~2.5 s each), and picks the
    best of several rather than whichever happens to be listed first. If the
    scan itself fails, fall back to trying the list in slot order.
    """
    try:
        candidates = _scan_known()
    except Exception as e:
        print("Wi-Fi scan failed, trying networks in list order:", e)
        candidates = [(None, s, p, 0, None) for s, p in _wifi_networks()]
    if not candidates:
        raise RuntimeError("No known Wi-Fi network in range")
    _join(candidates[:3])


def _mqtt_up():
    # is_connected() raises rather than returns False in some versions.
    try:
        return mqtt_client.is_connected()
    except Exception:
        return False


_cfg = {"subscribed": False, "pending": None}   # remote-config subscription state


def _drop_mqtt():
    _cfg["subscribed"] = False   # a new connection must re-subscribe
    try:
        mqtt_client.disconnect()
    except Exception:
        pass


def _on_config(client, topic, message):
    """Parse {"interval": N} (seconds, 10..3600) or {"interval": null} to revert
    to settings.toml. Bad payloads are ignored. Only records the request; the
    cycle applies it, since a reset must not happen inside an MQTT callback."""
    try:
        v = json.loads(message).get("interval")
        if v is None or v == 0:
            _cfg["pending"] = 0
        elif 10 <= int(v) <= 3600:
            _cfg["pending"] = int(v)
        else:
            print("Config: interval out of range, ignored:", v)
    except Exception as e:
        print("Config: bad payload ignored:", e)


mqtt_client.on_message = _on_config


def check_remote_config():
    """Subscribe (once per connection) and poll for a config message. Never raises.

    The message is retained, so it arrives on subscribe after any reboot or
    night wake, and a change is picked up within one cycle. A changed interval
    is saved to INTERVAL_FILE and the board reboots (backlog flushed first) so
    the schedule, drain budget and MQTT keep-alive all rebuild from the new
    value. An unchanged value does nothing, so the retained message cannot
    cause a reboot loop.
    """
    try:
        if not _mqtt_up():
            return
        if not _cfg["subscribed"]:
            mqtt_client.subscribe(CONFIG_TOPIC)
            _cfg["subscribed"] = True
        mqtt_client.loop(timeout=1.5)
    except Exception as e:
        print("Config check failed:", e)
        _cfg["subscribed"] = False
        return
    want = _cfg["pending"]
    _cfg["pending"] = None
    if want is None:
        return
    new = want if want else int(os.getenv("PUBLISH_INTERVAL", "120"))
    if new == PUBLISH_INTERVAL:
        if want == 0:
            try:
                os.remove(INTERVAL_FILE)
            except OSError:
                pass
        return
    print(f"Config: interval {PUBLISH_INTERVAL}s -> {new}s; saving and rebooting.")
    try:
        if want:
            with open(INTERVAL_FILE, "w") as f:
                f.write(str(want))
        else:
            os.remove(INTERVAL_FILE)
    except Exception as e:
        print("Config: could not save:", e)
        return
    try:
        flush_pending(force=True)
    except Exception:
        pass
    time.sleep(1)
    microcontroller.reset()


_net = {
    "next_attempt": 0.0,       # monotonic time before which no reconnect is tried
    "backoff": RECONNECT_MIN,
    "mqtt_failures": 0,        # consecutive broker failures while Wi-Fi was up
    "last_roam_check": 0.0,
}


def ensure_online(force=False):
    """True if Wi-Fi and MQTT are both up, reconnecting if it is time to. Never raises.

    A failed attempt schedules the next one on a doubling back-off, so being
    out of range costs one scan every couple of minutes, not every cycle.
    `force` ignores the back-off (the night heartbeat gets one shot per wake).
    """
    if wifi.radio.connected and _mqtt_up():
        return True
    now = time.monotonic()
    if not force and now < _net["next_attempt"]:
        return False
    try:
        if not wifi.radio.connected:
            _drop_mqtt()   # its socket belonged to the old link
            connect_wifi()
        if not _mqtt_up():
            print("Connecting to MQTT broker...")
            mqtt_client.connect()
            print("Connected to broker.")
            _cfg["subscribed"] = False   # fresh session: subscribe again
        _net["backoff"] = RECONNECT_MIN
        _net["mqtt_failures"] = 0
        sync_clock()
        return True
    except Exception as e:
        print(f"Offline (retry in {_net['backoff']}s):", e)
        _net["next_attempt"] = time.monotonic() + _net["backoff"]
        _net["backoff"] = min(_net["backoff"] * 2, RECONNECT_MAX)
        if wifi.radio.connected:
            # Wi-Fi is fine but the broker isn't answering: the one case a clean
            # reboot has historically fixed. The caller does it (reset_if_wedged)
            # once this cycle's reading is safely buffered.
            _net["mqtt_failures"] += 1
        return False


def reset_if_wedged():
    """Reboot after MAX_FAILURES broker failures in a row, saving the backlog first."""
    if _net["mqtt_failures"] >= MAX_FAILURES:
        print("Broker unreachable too many times in a row -- resetting board.")
        flush_pending(force=True)
        time.sleep(2)
        microcontroller.reset()


def maybe_roam():
    """If the current link is weak, move to a clearly stronger known AP. Never raises."""
    now = time.monotonic()
    if now - _net["last_roam_check"] < ROAM_CHECK_INTERVAL:
        return
    _net["last_roam_check"] = now
    try:
        ap = wifi.radio.ap_info
        if ap is None or ap.rssi >= ROAM_RSSI:
            return
        current_rssi, current_bssid = ap.rssi, bytes(ap.bssid)
        better = [c for c in _scan_known()
                  if c[4] != current_bssid and c[0] >= current_rssi + ROAM_MARGIN]
        if not better:
            return
        print(f"Roaming: {ap.ssid} at {current_rssi} dBm -> {better[0][1]} at {better[0][0]} dBm")
        _drop_mqtt()
        try:
            wifi.radio.stop_station()
        except Exception:
            pass
        _join(better[:2])
    except Exception as e:
        # A failed move leaves us offline at worst; the next ensure_online()
        # rescans and joins whatever is strongest.
        print("Roam failed:", e)
    _net["next_attempt"] = 0.0   # reconnect MQTT straight away, on whichever link we have


# --- Device clock ---
# The RTC is set from NTP whenever we are online (at most hourly) and keeps
# running through a network outage, a soft reset and deep sleep, so readings
# taken offline still get a true timestamp. Until it has been set once since
# power-up it reads 2000-01-01; readings taken then keep their monotonic time
# and are stamped retroactively the moment the clock is set.
_clock = {"last_sync": None}


def clock_valid():
    return time.localtime().tm_year >= CLOCK_VALID_YEAR


def sync_clock():
    """Set the RTC from NTP if it is unset or due a refresh. Never raises."""
    if _ntp is None:
        return
    last = _clock["last_sync"]
    if clock_valid() and last is not None and time.monotonic() - last < NTP_CACHE_SECONDS:
        return
    try:
        rtc.RTC().datetime = _ntp.datetime
        _clock["last_sync"] = time.monotonic()
        _resolve_pending_timestamps()
    except Exception as e:
        print("NTP sync failed:", e)


def _ota_local_version():
    """The firmware version currently installed (0 if never OTA'd)."""
    try:
        with open(OTA_VERSION_FILE) as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return 0


def _ota_url(name):
    url = OTA_BASE_URL.rstrip("/") + "/" + name
    if OTA_TOKEN:
        url += "?token=" + OTA_TOKEN
    return url


def check_and_apply_ota():
    """Pull newer firmware from the Droplet and self-install. Never raises.

    Flow: read the manifest, and if its version is newer than what we last
    applied, download code.py, verify its byte length matches the manifest
    (so a truncated download is rejected, not installed), write it via a temp
    file + atomic rename, record the new version, and reload. Because this runs
    early each awake cycle, a later bug in a freshly installed code.py can still
    be healed by pushing another update -- as long as Wi-Fi still comes up.
    """
    if _ota_requests is None:
        return
    try:
        r = _ota_requests.get(_ota_url("manifest.json"), timeout=20)
        manifest = r.json()
        r.close()
    except Exception as e:
        print("OTA: manifest check failed:", e)
        return

    remote_v = int(manifest.get("version", 0))
    if remote_v <= _ota_local_version():
        return  # already up to date

    print(f"OTA: version {remote_v} available (have {_ota_local_version()}); downloading.")
    try:
        r = _ota_requests.get(_ota_url("code.py"), timeout=60)
        source = r.content  # bytes
        r.close()
    except Exception as e:
        print("OTA: download failed:", e)
        return

    expected = manifest.get("size")
    if expected is not None and len(source) != expected:
        print(f"OTA: size mismatch (got {len(source)}, expected {expected}); aborting.")
        return

    try:
        # Stage to a temp file first (size already verified above), then swap it
        # in. FAT's rename won't overwrite an existing file, so remove the old
        # code.py first. The gap is tiny; if power is lost inside it, recover
        # over USB — code.py.new holds the complete new firmware.
        with open("code.py.new", "wb") as f:
            f.write(source)
        try:
            os.remove("code.py")
        except OSError:
            pass
        os.rename("code.py.new", "code.py")
        with open(OTA_VERSION_FILE, "w") as f:
            f.write(str(remote_v))
    except Exception as e:
        print("OTA: install failed (filesystem not writable? see boot.py):", e)
        return

    print(f"OTA: installed version {remote_v}; reloading.")
    flush_pending(force=True)  # a reload wipes RAM; keep any unsent readings
    import supervisor
    supervisor.reload()  # never returns: re-runs the new code.py from the top


OVERLOAD_FLAG = 0x08  # bit 3 of opt4048.flags == sensor saturated


def read_diagnostics():
    """Return raw-channel diagnostics as a dict, or {} on a sensor read error.

    - overload True  -> the sensor is saturated; the reading is unreliable.
    - low zx_ratio   -> little blue (Z) relative to red (X), e.g. window glass
      cutting blue, which warms the CCT.
    """
    try:
        x_ch, y_ch, z_ch, w_ch = opt4048.all_channels  # raw ADC counts
        flags = opt4048.flags
        return {
            "raw_x": x_ch,
            "raw_y": y_ch,
            "raw_z": z_ch,
            "raw_w": w_ch,
            "zx_ratio": round(z_ch / x_ch, 3) if x_ch else 0,
            "overload": bool(flags & OVERLOAD_FLAG),
        }
    except Exception as e:
        print("  diag read failed:", e)
        return {}


def read_battery():
    """Return battery voltage and SoC% from MAX17048, or {} on error.

    Temperature is handled autonomously by the BQ24074 via its THERM pin
    and is not read by the MCU in this configuration.
    """
    if max17048 is None:
        return {}
    try:
        return {
            "battery_v": round(max17048.cell_voltage, 3),
            "battery_pct": round(min(max17048.cell_percent, 100.0), 1),
        }
    except Exception as e:
        print("  battery read failed:", e)
        return {}


def read_wifi():
    """Return the current link's signal strength (dBm) and SSID, or {} if unknown.

    Lets a marginal site be diagnosed remotely: dropouts on the grid can be
    matched against the RSSI the unit reported just before them.
    """
    try:
        ap = wifi.radio.ap_info
        if ap is None:
            return {}
        return {"wifi_rssi": ap.rssi, "wifi_ssid": ap.ssid}
    except Exception as e:
        print("  wifi info read failed:", e)
        return {}


def build_reading(cie_x, cie_y, lux):
    """Assemble one reading's payload from already-read sensor values.

    The caller reads opt4048.cie once (radio off) to make the day/night
    decision and passes the values here, so the sensor is not read twice.
    `ts` (device Unix time) is included once the clock is set; the server
    files the reading at that time (within a sanity window) rather than at
    the time it arrived.
    """
    cct = opt4048.calculate_color_temperature(cie_x, cie_y)
    payload = {
        "sensor_id": SENSOR_ID,
        "cct": round(cct, 1),
        "lux": round(lux, 2),
        "cie_x": round(cie_x, 4),
        "cie_y": round(cie_y, 4),
    }
    if clock_valid():
        payload["ts"] = int(time.time())
    payload.update(read_battery())      # adds battery_v, battery_pct
    payload.update(read_diagnostics())  # adds raw_x/y/z/w, zx_ratio, overload
    payload.update(read_wifi())         # adds wifi_rssi, wifi_ssid
    payload["backlog_rows"] = backlog_rows()
    print(json.dumps(payload))
    return payload


def publish_live(payload, captured_mono):
    """Publish a reading now; if that fails, keep it for the backlog. Never raises.

    A reading taken before the clock was set (first cycle after a cold boot,
    where connecting is what sets it) is stamped retroactively from its
    capture time, so it lands at the moment it was taken, not a few seconds
    later when the join finished.
    """
    if "ts" not in payload and clock_valid():
        payload["ts"] = _epoch_at(captured_mono)
    try:
        mqtt_client.publish(TOPIC, json.dumps(payload))
        return True
    except Exception as e:
        print("Live publish failed, buffering:", e)
        _drop_mqtt()
        buffer_reading(payload, captured_mono)
        return False


# --- Store-and-forward backlog ---
# _pending holds [ts or None, monotonic, "cct,lux,x,y,bv,bp"] for readings not
# yet on the flash, oldest first.
_pending = []
_flush = {"last": time.monotonic()}
BACKLOG_TMP = "backlog.tmp"
BACKLOG_BAD = "backlog.bad"
BACKLOG_ROW_BYTES = 50   # rough size of one CSV row, for the backlog_rows estimate


def _fmt(v):
    return "" if v is None else str(v)


def buffer_reading(payload, captured_mono):
    rest = ",".join(_fmt(payload.get(k)) for k in BACKLOG_FIELDS[1:])
    _pending.append([payload.get("ts"), captured_mono, rest])
    if len(_pending) > PENDING_MAX:
        del _pending[0]


def _epoch_at(mono):
    """Unix time (int) at an earlier time.monotonic() reading.

    Kept in integer arithmetic on purpose: CircuitPython floats are 32-bit,
    which near 1.79e9 resolve to only 128 s, so time.time() - <float> puts a
    reading minutes out (seen on sensor-02, 2026-09-24). Only the small
    elapsed interval is ever a float.
    """
    return time.time() - int(time.monotonic() - mono + 0.5)


def _resolve_pending_timestamps():
    """Stamp readings taken before the clock was set, from their monotonic time."""
    if not clock_valid():
        return
    for rec in _pending:
        if rec[0] is None:
            rec[0] = _epoch_at(rec[1])


def _file_size(path):
    try:
        return os.stat(path)[6]
    except OSError:
        return 0


def _read_pos():
    try:
        with open(BACKLOG_POS_FILE) as f:
            return int(f.read().strip() or 0)
    except (OSError, ValueError):
        return 0


def _write_pos(pos):
    with open(BACKLOG_POS_FILE, "w") as f:
        f.write(str(pos))


def _clear_backlog_file():
    for path in (BACKLOG_FILE, BACKLOG_POS_FILE):
        try:
            os.remove(path)
        except OSError:
            pass


def backlog_rows():
    """Approximate count of readings waiting to be sent (RAM exact, flash estimated)."""
    unsent = max(0, _file_size(BACKLOG_FILE) - _read_pos())
    return len(_pending) + unsent // BACKLOG_ROW_BYTES


def _backlog_cap(size):
    """Largest the backlog file may grow to right now.

    Thinning writes a copy of up to ~5/8 of the file before deleting the
    original, so the file may only take ~3/5 of the space actually available
    (free space plus itself, less the OTA reserve).
    """
    try:
        st = os.statvfs("/")
        available = size + st[1] * st[4] - FS_RESERVE_BYTES   # f_frsize * f_bavail
    except Exception:
        return BACKLOG_MAX_BYTES
    return max(0, min(BACKLOG_MAX_BYTES, available * 3 // 5))


def _thin_backlog():
    """Drop every other unsent row in the oldest three quarters. True if it shrank.

    Repeated thinning halves the oldest data again each time, so a long
    outage ends up with the most recent stretch at full rate and earlier
    stretches progressively coarser, rather than a hard cut-off.
    """
    pos = _read_pos()
    n = 0
    with open(BACKLOG_FILE, "rb") as f:
        f.seek(pos)
        while f.readline():
            n += 1
            if n % 500 == 0:
                wdt.feed()
    if n < 8:
        return False
    keep_from = n - n // 4
    i = 0
    with open(BACKLOG_FILE, "rb") as src, open(BACKLOG_TMP, "wb") as dst:
        src.seek(pos)
        while True:
            line = src.readline()
            if not line:
                break
            if i >= keep_from or i % 2 == 0:
                dst.write(line)
            i += 1
            if i % 500 == 0:
                wdt.feed()
    os.remove(BACKLOG_FILE)
    os.rename(BACKLOG_TMP, BACKLOG_FILE)
    try:
        os.remove(BACKLOG_POS_FILE)   # the thinned file holds only unsent rows
    except OSError:
        pass
    print(f"Backlog: flash cap reached, thinned oldest {keep_from} of {n} rows by half.")
    return True


def flush_pending(force=False):
    """Append buffered readings to the flash backlog. Never raises.

    Batched (every FLUSH_RECORDS readings or FLUSH_SECONDS) to limit flash
    wear; `force` flushes now, before anything that would wipe RAM (deep
    sleep, a reset, an OTA reload). Readings still without a timestamp stay
    in RAM, since a row with no time can't be placed on the server.
    """
    if not _pending:
        return
    if (not force and len(_pending) < FLUSH_RECORDS
            and time.monotonic() - _flush["last"] < FLUSH_SECONDS):
        return
    _flush["last"] = time.monotonic()
    _resolve_pending_timestamps()
    ready = [r for r in _pending if r[0] is not None]
    if not ready:
        return
    data = "".join(f"{r[0]},{r[2]}\n" for r in ready).encode()
    try:
        for _ in range(6):
            size = _file_size(BACKLOG_FILE)
            if size + len(data) <= _backlog_cap(size) or not _thin_backlog():
                break
        size = _file_size(BACKLOG_FILE)
        if size + len(data) > _backlog_cap(size):
            raise OSError("backlog file full")
        with open(BACKLOG_FILE, "ab") as f:
            f.write(data)
    except Exception as e:
        print("Backlog flush failed (kept in RAM):", e)
        return
    keep = [r for r in _pending if r[0] is None]   # no slice assignment: not in every CircuitPython build
    _pending.clear()
    _pending.extend(keep)
    print(f"Backlog: saved {len(ready)} readings to flash.")


def _read_file_batch():
    """Up to BATCH_ROWS unsent rows from the flash backlog, and the offset after them."""
    size = _file_size(BACKLOG_FILE)
    if not size:
        return [], 0
    pos = _read_pos()
    if pos >= size:
        _clear_backlog_file()
        return [], 0
    lines = []
    with open(BACKLOG_FILE, "rb") as f:
        f.seek(pos)
        while len(lines) < BATCH_ROWS:
            line = f.readline()
            if not line:
                break
            line = line.strip()
            if line:
                lines.append(line.decode())
        new_pos = f.tell()
    return lines, new_pos


def drain_backlog(budget):
    """Send stored readings, oldest first, for up to `budget` seconds. Never raises.

    Each batch goes with QoS 1, and is only marked sent once the broker has
    acknowledged it; if the link drops mid-way the rest simply waits for the
    next connection (the server ignores rows it already has, so a batch
    re-sent after an ack was lost does no harm).
    """
    t0 = time.monotonic()
    sent = 0
    while time.monotonic() - t0 < budget:
        wdt.feed()
        take = 0
        try:
            lines, new_pos = _read_file_batch()
        except Exception as e:
            # Unreadable backlog: set it aside (kept for recovery over USB)
            # rather than wedge every cycle on it.
            print("Backlog file unreadable, moved to", BACKLOG_BAD, "--", e)
            try:
                os.remove(BACKLOG_BAD)
            except OSError:
                pass
            try:
                os.rename(BACKLOG_FILE, BACKLOG_BAD)
            except OSError:
                pass
            _clear_backlog_file()
            continue
        from_file = bool(lines)
        if not from_file:
            _resolve_pending_timestamps()
            for rec in _pending:
                if rec[0] is None or take == BATCH_ROWS:
                    break
                lines.append(f"{rec[0]},{rec[2]}")
                take += 1
        if not lines:
            break
        batch = {"sensor_id": SENSOR_ID, "fields": list(BACKLOG_FIELDS), "csv": "\n".join(lines)}
        try:
            mqtt_client.publish(BACKLOG_TOPIC, json.dumps(batch), qos=1)
        except Exception as e:
            print("Backlog send failed (kept for later):", e)
            _drop_mqtt()
            break
        if from_file:
            if new_pos >= _file_size(BACKLOG_FILE):
                _clear_backlog_file()
            else:
                _write_pos(new_pos)
        else:
            for _ in range(take):
                _pending.pop(0)
        sent += len(lines)
    if sent:
        print(f"Backlog: sent {sent} readings (~{backlog_rows()} left).")
    return sent


def idle(seconds):
    """Sleep for `seconds`, feeding the watchdog in short chunks.

    A single long time.sleep would let the 90s watchdog fire during the normal
    multi-minute gap between readings, so we sleep in WATCHDOG_SLEEP_CHUNK steps
    and feed the watchdog each step. A genuine mid-work hang still trips the
    watchdog; the long idle gap does not.
    """
    slept = 0
    while slept < seconds:
        chunk = min(WATCHDOG_SLEEP_CHUNK, seconds - slept)
        time.sleep(chunk)
        wdt.feed()
        slept += chunk


def seconds_to_next_capture():
    """Seconds to wait so the next reading lands on a wall-clock boundary.

    Reads the RTC (set from NTP whenever online), so alignment holds through a
    network outage too. Alignment is done modulo the hour rather than against
    a Unix epoch, which is exact only when PUBLISH_INTERVAL divides an hour
    evenly (120 s does; 300 s does too), so anything else free-runs rather
    than producing a jump at the top of each hour. So does a clock that has
    not been set since power-up.
    """
    if not SYNC_CAPTURES or 3600 % PUBLISH_INTERVAL or not clock_valid():
        return PUBLISH_INTERVAL
    now = time.localtime()
    wait = PUBLISH_INTERVAL - ((now.tm_min * 60 + now.tm_sec) % PUBLISH_INTERVAL)
    # Landing exactly on a boundary would otherwise mean a ~0 s wait and two
    # readings in the same slot, so take the next slot instead.
    if wait < 1:
        wait += PUBLISH_INTERVAL
    return wait


def deep_sleep(seconds):
    """Power the radio down and deep-sleep for `seconds`, then reboot into code.py.

    Deep sleep is the only real battery saving on this board (the always-on
    radio otherwise dominates). Buffered readings go to the flash first (RAM
    does not survive), then we disable the watchdog so its short timeout
    cannot reset us mid-sleep, and best-effort close the MQTT link (deep sleep
    drops the connection anyway). This never returns: the board resets and
    re-runs code.py (and boot.py) from the top on wake.
    """
    flush_pending(force=True)
    _drop_mqtt()
    wdt.mode = None  # a 90s watchdog must not fire during a multi-minute sleep
    time_alarm = alarm.time.TimeAlarm(monotonic_time=time.monotonic() + seconds)
    alarm.exit_and_deep_sleep_until_alarms(time_alarm)


# --- Main loop ---
# We do NOT connect at boot: read the light level first (radio off) so a boot
# that lands in the true-night window goes straight back to deep sleep without
# ever powering the radio. Capture runs on its own schedule whatever the
# network is doing: online, the reading goes out live and any backlog drains
# in the time left over; offline, it is buffered and the next reconnect waits
# for its back-off. Everything is wrapped so no single error halts the
# program; the watchdog reboots the board if the loop ever truly hangs.
DRAIN_BUDGET = min(20, max(0.3, PUBLISH_INTERVAL * 0.4))   # seconds per cycle spent on backlog
_last_ota_check = None   # monotonic time of the last OTA check; None = never
MAX_CYCLE_ERRORS = 10    # unexpected errors in a row before a clean reboot


def run_cycle():
    """One capture: read, publish or buffer, drain, then sleep to the next slot."""
    global _last_ota_check
    wdt.feed()  # "still alive" — resets the watchdog countdown

    # Read the sensor once (no radio needed) for the day/night decision and the
    # values to publish. If the sensor read itself fails, wait and retry.
    try:
        cie_x, cie_y, lux = opt4048.cie
    except Exception as e:
        print("Sensor read failed:", e)
        idle(PUBLISH_INTERVAL)
        return

    # Fix the next capture time now, before any network work: reconnects,
    # roaming and backlog draining take a variable time per unit, and letting
    # that shift the schedule would push units off the shared wall-clock grid.
    target_wait = seconds_to_next_capture()
    work_start = time.monotonic()
    reading = build_reading(cie_x, cie_y, lux)

    if lux < NIGHT_LUX_FLOOR:
        # NIGHT: colour data is noise. Send a best-effort battery/lux heartbeat,
        # use the awake window to drain any backlog (and stay catchable for
        # wireless flashing), then deep-sleep the radio off until the next check.
        print(f"Night (lux {lux:.2f} < {NIGHT_LUX_FLOOR}): heartbeat, then deep sleep {NIGHT_INTERVAL}s.")
        if ensure_online(force=True):
            publish_live(reading, work_start)
            check_remote_config()
            t0 = time.monotonic()
            drain_backlog(NIGHT_AWAKE_GRACE)
            idle(max(0, NIGHT_AWAKE_GRACE - (time.monotonic() - t0)))
        else:
            buffer_reading(reading, work_start)
        deep_sleep(NIGHT_INTERVAL)   # flushes the backlog; never returns

    # DAY: stay connected (always-on) so wireless flashing keeps working.
    online = ensure_online()
    if online:
        maybe_roam()
        online = ensure_online()
    if online:
        sync_clock()   # no-op unless the hourly refresh is due
        # Check for new firmware at most every OTA_CHECK_INTERVAL, so the fast
        # daytime cadence never hammers it. If an update installs,
        # check_and_apply_ota() reloads and never returns.
        now_mono = time.monotonic()
        if _last_ota_check is None or now_mono - _last_ota_check >= OTA_CHECK_INTERVAL:
            _last_ota_check = now_mono
            check_and_apply_ota()
        check_remote_config()
        if publish_live(reading, work_start):
            drain_backlog(DRAIN_BUDGET - (time.monotonic() - work_start))
    else:
        buffer_reading(reading, work_start)
        flush_pending()
        reset_if_wedged()

    # Sleep to the next wall-clock boundary, less whatever this cycle's work
    # already used, so the wake lands on the boundary however long it took.
    idle(max(1, target_wait - (time.monotonic() - work_start)))


# Safety net. Everything above that touches the network or the flash already
# handles its own errors; this catches anything that slips through (e.g. an API
# that behaves differently on a newer CircuitPython). Without it an uncaught
# exception would end code.py, and a stopped unit can't be healed over the air.
# With it the unit keeps capturing and keeps checking for OTA fixes; only a
# persistent fault gets a (backlog-saving) reboot. supervisor.reload() from an
# OTA install must pass straight through.
_cycle_errors = 0
while True:
    try:
        run_cycle()
        _cycle_errors = 0
    except Exception as e:
        if type(e).__name__ == "ReloadException":
            raise
        _cycle_errors += 1
        print(f"Cycle error ({_cycle_errors}/{MAX_CYCLE_ERRORS}):", e)
        if _cycle_errors >= MAX_CYCLE_ERRORS:
            print("Repeated cycle errors -- resetting board.")
            try:
                flush_pending(force=True)
            except Exception:
                pass
            time.sleep(2)
            microcontroller.reset()
        idle(PUBLISH_INTERVAL)
