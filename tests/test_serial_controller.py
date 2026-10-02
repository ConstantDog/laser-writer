from __future__ import annotations
import queue
import threading
import unittest
from unittest.mock import patch
from fluidnc_laser_writer.serial_controller import FluidNCController


class FakeSerial:

    def __init__(self, *args, **kwargs) -> None:
        self.is_open = True
        self.port = kwargs.get("port", args[0] if args else "COM_TEST")
        self.timeout = kwargs.get("timeout", 0.1)
        self.incoming: queue.Queue[bytes] = queue.Queue()
        self.writes: list[bytes] = []

    def write(self, payload: bytes) -> int:
        self.writes.append(payload)
        for line in payload.split(b"\n"):
            command = line.strip()
            if (
                command
                and command not in {b"?", b"!", b"~", b"\x18"}
                and (b"\x18" not in command)
            ):
                self.incoming.put(b"ok\n")
        return len(payload)

    def flush(self) -> None:
        return None

    def readline(self) -> bytes:
        if not self.is_open:
            return b""
        try:
            return self.incoming.get(timeout=self.timeout)
        except queue.Empty:
            return b""

    def close(self) -> None:
        self.is_open = False


class SerialControllerTests(unittest.TestCase):

    def make_controller(self):
        return FluidNCController(
            on_line=lambda _: None,
            on_connection=lambda *_: None,
            on_progress=lambda *_: None,
            on_complete=lambda *_: None,
        )

    def test_jog_distance_independent_of_feed_and_explicit_mm(self):
        controller = self.make_controller()
        with patch.object(controller, "send_program") as send:
            controller.jog("X", 0.1, 2)
            controller.jog("X", 0.1, 100)
            self.assertEqual(
                send.call_args_list[0].args[0],
                "M5\nG21 G94\nG91 G1 X0.1000 F2.000 S0\nG90\nG4 P0.01\n",
            )
            self.assertEqual(
                send.call_args_list[1].args[0],
                "M5\nG21 G94\nG91 G1 X0.1000 F100.000 S0\nG90\nG4 P0.01\n",
            )
            for feed in (0, -1, 100.01, float("nan"), float("inf")):
                with self.assertRaises(ValueError):
                    controller.jog("X", 0.1, feed)
            self.assertEqual(send.call_count, 2)

    def test_manual_pause_resume_preserves_move_without_reset(self):
        barrier = threading.Event()
        finished = threading.Event()

        class MotionSerial(FakeSerial):

            def write(self, payload):
                if payload == b"G4 P0.01\n":
                    self.writes.append(payload)
                    barrier.set()
                    return len(payload)
                return super().write(payload)

        with patch(
            "fluidnc_laser_writer.serial_controller.serial.Serial", MotionSerial
        ):
            controller = self.make_controller()
            controller.on_complete = lambda *_: finished.set()
            controller.connect("TEST")
            try:
                controller.jog("Y", 10, 5)
                self.assertTrue(barrier.wait(2))
                self.assertTrue(controller.streaming)
                controller.pause()
                self.assertTrue(controller.paused)
                with self.assertRaises(RuntimeError):
                    controller.jog("X", 1, 5)
                with self.assertRaises(RuntimeError):
                    controller.set_work_zero_xy()
                controller.resume()
                self.assertFalse(controller.paused)
                writes = controller._serial.writes
                self.assertIn(b"!", writes)
                self.assertIn(b"~", writes)
                self.assertFalse(any((b"\x18" in p or b"$J=" in p for p in writes)))
                self.assertEqual(writes.count(b"G91 G1 Y10.0000 F5.000 S0\n"), 1)
                controller._serial.incoming.put(b"ok\n")
                self.assertTrue(finished.wait(2))
                self.assertFalse(controller.streaming)
            finally:
                controller.disconnect()

    def test_manual_home_is_axis_specific_and_laser_off(self):
        controller = self.make_controller()
        with patch.object(controller, "send_program") as send:
            controller.home("X")
            self.assertEqual(send.call_args.args[0], "M5\n$HX\n")
            controller.home("Y")
            self.assertEqual(send.call_args.args[0], "M5\n$HY\n")
            with self.assertRaises(ValueError):
                controller.home("Z")

    def test_homing_and_commands_blocked_by_firmware_motion_after_sender_ends(self):
        controller = self.make_controller()
        controller._serial = FakeSerial()
        for state in ("Run", "Jog", "Hold", "Home", "Door"):
            controller._machine_state = state
            self.assertFalse(controller.streaming)
            self.assertTrue(controller.controls_locked)
            with self.assertRaises(RuntimeError):
                controller.home("X")
            with self.assertRaises(RuntimeError):
                controller.jog("Y", 1, 5)
            with self.assertRaises(RuntimeError):
                controller.set_work_zero_xy()
            self.assertEqual(controller._serial.writes, [])
        controller._machine_state = "Idle"
        self.assertFalse(controller.controls_locked)
        controller._streaming = True
        with self.assertRaises(RuntimeError):
            controller.home("Y")
        self.assertEqual(controller._serial.writes, [])

    def test_periodic_x_homing_returns_to_row_endpoint(self) -> None:

        class HomingSerial(FakeSerial):

            def write(self, payload):
                if payload == b"?":
                    self.writes.append(payload)
                    self.incoming.put(b"<Idle|MPos:0,0,0>\n")
                    return 1
                return super().write(payload)

        finished = threading.Event()
        results = []
        with patch(
            "fluidnc_laser_writer.serial_controller.serial.Serial", HomingSerial
        ):
            controller = FluidNCController(
                on_line=lambda _: None,
                on_connection=lambda *_: None,
                on_progress=lambda *_: None,
                on_complete=lambda *args: (results.append(args), finished.set()),
            )
            controller.connect("TEST")
            controller._ready_at = 0
            controller.send_program(
                "G21\nG90\nG94\nG1 X0 Y0 F5\nM3 S0\nG1 X1 S500\nG1 X2 S0\n; ROW_END 1 X2.0000 Y0.0000\nG1 Y0.025 S0\nG1 X0 S500\n; ROW_END 2 X0.0000 Y0.0250\nM5\n",
                x_home_interval=1,
            )
            self.assertTrue(finished.wait(4))
            self.assertTrue(results[0][0], results)
            writes = controller._serial.writes
            self.assertEqual(writes.count(b"$HX\n"), 2)
            self.assertEqual(writes.count(b"G10 L20 P0 X0\n"), 2)
            self.assertNotIn(b"$HY\n", writes)
            self.assertNotIn(b"G10 L20 P0 Y0\n", writes)
            second_home = [i for i, p in enumerate(writes) if p == b"$HX\n"][1]
            tail = writes[second_home:]
            self.assertLess(tail.index(b"G1 X2.0000 S0\n"), tail.index(b"M3 S0\n"))
            self.assertIn(b"G4 P0.01\n", writes[:second_home])
            controller.disconnect()

    def test_homing_alarm_aborts_before_exposure(self) -> None:
        controller = FluidNCController(
            on_line=lambda _: None,
            on_connection=lambda *_: None,
            on_progress=lambda *_: None,
            on_complete=lambda *_: None,
        )
        commands = []

        def command(text):
            commands.append(text)
            if text == "$HX":
                raise RuntimeError("ALARM:9")

        with patch.object(
            controller, "_checked_command", side_effect=command
        ), patch.object(controller, "_wait_idle"):
            with self.assertRaises(RuntimeError):
                controller._home_x(5.0)
        self.assertEqual(commands, ["M5", "$HX"])

    @patch("fluidnc_laser_writer.serial_controller.serial.Serial", FakeSerial)
    def test_stream_waits_for_ok_and_completes(self) -> None:
        completed = threading.Event()
        result: list[tuple[bool, str]] = []
        progress: list[tuple[int, int, int]] = []
        controller = FluidNCController(
            on_line=lambda _line: None,
            on_connection=lambda _connected, _text: None,
            on_progress=lambda done, total, line: progress.append((done, total, line)),
            on_complete=lambda success, text: (
                result.append((success, text)),
                completed.set(),
            ),
        )
        controller.connect("COM_TEST", 115200)
        controller.send_program("; comment\nG21\nG90\nM5\n")
        self.assertTrue(completed.wait(2.0))
        self.assertTrue(result[0][0], result)
        self.assertEqual(progress[-1][:2], (3, 3))
        handle = controller._serial
        self.assertIsNotNone(handle)
        payload = b"".join(handle.writes)
        self.assertIn(b"G21\n", payload)
        self.assertNotIn(b"comment", payload)
        controller.disconnect()

    @patch("fluidnc_laser_writer.serial_controller.serial.Serial", FakeSerial)
    def test_status_queries_do_not_accumulate(self) -> None:
        controller = FluidNCController(
            on_line=lambda _line: None,
            on_connection=lambda _connected, _text: None,
            on_progress=lambda _done, _total, _line: None,
            on_complete=lambda _success, _text: None,
        )
        controller.connect("COM_TEST", 115200)
        handle = controller._serial
        self.assertIsNotNone(handle)
        self.assertEqual(handle.writes, [])
        controller._ready_at = 0.0
        self.assertTrue(controller.query_status())
        initial_queries = b"".join(handle.writes).count(b"?")
        self.assertFalse(controller.query_status())
        self.assertFalse(controller.query_status())
        final_queries = b"".join(handle.writes).count(b"?")
        self.assertEqual(initial_queries, final_queries)
        controller.disconnect()


if __name__ == "__main__":
    unittest.main()
