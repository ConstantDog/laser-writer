# Third-party notices

The application license applies only to this project's code. Dependencies retain
their original copyrights and licenses. Copies from the build environment are in
`third_party_licenses`; exact versions and source hashes are in `DEPENDENCIES.json`.

| Component | License / notice |
|---|---|
| gdstk | Boost Software License 1.0 |
| Matplotlib | Matplotlib license; bundled DejaVu and STIX font notices |
| NumPy | BSD and bundled dependency notices in its complete license collection |
| Shapely | BSD; bundled GEOS is LGPL 2.1 |
| GEOS | GNU Lesser General Public License 2.1 |
| pySerial | BSD |
| Pillow | HPND and bundled third-party notices |
| contourpy, cycler, kiwisolver | BSD notices |
| fontTools | MIT and external component notices |
| packaging | Apache 2.0 / BSD |
| pyparsing, six | MIT |
| python-dateutil | Apache 2.0 / BSD |
| Python, Tcl, Tk | Their original runtime licenses |
| OpenSSL | Apache 2.0 |
| zlib-ng | zlib license |
| PyInstaller | GPL with bootloader exception; build tool license included |

The build uses unmodified installed dependency binaries. Standard toolbar icons
and fonts come from Matplotlib. No Microsoft font files or original application
artwork are distributed. Windows runtime notices supplied by Python and Shapely
are retained. Use the full upstream license texts for authoritative terms.

Portions of this software are copyright of The FreeType Project
(https://freetype.org). All rights reserved. Its license is included in the
complete Pillow notices. Bundled DejaVu and STIX fonts retain their own licenses.

## GEOS source and replacement

The release includes GEOS 3.13.1 and Shapely 2.1.2 source archives in
`third_party_sources`, as well as the complete application source and build recipe.
GEOS and Shapely are dynamically loaded libraries. You may modify or replace them,
and reverse engineer this application to debug modifications to those libraries.
There is no password gate, signature check, or restriction on modified builds.

To use modified GEOS, build it from the supplied source, build Shapely against
that installation according to its source `README.rst` and installation guide,
then install the resulting Shapely wheel into the Python build environment.
Run `python tools/collect_licenses.py` only for an unmodified upstream build;
for a modified dependency, supply its actual corresponding source and notices.
Rebuild using `python -m PyInstaller --noconfirm LaserWriter.spec`.
The modified installed libraries will be incorporated into the new EXE.
The application can also run directly from source against the modified libraries.

When publishing or redistributing binaries, keep their matching application and
GEOS/Shapely source archives and notices available in the same release.
Do not label the third-party libraries as MIT-only.

References:
- https://pyinstaller.org/en/stable/license.html
- https://libgeos.org/usage/download/
- https://shapely.readthedocs.io/en/2.1.2/installation.html
- https://numpy.org/doc/stable/license.html
