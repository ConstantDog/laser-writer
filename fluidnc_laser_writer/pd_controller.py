from __future__ import annotations
import collections
import re
import threading
import time
from dataclasses import dataclass
from typing import Callable
import serial
import serial.urlhandler.protocol_socket


@dataclass(frozen=True)
class PDSample:
    host_time_s: float
    device_time_ms: float | None
    raw: float | None
    millivolts: float | None

    @property
    def plot_value(self) -> float:
        if self.millivolts is not None:
            return self.millivolts
        return self.raw or 0.0

    @property
    def plot_unit(self) -> str:
        return "mV" if self.millivolts is not None else "ADC raw"


def parse_pd_line(line: str, *, host_time_s: float | None = None) -> PDSample | None:
    """Accept only the complete integer protocol emitted by the PD firmware."""
    cleaned = line.strip()
    if not cleaned or cleaned.startswith(("#", ";", "<", "[", "ok", "error")):
        return None
    match = re.fullmatch("PD,([0-9]{1,10}),([0-9]{1,4}),([0-9]{1,4})", cleaned)
    if match is None:
        return None
    device_time_ms, raw, millivolts = map(int, match.groups())
    if device_time_ms > 4294967295 or raw > 4095 or millivolts > 3300:
        return None
    return PDSample(
        host_time_s=time.time() if host_time_s is None else host_time_s,
        device_time_ms=device_time_ms,
        raw=raw,
        millivolts=millivolts,
    )


class PDSerialController:
    """Independent serial reader for a dedicated PD-sampling ESP32."""

    def __init__(
        self,
        *,
        on_connection: Callable[[bool, str], None],
        on_message: Callable[[str], None],
        buffer_size: int = 20000,
    ) -> None:
        self.on_connection = on_connection
        self.on_message = on_message
        self._serial: serial.Serial | None = None
        self._reader: threading.Thread | None = None
        self._stop = threading.Event()
        self._samples: collections.deque[PDSample] = collections.deque(
            maxlen=buffer_size
        )
        self._samples_lock = threading.Lock()
        self._write_lock = threading.Lock()
        self.dropped_samples = 0
        self.invalid_frames = 0

    @property
    def connected(self) -> bool:
        handle = self._serial
        return bool(handle and handle.is_open)

    @property
    def port(self) -> str | None:
        handle = self._serial
        return str(handle.port) if handle and handle.is_open else None

    def connect_wifi(self, host: str, port: int = 9000) -> None:
        host = host.strip()
        if not host or any((c in host for c in "/ :\\")) or (not 1 <= port <= 65535):
            raise ValueError("Invalid Wi-Fi host / TCP port")
        self.connect(f"socket://{host}:{port}")

    def connect(self, port: str, baud: int = 230400) -> None:
        if self._reader is not None:
            self.disconnect()
        options = dict(baudrate=baud, timeout=0.05, write_timeout=1.0)
        handle = (
            serial.serial_for_url(port, **options)
            if port.startswith("socket://")
            else serial.Serial(port=port, **options)
        )
        self._serial = handle
        self.invalid_frames = 0
        self.dropped_samples = 0
        self.clear_samples()
        self._stop.clear()
        self._reader = threading.Thread(
            target=self._reader_loop, name="pd-serial-reader", daemon=True
        )
        self._reader.start()
        self.on_connection(True, f"PD acquisition connected {port} @ {baud}")

    def disconnect(self) -> None:
        self._stop.set()
        handle = self._serial
        self._serial = None
        if handle:
            try:
                handle.close()
            except serial.SerialException:
                pass
        reader = self._reader
        if reader is not None and reader is not threading.current_thread():
            reader.join()
        self._reader = None
        self.on_connection(False, "PD acquisition disconnected")

    def send_command(self, command: str) -> None:
        handle = self._serial
        if not handle or not handle.is_open:
            raise RuntimeError("The PD acquisition serial port is not connected.")
        payload = command.strip().encode("ascii") + b"\n"
        with self._write_lock:
            handle.write(payload)
            handle.flush()

    def drain_samples(self, limit: int | None = None) -> list[PDSample]:
        with self._samples_lock:
            count = (
                len(self._samples) if limit is None else min(limit, len(self._samples))
            )
            return [self._samples.popleft() for _ in range(count)]

    def clear_samples(self) -> None:
        with self._samples_lock:
            self._samples.clear()

    def _reader_loop(self) -> None:
        pending = bytearray()
        last_data = time.monotonic()
        discard_until_newline = False
        while not self._stop.is_set():
            handle = self._serial
            if not handle or not handle.is_open:
                return
            try:
                raw = handle.readline(512)
                if raw:
                    last_data = time.monotonic()
                elif (
                    str(handle.port).startswith("socket://")
                    and time.monotonic() - last_data > 5
                ):
                    raise OSError("PD Wi-Fi: no data for 5 seconds")
            except (serial.SerialException, OSError) as exc:
                if not self._stop.is_set():
                    self.on_message(f"PD serial read failed: {exc}")
                    self.on_connection(False, "PD acquisition connection lost")
                try:
                    handle.close()
                except serial.SerialException:
                    pass
                if self._serial is handle:
                    self._serial = None
                return
            if not raw:
                continue
            for byte in raw:
                if byte != 10:
                    if not discard_until_newline:
                        pending.append(byte)
                        if len(pending) > 256:
                            pending.clear()
                            discard_until_newline = True
                            self.invalid_frames += 1
                    continue
                if discard_until_newline:
                    discard_until_newline = False
                    continue
                frame = bytes(pending).rstrip(b"\r")
                pending.clear()
                if not frame:
                    continue
                if frame.startswith(b"#"):
                    self.on_message(frame.decode("ascii", errors="replace"))
                    continue
                try:
                    text = frame.decode("ascii", errors="strict")
                except UnicodeDecodeError:
                    text = ""
                sample = parse_pd_line(text)
                if sample is None:
                    self.invalid_frames += 1
                    if self.invalid_frames <= 3 or self.invalid_frames % 100 == 0:
                        self.on_message(
                            f"PD invalid frame #{self.invalid_frames}: {frame[:100]!r}"
                        )
                    continue
                with self._samples_lock:
                    if len(self._samples) == self._samples.maxlen:
                        self.dropped_samples += 1
                    self._samples.append(sample)
