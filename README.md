# Laser Writer

An English-language desktop application for converting GDS polygons into
laser exposure paths and sending reviewed G-code to a FluidNC controller.

## Run

On Windows 10/11 x64, extract the release archive and open `Laser Writer Open.exe`.
Python is included in the EXE. No startup password is required.

To run the source (tested with Python 3.14 on Windows 11):

```console
python -m pip install -r requirements.txt
python start_laser_writer.py
```

1. Open a GDS file and select its cell and layer.
2. Set writing parameters and hardware reference values. Run **Check design**.
3. Generate G-code, inspect the preview, and approve the reviewed code.
4. Connect FluidNC, set the work origin, and send the job.

The main application also includes a PD monitor using serial or TCP input.
The receiving ESP32 firmware is supplied separately by the hardware operator.

## Parameters and motion

- Writing parameters change scan spacing, feed, laser S output, overscan and dwell.
- Steps/mm controls coordinate rounding; match the motor microsteps and firmware.
  A 1.8-degree motor, 1 mm lead and 1/16 microstepping uses 3200 steps/mm.
- Validation thresholds check geometry and machine bounds; they do not scale it.
- Firmware reference values do not configure the controller.
- Measured writing width affects Y edge inset, scan-row placement and preview.
  It does not change physical focus. Measure it at the intended power and feed.
- Maximum hatch spacing is an upper bound, not necessarily the final spacing.
  Actual spacing is no larger than this value or the measured writing width.
- X bidirectional compensation accepts -100 to +100 um (default 0). Positive
  values shift right-to-left exposure left; negative values shift it right.
  Forward exposure, manual moves and homing are unchanged. Match steps/mm.

For a 100 um-high rectangle with writing width 40 um and maximum spacing
40 um, the row centers are 20, 50 and 80 um above its lower edge. Each
disconnected component gets its own evenly distributed row grid. Both the
interactive preview and PNG use physical-width exposure bands, not screen
line thickness. Darker overlaps indicate geometry, not calculated dose.

Overlap increases exposure; test a small sample before a full job. The current
planner aligns rows to each component's Y bounds, not a complete spot-size
contour compensation: X endcaps can extend beyond the outline; sloped edges,
holes and thin branches can have gaps or overruns and produce a warning.
Regenerate and review G-code after changing these parameters. Existing G-code
and controller settings are not modified.

Exposure and laser-off travel use the same feed. Writing feed is capped at
10 mm/min; manual feed is capped at 100 mm/min. Scan filling uses horizontal
serpentine rows. Geometry checks are approximate, not manufacturing sign-off.
Pause/Resume preserves manual travel; Force Stop aborts the operation.
Software stop is not a physical E-stop.

FluidNC must already be installed and configured for the actual machine.
This project does not distribute FluidNC firmware or a ready-to-use pin mapping.
The development setup was tested with FluidNC 4.0.3. The original machine
reported an inline-laser-output problem with 4.1.0; check controller output
when changing firmware. Install the USB serial driver required by your board.

## Test and build

```console
python -m unittest discover -s tests
python -m pip install -r requirements-build.txt
python tools/collect_licenses.py
python -m PyInstaller --noconfirm LaserWriter.spec
```

The packaged application also supports an offline check:
`"Laser Writer Open.exe" --self-test report.json`. It tests GDS loading,
row placement, the hidden UI and PNG export without connecting hardware.

Distribute the EXE together with `LICENSE`, `THIRD_PARTY_NOTICES.md`,
`third_party_licenses`, this source tree, and `third_party_sources`.
The source archive can be a separate asset in the same GitHub release.

## License

Application code is offered under the MIT license. Third-party libraries,
fonts and runtimes retain their own licenses; see `THIRD_PARTY_NOTICES.md`.
No custom photos, memes, personal project files, passwords, or commercial
font files are included. Standard toolkit icons and bundled Matplotlib fonts
are covered by their upstream notices. FluidNC is an independent project;
this application is not an official FluidNC release.
