import socket
import time
import unittest
from fluidnc_laser_writer.pd_controller import PDSerialController


class WifiTests(unittest.TestCase):

    def test_tcp_fragment_disconnect_and_reconnect(self):
        with socket.socket() as server:
            server.bind(("127.0.0.1", 0))
            server.listen()
            server.settimeout(3)
            pd = PDSerialController(
                on_connection=lambda *_: None, on_message=lambda *_: None
            )
            try:
                for _ in range(2):
                    pd.connect_wifi("127.0.0.1", server.getsockname()[1])
                    conn, _ = server.accept()
                    with conn:
                        conn.sendall(b"PD,123,100,5")
                        time.sleep(0.1)
                        self.assertEqual(pd.drain_samples(), [])
                        conn.sendall(b"50\nPD,128,110,560\n")
                        result = []
                        deadline = time.monotonic() + 2
                        while len(result) < 2 and time.monotonic() < deadline:
                            result.extend(pd.drain_samples())
                            time.sleep(0.01)
                        self.assertEqual([s.millivolts for s in result], [550, 560])
                    deadline = time.monotonic() + 2
                    while pd.connected and time.monotonic() < deadline:
                        time.sleep(0.01)
                    self.assertFalse(pd.connected)
            finally:
                pd.disconnect()
