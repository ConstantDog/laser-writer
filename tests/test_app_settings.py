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
