from __future__ import annotations
import queue
import time
import unittest
from unittest.mock import patch
from fluidnc_laser_writer.pd_controller import PDSerialController, parse_pd_line
from fluidnc_laser_writer.serial_controller import FluidNCController


class IndependentFakeSerial:

    def __init__(self, *args, **kwargs) -> None:
        self.port = kwargs.get("port", args[0] if args else "COM_FAKE")
        self.timeout = kwargs.get("timeout", 0.05)
        self.is_open = True
        self.incoming: queue.Queue[bytes] = queue.Queue()
        self.writes: list[bytes] = []

    def write(self, payload: bytes) -> int:
        self.writes.append(payload)
        if self.port == "COM_FLUID":
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

    def readline(self, size: int = -1) -> bytes:
        if not self.is_open:
            return b""
        try:
            return self.incoming.get(timeout=self.timeout)
        except queue.Empty:
            return b""

    def close(self) -> None:
        self.is_open = False


class PDControllerTests(unittest.TestCase):

    def test_parse_preferred_pd_protocol(self) -> None:
        sample = parse_pd_line("PD,1234,1876,1512", host_time_s=100.0)
        self.assertIsNotNone(sample)
        assert sample is not None
        self.assertEqual(sample.device_time_ms, 1234)
        self.assertEqual(sample.raw, 1876)
        self.assertEqual(sample.millivolts, 1512)
        self.assertEqual(sample.host_time_s, 100.0)

    def test_parse_plain_adc_value(self) -> None:
        for line in (
            "2048",
            "50",
            "PD,100,500",
            "PD,1,2,3,4",
            "PD,1,,500,550",
            "PD,1,4096,550",
            "PD,1,500,-1",
            "PD,1,500,nan",
            "PD,1,500,550PD,2,500,550",
            "PD,1,500,55�",
        ):
            with self.subTest(line=line):
                self.assertIsNone(parse_pd_line(line))

    @patch("fluidnc_laser_writer.pd_controller.serial.Serial", IndependentFakeSerial)
    def test_partial_frames_and_corruption(self) -> None:
        pd = PDSerialController(
            on_connection=lambda *_: None, on_message=lambda *_: None
        )
        pd.connect("COM_PD")
        try:
            handle = pd._serial
            handle.incoming.put(b"PD,100,495,55")
            time.sleep(0.12)
            self.assertEqual(pd.drain_samples(), [])
            handle.incoming.put(b"8\r\nPD,105,496,559\n")
            handle.incoming.put(
                b"PD,110,495,55\xff\n50\nPD,115,495,558PD,120,495,558\n"
            )
            handle.incoming.put(b"x" * 300 + b"\nPD,125,495,558\n")
            handle.incoming.put(b"PD,130,0,0\nPD,135,10,50\n")
            samples = []
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                samples.extend(pd.drain_samples())
                if len(samples) == 5 and pd.invalid_frames == 4:
                    break
                time.sleep(0.01)
            self.assertEqual([s.millivolts for s in samples], [558, 559, 558, 0, 50])
            self.assertEqual(pd.invalid_frames, 4)
        finally:
            pd.disconnect()

    @patch("fluidnc_laser_writer.pd_controller.serial.Serial", IndependentFakeSerial)
    def test_reconnect_does_not_keep_partial_frame_or_reader(self) -> None:
        pd = PDSerialController(
            on_connection=lambda *_: None, on_message=lambda *_: None
        )
        pd.connect("COM_PD")
        old_reader = pd._reader
        pd._serial.incoming.put(b"PD,1,400,5")
        time.sleep(0.08)
        pd.connect("COM_PD")
        try:
            self.assertFalse(old_reader.is_alive())
            pd._serial.incoming.put(b"PD,2,400,550\n")
            samples = []
            deadline = time.monotonic() + 1
            while not samples and time.monotonic() < deadline:
                samples = pd.drain_samples()
                time.sleep(0.01)
            self.assertEqual([s.millivolts for s in samples], [550])
        finally:
            pd.disconnect()

    @patch(
        "fluidnc_laser_writer.serial_controller.serial.Serial", IndependentFakeSerial
    )
    @patch("fluidnc_laser_writer.pd_controller.serial.Serial", IndependentFakeSerial)
    def test_pd_disconnect_does_not_disconnect_fluidnc(self) -> None:
        fluid = FluidNCController(
            on_line=lambda _line: None,
            on_connection=lambda _connected, _text: None,
            on_progress=lambda _done, _total, _line: None,
            on_complete=lambda _success, _text: None,
        )
        pd = PDSerialController(
            on_connection=lambda _connected, _text: None, on_message=lambda _text: None
        )
        fluid.connect("COM_FLUID", 115200)
        pd.connect("COM_PD", 230400)
        self.assertTrue(fluid.connected)
        self.assertTrue(pd.connected)
        pd_handle = pd._serial
        assert pd_handle is not None
        pd_handle.incoming.put(b"PD,100,2000,1600\n")
        deadline = time.monotonic() + 1.0
        samples = []
        while not samples and time.monotonic() < deadline:
            samples = pd.drain_samples()
            time.sleep(0.01)
        self.assertEqual(len(samples), 1)
        pd.disconnect()
        self.assertFalse(pd.connected)
        self.assertTrue(fluid.connected)
        fluid.send_command("M5")
        fluid.disconnect()

    @patch(
        "fluidnc_laser_writer.serial_controller.serial.Serial", IndependentFakeSerial
    )
    @patch("fluidnc_laser_writer.pd_controller.serial.Serial", IndependentFakeSerial)
    def test_fluidnc_disconnect_does_not_disconnect_pd(self) -> None:
        fluid = FluidNCController(
            on_line=lambda _line: None,
            on_connection=lambda _connected, _text: None,
            on_progress=lambda _done, _total, _line: None,
            on_complete=lambda _success, _text: None,
        )
        pd = PDSerialController(
            on_connection=lambda _connected, _text: None, on_message=lambda _text: None
        )
        fluid.connect("COM_FLUID", 115200)
        pd.connect("COM_PD", 230400)
        fluid.disconnect()
        self.assertFalse(fluid.connected)
        self.assertTrue(pd.connected)
        pd.disconnect()


if __name__ == "__main__":
    unittest.main()
