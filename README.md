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
- Preview spot size affects the drawing only.

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
