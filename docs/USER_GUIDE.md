# Laser Writer user guide

Applies to **Laser Writer Open v1.4.1**. Documentation updated 2026-10-04.
The public Windows application has no startup password. This guide describes
the desktop application, not ESP32 driver-initialization or PD firmware.

## 1. Before connecting the machine

- Configure FluidNC for your actual motor steps/mm, directions, travel, homing
  switches, and laser output pin and S-to-power mapping.
- Establish hardware laser protection and an independent physical emergency
  stop. A software stop can fail if the computer, cable or controller fails.
- First test motion with laser emission physically disabled. Verify actual
  displacement and switch operation before exposing material.
- Close other applications using the same serial port. The FluidNC browser
  installer and Laser Writer cannot both own that port at the same time.
- Use an ASCII-only folder path for GDS files on Windows if opening or saving
  fails: the native GDS library can have problems with non-ASCII paths.

The development machine worked with FluidNC 4.0.3. Its operator observed an
inline-laser-output problem with 4.1.0 that disappeared after downgrading.
This is a machine-specific observation, not proof that all 4.1.0 installations
are affected. Check actual laser output when changing firmware.

## 2. Normal workflow

1. Open the **Design** tab and select a GDS file, **Top Cell**, and
   **Layer/Datatype**. Only the selected layer is converted.
2. Fill the parameter groups described below. The shipped defaults are not
   automatically read from your machine and are not universal recommendations.
3. Click **Check design**. Read the results rather than only the PASS/FAIL label.
   These are approximate geometry and command-resolution checks.
4. Click **Generate G-code**. The program saves G-code and a preview PNG beside
   the GDS, using the layer/datatype in their filenames. Regenerating the same
   design/layer can replace those outputs; copy versions you want to keep.
5. In **Review**, inspect coordinates, feed, S commands, travel bounds, warnings
   and estimated duration. After editing the text, click **Validate edits and
   refresh**, then approve the reviewed code.
6. In **Machine**, select the FluidNC COM port and baud, then connect. Establish
   a safe work origin; test individual axes and homing as appropriate.
7. Click **Send reviewed G-code**. New manual moves, homing and zeroing are
   blocked while an operation is active or paused; they are not queued.
8. Use **Pause / Resume** to suspend and continue. Use **Force Stop** to abort,
   not to pause. After an abort, inspect the machine and re-establish position
   if necessary before starting a new job.

The generated design is shifted to nonnegative coordinates. Its nominal left
edge is offset by the overscan distance; work X0 is the left laser-off travel
boundary, not the first exposed edge. Work Y0 corresponds to the design's
lower edge; the first scan center normally lies above it. Check the preview
and allow for finite spot width and any compensation.

Changing a Design-tab parameter invalidates the generation approval. Run the
checks, regenerate and review again before sending. Changing a UI field does
not retroactively change old G-code or an already running job.

## 3. Parameter roles at a glance

| Group | What it does | Writes FluidNC configuration? |
|---|---|---|
| A: Writing commands | Changes scan placement, exposure commands or travel/dwell | No; it changes generated G-code |
| B: Coordinate planning references | Rounds commanded coordinates and checks nominal step resolution | No |
| C: Validation thresholds | Determines whether a design passes software checks | No |
| D: Firmware references / validation | Checks overscan, S values and command travel against supplied references | No |
| Manual controls | Changes the next manual move; separate from writing parameters | No |

There are no purely record-only fields among these Design parameter groups.
However, validation/reference fields must not be mistaken for hardware controls.
The former preview-only spot field now has a planning function in v1.4.1.

All distances are in **mm** unless marked **um / µm**; 1000 µm = 1 mm.
Feed is **mm/min**, acceleration is **mm/s²**, and dwell is **ms**.

### A. Writing commands

| Field | Shipped default / accepted values | Function and effect on writing |
|---|---|---|
| Measured writing width/µm | 40; positive | Enter the measured width of one exposed track at your intended power, speed and material process. Controls Y edge inset, row placement and the width of preview bands. Does **not** change physical focus or force the real line to have that width. Changing it can change row count, overlap and duration. |
| Maximum hatch spacing/µm | 25; positive | Upper bound on neighboring scan-center separation within each component's row grid, not the clear gap between design features. The planner reduces spacing as needed to fit, and caps it at measured writing width. Smaller spacing usually means more rows, longer time and greater overlap/exposure. Separated components can have larger unexposed gaps. |
| Feed≤10/mm/min | 5; 0.001 to 10 | Commanded G1 feed for exposure **and laser-off travel**, including row changes. Lower feed generally increases exposure per unit length at fixed optical power and increases duration. It may broaden developed lines. Actual speed still depends on firmware limits and acceleration. |
| Laser output S | 500; integer from 0 to Validation S maximum | S command during exposed segments; dark segments use S0. The physical response depends on FluidNC's speed map, PWM/TTL electronics and laser driver. S500 is not inherently 500 mW or exactly half optical power. S0 requests no exposure. |
| Overscan/mm | 0.200; must satisfy the acceleration/compensation checks | Laser-off margin on both sides of the overall X scan range. Gives the axis room to accelerate, decelerate and reverse outside exposure; larger values add travel and duration. It does not enlarge the target GDS polygons. |
| X bidirectional compensation/µm [-100,100] | 0; finite values from -100 to +100, including decimals | Shifts right-to-left exposure intervals relative to unchanged left-to-right exposure. Positive shifts them left; negative shifts them right. Intended for measured alternating-row registration error. Shift is rounded to X steps/mm; small values may round to zero. It preserves each interval's commanded length. This is not a full mechanical backlash model and does not affect manual moves, homing or Y. |
| Row dwell/ms | 20; nonnegative integer | Inserts laser-off G4 waits before and after each scan row. Gives settling time but increases duration; 20 ms adds about 40 ms per row. Zero omits these waits. It does not request a laser-on exposure dwell. |

Overscan must cover the reference acceleration distance `d = (F / 60)^2 / (2a)`.
With nonzero X compensation, it must also cover the absolute step-rounded
compensation shift. This check assumes the reference acceleration matches
the real controller; it does not set acceleration or measure lost motion.

#### Width versus spacing: an example

A **100 µm-high rectangular strip**, measured writing width **40 µm**, and
maximum hatch spacing **40 µm** produces centers at **20, 50 and 80 µm** above
the lower edge. Actual spacing is 30 µm. Idealized bands cover 0–40, 30–70 and
60–100 µm, so neighboring tracks overlap by 10 µm.

This is geometric coverage, not uniform-dose control. The application does not
automatically lower S to compensate for overlap. Test a small sample after
changing width or spacing, especially with photosensitive material.

The row grid is fitted to the Y bounds of each connected component. It is not
a full contour-offset algorithm: X endpoints remain at target intersections,
round spot endcaps can extend beyond them, and sloped edges, holes and narrow
branches can have gaps or overruns. Connected serpentine geometry shares one
component grid; its individual horizontal arms do not each get a separate grid.
Inspect preview bands against outlines, including warnings for complex shapes.

### B. Coordinate planning references

| Field | Shipped default | Function and effect on writing |
|---|---:|---|
| X reference steps/mm | 200 | Rounds X endpoints, overscan positions and compensation to nominal motor increments. Also supplies the X command-resolution check. Incorrect values can coarsen or change the generated path; they do not reconfigure the driver's microsteps or FluidNC's pulse count per mm. |
| Y reference steps/mm | 200 | Rounds Y row centers and constrains achievable row spacing. Also supplies the Y command-resolution check. Too coarse a grid can prevent the requested coverage. It does not reconfigure the controller. |

For a directly driven 1.8-degree motor and 1 mm screw lead:

`steps/mm = (360 / 1.8) × microstep_factor / 1 = 200 × microstep_factor`

| Driver setting | FluidNC steps/mm and matching UI reference | Nominal command increment |
|---|---:|---:|
| Full step | 200 | 5 µm |
| 1/16 microstep | 3200 | 0.3125 µm |

For that 1/16 setup, enter **3200 for both axes**, provided both mechanisms
match. Separately confirm that the driver and FluidNC are configured that way.
Changing only the desktop fields cannot correct a firmware scaling mismatch.
Nominal increments do not prove equivalent positioning accuracy: backlash,
microstep nonlinearity, stiffness and missed steps are not measured here.

### C. Validation thresholds

These fields control acceptance, not feature fabrication. Lowering a threshold
does not shrink the laser spot or make the mechanism more accurate.

| Field | Shipped default | Function and effect on writing |
|---|---:|---|
| Max width/mm | 15 | Rejects selected GDS geometry wider than this bound. Does not scale the design. This is design width, separate from travel including overscan. |
| Max height/mm | 15 | Rejects selected GDS geometry taller than this bound. Does not scale the design. |
| Min feature/µm | 100 | Threshold for an approximate feature-size estimate using polygon dimensions/minimum clearance. A 40 µm feature can fail a 100 µm threshold; set a justified test threshold such as 40, rather than expecting automatic resizing. This is not a complete linewidth DRC. |
| Min spacing/µm | 100 | Checks separation between disconnected components. It does not comprehensively check internal gaps of one connected snake or all holes/notches. PASS is not proof that every internal gap is resolvable. |
| Target precision/µm | 100 | Checks nominal X/Y command increments and the **entered maximum hatch spacing** against this value. It does not change either parameter or set physical positioning accuracy. Even if fitted actual spacing is smaller, an entered maximum above this threshold can fail the check. |

### D. Firmware references / validation

| Field | Shipped default / accepted values | Function and effect on writing |
|---|---|---|
| Reference acceleration≤10/mm/s² | 5; 0.001 to 10 | Used to calculate the overscan requirement. A smaller reference requires more acceleration distance at the same feed. Does not send an acceleration setting to FluidNC, change driver current or change homing speed. |
| Validation S maximum | 1000; positive integer | Software upper bound on requested S values. Match the intended controller mapping. Changing it does not rewrite FluidNC's `speed_map`, change PWM frequency or rescale an existing S500 command. |
| X validation travel/mm | 100; positive | Checks analyzed X command coordinates against the allowed nonnegative work-coordinate range, including overscan/compensation endpoints. Does not write hardware travel/soft limits or measure clearance. Allow separately for actual spot footprint and physical work-zero position. |
| Y validation travel/mm | 100; positive | Equivalent Y command-coordinate check. Does not configure homing, soft limits or machine travel. |

## 4. Manual controls and optional homing

| Input or option | Default | Behavior and effect |
|---|---:|---|
| FluidNC serial port | Select a detected COM port | Chooses the motion controller. Do not select the separate PD ESP32 by mistake. |
| Baud | 115200 | Must match FluidNC's serial communication rate. It does not set motor speed. |
| Manual Step/mm | 0.100 | Distance of one X+/X-/Y+/Y- button click, not the electrical motor step or microstep size. One click requests one move. |
| Manual Feed/mm/min | 5; 0.001 to 100 | Speed requested for that manual move. Changing it changes duration, not requested distance. Separate from Design-tab writing feed. Actual motion remains firmware-limited. |
| Periodic X homing $HX | Off | Optional: homes X before a job and at selected scan-row checkpoints. Uses X's own switch; the pulled-off position becomes work X0 and Y is retained. It changes origin handling and adds travel/time, so test this mode before writing. |
| Scan rows per home | 10 | Positive integer interval when periodic X homing is enabled. Smaller intervals add more homing operations/time. Counts generated raster rows, not GDS strokes. Ignored when the option is off. Requires fresh, unedited generated G-code with row markers. |

- **Set current XY as zero** changes the active work-coordinate offset using
  `G10 L20 P0 X0 Y0`. It does not find a physical switch.
- **Home X / Home Y** runs only that axis's homing command. Direction, seek/feed,
  pull-off and switch assignment come from FluidNC, not manual Step or Feed.
- **Pause / Resume** preserves remaining manual/program travel. Other new
  control commands remain blocked until the operation is complete.
- **Force Stop** requests a hold, soft reset and laser-off command. It aborts
  the job and may leave FluidNC in Alarm; it is not resumable like Pause.
- **Unlock** does not home the axes or restore reliable position after a fault.
  Resolve the fault first; do not repeatedly unlock without checking its cause.

In v1.4.1 the homing confirmation still mentions a legacy **200 steps/mm
full-step test**. That text is not a requirement for 1/16 operation. Keep the
correct hardware and firmware settings (3200 in the example above); the dialog
does not change them. Homing rates must be set and tested in FluidNC separately.

## 5. Reading the preview and results

- Red bands show idealized finite-width exposure; darker overlap is not a
  calibrated exposure dose. Black outlines are the target, blue lines are dark travel.
- A rectangle's Y coverage can fit while its rounded X ends extend beyond the
  outline. Changing the displayed width changes planning assumptions, not optics.
- The generated path is an X-direction raster fill, not vector tracing of the
  GDS perimeter. Diagonal shapes become row intersections, not necessarily a
  single simultaneous diagonal X/Y move.
- Exposure and dark travel use the same commanded feed. Acceleration and row
  reversals still take place. Estimated duration is not a hardware timing guarantee.
- FluidNC-reported position is an internal coordinate, not an encoder measurement.
  The displayed laser command is not proof of emitted optical power.

For a small 40 µm test pattern, an example starting set is measured width 40
**only if actually measured**, maximum spacing 40, minimum feature/spacing 40,
and target precision 40. Keep feed/S at previously tested values. Set steps/mm
from hardware, not from the desired resolution. Inspect coverage and actual
developed lines before treating the result as evidence of achievable resolution.

## 6. Optional PD window: desktop inputs only

These fields configure acquisition/display, not motion or laser output.
There is no automatic power-control feedback loop from this window.

| Field | Default | Purpose |
|---|---|---|
| Transport | Wi-Fi | Choose TCP acquisition or USB serial from a separate compatible PD ESP32. |
| IP / Host | 192.168.4.1 | Address of the PD device/server in Wi-Fi mode; must be reachable from the PC. |
| TCP | 9000 | Server port in Wi-Fi mode; must match the PD firmware. |
| PD serial port | Select a port | Acquisition device in USB mode, separate from the FluidNC port. |
| PD Baud | 230400 | USB serial rate; must match PD firmware. Does not set ADC sample rate. |
| Window/s | 10 | Visible time span of the graph, clamped to 0.5–60 s. Does not change the firmware sampling rate or commanded exposure. |

**Save buffered CSV** exports retained samples; **Clear plot** clears the
display/history. PD firmware, wiring and electrical input protection are outside
this guide. Use firmware compatible with the application's input protocol.

## 7. Quick troubleshooting

| Symptom | First checks |
|---|---|
| Fine design fails the check | Check Min feature, Min spacing, Target precision and entered maximum hatch; do not assume the defaults describe your experiment. |
| Motion distance is wrong | Check actual driver microsteps, screw lead and FluidNC steps/mm first; then match desktop references. Changing manual feed should not change commanded distance. |
| Fine gaps merge or lines become wider | Check real writing width at current S/feed, overlap from fitted spacing, focus, and the material/development process. The preview is not a dose simulation. |
| Alternating raster rows are offset | Measure the signed row offset and try a small X compensation; inspect direction and repeatability before increasing it. |
| Homing speed does not follow manual feed | Homing uses FluidNC's own homing settings. The desktop feed fields do not override them. |
| Cannot send after changing a parameter | Run checks and regenerate, then validate and approve the new G-code. |
| No laser during a job | Verify the configured pin/mapping, controller state, S commands and actual TTL output safely. Do not infer optical output from the preview alone. |
| GDS fails to open in a non-ASCII folder | Copy the file to a simple ASCII path and retry. |

For source setup, builds and dependency licenses, return to the [README](../README.md).
