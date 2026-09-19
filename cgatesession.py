import asyncio
import logging
import re
from typing import Any, Callable, Dict, List, Optional, Tuple

_LOGGER = logging.getLogger(__name__)


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# Match C-Gate response codes: "300 something..."
CODE_RE = re.compile(r"^(\d{3})\s")

# Match group paths and levels:
# Examples (events or status lines):
#   701 //MANOR/254/56/6 ... level=0
# Updated to make "level=" optional so it captures the raw number
GROUP_LEVEL_RE = re.compile(
    r"//([^/]+)/(\d+)/(\d+)/(\d+).*?(?:level[=\s]+)?(\d+)",
    re.IGNORECASE,
)

# Match load-change port lines:
#   lighting on  //MANOR/254/56/6  #...
# Updated to capture the level even if "level=" is omitted
LIGHTING_RE = re.compile(
    r"lighting\s+(on|off|ramp)\s+//([^/]+)/(\d+)/(\d+)/(\d+)(?:\s+(\d+))?",
    re.IGNORECASE,
)

# C-Gate tags every event / load-change line with the unit that originated
# the change:  "... #sourceunit=12 OID=..."  (load-change port)
#              "... new level=255 sourceunit=12 ramptime=0"  (event port, 730)
SOURCEUNIT_RE = re.compile(r"#?sourceunit=(\d+)", re.IGNORECASE)

# "300 //PROJ/254/p/12: LightLevel=123"  (GET on a unit parameter)
PARAM_VALUE_RE = re.compile(r"^3\d\d[-\s]+//[^:]+:\s+([A-Za-z0-9_]+)=(.*)$")


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

        # Project / network context, set via set_context() so the keepalive
        # can watch the C-Bus network interface and reopen it if it closes.
        self.project: Optional[str] = None
        self.network: Optional[str] = None
        # How often (in keepalive cycles) to poll InterfaceState.
        self._netcheck_every = 6
        self._ka_count = 0
        self._net_running = True
        self._resync_running = False
        # Async callback (coordinator.async_resync) run after a recovery to
        # refresh HA state for anything missed while the link was down.
        self._resync_callback: Optional[Callable[[], Any]] = None

        # Link health: True while the command port is up AND (if we know
        # about it) the C-Bus network interface is running. Entities use
        # this for availability.
        self._link_ok = True
        self._link_callback: Optional[Callable[[bool], None]] = None
        self.server_version: Optional[str] = None
        self.stats: Dict[str, Any] = {
            "reconnects": 0,
            "stream_reattaches": 0,
            "resyncs": 0,
            "last_resync": None,
            "network_state": None,
            "last_link_change": None,
            "last_link_reason": None,
        }

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
            Callable[..., Any]
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
        """Coordinator registers a callback for all group-level events.

        Called as ``cb(project, network, app, group, level, source_unit=...)``.
        """
        self._group_update_callback = cb

    def set_context(self, project: str, network: str) -> None:
        """Record project/network so the keepalive can watch the interface."""
        self.project = project
        self.network = str(network)

    def set_resync_callback(self, cb: Callable[[], Any]) -> None:
        """Register an async callback run after a link recovery to refresh state."""
        self._resync_callback = cb

    def set_link_callback(self, cb: Callable[[bool], None]) -> None:
        """Register a sync callback invoked when link health changes."""
        self._link_callback = cb

    @property
    def link_ok(self) -> bool:
        return self._link_ok

    def _set_link(self, ok: bool, reason: str) -> None:
        if ok == self._link_ok:
            return
        self._link_ok = ok
        self.stats["last_link_change"] = _now_iso()
        self.stats["last_link_reason"] = reason
        (_LOGGER.info if ok else _LOGGER.warning)(
            "C-Gate link %s (%s)", "UP" if ok else "DOWN", reason
        )
        if self._link_callback:
            try:
                self._link_callback(ok)
            except Exception as exc:  # noqa: BLE001
                _LOGGER.error("Link callback failed: %s", exc)

    async def _trigger_resync(self, reason: str) -> None:
        if self._resync_callback is None or self._resync_running:
            return
        self._resync_running = True
        try:
            _LOGGER.info("C-Gate link recovered (%s) — resyncing state", reason)
            await self._resync_callback()
            self.stats["resyncs"] += 1
            self.stats["last_resync"] = _now_iso()
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning("Resync after %s failed: %s", reason, exc)
        finally:
            self._resync_running = False

    async def check_network(self) -> bool:
        """Poll the C-Bus network InterfaceState; reopen it if it has closed.

        C-Gate keeps answering ``noop`` on the command port even when the
        network interface is closed, so the keepalive alone can't tell that
        events have stopped. Returns True if the interface is running (or if
        we don't have enough context to check, so we never falsely flag a
        healthy link as down).
        """
        if not self.project or not self.network:
            return True

        path = f"//{self.project}/{self.network}"
        try:
            resp = await self.send_command(f"get {path} InterfaceState")
        except Exception:  # noqa: BLE001
            return False  # command-layer problem; handled by reconnect logic

        state = None
        for line in resp:
            m = re.search(r"InterfaceState=(\w+)", line)
            if m:
                state = m.group(1).lower()
        self.stats["network_state"] = state

        if state == "running":
            self._set_link(True, "network running")
            if not self._net_running:
                self._net_running = True
                await self._trigger_resync("network back to running")
            return True

        # Not running: flag it and (re)open only from a settled closed state,
        # so we don't spam net-open while it is already opening/syncing.
        if self._net_running:
            _LOGGER.warning("C-Bus network %s InterfaceState=%s", path, state)
        self._net_running = False
        self._set_link(False, f"network {state}")

        if state in ("closed", "new", None):
            try:
                _LOGGER.warning("Reopening C-Bus network %s", path)
                await self.send_command(f"net open {path}")
            except Exception as exc:  # noqa: BLE001
                _LOGGER.warning("net open %s failed: %s", path, exc)
        return False

    def register_group_callback(self, project, network, app, group, callback):
        """Legacy per-group callback (kept for flexibility)."""
        key = (project, str(network), int(app), int(group))
        self._group_callbacks.setdefault(key, []).append(callback)

    def register_global_callback(self, callback):
        """Legacy global callback."""
        self._global_callbacks.append(callback)

    def _emit_group_update(self, project, network, app, group, level, source_unit=None):
        """Unified event fan-out."""

        # 1) Coordinator (primary path) — receives the originating unit too
        if self._group_update_callback:
            try:
                self._group_update_callback(
                    project, network, app, group, level, source_unit=source_unit
                )
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

    async def get_unit_param(self, project, network, unit, param):
        """Read a single parameter of a physical unit, e.g. LightLevel on a PIR.

        Returns the raw string value, or None if C-Gate did not report it.
        """
        path = f"//{project}/{network}/p/{int(unit)}"
        resp = await self.send_command(f"get {path} {param}")
        for line in resp:
            m = PARAM_VALUE_RE.match(line)
            if m and m.group(1).lower() == param.lower():
                return m.group(2).strip()
        return None

    @staticmethod
    def _ramp_time_arg(seconds) -> str | None:
        """Format an HA transition (s) as a C-Gate ramp-time: 1-2 digits + s|m."""
        if seconds is None:
            return None
        try:
            s = int(round(float(seconds)))
        except (TypeError, ValueError):
            return None
        if s <= 0:
            return None
        if s <= 99:
            return f"{s}s"
        return f"{min(99, max(1, round(s / 60)))}m"

    async def set_group_level(self, project, network, app, group, level, ramp_time=None):
        """Send ON / OFF / RAMP commands.

        ``ramp_time`` (seconds, e.g. HA's ``transition``) makes any level
        change a timed ramp: ``ramp path level 4s``. Requires a C-Gate that
        supports the ramp-time argument (confirmed OK on v3.7.0+; older
        C-Gate v2.x installs may reject it, in which case leave it unset).
        """
        path = f"//{project}/{network}/{app}/{group}"
        level = max(0, min(255, int(level)))
        rt = self._ramp_time_arg(ramp_time)

        if rt:
            cmd = f"ramp {path} {level} {rt}"

        # OFF
        elif level <= 0:
            cmd = f"off {path}"

        # FULL ON
        elif level >= 255:
            cmd = f"on {path}"

        # STANDARD RAMP (instant)
        else:
            cmd = f"ramp {path} {level}"

        _LOGGER.debug("CMD >> %s", cmd)
        async with self._cmd_lock:
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

            # FIX: Always process the line as an event,
            # EVEN IF it is the final response code (mcode).
            try:
                self._handle_event_line(line)
            except Exception as exc:
                _LOGGER.error("Error handling line as event: %s", exc)

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
        try:
            await self._open_command_connection()
        except Exception:
            self._set_link(False, "command port reconnect failed")
            raise
        self.stats["reconnects"] += 1

    # -------------------------------------------------------------------------
    # Connection open
    # -------------------------------------------------------------------------

    async def _open_command_connection(self) -> None:
        reader, writer = await asyncio.open_connection(self.host, self.port_cmd)
        greet = await reader.readline()
        greet_txt = greet.decode(errors="ignore").strip()
        _LOGGER.debug("Command greeting: %s", greet_txt)
        # "201 Service ready: Clipsal C-Gate Version: v3.7.0 (build 2285) ..."
        m = re.search(r"Version:\s*(v?[\d.]+(?:\s*\(build \d+\))?)", greet_txt)
        if m:
            self.server_version = m.group(1)
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
        m_src = SOURCEUNIT_RE.search(line)
        source_unit = int(m_src.group(1)) if m_src else None

        m_light = LIGHTING_RE.search(line)
        if m_light:
            action, project, net, app, group, lvl = m_light.groups()
            action = action.lower()

            if action == "on":
                level = 255
            elif action == "off":
                level = 0
            elif action == "ramp":
                # If we captured a level number from the positional argument
                if lvl is not None:
                    try:
                        level = int(lvl)
                    except ValueError:
                        return
                else:
                    # If it's a ramp but no level is present, don't guess 0
                    return
            else:
                return

            self._emit_group_update(
                project, net, int(app), int(group), int(level), source_unit
            )
            return

        lower = line.lower()

        # 3) state=on events (no explicit level, assume 255)
        if "state=on" in lower:
            m2 = re.search(r"//([^/]+)/(\d+)/(\d+)/(\d+)", line)
            if m2:
                project, net, app, group = m2.groups()
                self._emit_group_update(
                    project, net, int(app), int(group), 255, source_unit
                )
            return

        # 4) state=off events (assume 0)
        if "state=off" in lower:
            m2 = re.search(r"//([^/]+)/(\d+)/(\d+)/(\d+)", line)
            if m2:
                project, net, app, group = m2.groups()
                self._emit_group_update(
                    project, net, int(app), int(group), 0, source_unit
                )
            return

        m_level = GROUP_LEVEL_RE.search(line)
        if m_level:
            project, net, app, group, level = m_level.groups()
            self._emit_group_update(
                project, net, int(app), int(group), int(level), source_unit
            )
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
          * periodically confirm the C-Bus network interface is still open
        """
        try:
            while not self._closed:
                await asyncio.sleep(self.keepalive_interval)

                # 1) Poll via noop (response is parsed in _send_and_wait_once)
                try:
                    await self.send_command("noop")
                except Exception as exc:
                    _LOGGER.warning("Keepalive failed: %s", exc)
                    self._set_link(False, "command port unreachable")
                    # Don't immediately kill the loop; we may recover.
                    continue
                if self._net_running:
                    self._set_link(True, "command port ok")

                loop = asyncio.get_running_loop()
                reattached = False

                # 2) If EVENT stream has died, try to reconnect it in the background
                if self._event_reader is None and not self._closed:
                    try:
                        await self._open_event_connection()
                        if self._event_reader:
                            self._event_task = loop.create_task(
                                self._read_event_stream()
                            )
                            _LOGGER.info("Reattached C-Gate EVENT stream after loss")
                            reattached = True
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
                            reattached = True
                    except Exception as exc3:
                        _LOGGER.warning(
                            "Failed to reconnect LOAD-CHANGE port: %s", exc3
                        )

                # 4) Periodically confirm the C-Bus network is still open.
                #    (noop keeps succeeding even when the interface has closed,
                #    which would silently stop all events.)
                self._ka_count += 1
                if self._ka_count % self._netcheck_every == 0 and not self._closed:
                    await self.check_network()

                # 5) A stream reattach means we were blind for a moment —
                #    refresh state so entities aren't left stale.
                if reattached and not self._closed:
                    self.stats["stream_reattaches"] += 1
                    await self._trigger_resync("event stream reattach")

        except asyncio.CancelledError:
            return
