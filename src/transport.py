"""
Uniform Modbus transport for either:
  - "solarman": the Solarman WiFi dongle's local V5 protocol over TCP
    (no wiring needed — LAN reachability to the dongle is enough, which is
    why this became the default: the RS485_METER port needs a cable run
    to the inverter, and the Nagios Pi isn't near it).
  - "serial": direct RS485 wired into the RS485_METER port (pins 7/8),
    kept available for later if a wired link ever makes sense.

Both transports expose the same connect()/close()/read_holding_registers()/
read_input_registers() surface so discover.py and poller.py don't need to
know which one they're talking to.
"""
from __future__ import annotations

from typing import Any


class ModbusTransportError(Exception):
    """Normalized error for both transports — callers only need to catch this."""


class SerialTransport:
    def __init__(self, port: str, baudrate: int, parity: str, stopbits: int, bytesize: int, timeout: float, unit: int):
        from pymodbus.client import ModbusSerialClient

        self._unit = unit
        self._client = ModbusSerialClient(
            port=port,
            framer="rtu",
            baudrate=baudrate,
            parity=parity,
            stopbits=stopbits,
            bytesize=bytesize,
            timeout=timeout,
        )

    def connect(self) -> bool:
        return self._client.connect()

    def close(self) -> None:
        self._client.close()

    def is_connected(self) -> bool:
        return self._client.is_socket_open()

    def read_holding_registers(self, address: int, count: int) -> list[int]:
        from pymodbus.exceptions import ModbusException

        try:
            rr = self._client.read_holding_registers(address=address, count=count, device_id=self._unit)
        except ModbusException as e:
            raise ModbusTransportError(str(e)) from e
        if rr is None or rr.isError():
            raise ModbusTransportError(str(rr))
        return rr.registers

    def read_input_registers(self, address: int, count: int) -> list[int]:
        from pymodbus.exceptions import ModbusException

        try:
            rr = self._client.read_input_registers(address=address, count=count, device_id=self._unit)
        except ModbusException as e:
            raise ModbusTransportError(str(e)) from e
        if rr is None or rr.isError():
            raise ModbusTransportError(str(rr))
        return rr.registers


class SolarmanTransport:
    """Wraps pysolarmanv5.PySolarmanV5. Note the unit id (mb_slave_id) is
    fixed at construction time in the underlying library, unlike pymodbus
    where it's passed per-call — so switching units means reconnecting."""

    def __init__(
        self,
        host: str,
        dongle_serial: int,
        unit: int,
        port: int = 8899,
        timeout: float = 10.0,
        logger=None,
        verbose: bool = False,
    ):
        self._host = host
        self._dongle_serial = dongle_serial
        self._unit = unit
        self._port = port
        self._timeout = timeout
        self._logger = logger
        self._verbose = verbose
        self._client = None

    def connect(self) -> bool:
        from pysolarmanv5 import NoSocketAvailableError, PySolarmanV5

        # The logger serial gets packed into a 4-byte unsigned field
        # (struct.pack("<I", ...)) deep inside PySolarmanV5.__init__. Out of
        # that range — most likely a typo, or pasting in a serial with an
        # extra digit or two — raises a bare struct.error there with no
        # indication of what's wrong. Caught this by actually feeding it a
        # bad value during testing, not by reading the source in advance.
        if not (0 <= self._dongle_serial <= 0xFFFFFFFF):
            raise ModbusTransportError(
                f"--dongle-serial {self._dongle_serial} is out of range for a Solarman logger "
                "serial (must fit in 4 bytes, 0-4294967295). Double-check the digits against "
                "the label on the dongle — it's easy to mistype or grab the wrong number."
            )

        kwargs = dict(
            port=self._port,
            mb_slave_id=self._unit,
            socket_timeout=self._timeout,
            auto_reconnect=False,
        )
        if self._logger is not None:
            # Hands pysolarmanv5 our own logger so its internal SENT/RECD
            # hex-frame debug lines (and V5 frame validation failures that
            # only log, not raise) show up wherever the caller's logging is
            # already going, instead of a separate unconfigured logger
            # nobody sees. verbose=True is what makes it actually emit
            # those at DEBUG rather than staying silent.
            kwargs["logger"] = self._logger
            kwargs["verbose"] = self._verbose

        try:
            self._client = PySolarmanV5(self._host, self._dongle_serial, **kwargs)
            return True
        except (OSError, TimeoutError, NoSocketAvailableError) as e:
            # NoSocketAvailableError is pysolarmanv5's own exception for a
            # failed connect (e.g. dongle unreachable/wrong port) — it is
            # NOT an OSError subclass, so it has to be caught explicitly.
            # Confirmed by actually triggering this path against an
            # unreachable host during testing, not assumed from docs.
            raise ModbusTransportError(f"Could not connect to dongle at {self._host}:{self._port}: {e}") from e

    def close(self) -> None:
        if self._client is not None:
            try:
                self._client.disconnect()
            except Exception:  # noqa: BLE001 - best-effort close
                pass
            self._client = None

    def is_connected(self) -> bool:
        return self._client is not None

    def read_holding_registers(self, address: int, count: int) -> list[int]:
        return self._read("read_holding_registers", address, count)

    def read_input_registers(self, address: int, count: int) -> list[int]:
        return self._read("read_input_registers", address, count)

    def _read(self, method_name: str, address: int, count: int) -> list[int]:
        import queue

        from pysolarmanv5 import NoSocketAvailableError, V5FrameError
        from umodbus.exceptions import ModbusError

        if self._client is None:
            raise ModbusTransportError("Not connected")
        try:
            method = getattr(self._client, method_name)
            return method(address, count)
        except (
            V5FrameError,
            NoSocketAvailableError,
            TimeoutError,
            OSError,
            ModbusError,
            queue.Empty,
        ) as e:
            # Three distinct real failure modes found by actually running
            # this against the live inverter, not by reading docs:
            #   - ModbusError (umodbus, which pysolarmanv5 uses under the
            #     hood for RTU parsing): a real Modbus exception response
            #     from the inverter — illegal data address, illegal
            #     function, etc. Expected constantly, since discover.py
            #     deliberately probes register windows the inverter
            #     doesn't implement.
            #   - queue.Empty: pysolarmanv5 waits on an internal queue for
            #     the reader thread to deliver a response and lets a plain
            #     queue.Empty escape on timeout (see its
            #     _send_receive_v5_frame) instead of wrapping it — this
            #     fires when the dongle just doesn't answer a given
            #     address at all (as opposed to rejecting it quickly).
            # Both are exactly the "nothing at this address" case
            # scan()'s per-window try/except ModbusTransportError expects
            # to see and move past, not a reason to crash the whole run.
            raise ModbusTransportError(str(e)) from e


def make_transport(cfg: dict[str, Any]):
    """Build the configured transport from config.yaml's `connection:` block."""
    conn = cfg.get("connection")
    if conn is None:
        raise ValueError(
            "config.yaml has no `connection:` block. Set connection.type to "
            "'solarman' or 'serial' — see etc/config.example.yaml."
        )
    unit = cfg.get("unit_id", 1)
    ctype = conn.get("type")

    if ctype == "solarman":
        return SolarmanTransport(
            host=conn["host"],
            dongle_serial=int(conn["dongle_serial"]),
            unit=unit,
            port=conn.get("port", 8899),
            timeout=conn.get("timeout", 10.0),
        )
    if ctype == "serial":
        return SerialTransport(
            port=conn["port"],
            baudrate=conn.get("baudrate", 9600),
            parity=conn.get("parity", "N"),
            stopbits=conn.get("stopbits", 1),
            bytesize=conn.get("bytesize", 8),
            timeout=conn.get("timeout", 2.0),
            unit=unit,
        )
    raise ValueError(f"Unknown connection.type '{ctype}' — expected 'solarman' or 'serial'")
