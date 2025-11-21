import asyncio
import logging
import re
from typing import Callable, Dict, List, Optional, Tuple

_LOGGER = logging.getLogger(__name__)

# Match C-Gate response codes: "300 something..."
CODE_RE = re.compile(r"^(\d{3})\s")

# Match group paths and levels:
# Examples (events or status lines):
#   701 //MANOR/254/56/6 ... level=0
#   701 //MANOR/254/56/13 ... level=255
#   300 //MANOR/254/56/6: level=0
GROUP_LEVEL_RE = re.compile(
    r"//([^/]+)/(\d+)/(\d+)/(\d+).*?level[=\s]+(\d+)",
    re.IGNORECASE,
)

# Match load-change port lines:
#   lighting on  //MANOR/254/56/6  #...
#   lighting off //MANOR/254/56/6  #...
#   lighting ramp //MANOR/254/56/6 level=100 #...
LIGHTING_RE = re.compile(
    r"lighting\s+(on|off|ramp)\s+//([^/]+)/(\d+)/(\d+)/(\d+)(?:.*?level[=\s]+(\d+))?",
    re.IGNORECASE,
)


class CGateSession:
    """Async connection to C-Gate with event forwarding to HA."""

    def __init__(
        self,
        host: str,
        port_cmd: int = 20023,
        port_event: int = 20024,
        port_status: int = 20025,  # load-change port
        # Use keepalive as a poll; short by default
        keepalive_interval: int = 5,
    ) -> None:

        self.host = host
        self.port_cmd = port_cmd
        self.port_event = port_event
        self.port_status = port_status
        self.keepalive_interval = keepalive_interval

        # Streams
        self._cmd_reader: Optional[asyncio.StreamReader] = None
        self._cmd_writer: Optional[asyncio.StreamWriter] = None
        self._cmd_lock = asyncio.Lock()

        self._event_reader: Optional[asyncio.StreamReader] = None
        self._event_writer: Optional[asyncio.StreamWriter] = None

        # NOTE: this is actually the load-change port
        self._status_reader: Optional[asyncio.StreamReader] = None
        self._status_writer: Optional[asyncio.StreamWriter] = None

        # Tasks
        self._event_task: Optional[asyncio.Task] = None
        self._status_task: Optional[asyncio.Task] = None
        self._keepalive_task: Optional[asyncio.Task] = None

        # Per-group direct callbacks (legacy support)
        self._group_callbacks: Dict[
            Tuple[str, str, int, int], List[Callable[[int], None]]
        ] = {}

        # Global fan-out callbacks (legacy support)
        self._global_callbacks: List[
            Callable[[str, str, int, int, int], None]
        ] = []

        # Coordinator callback (for HA entities)
        self._group_update_callback: Optional[
            Callable[[str, str, int, int, int], None]
        ] = None

        self._closed = False

    # -------------------------------------------------------------------------
    # Public API
    # -------------------------------------------------------------------------

    async def async_connect(self) -> None:
        """Open all C-Gate ports with timeouts and start reader loops."""
        _LOGGER.info("Connecting to C-Gate at %s", self.host)
        self._closed = False

        await self._open_command_connection()
        await self._open_event_connection()
        await self._open_status_connection()

        loop = asyncio.get_running_loop()
        # Event stream (port 20024) - may be disabled, but harmless if it is
        self._event_task = loop.create_task(self._read_event_stream())
        # Load-change stream (port 20025) - main source of lighting on/off
        self._status_task = loop.create_task(self._read_status_stream())
        # Keepalive + auto-reconnect
        self._keepalive_task = loop.create_task(self._keepalive())

        _LOGGER.info("C-Gate session connected.")

    async def close(self) -> None:
        """Close all connections and kill tasks."""
        self._closed = True

        writers = (self._cmd_writer, self._event_writer, self._status_writer)
        for w in writers:
            if w:
                try:
                    w.close()
                except Exception:
                    pass

        tasks = (self._event_task, self._status_task, self._keepalive_task)
        for t in tasks:
            if t:
                t.cancel()

    async def async_close(self) -> None:
        await self.close()

    # -------------------------------------------------------------------------
    # Callback registration
    # -------------------------------------------------------------------------

    def set_group_update_callback(self, cb):
        """Coordinator registers a callback for all group-level events."""
        self._group_update_callback = cb

    def register_group_callback(self, project, network, app, group, callback):
        """Legacy per-group callback (kept for flexibility)."""
        key = (project, str(network), int(app), int(group))
        self._group_callbacks.setdefault(key, []).append(callback)

    def register_global_callback(self, callback):
        """Legacy global callback."""
        self._global_callbacks.append(callback)

    def _emit_group_update(self, project, network, app, group, level):
        """Unified event fan-out."""

        # 1) Coordinator (primary path)
        if self._group_update_callback:
            try:
                self._group_update_callback(project, network, app, group, level)
            except Exception as exc:
                _LOGGER.error("Coordinator callback failed: %s", exc)

        # 2) Per-group legacy callbacks
        key = (project, network, app, group)
        callbacks = self._group_callbacks.get(key)
        if callbacks:
            for cb in callbacks:
                try:
                    cb(level)
                except Exception as exc:
                    _LOGGER.warning("Group callback failed for %s: %s", key, exc)

        # 3) Global legacy callbacks
        for gcb in self._global_callbacks:
            try:
                gcb(project, network, app, group, level)
            except Exception as exc:
                _LOGGER.warning("Global callback failed: %s", exc)

    # -------------------------------------------------------------------------
    # Command pipe
    # -------------------------------------------------------------------------

    async def send_command(self, cmd: str) -> List[str]:
        if self._closed:
            raise ConnectionError("C-Gate session closed")

        async with self._cmd_lock:
            return await self._send_and_wait(cmd)

    async def get_group_level(self, project, network, app, group):
        """Read the level of a C-Bus group."""
        path = f"//{project}/{network}/{app}/{group}"
        resp = await self.send_command(f"get {path} level")
        for line in resp:
            m = re.search(r"level=(\d+)", line)
            if m:
                return int(m.group(1))
        return None

    async def set_group_level(self, project, network, app, group, level):
        """Send ON / OFF / RAMP commands."""
        path = f"//{project}/{network}/{app}/{group}"
        level = int(level)

        # OFF
        if level <= 0:
            cmd = f"off {path}"

        # FULL ON
        elif level >= 255:
            cmd = f"on {path}"

        # STANDARD RAMP (C-Gate v2 does NOT support ramp time or force)
        else:
            cmd = f"ramp {path} {level}"

        _LOGGER.debug("CMD >> %s", cmd)
        resp = await self._send_and_wait(cmd, retries=1)
        _LOGGER.debug("CMD << %s", "; ".join(resp))

    async def _send_and_wait(self, cmd: str, retries: int = 0) -> List[str]:
        """Send a command with retries."""
        attempt = 0
        while True:
            attempt += 1
            try:
                return await self._send_and_wait_once(cmd)
            except (ConnectionError, OSError) as exc:
                if attempt > retries + 1:
                    _LOGGER.error("Command '%s' failed after retries: %s", cmd, exc)
                    raise
                _LOGGER.warning("Command '%s' failed (%s), reconnecting...", cmd, exc)
                await self._reconnect_cmd()

    async def _send_and_wait_once(self, cmd: str) -> List[str]:
        """
        Send a command and wait for the final 2xx/4xx line.

        IMPORTANT: while reading, we also feed any non-2xx/4xx lines through
        _handle_event_line(), which lets us treat things like the big 701
        state/level dump (after noop) as a "poll" of the bus.
        """
        if not self._cmd_writer or not self._cmd_reader:
            raise ConnectionError("Command connection not ready")

        data = f"{cmd}\r\n".encode()
        _LOGGER.debug("CMD >> %s", cmd)
        self._cmd_writer.write(data)
        await self._cmd_writer.drain()

        lines: List[str] = []
        while True:
            raw = await self._cmd_reader.readline()
            if not raw:
                raise ConnectionError("C-Gate closed the command connection")

            line = raw.decode(errors="ignore").rstrip("\r\n")
            _LOGGER.debug("CMD << %s", line)
            lines.append(line)

            mcode = CODE_RE.match(line)

            # Use the same parser as the event stream for "701 ... level="
            # or any line that looks like a state/level update.
            if not mcode:
                try:
                    self._handle_event_line(line)
                except Exception as exc:
                    _LOGGER.error(
                        "Error handling command line as event: %s (line=%s)",
                        exc,
                        line,
                    )

            if mcode:
                break

        # 400+ => C-Gate error
        first = lines[0]
        m0 = CODE_RE.match(first)
        if m0 and int(m0.group(1)) >= 400:
            raise RuntimeError(f"C-Gate error in command '{cmd}': {first}")

        return lines

    async def _reconnect_cmd(self):
        try:
            if self._cmd_writer:
                self._cmd_writer.close()
        except Exception:
            pass

        self._cmd_writer = None
        self._cmd_reader = None

        _LOGGER.info("Reconnecting command port...")
        await self._open_command_connection()

    # -------------------------------------------------------------------------
    # Connection open
    # -------------------------------------------------------------------------

    async def _open_command_connection(self) -> None:
        reader, writer = await asyncio.open_connection(self.host, self.port_cmd)
        greet = await reader.readline()
        _LOGGER.debug("Command greeting: %s", greet.decode().strip())
        self._cmd_reader = reader
        self._cmd_writer = writer

    async def _open_event_connection(self):
        self._event_reader, self._event_writer = await self._open_with_timeout(
            self.port_event, "EVENT"
        )

        if self._event_reader is None:
            _LOGGER.warning("C-Gate EVENT port disabled")

    async def _open_status_connection(self):
        # NOTE: this is the load-change port in your config
        self._status_reader, self._status_writer = await self._open_with_timeout(
            self.port_status, "LOAD-CHANGE"
        )

        if self._status_reader is None:
            _LOGGER.warning("C-Gate LOAD-CHANGE port (20025) disabled or unavailable")

    async def _open_with_timeout(self, port, label):
        """
        Unified timeout-safe connection routine.

        NOTE: C-Gate EVENT/STATUS ports do not always send a greeting, so we
        treat "no greeting within 1s" as OK rather than failure.
        """
        try:
            _LOGGER.debug("Opening %s port %s", label, port)
            fut = asyncio.open_connection(self.host, port)
            reader, writer = await asyncio.wait_for(fut, timeout=3.0)

            # Try to read an optional greeting; it's fine if we get nothing.
            try:
                greet = await asyncio.wait_for(reader.readline(), timeout=1.0)
                if greet:
                    _LOGGER.debug("%s greeting: %s", label, greet.decode().strip())
                else:
                    _LOGGER.debug("%s port connected (no greeting)", label)
            except Exception:
                _LOGGER.debug("%s port connected (no greeting)", label)

            return reader, writer

        except asyncio.TimeoutError:
            _LOGGER.error("%s port %s timed out", label, port)
            return None, None

        except Exception as exc:
            _LOGGER.error("Error opening %s port %s: %s", label, port, exc)
            return None, None

    # -------------------------------------------------------------------------
    # Event decoding
    # -------------------------------------------------------------------------

    async def _read_event_stream(self):
        """
        Dedicated event reader loop (port 20024).

        This may or may not be active in your C-Gate configuration; with the
        load-change (20025) and polling via noop, the integration will still
        function even if the EVENT port is disabled. We also mark it dead if it
        drops so keepalive can attempt reconnect.
        """
        reader = self._event_reader

        if not reader:
            _LOGGER.debug("Event port is disabled — no reader loop")
            return

        _LOGGER.debug("Event reader started")

        try:
            while not self._closed:
                raw = await reader.readline()
                if not raw:
                    _LOGGER.warning("Event stream closed by C-Gate")
                    # Mark as dead so keepalive can try to reconnect.
                    self._event_reader = None
                    self._event_writer = None
                    break

                line = raw.decode(errors="ignore").strip()
                if not line:
                    continue

                _LOGGER.debug("EVT << %s", line)
                self._handle_event_line(line)

        except asyncio.CancelledError:
            return

        except Exception as exc:
            _LOGGER.exception("Error while reading event stream: %s", exc)
            self._event_reader = None
            self._event_writer = None

    async def _read_status_stream(self):
        """
        Dedicated load-change reader loop (port 20025).

        This is where we see lines like:
          lighting on  //MANOR/254/56/6 ...
          lighting off //MANOR/254/56/6 ...
        which we translate into level 255 / 0 updates.
        """
        reader = self._status_reader

        if not reader:
            _LOGGER.debug("LOAD-CHANGE port is disabled — no status reader loop")
            return

        _LOGGER.debug("LOAD-CHANGE reader started")

        try:
            while not self._closed:
                raw = await reader.readline()
                if not raw:
                    _LOGGER.warning("LOAD-CHANGE stream closed by C-Gate")
                    self._status_reader = None
                    self._status_writer = None
                    break

                line = raw.decode(errors="ignore").strip()
                if not line:
                    continue

                _LOGGER.debug("LC << %s", line)
                self._handle_event_line(line)

        except asyncio.CancelledError:
            return

        except Exception as exc:
            _LOGGER.exception("Error while reading LOAD-CHANGE stream: %s", exc)
            self._status_reader = None
            self._status_writer = None

    def _handle_event_line(self, line: str):
        """Match level, lighting or state events and emit updates."""

        # 1) Any "level=" style events/status:
        #    701 //MANOR/254/56/6 ... level=255
        m = GROUP_LEVEL_RE.search(line)
        if m:
            project, net, app, group, lvl = m.groups()
            self._emit_group_update(project, net, int(app), int(group), int(lvl))
            return

        # 2) Load-change "lighting on/off/ramp" lines
        m_light = LIGHTING_RE.search(line)
        if m_light:
            action, project, net, app, group, lvl = m_light.groups()
            action = action.lower()

            if action == "on":
                level = 255
            elif action == "off":
                level = 0
            else:  # ramp
                if lvl is not None:
                    try:
                        level = int(lvl)
                    except ValueError:
                        level = 0
                else:
                    # If ramp but no level given, very conservative
                    level = 0

            self._emit_group_update(project, net, int(app), int(group), int(level))
            return

        lower = line.lower()

        # 3) state=on events (no explicit level, assume 255)
        if "state=on" in lower:
            m2 = re.search(r"//([^/]+)/(\d+)/(\d+)/(\d+)", line)
            if m2:
                project, net, app, group = m2.groups()
                self._emit_group_update(project, net, int(app), int(group), 255)
            return

        # 4) state=off events (assume 0)
        if "state=off" in lower:
            m2 = re.search(r"//([^/]+)/(\d+)/(\d+)/(\d+)", line)
            if m2:
                project, net, app, group = m2.groups()
                self._emit_group_update(project, net, int(app), int(group), 0)
            return

    # -------------------------------------------------------------------------
    # Keepalive + polling + auto-reconnect
    # -------------------------------------------------------------------------

    async def _keepalive(self):
        """
        Periodic keepalive + polling.

        Every keepalive_interval seconds we:
          * send 'noop' on the command pipe
          * parse any 701 / level=... lines returned as a full state poll
          * attempt to reconnect the EVENT and LOAD-CHANGE ports if they dropped
        """
        try:
            while not self._closed:
                await asyncio.sleep(self.keepalive_interval)

                # 1) Poll via noop (response is parsed in _send_and_wait_once)
                try:
                    await self.send_command("noop")
                except Exception as exc:
                    _LOGGER.warning("Keepalive failed: %s", exc)
                    # Don't immediately kill the loop; we may recover.
                    continue

                loop = asyncio.get_running_loop()

                # 2) If EVENT stream has died, try to reconnect it in the background
                if self._event_reader is None and not self._closed:
                    try:
                        await self._open_event_connection()
                        if self._event_reader:
                            self._event_task = loop.create_task(
                                self._read_event_stream()
                            )
                            _LOGGER.info("Reattached C-Gate EVENT stream after loss")
                    except Exception as exc2:
                        _LOGGER.warning(
                            "Failed to reconnect EVENT port: %s", exc2
                        )

                # 3) If LOAD-CHANGE stream has died, try to reconnect it
                if self._status_reader is None and not self._closed:
                    try:
                        await self._open_status_connection()
                        if self._status_reader:
                            self._status_task = loop.create_task(
                                self._read_status_stream()
                            )
                            _LOGGER.info(
                                "Reattached C-Gate LOAD-CHANGE stream after loss"
                            )
                    except Exception as exc3:
                        _LOGGER.warning(
                            "Failed to reconnect LOAD-CHANGE port: %s", exc3
                        )

        except asyncio.CancelledError:
            return
