from __future__ import annotations
import queue
import math
import re
import threading
import time
from dataclasses import dataclass
from typing import Callable
import serial
from serial.tools import list_ports

LineCallback = Callable[[str], None]
ConnectionCallback = Callable[[bool, str], None]
ProgressCallback = Callable[[int, int, int], None]
CompleteCallback = Callable[[bool, str], None]


@dataclass(frozen=True)
class SerialPortInfo:
    device: str
    description: str
    hwid: str
    serial_number: str | None

    @property
    def display_name(self) -> str:
        serial_suffix = f"｜SN {self.serial_number}" if self.serial_number else ""
        return f"{self.device}｜{self.description}{serial_suffix}"


def available_ports() -> list[SerialPortInfo]:
    ports = []
    for port in list_ports.comports():
        ports.append(
            SerialPortInfo(
                device=port.device,
                description=port.description or "Serial device",
                hwid=port.hwid or "",
                serial_number=port.serial_number,
            )
        )
    return sorted(ports, key=lambda item: item.device)


class FluidNCController:
    """Conservative line-by-line FluidNC/Grbl serial sender.

    File streaming sends one line and waits for ``ok`` or ``error`` before the
    next line. Real-time Grbl characters bypass the line queue.
    """

    def __init__(
        self,
        *,
        on_line: LineCallback,
        on_connection: ConnectionCallback,
        on_progress: ProgressCallback,
        on_complete: CompleteCallback,
    ) -> None:
        self.on_line = on_line
        self.on_connection = on_connection
        self.on_progress = on_progress
        self.on_complete = on_complete
        self._serial: serial.Serial | None = None
        self._reader_thread: threading.Thread | None = None
        self._sender_thread: threading.Thread | None = None
        self._reader_stop = threading.Event()
        self._stream_stop = threading.Event()
        self._run_gate = threading.Event()
        self._run_gate.set()
        self._stream_active = threading.Event()
        self._response_queue: queue.Queue[str] = queue.Queue()
        self._write_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._status_lock = threading.Lock()
        self._status_pending = False
        self._status_requested_at = 0.0
        self._ready_at = 0.0
        self._last_write_error_reported_at = 0.0
        self._streaming = False
        self._paused = False
        self._machine_state: str | None = None
        self._status_frames: queue.Queue[str] = queue.Queue()

    @property
    def connected(self) -> bool:
        handle = self._serial
        return bool(handle and handle.is_open)

    @property
    def port(self) -> str | None:
        handle = self._serial
        return str(handle.port) if handle and handle.is_open else None

    @property
    def streaming(self) -> bool:
        with self._state_lock:
            return self._streaming

    @property
    def paused(self) -> bool:
        with self._state_lock:
            return self._paused

    @property
    def controls_locked(self) -> bool:
        with self._state_lock:
            return (
                self._streaming
                or self._paused
                or self._machine_state not in {None, "Idle", "Alarm"}
            )

    def _require_control_idle(self) -> None:
        if self.controls_locked:
            raise RuntimeError(
                "New control commands are blocked while running or paused; rejected, not queued."
            )

    def connect(self, port: str, baud: int = 115200) -> None:
        if self.connected:
            self.disconnect()
        handle = serial.Serial(port=port, baudrate=baud, timeout=0.1, write_timeout=3.0)
        self._serial = handle
        self._machine_state = None
        self._ready_at = time.monotonic() + 1.0
        self._reader_stop.clear()
        self._reader_thread = threading.Thread(
            target=self._reader_loop, name="fluidnc-reader", daemon=True
        )
        self._reader_thread.start()
        self.on_connection(True, f"Connected {port} @ {baud}")

    def disconnect(self) -> None:
        if self.streaming:
            self.force_stop()
        self._reader_stop.set()
        handle = self._serial
        self._serial = None
        self._ready_at = 0.0
        if handle:
            try:
                handle.close()
            except serial.SerialException:
                pass
        with self._state_lock:
            self._streaming = False
            self._paused = False
        self._stream_active.clear()
        with self._status_lock:
            self._status_pending = False
        self.on_connection(False, "Disconnected")

    def send_command(self, command: str) -> None:
        if not self.connected:
            raise RuntimeError("FluidNC is not connected.")
        self._require_control_idle()
        cleaned = command.strip()
        if not cleaned:
            return
        self._write((cleaned + "\n").encode("ascii"))
        self.on_line(f">>> {cleaned}")

    def query_status(self, *, force: bool = False) -> bool:
        """Request one real-time status frame without allowing a query backlog."""
        if not self.connected:
            return False
        now = time.monotonic()
        if now < self._ready_at:
            return False
        with self._status_lock:
            if (
                self._status_pending
                and (not force)
                and (now - self._status_requested_at < 0.75)
            ):
                return False
            self._status_pending = True
            self._status_requested_at = now
        try:
            self.write_realtime(b"?")
        except (serial.SerialTimeoutException, serial.SerialException, OSError) as exc:
            with self._status_lock:
                self._status_pending = False
            if now - self._last_write_error_reported_at >= 5.0:
                self._last_write_error_reported_at = now
                self.on_line(f"*** Status-query write temporarily failed: {exc}")
            return False
        return True

    def jog(self, axis: str, distance_mm: float, feed_mm_min: float) -> None:
        axis = axis.upper()
        if axis not in {"X", "Y"}:
            raise ValueError("Only X/Y jogging is supported.")
        if not math.isfinite(distance_mm) or abs(distance_mm) < 0.0001:
            raise ValueError("Jog distance must be finite and nonzero in mm.")
        if not math.isfinite(feed_mm_min) or not 0.001 <= feed_mm_min <= 100:
            raise ValueError("Jog feed must be 0.001–100 mm/min.")
        self.send_program(
            f"M5\nG21 G94\nG91 G1 {axis}{distance_mm:.4f} F{feed_mm_min:.3f} S0\nG90\nG4 P0.01\n"
        )

    def set_work_zero_xy(self) -> None:
        self.send_command("G10 L20 P0 X0 Y0")

    def home(self, axis: str) -> None:
        axis = axis.upper()
        if axis not in {"X", "Y"}:
            raise ValueError("Select X or Y for single-axis homing")
        self.send_program("M5\n$H" + axis + "\n")

    def unlock(self) -> None:
        self.send_command("$X")

    def send_program(self, text: str, *, x_home_interval: int = 0) -> None:
        if not self.connected:
            raise RuntimeError("FluidNC is not connected.")
        self._require_control_idle()
        prepared: list[tuple[int, str]] = []
        if x_home_interval < 0:
            raise ValueError("Invalid X homing interval")
        markers = list(
            re.finditer("(?m)^; ROW_END (\\d+) X([0-9.]+) Y([0-9.]+)\\s*$", text)
        )
        if x_home_interval and (not markers):
            raise ValueError("Regenerate G-code with scan-row markers.")
        for line_index, raw in enumerate(
            text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
        ):
            marker = re.fullmatch("; ROW_END (\\d+) X([0-9.]+) Y([0-9.]+)", raw.strip())
            if (
                x_home_interval
                and marker
                and (int(marker[1]) % x_home_interval == 0)
                and (int(marker[1]) < len(markers))
            ):
                prepared.append((line_index, "@HOME_X " + marker[2]))
            cleaned = raw.split(";", 1)[0]
            cleaned = re.sub("\\([^)]*\\)", "", cleaned).strip()
            if cleaned:
                prepared.append((line_index, cleaned))
        if not prepared:
            raise ValueError("There is no G-code to send.")
        self._drain_responses()
        self._stream_stop.clear()
        self._run_gate.set()
        self._stream_active.set()
        with self._state_lock:
            self._streaming = True
            self._paused = False
        self._sender_thread = threading.Thread(
            target=self._sender_loop,
            args=(prepared, bool(x_home_interval)),
            name="fluidnc-sender",
            daemon=True,
        )
        self._sender_thread.start()

    def pause(self) -> None:
        if not self.streaming or self.paused:
            return
        self._run_gate.clear()
        with self._state_lock:
            self._paused = True
        self.write_realtime(b"!")
        self.on_line("*** Real-time pause ! sent")

    def resume(self) -> None:
        if not self.streaming or not self.paused:
            return
        self.write_realtime(b"~")
        with self._state_lock:
            self._paused = False
        self._run_gate.set()
        self.on_line("*** Real-time resume ~ sent")

    def force_stop(self) -> None:
        """Immediately request feed hold, clear the planner, then command M5."""
        if not self.connected:
            return
        self._stream_stop.set()
        self._run_gate.set()
        self.write_realtime(b"!\x18")
        self.on_line("*** Force stop: ! and Ctrl-X sent; sending M5")

        def finish_laser_off() -> None:
            time.sleep(0.15)
            if self.connected:
                try:
                    self._write(b"M5\n")
                except (serial.SerialException, serial.SerialTimeoutException):
                    pass

        threading.Thread(
            target=finish_laser_off, name="fluidnc-force-stop", daemon=True
        ).start()

    def write_realtime(self, payload: bytes) -> None:
        if not self.connected:
            return
        self._write(payload)

    def _write(self, payload: bytes) -> None:
        handle = self._serial
        if not handle or not handle.is_open:
            raise RuntimeError("The serial port is disconnected.")
        with self._write_lock:
            handle.write(payload)
            handle.flush()

    def _reader_loop(self) -> None:
        while not self._reader_stop.is_set():
            handle = self._serial
            if not handle or not handle.is_open:
                return
            try:
                raw = handle.readline()
            except (serial.SerialException, OSError) as exc:
                if not self._reader_stop.is_set():
                    self.on_line(f"*** Serial read failed: {exc}")
                    self.on_connection(False, "Serial connection lost")
                self._stream_stop.set()
                self._run_gate.set()
                try:
                    handle.close()
                except serial.SerialException:
                    pass
                if self._serial is handle:
                    self._serial = None
                return
            if not raw:
                continue
            line = raw.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            if line.startswith("<") and line.endswith(">"):
                with self._state_lock:
                    self._machine_state = line[1:].split("|", 1)[0].split(":", 1)[0]
                self._status_frames.put(line)
                while self._status_frames.qsize() > 20:
                    self._status_frames.get_nowait()
                with self._status_lock:
                    self._status_pending = False
            self.on_line(line)
            normalized = line.lower()
            if self._stream_active.is_set() and (
                normalized == "ok"
                or normalized.startswith("error")
                or normalized.startswith("alarm")
            ):
                self._response_queue.put(line)

    def _sender_loop(
        self, prepared: list[tuple[int, str]], periodic_x: bool = False
    ) -> None:
        total = len(prepared)
        success = False
        message = "Sending was aborted."
        try:
            if periodic_x:
                self._home_x(None)
            for completed, (line_index, line) in enumerate(prepared, start=1):
                while not self._run_gate.wait(timeout=0.1):
                    if self._stream_stop.is_set():
                        break
                if self._stream_stop.is_set():
                    break
                if line.startswith("@HOME_X "):
                    self._home_x(float(line.split()[1]))
                    self.on_progress(completed, total, line_index)
                    continue
                self._write((line + "\n").encode("ascii", errors="strict"))
                response = self._wait_for_response(timeout=1800.0)
                if response is None:
                    message = (
                        "Response timeout, stop or disconnect; check controller state."
                    )
                    if self.connected and (not self._stream_stop.is_set()):
                        self.force_stop()
                    break
                normalized = response.lower()
                if normalized != "ok":
                    message = f"FluidNC returned at line {line_index + 1}: {response}"
                    break
                self.on_progress(completed, total, line_index)
            else:
                success = True
                message = "All G-code was sent and acknowledged by FluidNC."
        except (
            serial.SerialException,
            serial.SerialTimeoutException,
            UnicodeEncodeError,
            RuntimeError,
        ) as exc:
            message = f"Send failed: {exc}"
            if periodic_x and self.connected and (not self._stream_stop.is_set()):
                self.force_stop()
        finally:
            with self._state_lock:
                self._streaming = False
                self._paused = False
            self._stream_active.clear()
            self._run_gate.set()
            self._drain_responses()
            self.on_complete(success, message)

    def _checked_command(self, command: str) -> None:
        while not self._run_gate.wait(0.1):
            if self._stream_stop.is_set():
                raise RuntimeError("Stopped")
        if self._stream_stop.is_set():
            raise RuntimeError("Stopped")
        self.on_line(">>> " + command)
        self._write((command + "\n").encode("ascii"))
        response = self._wait_for_response(timeout=1800.0)
        if response is None or response.lower() != "ok":
            raise RuntimeError(f"{command}: {response or 'timeout / stopped'}")

    def _wait_idle(self, timeout: float = 180.0) -> None:
        self._checked_command("G4 P0.01")
        while not self._status_frames.empty():
            self._status_frames.get_nowait()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._stream_stop.is_set() or not self.connected:
                raise RuntimeError("Stopped / disconnected")
            self.query_status()
            try:
                frame = self._status_frames.get(timeout=0.2)
            except queue.Empty:
                continue
            state = frame[1:].split("|", 1)[0].split(":", 1)[0]
            if state in {"Alarm", "Door", "Check"}:
                raise RuntimeError("Cannot home in state: " + state)
            if state == "Idle" and self._run_gate.is_set():
                return
        raise RuntimeError("Idle timeout")

    def _home_x(self, return_x: float | None) -> None:
        self.on_line("*** X homing calibration (Y unchanged)")
        self._checked_command("M5")
        self._wait_idle()
        self._checked_command("$HX")
        self._wait_idle()
        self._checked_command("G21 G90 G94")
        self._checked_command("G10 L20 P0 X0")
        if return_x is not None:
            self._checked_command(f"G1 X{return_x:.4f} S0")
            self._wait_idle()
            self._checked_command("M3 S0")
        self.on_line("*** X calibration complete")

    def _wait_for_response(self, timeout: float | None = None) -> str | None:
        deadline = time.monotonic() + timeout if timeout else float("inf")
        previous = time.monotonic()
        while not self._stream_stop.is_set():
            now = time.monotonic()
            if self.paused:
                deadline += now - previous
            previous = now
            if time.monotonic() >= deadline:
                return None
            try:
                return self._response_queue.get(timeout=0.1)
            except queue.Empty:
                if not self.connected:
                    return None
        return None

    def _drain_responses(self) -> None:
        while True:
            try:
                self._response_queue.get_nowait()
            except queue.Empty:
                return
