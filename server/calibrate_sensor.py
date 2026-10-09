#!/usr/bin/env python3
"""Capture and fit a per-sensor CCT + tint calibration against a reference meter.

The OPT4048 computes CCT from its own X/Y/Z channels, but a sensor behind a
diffuser/window drifts from the truth -- a warm/cool bias and a green-magenta
(tint/Duv) bias. This script builds a small affine correction for each axis
from a handful of paired readings (device vs. a professional colour meter)
and applies it server-side, without touching the device or its firmware.

Deployment constraint: each sensor sits permanently outside in open sky, so
it only ever sees daylight -- roughly 4500 K (low sun) up to 8000-10000 K+
(clear blue sky / overcast). Capture points across that band only, in situ
over real sky conditions (not indoor lamps) -- an indoor/tungsten point is
outside the sensor's operating range and would only degrade the fit where it
actually matters:

    - clear low sun / near golden hour   (~4500-5500 K)
    - clear midday                        (~5500-6500 K)
    - heavy overcast                      (~6500-7500 K)
    - clear blue sky / open shade         (~7500-10000 K+)

"Open shade" means uniformly overcast sky or the shaded side of an open
site, not a spot where a building/roofline physically blocks part of the
sky. A deployed unit sits somewhere with an unobstructed view of the sky,
so it will never see that partial-obstruction geometry -- and readings
taken there aren't just noisy, they're not even measuring the same thing
the meter is (a wide-FOV sensor integrating a mixed scene vs. a narrow-spot
meter reading a sliver of pure blue sky), so the pairing itself is invalid.
Caught 2026-07-29 from a kitchen-roof test spot that fell into its own
shadow after ~5pm and produced ~11-12000 K meter readings -- see
remove-point below.

Only one sensor needs the meter directly: the designated reference unit
(sensor-00-calibration-source), which stays permanently on the bench for
exactly this reason. Every other unit is calibrated against *it* instead --
placed beside it, no meter needed -- since its own cct_cal/duv_cal are
already a trustworthy stand-in for ground truth once it has a fit.

Workflow, five subcommands:

    1. add-point  -- aim the meter at the same light and geometry as the
       sensor, read its CCT + Duv, then run this within the same reading
       interval. It pairs the meter's numbers with whatever the sensor most
       recently published (nothing is read from the meter automatically).
       Use this for the reference sensor only.

           python3 calibrate_sensor.py add-point \\
               --sensor-id sensor-00-calibration-source \\
               --cct-ref 5200 --duv-ref 0.0021 \\
               --note "clear midday, garden table"

    2. add-point-vs-sensor  -- for every other unit: place it beside an
       already-calibrated reference sensor and pair its latest reading with
       the reference's latest cct_cal/duv_cal (no meter). Fails if the
       reference sensor has no fit yet.

           python3 calibrate_sensor.py add-point-vs-sensor \\
               --sensor-id sensor-02 \\
               --reference-sensor-id sensor-00-calibration-source \\
               --note "clear midday, garden table"

    3. fit  -- once a few points are captured (either kind), fit both affine
       corrections and backfill cct_cal/duv_cal across that sensor's history.

           python3 calibrate_sensor.py fit --sensor-id sensor-00-calibration-source

    4. show -- print the current coefficients and every captured point, e.g.
       to sanity-check before re-fitting, or to compare two sensors' fits.

           python3 calibrate_sensor.py show --sensor-id sensor-00-calibration-source

    5. remove-point -- drop a bad point by id (from `show`'s point listing),
       e.g. one captured somewhere the sensor's view of the sky turned out
       to be obstructed. Doesn't re-fit by itself -- run fit again afterward
       so the correction reflects the remaining points.

           python3 calibrate_sensor.py remove-point --id 20
           python3 calibrate_sensor.py fit --sensor-id sensor-00-calibration-source

Run with no arguments (or -h) for the full option list on any subcommand.
"""

import argparse
import sqlite3
import json
import sys
from datetime import datetime, timezone

from main import (
    DB_PATH,
    add_calibration_point,
    add_calibration_point_vs_sensor,
    backfill_calibration,
    fit_calibration,
    init_db,
    remove_calibration_point,
)


def cmd_add_point(args):
    init_db()
    conn = sqlite3.connect(DB_PATH)
    try:
        result = add_calibration_point(
            conn, args.sensor_id, args.cct_ref, args.duv_ref, note=args.note,
        )
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    finally:
        conn.close()

    print(
        f"Captured point for {args.sensor_id!r}: "
        f"device cct={result['cct_raw']:.1f} duv={result['duv_raw']:.5f}  <->  "
        f"meter cct={args.cct_ref:.1f} duv={args.duv_ref:.5f}"
    )

    device_ts = datetime.fromisoformat(result["device_reading_timestamp"])
    if device_ts.tzinfo is None:
        device_ts = device_ts.replace(tzinfo=timezone.utc)
    age_s = (datetime.now(timezone.utc) - device_ts).total_seconds()
    if age_s > 30:
        print(
            f"Warning: the paired device reading is {age_s:.0f}s old -- make "
            "sure the meter reading was taken at roughly the same moment.",
            file=sys.stderr,
        )


def cmd_add_point_vs_sensor(args):
    init_db()
    conn = sqlite3.connect(DB_PATH)
    try:
        result = add_calibration_point_vs_sensor(
            conn, args.sensor_id, args.reference_sensor_id, note=args.note,
        )
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    finally:
        conn.close()

    print(
        f"Captured point for {args.sensor_id!r}: "
        f"device cct={result['cct_raw']:.1f} duv={result['duv_raw']:.5f}  <->  "
        f"{args.reference_sensor_id} cct={result['cct_ref']:.1f} duv={result['duv_ref']:.5f}"
    )

    for label, ts_str in (
        ("device", result["device_reading_timestamp"]),
        ("reference", result["reference_reading_timestamp"]),
    ):
        ts = datetime.fromisoformat(ts_str)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        age_s = (datetime.now(timezone.utc) - ts).total_seconds()
        if age_s > 60:
            print(
                f"Warning: the paired {label} reading is {age_s:.0f}s old -- "
                "make sure both sensors are live and sitting together.",
                file=sys.stderr,
            )


def cmd_fit(args):
    init_db()
    conn = sqlite3.connect(DB_PATH)
    try:
        result = fit_calibration(conn, args.sensor_id, notes=args.notes)
        filled = backfill_calibration(conn, args.sensor_id)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    finally:
        conn.close()

    print(f"Fitted {args.sensor_id!r} from {result['n_points']} point(s), "
          f"model: {result['model']}:")
    if result["n_excluded_peer_points"]:
        print(f"  ({result['n_excluded_peer_points']} peer point(s) left out -- meter points take precedence)")
    if result["ccm"]:
        print("  3x3 matrix on raw xy: " + ", ".join(f"{v:.5g}" for v in result["ccm"]))
    print(f"  mireds:  cal = {result['mired_a']:.4f} * raw + {result['mired_b']:.2f}"
          + ("  (fallback only)" if result["ccm"] else ""))
    print(f"  duv:     cal = {result['duv_c']:.4f} * raw + {result['duv_d']:.5f}"
          + ("  (fallback only)" if result["ccm"] else ""))
    if result["rms_cct"] is not None:
        print(f"  RMS residual: {result['rms_cct']:.1f} K, {result['rms_duv']:.5f} Duv")
    print(f"Backfilled {filled} historical reading(s).")


def cmd_show(args):
    init_db()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        coeffs = conn.execute(
            "SELECT * FROM calibration WHERE sensor_id = ?", (args.sensor_id,),
        ).fetchone()
        points = conn.execute(
            "SELECT * FROM calibration_points WHERE sensor_id = ? ORDER BY id",
            (args.sensor_id,),
        ).fetchall()
    finally:
        conn.close()

    if coeffs:
        print(f"Calibration for {args.sensor_id!r} (fitted {coeffs['fitted_at']}):")
        if coeffs["ccm"]:
            print("  model: 3x3 matrix on raw xy: " + ", ".join(f"{v:.5g}" for v in json.loads(coeffs["ccm"])))
        print(f"  mireds: cal = {coeffs['mired_a']:.4f} * raw + {coeffs['mired_b']:.2f}"
              + ("  (fallback only)" if coeffs["ccm"] else ""))
        print(f"  duv:    cal = {coeffs['duv_c']:.4f} * raw + {coeffs['duv_d']:.5f}"
              + ("  (fallback only)" if coeffs["ccm"] else ""))
        rms_bits = []
        if coeffs["rms_cct"] is not None:
            rms_bits.append(f"{coeffs['rms_cct']:.1f} K")
        if coeffs["rms_duv"] is not None:
            rms_bits.append(f"{coeffs['rms_duv']:.5f} Duv")
        rms_str = ", ".join(rms_bits) if rms_bits else "n/a"
        print(f"  fitted from {coeffs['n_points']} point(s), RMS {rms_str}")
        if coeffs["notes"]:
            print(f"  notes: {coeffs['notes']}")
    else:
        print(f"No fit yet for {args.sensor_id!r}.")

    print(f"\n{len(points)} captured point(s):")
    for p in points:
        note = f"  ({p['note']})" if p["note"] else ""
        source = p["ref_source"] or "meter"
        print(
            f"  [{p['id']}] {p['captured_at']}  "
            f"device cct={p['cct_raw']:.1f} duv={p['duv_raw']:.5f}  <->  "
            f"{source} cct={p['cct_ref']:.1f} duv={p['duv_ref']:.5f}{note}"
        )


def cmd_remove_point(args):
    init_db()
    conn = sqlite3.connect(DB_PATH)
    try:
        removed = remove_calibration_point(conn, args.id)
    finally:
        conn.close()

    if removed is None:
        print(f"Error: no calibration point with id {args.id}", file=sys.stderr)
        sys.exit(1)

    note = f"  ({removed['note']})" if removed["note"] else ""
    print(
        f"Removed point [{removed['id']}] for {removed['sensor_id']!r}: "
        f"device cct={removed['cct_raw']:.1f} duv={removed['duv_raw']:.5f}  <->  "
        f"{removed['ref_source'] or 'meter'} cct={removed['cct_ref']:.1f} "
        f"duv={removed['duv_ref']:.5f}{note}"
    )
    print(
        f"Note: the existing fit for {removed['sensor_id']!r} still reflects "
        "this point -- run `fit` again to update it from what's left."
    )


def main():
    parser = argparse.ArgumentParser(
        description="Capture and fit a per-sensor CCT + tint calibration against a reference meter.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_add = sub.add_parser(
        "add-point",
        help="Pair the sensor's latest reading with a meter reading, taken now.",
    )
    p_add.add_argument("--sensor-id", required=True)
    p_add.add_argument("--cct-ref", type=float, required=True, help="Meter's CCT reading, Kelvin")
    p_add.add_argument("--duv-ref", type=float, required=True, help="Meter's Duv (tint) reading")
    p_add.add_argument("--note", default=None, help="e.g. 'clear midday, garden table'")
    p_add.set_defaults(func=cmd_add_point)

    p_addvs = sub.add_parser(
        "add-point-vs-sensor",
        help="Pair this sensor's latest reading with a reference sensor's cct_cal/duv_cal.",
    )
    p_addvs.add_argument("--sensor-id", required=True, help="The sensor being calibrated")
    p_addvs.add_argument(
        "--reference-sensor-id", required=True,
        help="An already-calibrated sensor sitting beside it, e.g. sensor-00-calibration-source",
    )
    p_addvs.add_argument("--note", default=None, help="e.g. 'clear midday, garden table'")
    p_addvs.set_defaults(func=cmd_add_point_vs_sensor)

    p_fit = sub.add_parser(
        "fit", help="Fit both affine corrections from this sensor's captured points.",
    )
    p_fit.add_argument("--sensor-id", required=True)
    p_fit.add_argument("--notes", default=None)
    p_fit.set_defaults(func=cmd_fit)

    p_show = sub.add_parser(
        "show", help="Print this sensor's stored coefficients and captured points.",
    )
    p_show.add_argument("--sensor-id", required=True)
    p_show.set_defaults(func=cmd_show)

    p_remove = sub.add_parser(
        "remove-point", help="Delete one captured point by id (see `show`'s listing).",
    )
    p_remove.add_argument("--id", type=int, required=True, dest="id")
    p_remove.set_defaults(func=cmd_remove_point)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
