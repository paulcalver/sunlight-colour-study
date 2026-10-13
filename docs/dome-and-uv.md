# The dome and UV

Field readings through the summer of 2026 showed a colour-temperature error that tracked ambient UV rather than the light being measured: on the first outdoor comparison against a reference meter, all three units read between 900K and 1,160K too high, consistently, within seconds of each other. Duv barely moved, which is the signature of added blue light rather than a mis-aimed sensor or a bad fit.

## The cause

The cause was optical brighteners fluorescing under sunlight — the material absorbs ultraviolet and re-emits it as visible blue, straight into the sensor. A 365nm test in August 2026 confirmed the source: the dome glowed strongly, the printed mixing tube behind it only slightly. Studio calibration had been structurally blind to this, because the lamp used for it emits almost no UV. The error only ever appeared in real sun.

## Why calibration could not fix it

A [calibration fit](how-it-works.md#calibration-computational) can absorb a bias that is stable, or one that varies with colour temperature itself, because both are recoverable from the reading. It cannot absorb one driven by ambient UV, which varies independently of the colour being measured. The fix had to be a material change rather than a better fit.

## The fix

**LIT Systems**, who make the Lit Duo 1 spectrometer, supplied dome diffusers, UV/IR cutoff filters and black foam mounting rings from their own instrument — known-good parts from a working commercial colour meter rather than another material of unknown provenance. All of them pass the 365nm test. Domes were chosen over the flat diffusers deliberately, to keep the warmth of low sun in the readings rather than have it cosine-weighted away.

The enclosure has been reworked around them, and the printed mixing chamber is gone entirely: the foam ring mounts the filter directly over the OPT4048 and the dome sits close above it, so the light path is dome, filter, sensor, with no white plastic in it anywhere. That second change is not only about fluorescence — white ASA yellows over time, a slow, invisible spectral drift that would quietly invalidate any calibration fit. The files in [`mechanical/`](../mechanical/) are this revision.

Whether the new optics have removed the error for good is still to be confirmed by a check against the reference meter under real sun.
