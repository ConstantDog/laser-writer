"""Opt-in, offline packaging check. Never connects to machine hardware."""

import json
from pathlib import Path
import tempfile
import traceback


def run(report_path: str) -> int:
    report = {"ok": False, "hardware_connected": False}
    root = None
    try:
        import tkinter as tk
        import gdstk
        from shapely.ops import unary_union
        from .app import LaserWriterApp
        from .core import (
            APP_VERSION,
            load_gds,
            list_layers,
            polygons_for_layer,
            generate_serpentine_plan,
            build_gcode,
            save_simulation_png,
        )

        root = tk.Tk()
        root.withdraw()
        app = LaserWriterApp(root)
        assert not app.controller.connected
        for key, value in {
            "spot_um": "40",
            "hatch_um": "40",
            "x_steps": "3200",
            "y_steps": "3200",
        }.items():
            app.param_vars[key].set(value)
        with tempfile.TemporaryDirectory(prefix="laser-writer-check-") as directory:
            path = Path(directory)
            library = gdstk.Library(unit=1e-6, precision=1e-9)
            library.new_cell("TEST").add(gdstk.rectangle((0, 0), (1000, 100), layer=1))
            library.write_gds(str(path / "test.gds"))
            document = load_gds(path / "test.gds")
            geometry = unary_union(
                polygons_for_layer(document, "TEST", 1, 0, shift_to_origin=True)
            )
            settings = app._path_settings()
            plan = generate_serpentine_plan(geometry, settings)
            assert [row.y_mm for row in plan.rows] == [0.02, 0.05, 0.08]
            program = build_gcode(
                plan,
                settings,
                source_path=path / "test.gds",
                cell_name="TEST",
                layer_info=list_layers(document, "TEST")[0],
                machine_width_mm=5,
                machine_height_mm=5,
            )
            app.current_geometry = geometry
            app._draw_preview(program)
            root.update_idletasks()
            output = save_simulation_png(program, path / "preview.png", 0.04)
            assert output.stat().st_size > 1000
        assert not app.controller.connected
        report.update(
            ok=True,
            version=APP_VERSION,
            centers_um=[20, 50, 80],
            writing_width_um=40,
            ui_preview=True,
            png_export=True,
            gds_read_write=True,
        )
    except Exception:
        report["error"] = traceback.format_exc()
    finally:
        if root is not None:
            root.destroy()
    Path(report_path).write_text(json.dumps(report, indent=2), encoding="utf-8")
    return 0 if report["ok"] else 1
