"""
CCT sensor backend (v2 — own broker).

Subscribes directly to your self-hosted Mosquitto broker for CCT/lux/CIE
readings published by the QT Py, stores them in SQLite, and serves
them to a browser-based visualisation.

Run with:
    python3 -m uvicorn main:app --reload --port 8000

Then open http://localhost:8000 in your browser.
"""

import math
import os
import socket
import sqlite3
import json
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

import paho.mqtt.client as mqtt
from astral import Observer
from astral.sun import azimuth, elevation
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

# --- Configuration ---
# Set these as environment variables before running, e.g.:
#   export MQTT_BROKER=broker.example.com
#   export MQTT_USERNAME=feather
#   export MQTT_PASSWORD=yourpassword
MQTT_BROKER = os.environ.get("MQTT_BROKER", "localhost")
MQTT_PORT = int(os.environ.get("MQTT_PORT", "1883"))
MQTT_USERNAME = os.environ.get("MQTT_USERNAME")
MQTT_PASSWORD = os.environ.get("MQTT_PASSWORD")
MQTT_TOPIC = "cct-sensor/#"  # subscribes to all sensor IDs underneath
# Store-and-forward batches: readings a unit took while it couldn't reach the
# broker, sent later, each row stamped with the device's own clock. A separate
# prefix (not cct-sensor/<id>/...) because the colour display subscribes to
# cct-sensor/# and treats every message there as the current colour.
MQTT_BACKLOG_TOPIC = "cct-backlog/#"
# How far a device timestamp may sit from the server's clock and still be used.
# Live readings arrive within seconds, so a wide gap means the device clock is
# wrong and the receive time is safer; backlog rows are old by design.
LIVE_TS_TOLERANCE = timedelta(minutes=10)
BACKLOG_MAX_AGE = timedelta(days=30)
BACKLOG_MAX_FUTURE = timedelta(minutes=2)

DB_PATH = os.path.join(os.path.dirname(__file__), "readings.db")

# Over-the-air firmware: the device pulls new code.py from here on its next
# check, so flashing works from anywhere (no home-network access needed) — the
# device makes the outbound call, nothing connects in to it. Files are staged
# with publish_firmware.py and served over the existing HTTPS/Nginx setup, so
# there is no new firewall/port to open. If OTA_TOKEN is set, both routes
# require a matching ?token=; leave it unset to serve the firmware openly.
FIRMWARE_DIR = os.path.join(os.path.dirname(__file__), "firmware")
OTA_TOKEN = os.environ.get("OTA_TOKEN")

# Gates the calibration-entry endpoints below (add-point/fit), which mutate
# stored data and are reachable from the public debug page. Same open-unless-set
# pattern as OTA_TOKEN: unset by default so this works with no extra setup,
# settable later without a code change by adding CALIBRATION_TOKEN to
# /etc/cct-backend.env and restarting. Read-only calibration status stays
# ungated, matching the rest of the public GET API.
CALIBRATION_TOKEN = os.environ.get("CALIBRATION_TOKEN")

# Below this lux the measured chromaticity is unreliable (noise near the
# sensor's floor), so colour aggregates exclude these readings. Matches
# LUX_FLOOR in the frontend (debug.html), which greys such cells rather
# than painting noise as a vivid colour.
DISPLAY_LUX_FLOOR = 1.0


def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS readings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            sensor_id TEXT NOT NULL,
            cct REAL,
            lux REAL,
            cie_x REAL,
            cie_y REAL,
            battery_v REAL,
            battery_pct REAL,
            battery_temp_c REAL
        )
        """
    )
    # Migrate existing databases that predate the battery columns.
    for col in ("battery_v", "battery_pct", "battery_temp_c"):
        try:
            conn.execute(f"ALTER TABLE readings ADD COLUMN {col} REAL")
        except sqlite3.OperationalError:
            pass  # column already exists

    # Per-sensor location metadata, set once at install via register_sensor.py.
    # The MQTT payload carries no location, so this is the source of truth for
    # where each unit sits. The `timestamp` column on `readings` is already a
    # UTC instant (SQLite datetime('now') is UTC), so it doubles as the
    # canonical timestamp for solar maths; no separate ts_utc column is added.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS sensors (
            sensor_id     TEXT PRIMARY KEY,
            label         TEXT,            -- human name, e.g. 'Home roof, London'
            latitude      REAL,
            longitude     REAL,
            altitude_m    REAL,            -- optional, metres above sea level
            timezone      TEXT,            -- IANA, e.g. 'Europe/London'
            exposure      TEXT,            -- e.g. 'open sky', 'partial occlusion to east'
            installed_at  TEXT,            -- ISO 8601 UTC
            updated_at    TEXT             -- ISO 8601 UTC
        )
        """
    )

    # Location is copied onto each reading (denormalised) so historical rows
    # stay self-describing if a unit is later relocated. Solar elevation and
    # azimuth are computed once at insert time. All nullable: readings can
    # arrive before a unit's location has been registered, and are filled in
    # afterwards by the backfill step.
    for col in (
        "latitude",
        "longitude",
        "solar_elevation_deg",
        "solar_azimuth_deg",
    ):
        try:
            conn.execute(f"ALTER TABLE readings ADD COLUMN {col} REAL")
        except sqlite3.OperationalError:
            pass  # column already exists

    # Duv: the off-locus tint (green/magenta) that CCT alone discards. Derived
    # from cie_x/cie_y, so existing rows can be filled retroactively by
    # backfill_duv. Nullable: rows without a valid chromaticity stay NULL.
    try:
        conn.execute("ALTER TABLE readings ADD COLUMN duv REAL")
    except sqlite3.OperationalError:
        pass  # column already exists

    # Calibrated CCT/Duv: a per-sensor affine correction (see `calibration`
    # below) applied against the reference meter. Raw cct/duv above are never
    # overwritten -- these stay NULL until a sensor has a fit, exactly like the
    # solar fields before a unit is registered.
    for col in ("cct_cal", "duv_cal"):
        try:
            conn.execute(f"ALTER TABLE readings ADD COLUMN {col} REAL")
        except sqlite3.OperationalError:
            pass  # column already exists

    # Calibrated chromaticity, derived from cct_cal/duv_cal (see
    # xy_from_cct_duv) -- the corrected colour a swatch should render once a
    # sensor has a fit, since the raw x,y is the biased reading the C-800
    # calibration exists to correct, not a more truthful one. Same NULL-until-
    # fitted rule as cct_cal/duv_cal above.
    for col in ("cie_x_cal", "cie_y_cal"):
        try:
            conn.execute(f"ALTER TABLE readings ADD COLUMN {col} REAL")
        except sqlite3.OperationalError:
            pass  # column already exists

    # One row per sensor: the fitted affine coefficients, in the two domains
    # where the correction is near-linear (see fit_linear/apply_calibration).
    # Re-fit in place by calling fit_calibration again -- it's an upsert.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS calibration (
            sensor_id  TEXT PRIMARY KEY,
            mired_a    REAL,   -- mired_cal = mired_a * mired_raw + mired_b
            mired_b    REAL,
            duv_c      REAL,   -- duv_cal = duv_c * duv_raw + duv_d
            duv_d      REAL,
            n_points   INTEGER,
            rms_cct    REAL,   -- Kelvin, fit-time residual
            rms_duv    REAL,
            fitted_at  TEXT,   -- ISO 8601 UTC
            notes      TEXT
        )
        """
    )
    # 3x3 colour-correction matrix on raw chromaticity (see fit_ccm), stored as
    # a JSON list of 9 numbers. When present it replaces the affine pair above
    # for this sensor; NULL means the affine pair is the correction (too few
    # points for a matrix, or a fit made before this column existed).
    try:
        conn.execute("ALTER TABLE calibration ADD COLUMN ccm TEXT")
    except sqlite3.OperationalError:
        pass  # column already exists

    # The captured reference pairs a fit is built from: one row per reference
    # reading (either the meter, or another already-calibrated sensor), paired
    # with whatever the target sensor's own latest stored reading was at that
    # moment. Kept as an audit trail and re-fit source -- nothing here is
    # derived, so a fit can always be reproduced or redone with more points.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS calibration_points (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            sensor_id   TEXT NOT NULL,
            captured_at TEXT,    -- ISO 8601 UTC, when this point was added
            cct_raw     REAL,
            cie_x       REAL,
            cie_y       REAL,
            duv_raw     REAL,
            cct_ref     REAL,    -- the reference reading (meter, or a peer sensor's cct_cal)
            duv_ref     REAL,
            note        TEXT
        )
        """
    )
    # Where cct_ref/duv_ref came from: 'meter' for a typed-in reference-meter
    # reading, or the sensor_id of a peer sensor whose own cct_cal/duv_cal was
    # used instead (the "calibrate against sensor-00" chain). Existing rows
    # predate this column and stay NULL -- they're all meter points from
    # before peer calibration existed.
    try:
        conn.execute("ALTER TABLE calibration_points ADD COLUMN ref_source TEXT")
    except sqlite3.OperationalError:
        pass  # column already exists
    # Backlog ingest checks (sensor_id, timestamp) for every row it inserts, so
    # a batch re-sent after a lost ack doesn't double up. Also serves the
    # per-sensor time-range reads in /api/history and /api/frames. Building it
    # the first time on a large table takes a few seconds at startup.
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_readings_sensor_ts ON readings (sensor_id, timestamp)"
    )
    conn.commit()
    conn.close()


def solar_position(latitude, longitude, dt_utc):
    """Return (elevation_deg, azimuth_deg) for a UTC-aware datetime.

    dt_utc must be timezone-aware and in UTC. astral rejects naive datetimes.
    """
    obs = Observer(latitude=latitude, longitude=longitude)
    return elevation(obs, dt_utc), azimuth(obs, dt_utc)


def compute_duv(cie_x, cie_y):
    """Signed distance of a chromaticity from the Planckian locus, i.e. the tint.

    Uses the CIE 1960 UCS and the Ohno (2011) closed-form approximation. A
    positive Duv sits above the locus (greenish); negative sits below (magenta/
    pink). Accurate to a few parts in 1e-4 across the daylight band, which is
    ample for this uncalibrated sensor. CCT reports the nearest blackbody point
    and throws this axis away, so Duv is what recovers the true green/magenta
    cast. Returns None if the chromaticity is missing or degenerate.
    """
    if cie_x is None or cie_y is None:
        return None
    denom = -2.0 * cie_x + 12.0 * cie_y + 3.0
    if denom == 0:
        return None
    u = 4.0 * cie_x / denom
    v = 6.0 * cie_y / denom
    lfp = math.hypot(u - 0.292, v - 0.24)
    if lfp == 0:
        return 0.0
    # Angle from the reference point, clamped so float error can't push the
    # arccos argument just past +/-1.
    a = math.acos(max(-1.0, min(1.0, (u - 0.292) / lfp)))
    # Length of the locus at that angle: k6*a^6 + ... + k1*a + k0 (Horner form).
    k = (-0.00616793, 0.0893944, -0.5179722, 1.5317403,
         -2.4243787, 1.925865, -0.471106)
    lbb = k[0]
    for c in k[1:]:
        lbb = lbb * a + c
    return lfp - lbb


def planckian_locus_uv(cct):
    """The Planckian (black-body) locus in CIE 1960 UCS (u, v) at a given CCT.

    Krystek (1985) rational-polynomial approximation, valid ~1000-15000 K --
    comfortably covers this project's ~4500-10000 K daylight band. Used only
    to reconstruct a calibrated swatch colour from (cct_cal, duv_cal); the
    forward direction (measured x,y -> Duv) still goes through compute_duv
    above, a different but comparably accurate approximation -- round-tripping
    through both isn't bit-exact, just visually indistinguishable (checked
    2026-07-29: reconstruction error under 0.0005 in x,y for real daylight
    points, an order of magnitude below what's visible in a swatch).
    """
    t = cct
    u = (0.860117757 + 1.54118254e-4 * t + 1.28641212e-7 * t * t) / \
        (1 + 8.42420235e-4 * t + 7.08145163e-7 * t * t)
    v = (0.317398726 + 4.22806245e-5 * t + 4.20481691e-8 * t * t) / \
        (1 - 2.89741816e-5 * t + 1.61456053e-7 * t * t)
    return u, v


def xy_from_cct_duv(cct, duv):
    """Invert (CCT, Duv) back to CIE xy -- the calibrated counterpart of
    compute_duv, for rendering a swatch from the *corrected* chromaticity
    rather than the sensor's raw one.

    Finds the locus point at `cct`, its local tangent direction (via a 1 K
    finite difference), and offsets perpendicular to that tangent by `duv`.
    The perpendicular sign was fixed empirically against compute_duv's own
    sign convention (positive Duv = greenish) by round-tripping real daylight
    x,y points through compute_duv then back through this function and
    checking the reconstruction lands close to the original -- do not flip it
    without re-checking that. Returns None, None if cct is invalid.
    """
    if cct is None or cct <= 0:
        return None, None
    u0, v0 = planckian_locus_uv(cct)
    u1, v1 = planckian_locus_uv(cct + 1.0)
    du, dv = u1 - u0, v1 - v0
    tangent_len = math.hypot(du, dv)
    if tangent_len == 0:
        return None, None
    du, dv = du / tangent_len, dv / tangent_len
    # Perpendicular to the tangent, sign fixed as described above.
    nu, nv = dv, -du
    u = u0 + duv * nu
    v = v0 + duv * nv
    denom = 2 * u - 8 * v + 4
    if denom == 0:
        return None, None
    return 3 * u / denom, 2 * v / denom


def fit_linear(xs, ys):
    """Least-squares line y = slope*x + intercept, degrading gracefully.

    1 point: offset-only (slope fixed at 1), so a single calibration point
    still produces a usable correction. 2+ points: ordinary least squares,
    which passes exactly through both points in the 2-point case. Returns
    (slope, intercept, rms). Raises ValueError on an empty input.
    """
    n = len(xs)
    if n == 0:
        raise ValueError("need at least one point to fit")
    if n == 1:
        slope = 1.0
        intercept = ys[0] - slope * xs[0]
    else:
        mean_x = sum(xs) / n
        mean_y = sum(ys) / n
        sxx = sum((x - mean_x) ** 2 for x in xs)
        if sxx == 0:  # every x identical -- fall back to slope 1 like the 1-point case
            slope = 1.0
        else:
            sxy = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
            slope = sxy / sxx
        intercept = mean_y - slope * mean_x
    residuals = [y - (slope * x + intercept) for x, y in zip(xs, ys)]
    rms = math.sqrt(sum(r * r for r in residuals) / n)
    return slope, intercept, rms


def apply_calibration(cct, duv_raw, coeffs):
    """Apply a sensor's stored affine calibration to one raw cct/duv pair,
    and derive the calibrated chromaticity (cie_x_cal, cie_y_cal) from the
    result -- the corrected colour a swatch should actually render, since the
    whole point of calibrating against the C-800 is that the raw reading is
    the biased one, not the "more true" one.

    coeffs is (mired_a, mired_b, duv_c, duv_d), as stored in the `calibration`
    table. Any output is None if its inputs are missing or degenerate -- e.g.
    cct is None at night, duv is None on a degenerate chromaticity, or the
    calibrated chromaticity is left out entirely (None, None) whenever either
    cct_cal or duv_cal themselves came out None.

    Returns (cct_cal, duv_cal, cie_x_cal, cie_y_cal).
    """
    mired_a, mired_b, duv_c, duv_d = coeffs
    cct_cal = None
    if cct is not None and cct > 0:
        mired_cal = mired_a * (1e6 / cct) + mired_b
        if mired_cal > 0:
            cct_cal = 1e6 / mired_cal
    duv_cal = duv_c * duv_raw + duv_d if duv_raw is not None else None
    if cct_cal is not None and duv_cal is not None:
        cie_x_cal, cie_y_cal = xy_from_cct_duv(cct_cal, duv_cal)
    else:
        cie_x_cal = cie_y_cal = None
    return cct_cal, duv_cal, cie_x_cal, cie_y_cal


# A 3x3 matrix has 8 free parameters, so it needs well over 8 points spread
# across both axes (CCT and tint) to be trusted; below this the affine pair
# stays the correction.
MIN_MATRIX_POINTS = 12


def _solve_linear(a, b):
    """Solve a small dense system a.x = b by Gaussian elimination with partial
    pivoting (no numpy on the Droplet). Raises ValueError if singular."""
    n = len(b)
    m = [row[:] + [b[i]] for i, row in enumerate(a)]
    for col in range(n):
        piv = max(range(col, n), key=lambda r: abs(m[r][col]))
        if abs(m[piv][col]) < 1e-15:
            raise ValueError("singular system")
        m[col], m[piv] = m[piv], m[col]
        for r in range(col + 1, n):
            f = m[r][col] / m[col][col]
            for c in range(col, n + 1):
                m[r][c] -= f * m[col][c]
    x = [0.0] * n
    for r in range(n - 1, -1, -1):
        x[r] = (m[r][n] - sum(m[r][c] * x[c] for c in range(r + 1, n))) / m[r][r]
    return x


def fit_ccm(src, dst):
    """Least-squares 3x3 colour-correction matrix mapping raw chromaticity to
    reference chromaticity, as a projective transform of CIE xy:

        [X, Y, W] = H . [x, y, 1],   x_cal = X / W,   y_cal = Y / W

    This is a full 3x3 correction of the sensor's XYZ, seen in chromaticity
    (brightness divides out). Unlike the affine pair it captures the sensor's
    CCT and tint errors interacting -- found 2026-09-28: the tint response
    grows at low CCT, which no separate CCT/Duv correction can model.
    Leave-one-out on that session's 21 SkyPanel points per unit: ~30-50 K
    on-locus and ~0.001 Duv, vs ~140-260 K and ~0.003-0.005 for the affine
    pair. h33 is fixed at 1; coordinates are centred and scaled first to keep
    the normal equations well conditioned. Returns the 9 entries row-major.
    """
    n = len(src)
    cx = sum(p[0] for p in src) / n
    cy = sum(p[1] for p in src) / n
    s = max(max(abs(p[0] - cx), abs(p[1] - cy)) for p in src) or 1.0
    ata = [[0.0] * 8 for _ in range(8)]
    atb = [0.0] * 8
    for (x, y), (xr, yr) in zip(src, dst):
        u, v = (x - cx) / s, (y - cy) / s
        for row, rhs in (([u, v, 1, 0, 0, 0, -xr * u, -xr * v], xr),
                         ([0, 0, 0, u, v, 1, -yr * u, -yr * v], yr)):
            for i in range(8):
                atb[i] += row[i] * rhs
                for j in range(8):
                    ata[i][j] += row[i] * row[j]
    h = _solve_linear(ata, atb) + [1.0]
    # Fold the centring/scaling back in so the stored matrix takes raw x, y:
    # H_raw = H_norm . T, with T mapping (x, y, 1) to (u, v, 1).
    t = [[1 / s, 0, -cx / s], [0, 1 / s, -cy / s], [0, 0, 1]]
    hm = [h[0:3], h[3:6], h[6:9]]
    raw = [[sum(hm[i][k] * t[k][j] for k in range(3)) for j in range(3)] for i in range(3)]
    return [v for row in raw for v in row]


def apply_ccm(ccm, x, y):
    """Corrected (x, y) from raw (x, y) through a stored matrix, or (None, None)."""
    if x is None or y is None:
        return None, None
    w = ccm[6] * x + ccm[7] * y + ccm[8]
    if w == 0:
        return None, None
    return (ccm[0] * x + ccm[1] * y + ccm[2]) / w, (ccm[3] * x + ccm[4] * y + ccm[5]) / w


def cct_duv_from_xy(x, y):
    """CCT and Duv of a chromaticity against the Krystek locus -- the exact
    inverse of xy_from_cct_duv, so a matrix-corrected reading and the
    reference it was fitted to are measured on the same curve.

    Newton-style projection onto the locus, started from McCamy's CCT: step
    along the local tangent by the offset's component along it. Converges in a
    few iterations across 1500-25000 K. Returns (None, None) outside that range
    or for a degenerate chromaticity.
    """
    denom = -2.0 * x + 12.0 * y + 3.0
    if denom == 0 or (0.1858 - y) == 0:
        return None, None
    u, v = 4.0 * x / denom, 6.0 * y / denom
    nmc = (x - 0.3320) / (0.1858 - y)
    t = 449.0 * nmc ** 3 + 3525.0 * nmc ** 2 + 6823.3 * nmc + 5520.33
    t = min(max(t, 1500.0), 25000.0)
    for _ in range(30):
        u0, v0 = planckian_locus_uv(t)
        u1, v1 = planckian_locus_uv(t + 1.0)
        du, dv = u1 - u0, v1 - v0
        step = ((u - u0) * du + (v - v0) * dv) / (du * du + dv * dv)
        t = min(max(t + step, 1000.0), 30000.0)
        if abs(step) < 0.01:
            break
    if not 1500.0 <= t <= 25000.0:
        return None, None
    u0, v0 = planckian_locus_uv(t)
    u1, v1 = planckian_locus_uv(t + 1.0)
    du, dv = u1 - u0, v1 - v0
    tl = math.hypot(du, dv)
    # Signed so positive = greenish, matching xy_from_cct_duv / compute_duv.
    duv = ((u - u0) * dv - (v - v0) * du) / tl
    return t, duv


def apply_fit(fit, cct, duv_raw, cie_x, cie_y):
    """Apply a sensor's stored calibration row (mired_a, mired_b, duv_c, duv_d,
    ccm) to one reading. Uses the matrix when the fit has one, else the affine
    pair. Returns (cct_cal, duv_cal, cie_x_cal, cie_y_cal)."""
    mired_a, mired_b, duv_c, duv_d, ccm = fit
    if ccm:
        if isinstance(ccm, str):
            ccm = json.loads(ccm)
        # Night heartbeats and dark readings carry no usable colour; keep them
        # NULL like the affine path does when the device reports no CCT.
        if cct is None or cct <= 0:
            return None, None, None, None
        x_cal, y_cal = apply_ccm(ccm, cie_x, cie_y)
        if x_cal is None:
            return None, None, None, None
        cct_cal, duv_cal = cct_duv_from_xy(x_cal, y_cal)
        if cct_cal is None:
            return None, None, None, None
        return cct_cal, duv_cal, x_cal, y_cal
    return apply_calibration(cct, duv_raw, (mired_a, mired_b, duv_c, duv_d))


FIT_COLUMNS = "mired_a, mired_b, duv_c, duv_d, ccm"


def parse_db_timestamp(ts):
    """Parse a stored `timestamp` value into a UTC-aware datetime.

    Rows are stamped in UTC ('YYYY-MM-DD HH:MM:SS'), but the string carries no
    zone, so attach UTC explicitly for astral.
    """
    dt = datetime.fromisoformat(ts)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def to_local(ts_utc_iso, timezone_name):
    """Convert a stored UTC timestamp to local time using a sensor's IANA zone.

    Used for the "same local clock time" comparison: SQLite has no timezone
    awareness, so the conversion (with daylight saving handled) is done here in
    Python from the zone stored against the sensor.
    """
    return parse_db_timestamp(ts_utc_iso).astimezone(ZoneInfo(timezone_name))


def reading_time(data: dict, backlog: bool = False):
    """The UTC instant to file a reading under, or None to reject a backlog row.

    Firmware from 2026-09-24 sends `ts` (Unix seconds from its NTP-set clock)
    with every reading. A live reading uses it if it is within
    LIVE_TS_TOLERANCE of now (so a reading published a few seconds late, e.g.
    right after a Wi-Fi rejoin, still lands when it was taken), else falls
    back to the receive time, which is also what older firmware without `ts`
    gets. A backlog row has no receive-time fallback -- it is old by design --
    so one without a sane `ts` is rejected.
    """
    now = datetime.now(timezone.utc)
    ts = data.get("ts")
    try:
        device = datetime.fromtimestamp(int(ts), timezone.utc) if ts not in (None, "") else None
    except (TypeError, ValueError, OverflowError, OSError):
        device = None
    if backlog:
        if device is None or not (now - BACKLOG_MAX_AGE <= device <= now + BACKLOG_MAX_FUTURE):
            return None
        return device
    if device is not None and abs(device - now) <= LIVE_TS_TOLERANCE:
        return device
    return now


def store_reading(data: dict, when: Optional[datetime] = None, conn=None):
    """Insert one reading. `when` defaults to reading_time(data); pass `conn`
    to batch several inserts into one transaction (the caller then commits)."""
    own_conn = conn is None
    if own_conn:
        conn = sqlite3.connect(DB_PATH)

    sensor_id = data.get("sensor_id", "unknown")

    # Fix the instant once so the same UTC time is used for both the stored
    # timestamp and the solar maths. Format matches the old datetime('now')
    # rows so history stays uniform.
    now = when or reading_time(data)
    timestamp = now.strftime("%Y-%m-%d %H:%M:%S")

    # Look up where this unit sits. If it has not been registered yet, location
    # and solar fields stay NULL and are filled later by the backfill step.
    latitude = longitude = solar_elevation = solar_azimuth = None
    loc = conn.execute(
        "SELECT latitude, longitude FROM sensors WHERE sensor_id = ?",
        (sensor_id,),
    ).fetchone()
    if loc and loc[0] is not None and loc[1] is not None:
        latitude, longitude = loc[0], loc[1]
        solar_elevation, solar_azimuth = solar_position(latitude, longitude, now)

    # Tint (Duv) is computed here from the published chromaticity rather than on
    # the device, so it needs no reflash and old rows can be backfilled.
    cct_raw = data.get("cct")
    duv = compute_duv(data.get("cie_x"), data.get("cie_y"))

    # If this sensor has a fitted calibration, also store the corrected
    # cct/duv/chromaticity alongside the raw values -- never in place of them.
    cct_cal = duv_cal = cie_x_cal = cie_y_cal = None
    fit = conn.execute(
        f"SELECT {FIT_COLUMNS} FROM calibration WHERE sensor_id = ?",
        (sensor_id,),
    ).fetchone()
    if fit is not None:
        cct_cal, duv_cal, cie_x_cal, cie_y_cal = apply_fit(
            fit, cct_raw, duv, data.get("cie_x"), data.get("cie_y"))

    conn.execute(
        """
        INSERT INTO readings
            (timestamp, sensor_id, cct, lux, cie_x, cie_y, duv, cct_cal, duv_cal,
             cie_x_cal, cie_y_cal,
             battery_v, battery_pct, battery_temp_c,
             latitude, longitude, solar_elevation_deg, solar_azimuth_deg)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            timestamp,
            sensor_id,
            cct_raw,
            data.get("lux"),
            data.get("cie_x"),
            data.get("cie_y"),
            round(duv, 5) if duv is not None else None,
            round(cct_cal, 1) if cct_cal is not None else None,
            round(duv_cal, 5) if duv_cal is not None else None,
            round(cie_x_cal, 4) if cie_x_cal is not None else None,
            round(cie_y_cal, 4) if cie_y_cal is not None else None,
            data.get("battery_v"),
            data.get("battery_pct"),
            data.get("battery_temp_c"),
            latitude,
            longitude,
            solar_elevation,
            solar_azimuth,
        ),
    )
    if own_conn:
        conn.commit()
        conn.close()


def _to_float(v):
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def store_backlog(batch: dict):
    """Insert a store-and-forward batch; returns (inserted, skipped).

    Payload: {"sensor_id", "fields": [...], "csv": "row\nrow..."}, one CSV row
    per reading, fields in the order given (the first is always `ts`). Rows
    already on file for this sensor at the same second are skipped, which is
    what makes a re-sent batch harmless; rows with no usable `ts` are dropped.
    One transaction per batch.
    """
    sensor_id = batch.get("sensor_id") or "unknown"
    fields = batch.get("fields") or []
    inserted = skipped = 0
    # Generous lock timeout: a batch lost to "database is locked" is already
    # acknowledged, so it is gone for good.
    conn = sqlite3.connect(DB_PATH, timeout=30)
    try:
        for line in (batch.get("csv") or "").splitlines():
            values = line.strip().split(",")
            if len(values) != len(fields):
                skipped += 1
                continue
            row = dict(zip(fields, values))
            data = {k: _to_float(v) for k, v in row.items() if k != "ts"}
            data["ts"] = row.get("ts")
            data["sensor_id"] = sensor_id
            when = reading_time(data, backlog=True)
            if when is None:
                skipped += 1
                continue
            if conn.execute(
                "SELECT 1 FROM readings WHERE sensor_id = ? AND timestamp = ? LIMIT 1",
                (sensor_id, when.strftime("%Y-%m-%d %H:%M:%S")),
            ).fetchone():
                skipped += 1
                continue
            store_reading(data, when=when, conn=conn)
            inserted += 1
        conn.commit()
    finally:
        conn.close()
    return inserted, skipped


def register_sensor(conn, sensor_id, label, latitude, longitude, timezone_name,
                    altitude_m=None, exposure=None):
    """Insert or update a sensor's install metadata (location, zone, exposure)."""
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """
        INSERT INTO sensors
            (sensor_id, label, latitude, longitude, altitude_m, timezone,
             exposure, installed_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(sensor_id) DO UPDATE SET
            label=excluded.label, latitude=excluded.latitude,
            longitude=excluded.longitude, altitude_m=excluded.altitude_m,
            timezone=excluded.timezone, exposure=excluded.exposure,
            updated_at=excluded.updated_at
        """,
        (sensor_id, label, latitude, longitude, altitude_m, timezone_name,
         exposure, now, now),
    )
    conn.commit()


def backfill_solar(conn):
    """Fill location and solar fields on earlier rows that arrived before their
    unit was registered. Idempotent: only touches rows where
    solar_elevation_deg IS NULL and whose sensor now has a known location.
    Returns the number of rows updated.
    """
    rows = conn.execute(
        """
        SELECT r.id, r.timestamp, s.latitude, s.longitude
        FROM readings r
        JOIN sensors s ON s.sensor_id = r.sensor_id
        WHERE r.solar_elevation_deg IS NULL
          AND s.latitude IS NOT NULL
          AND s.longitude IS NOT NULL
        """
    ).fetchall()
    for row_id, ts, lat, lon in rows:
        dt_utc = parse_db_timestamp(ts)
        elev, azi = solar_position(lat, lon, dt_utc)
        conn.execute(
            """
            UPDATE readings
            SET latitude = ?, longitude = ?,
                solar_elevation_deg = ?, solar_azimuth_deg = ?
            WHERE id = ?
            """,
            (lat, lon, elev, azi, row_id),
        )
    conn.commit()
    return len(rows)


def backfill_duv(conn):
    """Fill Duv on earlier rows stored before the tint column existed.

    Idempotent: only touches rows where duv IS NULL but a chromaticity is
    present. Runs automatically at startup because Duv depends only on the
    already-stored cie_x/cie_y, never on external metadata. Returns the number
    of rows updated.
    """
    rows = conn.execute(
        """
        SELECT id, cie_x, cie_y FROM readings
        WHERE duv IS NULL AND cie_x IS NOT NULL AND cie_y IS NOT NULL
        """
    ).fetchall()
    updated = 0
    for row_id, cie_x, cie_y in rows:
        duv = compute_duv(cie_x, cie_y)
        if duv is None:
            continue
        conn.execute(
            "UPDATE readings SET duv = ? WHERE id = ?",
            (round(duv, 5), row_id),
        )
        updated += 1
    conn.commit()
    return updated


def add_calibration_point(conn, sensor_id, cct_ref, duv_ref, note=None):
    """Pair a reference-meter reading with this sensor's most recent stored
    reading and record it as a calibration point.

    There's no live handshake with the meter -- the workflow is "read the
    meter, then run this within the same reading interval" -- so this simply
    snapshots whatever the sensor last published. Raises ValueError if the
    sensor has no stored readings yet, or its latest reading lacks a valid
    cct/chromaticity to pair against.
    """
    row = conn.execute(
        "SELECT timestamp, cct, cie_x, cie_y, duv FROM readings "
        "WHERE sensor_id = ? ORDER BY id DESC LIMIT 1",
        (sensor_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"no stored readings for {sensor_id!r} to pair against")
    device_ts, cct_raw, cie_x, cie_y, duv_raw = row
    if cct_raw is None or duv_raw is None:
        raise ValueError(
            f"latest reading for {sensor_id!r} (at {device_ts}) is missing cct or duv"
        )
    captured_at = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """
        INSERT INTO calibration_points
            (sensor_id, captured_at, cct_raw, cie_x, cie_y, duv_raw,
             cct_ref, duv_ref, note, ref_source)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'meter')
        """,
        (sensor_id, captured_at, cct_raw, cie_x, cie_y, duv_raw, cct_ref, duv_ref, note),
    )
    conn.commit()
    return {
        "captured_at": captured_at,
        "device_reading_timestamp": device_ts,
        "cct_raw": cct_raw,
        "duv_raw": duv_raw,
        "cct_ref": cct_ref,
        "duv_ref": duv_ref,
    }


def add_calibration_point_vs_sensor(conn, sensor_id, reference_sensor_id, note=None):
    """Pair this sensor's most recent stored reading with another sensor's
    most recent *calibrated* reading, instead of a typed-in meter value.

    This is the "calibrate sensor-02 against sensor-00" chain: once a
    reference sensor has its own fit against the C-800, its cct_cal/duv_cal
    become a usable stand-in for ground truth, so other sensors placed beside
    it can be calibrated with no meter at all. Raises ValueError if the
    reference sensor has no fit yet (only raw values available -- pairing
    against those would just calibrate one sensor's bias onto another's,
    which defeats the point), or if either sensor has no stored readings.
    """
    ref_row = conn.execute(
        "SELECT timestamp, cct_cal, duv_cal FROM readings "
        "WHERE sensor_id = ? ORDER BY id DESC LIMIT 1",
        (reference_sensor_id,),
    ).fetchone()
    if ref_row is None:
        raise ValueError(f"no stored readings for reference sensor {reference_sensor_id!r}")
    ref_ts, cct_ref, duv_ref = ref_row
    if cct_ref is None or duv_ref is None:
        raise ValueError(
            f"reference sensor {reference_sensor_id!r} has no calibration fit yet "
            "-- calibrate it first"
        )

    row = conn.execute(
        "SELECT timestamp, cct, cie_x, cie_y, duv FROM readings "
        "WHERE sensor_id = ? ORDER BY id DESC LIMIT 1",
        (sensor_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"no stored readings for {sensor_id!r} to pair against")
    device_ts, cct_raw, cie_x, cie_y, duv_raw = row
    if cct_raw is None or duv_raw is None:
        raise ValueError(
            f"latest reading for {sensor_id!r} (at {device_ts}) is missing cct or duv"
        )

    captured_at = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """
        INSERT INTO calibration_points
            (sensor_id, captured_at, cct_raw, cie_x, cie_y, duv_raw,
             cct_ref, duv_ref, note, ref_source)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (sensor_id, captured_at, cct_raw, cie_x, cie_y, duv_raw, cct_ref, duv_ref,
         note, reference_sensor_id),
    )
    conn.commit()
    return {
        "captured_at": captured_at,
        "device_reading_timestamp": device_ts,
        "reference_reading_timestamp": ref_ts,
        "cct_raw": cct_raw,
        "duv_raw": duv_raw,
        "cct_ref": cct_ref,
        "duv_ref": duv_ref,
        "reference_sensor_id": reference_sensor_id,
    }


def remove_calibration_point(conn, point_id):
    """Delete one captured calibration point by id (e.g. a reading taken
    somewhere the sensor's own view of the sky was obstructed -- a partial
    shadow, not the open, unobstructed-sky condition the deployed unit will
    actually see -- so it doesn't belong in the fit at all, not just noise to
    average out). Does NOT re-fit or backfill; call fit_calibration yourself
    afterward to update the sensor's correction from the remaining points.
    Returns the removed row (as a dict) so the caller can confirm what went,
    or None if no point had that id. Doesn't touch conn.row_factory, so it's
    safe to call on a connection the caller reuses afterward (e.g. for a
    follow-up fit_calibration).
    """
    cur = conn.execute(
        "SELECT id, sensor_id, captured_at, cct_raw, cie_x, cie_y, duv_raw, "
        "cct_ref, duv_ref, note, ref_source FROM calibration_points WHERE id = ?",
        (point_id,),
    )
    row = cur.fetchone()
    if row is None:
        return None
    result = dict(zip([d[0] for d in cur.description], row))
    conn.execute("DELETE FROM calibration_points WHERE id = ?", (point_id,))
    conn.commit()
    return result


def fit_calibration(conn, sensor_id, notes=None):
    """Fit this sensor's correction from its captured calibration points and
    upsert it into the `calibration` table. Re-running this (e.g. after adding
    more points) simply replaces the previous fit -- it does not touch
    already-stored readings, see backfill_calibration for that. Raises
    ValueError if no points have been captured yet.

    Meter points take precedence: if the sensor has any, peer points
    (ref_source = another sensor) are left out, so a peer cross-check can sit
    in the table as an audit record without being folded into the fit. A
    sensor with no meter points is fitted from its peer points.

    The affine pair (mireds + Duv) is always fitted. With MIN_MATRIX_POINTS or
    more points the 3x3 matrix is fitted too and becomes the correction (see
    fit_ccm); the reported RMS is for whichever model is in use.
    """
    rows = conn.execute(
        "SELECT cct_raw, duv_raw, cie_x, cie_y, cct_ref, duv_ref, ref_source "
        "FROM calibration_points WHERE sensor_id = ?",
        (sensor_id,),
    ).fetchall()
    if not rows:
        raise ValueError(f"no calibration points captured for {sensor_id!r}")
    # NULL ref_source rows predate peer calibration and are all meter points.
    meter = [r for r in rows if r[6] in (None, "meter")]
    used = meter or rows
    excluded = len(rows) - len(used)

    mired_a, mired_b, _ = fit_linear([1e6 / r[0] for r in used], [1e6 / r[4] for r in used])
    duv_c, duv_d, _ = fit_linear([r[1] for r in used], [r[5] for r in used])

    ccm = None
    if len(used) >= MIN_MATRIX_POINTS and all(r[2] is not None and r[3] is not None for r in used):
        ccm = fit_ccm([(r[2], r[3]) for r in used],
                      [xy_from_cct_duv(r[4], r[5]) for r in used])

    # Report the fit-time residual in Kelvin (the unit anyone reading this
    # will actually think in) by applying the just-fitted correction back to
    # each point and diffing against the reference directly.
    fit = (mired_a, mired_b, duv_c, duv_d, ccm)
    k_res, d_res = [], []
    for cct_raw_i, duv_raw_i, x_i, y_i, cct_ref_i, duv_ref_i, _ in used:
        cct_cal_i, duv_cal_i, _, _ = apply_fit(fit, cct_raw_i, duv_raw_i, x_i, y_i)
        if cct_cal_i is not None:
            k_res.append(cct_ref_i - cct_cal_i)
        if duv_cal_i is not None:
            d_res.append(duv_ref_i - duv_cal_i)
    rms_cct = math.sqrt(sum(r * r for r in k_res) / len(k_res)) if k_res else None
    rms_duv = math.sqrt(sum(r * r for r in d_res) / len(d_res)) if d_res else None

    fitted_at = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """
        INSERT INTO calibration
            (sensor_id, mired_a, mired_b, duv_c, duv_d, ccm, n_points,
             rms_cct, rms_duv, fitted_at, notes)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(sensor_id) DO UPDATE SET
            mired_a=excluded.mired_a, mired_b=excluded.mired_b,
            duv_c=excluded.duv_c, duv_d=excluded.duv_d, ccm=excluded.ccm,
            n_points=excluded.n_points, rms_cct=excluded.rms_cct,
            rms_duv=excluded.rms_duv, fitted_at=excluded.fitted_at,
            notes=excluded.notes
        """,
        (sensor_id, mired_a, mired_b, duv_c, duv_d,
         json.dumps(ccm) if ccm else None, len(used),
         rms_cct, rms_duv, fitted_at, notes),
    )
    conn.commit()
    return {
        "sensor_id": sensor_id,
        "model": "matrix" if ccm else "affine",
        "n_points": len(used),
        "n_excluded_peer_points": excluded,
        "mired_a": mired_a, "mired_b": mired_b,
        "duv_c": duv_c, "duv_d": duv_d,
        "ccm": ccm,
        "rms_cct": rms_cct, "rms_duv": rms_duv,
    }


def backfill_calibration(conn, sensor_id):
    """Recompute cct_cal/duv_cal/cie_x_cal/cie_y_cal for every stored reading
    of one sensor from its current `calibration` row.

    Unlike backfill_solar/backfill_duv this recomputes unconditionally rather
    than only filling NULLs, because a re-fit (more points added later) must
    update history, not just gaps. Raises ValueError if the sensor has no fit
    yet. Returns the number of rows updated.
    """
    fit = conn.execute(
        f"SELECT {FIT_COLUMNS} FROM calibration WHERE sensor_id = ?",
        (sensor_id,),
    ).fetchone()
    if fit is None:
        raise ValueError(f"{sensor_id!r} has no calibration fit yet")
    fit = fit[:4] + ((json.loads(fit[4]) if fit[4] else None),)
    rows = conn.execute(
        "SELECT id, cct, duv, cie_x, cie_y FROM readings WHERE sensor_id = ?",
        (sensor_id,),
    ).fetchall()

    def updates():
        for row_id, cct, duv, cie_x, cie_y in rows:
            cct_cal, duv_cal, cie_x_cal, cie_y_cal = apply_fit(fit, cct, duv, cie_x, cie_y)
            yield (
                round(cct_cal, 1) if cct_cal is not None else None,
                round(duv_cal, 5) if duv_cal is not None else None,
                round(cie_x_cal, 4) if cie_x_cal is not None else None,
                round(cie_y_cal, 4) if cie_y_cal is not None else None,
                row_id,
            )

    conn.executemany(
        "UPDATE readings SET cct_cal = ?, duv_cal = ?, cie_x_cal = ?, cie_y_cal = ? WHERE id = ?",
        updates(),
    )
    conn.commit()
    return len(rows)


# --- MQTT subscriber setup ---

# The most recent full payload received over MQTT, per sensor_id, kept in memory
# (not the DB) so the page can show live diagnostic fields (raw channels,
# overload) without bloating the schema. Reset on restart; refilled by the next
# reading. Keyed by sensor_id so multiple units publishing concurrently don't
# overwrite each other's diagnostics.
latest_payloads: dict = {}
_last_sensor_id: Optional[str] = None  # most recently updated key in latest_payloads


def on_connect(client, userdata, flags, reason_code, properties=None):
    print(f"Connected to MQTT broker (reason code: {reason_code})")
    client.subscribe(MQTT_TOPIC)
    print(f"Subscribed to: {MQTT_TOPIC}")
    # QoS 1 so the broker holds the device to its acknowledged delivery all the
    # way through; the device only deletes a batch once its PUBACK arrives.
    client.subscribe(MQTT_BACKLOG_TOPIC, qos=1)
    print(f"Subscribed to: {MQTT_BACKLOG_TOPIC}")


def on_message(client, userdata, msg):
    global _last_sensor_id
    try:
        data = json.loads(msg.payload.decode())
        if msg.topic.startswith("cct-backlog/"):
            # Old readings: stored at their own time, but never shown as the
            # sensor's live diagnostics.
            inserted, skipped = store_backlog(data)
            print(f"Backlog from {data.get('sensor_id')}: {inserted} stored, {skipped} skipped")
            return
        store_reading(data)
        sensor_id = data.get("sensor_id", "unknown")
        latest_payloads[sensor_id] = data
        _last_sensor_id = sensor_id
        print(f"Stored reading from {msg.topic}: {data}")
    except Exception as e:
        print(f"Error handling message on {msg.topic}: {e}")


def build_mqtt_client():
    # A persistent session (fixed client id, clean_session=False) makes the
    # broker hold QoS 1 backlog batches while this backend is down or
    # restarting. Without it the broker would still PUBACK the device -- which
    # then deletes the batch -- and drop the message on the floor. The id is
    # per machine so a local dev run can never take over the Droplet's session.
    client_id = os.environ.get("MQTT_CLIENT_ID") or f"cct-backend-{socket.gethostname()}"
    client = mqtt.Client(
        callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
        client_id=client_id,
        clean_session=False,
    )
    if MQTT_USERNAME and MQTT_PASSWORD:
        client.username_pw_set(MQTT_USERNAME, MQTT_PASSWORD)
    client.on_connect = on_connect
    client.on_message = on_message
    return client


mqtt_client = build_mqtt_client()


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    # Fill Duv on any rows that predate the tint column (derivable from stored
    # cie_x/cie_y, so this needs no sensor metadata and is safe to run each boot).
    conn = sqlite3.connect(DB_PATH)
    filled = backfill_duv(conn)
    conn.close()
    if filled:
        print(f"Backfilled Duv on {filled} row(s).")
    mqtt_client.connect(MQTT_BROKER, MQTT_PORT, keepalive=60)
    mqtt_client.loop_start()  # runs the MQTT network loop in a background thread
    yield
    mqtt_client.loop_stop()
    mqtt_client.disconnect()


app = FastAPI(lifespan=lifespan)

# Allow browser-based clients (e.g. a p5.js sketch served from any origin)
# to fetch the read-only API. This is a public hobby API, so "*" is fine.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)


@app.get("/api/latest")
def get_latest(sensor_id: Optional[str] = None):
    """Return the most recent reading, optionally filtered by sensor_id."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    if sensor_id:
        row = conn.execute(
            "SELECT * FROM readings WHERE sensor_id = ? ORDER BY id DESC LIMIT 1",
            (sensor_id,),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT * FROM readings ORDER BY id DESC LIMIT 1"
        ).fetchone()
    conn.close()
    if row is None:
        return {"error": "no readings yet"}
    return dict(row)


@app.get("/api/history")
def get_history(limit: int = 50, sensor_id: Optional[str] = None):
    """Return the most recent N readings, oldest first."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    if sensor_id:
        rows = conn.execute(
            "SELECT * FROM readings WHERE sensor_id = ? ORDER BY id DESC LIMIT ?",
            (sensor_id, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM readings ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    conn.close()
    return [dict(r) for r in reversed(rows)]


@app.get("/api/sensors")
def get_sensors():
    """Return registered sensors with their install metadata and last-seen time.

    Sensors that have published readings but have not been registered yet still
    appear (with NULL location), so a freshly powered-on unit is visible before
    its location has been entered.
    """
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT
            ids.sensor_id            AS sensor_id,
            s.label                  AS label,
            s.latitude               AS latitude,
            s.longitude              AS longitude,
            s.altitude_m             AS altitude_m,
            s.timezone               AS timezone,
            s.exposure               AS exposure,
            s.installed_at           AS installed_at,
            s.updated_at             AS updated_at,
            last.last_seen           AS last_seen
        FROM (
            SELECT sensor_id FROM sensors
            UNION
            SELECT DISTINCT sensor_id FROM readings
        ) AS ids
        LEFT JOIN sensors s ON s.sensor_id = ids.sensor_id
        LEFT JOIN (
            SELECT sensor_id, MAX(timestamp) AS last_seen
            FROM readings GROUP BY sensor_id
        ) AS last ON last.sensor_id = ids.sensor_id
        ORDER BY ids.sensor_id
        """
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.get("/api/diag")
def get_diag(sensor_id: Optional[str] = None):
    """The most recent full payload received over MQTT for one sensor, including
    diagnostic fields (raw_x/y/z/w, zx_ratio, overload) that aren't stored in the
    database. Empty until that sensor's next reading arrives after a backend
    restart. Without sensor_id, falls back to whichever sensor published most
    recently (legacy single-sensor behaviour)."""
    if sensor_id:
        return latest_payloads.get(sensor_id) or {"error": "no readings yet"}
    if _last_sensor_id is None:
        return {"error": "no readings yet"}
    return latest_payloads.get(_last_sensor_id, {"error": "no readings yet"})


@app.get("/api/hourly")
def get_hourly(hours: int = 24, sensor_id: Optional[str] = None):
    """Return hourly-averaged readings, the most recent `hours` buckets, oldest first.

    Each bucket averages every reading whose timestamp falls within that UTC hour
    (e.g. "2026-06-22 13:00" covers 13:00:00–13:59:59 UTC). The current,
    still-in-progress hour appears as a running average that grows as new readings
    arrive. Hours with no readings are simply absent — the client fills the gaps.
    Convert the UTC `hour` string to local time in the browser for display.
    """
    hours = max(1, min(hours, 1000))  # sane bound
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    # Colour fields (cct, x, y, duv) average only over readings above the
    # display floor, so night noise below ~1 lux (including night heartbeats)
    # does not pollute the hourly colour. Lux and the sample count keep every
    # reading, since lux is meaningful even at night. An all-night bucket
    # therefore reports its true low lux with NULL colour fields.
    select = (
        "SELECT strftime('%Y-%m-%d %H:00', timestamp) AS hour, "
        f"ROUND(AVG(CASE WHEN lux >= {DISPLAY_LUX_FLOOR} THEN cct END), 1) AS cct, "
        "ROUND(AVG(lux), 2) AS lux, "
        f"ROUND(AVG(CASE WHEN lux >= {DISPLAY_LUX_FLOOR} THEN cie_x END), 4) AS cie_x, "
        f"ROUND(AVG(CASE WHEN lux >= {DISPLAY_LUX_FLOOR} THEN cie_y END), 4) AS cie_y, "
        f"ROUND(AVG(CASE WHEN lux >= {DISPLAY_LUX_FLOOR} THEN duv END), 5) AS duv, "
        f"ROUND(AVG(CASE WHEN lux >= {DISPLAY_LUX_FLOOR} THEN cct_cal END), 1) AS cct_cal, "
        f"ROUND(AVG(CASE WHEN lux >= {DISPLAY_LUX_FLOOR} THEN duv_cal END), 5) AS duv_cal, "
        f"ROUND(AVG(CASE WHEN lux >= {DISPLAY_LUX_FLOOR} THEN cie_x_cal END), 4) AS cie_x_cal, "
        f"ROUND(AVG(CASE WHEN lux >= {DISPLAY_LUX_FLOOR} THEN cie_y_cal END), 4) AS cie_y_cal, "
        "COUNT(*) AS samples FROM readings "
    )
    if sensor_id:
        rows = conn.execute(
            select + "WHERE sensor_id = ? GROUP BY hour ORDER BY hour DESC LIMIT ?",
            (sensor_id, hours),
        ).fetchall()
    else:
        rows = conn.execute(
            select + "GROUP BY hour ORDER BY hour DESC LIMIT ?",
            (hours,),
        ).fetchall()
    conn.close()
    return [dict(r) for r in reversed(rows)]


@app.get("/api/frames")
def get_frames(
    bucket: float = 1.0,
    start: Optional[str] = None,
    end: Optional[str] = None,
    sensor_id: Optional[str] = None,
):
    """Bucket-averaged readings for building a colour timelapse video.

    Averages every reading into fixed-width `bucket`-second windows aligned to
    the UTC epoch, oldest first. This decouples video frame rate from the raw
    capture rate: re-query the same dataset with a different `bucket` to retime
    the video (frames = video_length_s * fps; bucket = daylight_window_s / frames).
    `start`/`end` (UTC "YYYY-MM-DD HH:MM:SS", inclusive) scope the query to a
    single daylight window; omit to bucket the whole table.

    Colour fields (cct, cie_x, cie_y, duv, and their _cal counterparts) average
    only over readings above DISPLAY_LUX_FLOOR, matching /api/hourly, so
    night/dusk noise below the sensor's floor doesn't pollute a frame's colour
    -- a bucket with no reading above the floor comes back with those fields
    NULL rather than a misleading near-black average. Lux and sample count
    keep every reading.
    """
    bucket_s = max(1, int(bucket))
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    where = []
    params: list = []
    if sensor_id:
        where.append("sensor_id = ?")
        params.append(sensor_id)
    if start:
        where.append("timestamp >= ?")
        params.append(start)
    if end:
        where.append("timestamp <= ?")
        params.append(end)
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    rows = conn.execute(
        "SELECT datetime((CAST(strftime('%s', timestamp) AS INTEGER) / ?) * ?, 'unixepoch') AS bucket_start, "
        f"ROUND(AVG(CASE WHEN lux >= {DISPLAY_LUX_FLOOR} THEN cct END), 1) AS cct, "
        "ROUND(AVG(lux), 2) AS lux, "
        f"ROUND(AVG(CASE WHEN lux >= {DISPLAY_LUX_FLOOR} THEN cie_x END), 4) AS cie_x, "
        f"ROUND(AVG(CASE WHEN lux >= {DISPLAY_LUX_FLOOR} THEN cie_y END), 4) AS cie_y, "
        f"ROUND(AVG(CASE WHEN lux >= {DISPLAY_LUX_FLOOR} THEN duv END), 5) AS duv, "
        f"ROUND(AVG(CASE WHEN lux >= {DISPLAY_LUX_FLOOR} THEN cct_cal END), 1) AS cct_cal, "
        f"ROUND(AVG(CASE WHEN lux >= {DISPLAY_LUX_FLOOR} THEN duv_cal END), 5) AS duv_cal, "
        f"ROUND(AVG(CASE WHEN lux >= {DISPLAY_LUX_FLOOR} THEN cie_x_cal END), 4) AS cie_x_cal, "
        f"ROUND(AVG(CASE WHEN lux >= {DISPLAY_LUX_FLOOR} THEN cie_y_cal END), 4) AS cie_y_cal, "
        "COUNT(*) AS samples "
        f"FROM readings {where_sql} "
        "GROUP BY bucket_start ORDER BY bucket_start",
        [bucket_s, bucket_s] + params,
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def _ota_authorised(token: Optional[str]) -> bool:
    """True if OTA access is allowed. Open unless OTA_TOKEN is configured."""
    return not OTA_TOKEN or token == OTA_TOKEN


def _calibration_authorised(token: Optional[str]) -> bool:
    """True if calibration writes are allowed. Open unless CALIBRATION_TOKEN
    is configured (see the comment by its definition above)."""
    return not CALIBRATION_TOKEN or token == CALIBRATION_TOKEN


@app.get("/api/calibration/status")
def calibration_status(sensor_id: str):
    """Current fit (if any) and every captured point for one sensor. Read-only,
    so no token needed -- matches the rest of the public GET API."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        coeffs = conn.execute(
            "SELECT * FROM calibration WHERE sensor_id = ?", (sensor_id,)
        ).fetchone()
        points = conn.execute(
            "SELECT * FROM calibration_points WHERE sensor_id = ? ORDER BY id",
            (sensor_id,),
        ).fetchall()
    finally:
        conn.close()
    return {
        "calibration": dict(coeffs) if coeffs else None,
        "points": [dict(p) for p in points],
    }


@app.post("/api/calibration/add-point")
def calibration_add_point(
    sensor_id: str,
    cct_ref: float,
    duv_ref: float,
    note: Optional[str] = None,
    token: Optional[str] = None,
):
    """Pair a reference-meter reading with this sensor's most recent stored
    reading. The debug page's calibration form calls this directly; it's the
    same operation as calibrate_sensor.py's add-point subcommand, for use
    during a live session without needing SSH access."""
    if not _calibration_authorised(token):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    conn = sqlite3.connect(DB_PATH)
    try:
        result = add_calibration_point(conn, sensor_id, cct_ref, duv_ref, note=note)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    finally:
        conn.close()
    return result


@app.post("/api/calibration/add-point-vs-sensor")
def calibration_add_point_vs_sensor(
    sensor_id: str,
    reference_sensor_id: str,
    note: Optional[str] = None,
    token: Optional[str] = None,
):
    """Pair this sensor's latest reading with another (already-calibrated)
    sensor's latest cct_cal/duv_cal, instead of a typed-in meter reading --
    the "calibrate against sensor-00" chain, for units that don't have their
    own meter session. See add_calibration_point_vs_sensor's docstring."""
    if not _calibration_authorised(token):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    conn = sqlite3.connect(DB_PATH)
    try:
        result = add_calibration_point_vs_sensor(
            conn, sensor_id, reference_sensor_id, note=note
        )
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    finally:
        conn.close()
    return result


@app.post("/api/calibration/fit")
def calibration_fit(
    sensor_id: str,
    notes: Optional[str] = None,
    token: Optional[str] = None,
):
    """Fit both affine corrections from this sensor's captured points and
    backfill cct_cal/duv_cal across its stored history."""
    if not _calibration_authorised(token):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    conn = sqlite3.connect(DB_PATH)
    try:
        result = fit_calibration(conn, sensor_id, notes=notes)
        result["backfilled"] = backfill_calibration(conn, sensor_id)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    finally:
        conn.close()
    return result


@app.get("/firmware/manifest.json")
def firmware_manifest(token: Optional[str] = None):
    """Tiny JSON the device polls: {"version": N, "size": bytes}. The device
    downloads new firmware only when this version exceeds the one it last
    applied, so this is cheap to hit often."""
    if not _ota_authorised(token):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    path = os.path.join(FIRMWARE_DIR, "manifest.json")
    if not os.path.exists(path):
        return JSONResponse({"error": "no firmware staged"}, status_code=404)
    return FileResponse(path, media_type="application/json")


@app.get("/firmware/code.py")
def firmware_code(token: Optional[str] = None):
    """The staged device firmware. The device size-checks this against the
    manifest before overwriting its own code.py, so a truncated download is
    rejected rather than installed."""
    if not _ota_authorised(token):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    path = os.path.join(FIRMWARE_DIR, "code.py")
    if not os.path.exists(path):
        return JSONResponse({"error": "no firmware staged"}, status_code=404)
    return FileResponse(path, media_type="text/x-python")


@app.get("/")
def index():
    """Just a small "8 Minutes 20" text link (top-left, styled to match
    8minutes20.studio) back to the main site -- everything else on this
    domain is /debug (see 2026-09-17: the old p5.js full-screen colour view
    that used to live here, and main.js it depended on, are retired)."""
    return FileResponse(os.path.join(os.path.dirname(__file__), "static", "index.html"))


@app.get("/debug")
def debug():
    """The logging/diagnostics page (live CCT swatch, raw channels, battery)."""
    return FileResponse(os.path.join(os.path.dirname(__file__), "static", "debug.html"))


app.mount("/static", StaticFiles(directory=os.path.join(os.path.dirname(__file__), "static")), name="static")
