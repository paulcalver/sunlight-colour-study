# Sunlight Colour Study

The sun's colours, gathered from many places into one view, moment by moment.

The light that leaves the sun is the same white for all of us. Everything that happens to its colour happens here: miles of air, cloud, dust, the haze over a city. All of it the same sun, met differently.

Small sensors on rooftops in different parts of the world read the colour of the daylight above them. For now there is one, in London, and the days gather on the project page as cells played in step, so one morning can be watched beside another.

You already know this light. It is the cool blue of an overcast day, the warmth of low sun, falling on you while your eye quietly edits it away. Alone, each colour is almost nothing. Together, their differences make each one visible, first as days, then as places.

A work in progress.

**[8minutes20.studio/sunlight-colour-study](https://8minutes20.studio/sunlight-colour-study)**

![A sensor unit mounted at the top of a pole, seen from below against bright sky — the enclosure with its dome facing up, solar panel angled beneath it, sun and cumulus cloud behind.](media/deployed-prototype-still.jpg)

## Status

Three units, all rebuilt around new optics and recalibrated in September 2026:

- `sensor-00` is the bench reference. It stays indoors, and every future unit is checked against it.
- `sensor-01` has been on a London rooftop since 6 October 2026. Each finished day becomes a cell in the grid.
- `sensor-02` is on the bench, waiting for a site.

Still to do: check the calibration against the reference meter under real sun.

## How it works

Each unit is a solar-powered, hand-built sensor: an OPT4048 tristimulus colour sensor under a diffusing dome, read by an Adafruit QT Py ESP32-S2 and published over MQTT. The server adds tint (Duv), solar position and a per-unit calibration, and renders the measured chromaticity as true colour for the grid.

- [How it works](docs/how-it-works.md): hardware, the path from sensor to colour, and calibration.
- [The dome and UV](docs/dome-and-uv.md): how the original dome fluoresced in sunlight and pushed readings about 1,000K too blue, and the fix.

## This repository

    firmware/     CircuitPython for the QT Py ESP32-S2. Reads the OPT4048,
                  derives CCT on-device, publishes over MQTT. Strongest-network
                  Wi-Fi with fallbacks, NTP-aligned capture, an offline backlog
                  sent on reconnect, a remotely settable publish interval, and
                  optional OTA pull.
    mechanical/   STEP exports of the IP65 enclosure and its pole and
                  wall-mount brackets, built around the LIT Systems dome.
                  Generated from a parametric model, so geometry is derived
                  rather than drawn: change a component and the box follows.
    server/       Ingests MQTT, computes Duv and solar position, stores
                  readings, serves 10-minute samples for the daily grid, and
                  holds the calibration and diagnostics pages.
    docs/         The longer write-ups linked above.

Host addresses in `firmware/qt-py/settings.toml.example` are placeholders. Set `MQTT_BROKER`, `MQTT_USERNAME`, `MQTT_PASSWORD` and, if you want over-the-air updates, `OTA_BASE_URL` for your own deployment.

A full build guide — bill of materials, wiring, assembly, print settings — is still to come.

## Contact

**Paul Calver** — [paulcalver@me.com](mailto:paulcalver@me.com)

Get in touch about putting a sensor on a roof somewhere the network does not yet reach, or about the project generally.

## Licence

MIT, see [LICENSE](LICENSE). The CircuitPython libraries under `firmware/qt-py/lib/` are redistributed from the Adafruit bundle and keep their own MIT copyright.
