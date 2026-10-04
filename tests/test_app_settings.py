import tkinter as tk
import unittest
from fluidnc_laser_writer.app import LaserWriterApp


class AppSettingsTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.root = tk.Tk()
        cls.root.withdraw()
        cls.app = LaserWriterApp(cls.root)

    @classmethod
    def tearDownClass(cls):
        cls.root.destroy()

    def test_defaults_and_parameter_caps(self):
        app = self.app
        self.assertEqual(app.param_vars["x_steps"].get(), "200")
        self.assertEqual(app.param_vars["y_steps"].get(), "200")
        for key in ("feed", "acceleration"):
            self.assertEqual(app.param_vars[key].get(), "5")
            for value in ("11", "nan", "inf", "0"):
                app.param_vars[key].set(value)
                with self.assertRaises(ValueError):
                    app._float(key, key)
            app.param_vars[key].set("5")
        app.param_vars["jog_feed"].set("100")
        self.assertEqual(app._float("jog_feed", "Jog feed"), 100)
        for value in ("100.01", "nan", "inf", "0"):
            app.param_vars["jog_feed"].set(value)
            with self.assertRaises(ValueError):
                app._float("jog_feed", "Jog feed")
        app.param_vars["jog_feed"].set("5")
        self.assertFalse(app.x_home_enabled.get())
        self.assertEqual(app.x_home_interval.get(), "10")
        self.root.update_idletasks()

    def test_x_backlash_input_settings_and_invalidation(self):
        app = self.app
        var = app.param_vars["x_backlash_um"]
        self.assertEqual(var.get(), "0")
        baseline = app._generation_signature()
        try:
            for value in ("-100", "-2.5", "0", "2.5", "100"):
                var.set(value)
                self.assertEqual(app._path_settings().x_backlash_um, float(value))
            for value in ("-100.1", "100.1", "nan", "inf", "-inf", "", "abc"):
                var.set(value)
                with self.assertRaises(ValueError):
                    app._path_settings()
            var.set("20")
            self.assertNotEqual(app._generation_signature(), baseline)
            app.generated_program = object()
            app.review_var.set(True)
            var.set("-20")
            self.assertFalse(app.review_var.get())
        finally:
            app.generated_program = None
            var.set("0")

    def test_measured_width_is_independent_and_requires_regeneration(self):
        app = self.app
        old_width = app.param_vars["spot_um"].get()
        old_hatch = app.param_vars["hatch_um"].get()
        try:
            app.param_vars["hatch_um"].set("40")
            app.param_vars["spot_um"].set("40")
            before = app._generation_signature()
            self.assertEqual(app._path_settings().laser_spot_mm, 0.04)
            app.generated_program = object()
            app.review_var.set(True)
            app.param_vars["spot_um"].set("50")
            self.assertFalse(app.review_var.get())
            self.assertNotEqual(before, app._generation_signature())
            self.assertEqual(app._path_settings().hatch_spacing_mm, 0.04)
        finally:
            app.generated_program = None
            app.param_vars["spot_um"].set(old_width)
            app.param_vars["hatch_um"].set(old_hatch)
