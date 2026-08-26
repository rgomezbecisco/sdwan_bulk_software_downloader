import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path

from rich import box
from rich.console import Console
from rich.live import Live
from rich.table import Table

from find_manager import run_find_manager
from install_iosxe_image import run_install_iosxe_image
from utils.additional_functions import find_software_for_model
from utils.api_manager import sdwanManager
from utils.ssh_manager import sshManager
from verify_iosxe_image import run_verify_iosxe_image

POLL_INTERVAL_DEFAULT = 60   # seconds
MAX_WORKERS = 20
MAX_DOWNLOAD_RETRIES = 5     # auto-resume retries before giving up


class DownloadStatus(str, Enum):
    PENDING     = "PENDING"
    FIRING      = "FIRING"
    DOWNLOADING = "DOWNLOADING"
    COMPLETE    = "COMPLETE"
    VERIFYING   = "VERIFYING"
    INSTALLING  = "INSTALLING"
    CONFIRMING  = "CONFIRMING"
    DONE        = "DONE"
    FAILED      = "FAILED"


_STATUS_COLOR = {
    "PENDING":     "dim",
    "FIRING":      "yellow",
    "DOWNLOADING": "cyan",
    "COMPLETE":    "blue",
    "VERIFYING":   "magenta",
    "INSTALLING":  "magenta",
    "CONFIRMING":  "blue",
    "DONE":        "green",
    "FAILED":      "red",
}

_STATUS_ICON = {
    "PENDING":     "○",
    "FIRING":      "⚡",
    "DOWNLOADING": "↓",
    "COMPLETE":    "✓",
    "VERIFYING":   "⊙",
    "INSTALLING":  "⚙",
    "CONFIRMING":  "⊛",
    "DONE":        "✔",
    "FAILED":      "✗",
}


@dataclass
class SiteState:
    hostname:          str
    target_manager:    dict  = field(default=None)
    device_model:      str   = ""
    software_filename: str   = ""
    expected_bytes:    int   = 0
    status:            DownloadStatus = DownloadStatus.PENDING
    progress_pct:      float = 0.0
    bytes_downloaded:  int   = 0
    vmanage_ip:        str   = ""
    error:             str   = ""
    last_updated:      datetime = field(default_factory=datetime.now)
    # ETA tracking — bytes/time of previous poll
    _prev_bytes:       int   = field(default=0, repr=False)
    _prev_poll_time:   float = field(default=0.0, repr=False)
    _eta_seconds:      float = field(default=-1.0, repr=False)
    _stall_polls:      int   = field(default=0, repr=False)   # consecutive polls with 0 bytes
    _retry_count:      int   = field(default=0, repr=False)   # auto-resume attempts
    wget_speed:        str   = ""
    wget_eta:          str   = ""
    _lock:             threading.Lock = field(default_factory=threading.Lock, repr=False)

    def update(self, **kwargs):
        with self._lock:
            for k, v in kwargs.items():
                setattr(self, k, v)
            self.last_updated = datetime.now()

    def record_poll(self, bytes_now: int):
        """Update ETA using delta from previous poll."""
        with self._lock:
            now = time.time()
            if self._prev_poll_time > 0 and bytes_now > self._prev_bytes and self.expected_bytes > 0:
                elapsed = now - self._prev_poll_time
                rate = (bytes_now - self._prev_bytes) / elapsed   # bytes/sec
                remaining = self.expected_bytes - bytes_now
                self._eta_seconds = remaining / rate if rate > 0 else -1.0
            self._prev_bytes = bytes_now
            self._prev_poll_time = now

    @property
    def eta_str(self) -> str:
        with self._lock:
            # prefer wget's own ETA; fall back to our calculated value
            if self.wget_eta:
                speed = f" @ {self.wget_speed}" if self.wget_speed else ""
                return f"{self.wget_eta}{speed}"
            if self._eta_seconds <= 0:
                return "—"
            return str(timedelta(seconds=int(self._eta_seconds)))


# ---------------------------------------------------------------------------
# Per-site workers
# ---------------------------------------------------------------------------

def _resolve_site(state: SiteState):
    try:
        target_manager = run_find_manager(state.hostname)
        if not target_manager:
            state.update(status=DownloadStatus.FAILED, error="manager not found")
            return

        device_model = target_manager.get("edge_device_model", "")
        software = find_software_for_model(device_model)
        if not software:
            state.update(status=DownloadStatus.FAILED, device_model=device_model,
                         error=f"no image mapped for model '{device_model or 'unknown'}'")
            return

        state.update(
            target_manager=target_manager,
            vmanage_ip=target_manager.get("manager_system_ip", ""),
            device_model=device_model,
            software_filename=software["filename"],
            expected_bytes=software["bytes"],
        )
    except Exception as exc:
        state.update(status=DownloadStatus.FAILED, error=f"resolve: {exc}")


def _resolve_image_sizes(states: list["SiteState"], max_workers: int):
    """Fill expected_bytes for images whose size was left at 0 in SOFTWARE_MAP.yaml."""
    pending: dict[str, list[SiteState]] = {}
    for s in states:
        if s.status != DownloadStatus.FAILED and s.expected_bytes <= 0:
            pending.setdefault(s.software_filename, []).append(s)

    if not pending:
        return

    def _lookup(filename: str, sites: list[SiteState]):
        probe = sites[0]
        try:
            size = sshManager(probe.target_manager).get_remote_file_size(
                probe.vmanage_ip, filename)
            err = f"'{filename}' not found in vManage software repository"
        except Exception as exc:
            size, err = None, f"size lookup failed: {exc}"
        for s in sites:
            if size:
                s.update(expected_bytes=size)
            else:
                s.update(status=DownloadStatus.FAILED, error=err)

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        list(as_completed({pool.submit(_lookup, f, ss): f for f, ss in pending.items()}))


def _fire_site(state: SiteState, cleanup: bool = False):
    state.update(status=DownloadStatus.FIRING)
    try:
        client = sshManager(state.target_manager)
        if cleanup:
            client.cleanup_before_download(state.software_filename)
        pid = client.fire_background_wget(state.vmanage_ip, state.software_filename)
        # Transition regardless of PID — first poll will confirm whether wget is alive
        state.update(status=DownloadStatus.DOWNLOADING,
                     error="" if pid else "PID unknown — will confirm on first poll")
    except Exception as exc:
        state.update(status=DownloadStatus.FAILED, error=f"fire: {exc}")


def _poll_site(state: SiteState):
    try:
        client = sshManager(state.target_manager)
        poll = client.poll_download_progress(state.software_filename, state.expected_bytes)

        bytes_dl      = poll["bytes_downloaded"]
        process_alive = poll["process_alive"]
        log_pct       = poll["progress_pct"]    # from wget log; -1 if not yet available
        log_tail      = poll["log_tail"]

        # prefer wget's own percentage; fall back to bytes ratio only if log unavailable
        if log_pct >= 0:
            pct = log_pct
        elif state.expected_bytes > 0:
            pct = round(min(bytes_dl / state.expected_bytes * 100, 100.0), 1)
        else:
            pct = 0.0

        state.update(wget_speed=poll["wget_speed"], wget_eta=poll["wget_eta"])
        prev_bytes = state._prev_bytes   # capture before record_poll updates it
        state.record_poll(bytes_dl)
        file_growing = bytes_dl > prev_bytes

        if log_pct >= 100.0 or (state.expected_bytes > 0 and bytes_dl >= state.expected_bytes):
            state.update(status=DownloadStatus.COMPLETE, progress_pct=100.0,
                         bytes_downloaded=bytes_dl, error="")
        elif bytes_dl > 0 and not process_alive and not file_growing and log_pct < 99.0:
            with state._lock:
                state._retry_count += 1
                retries = state._retry_count
            if retries <= MAX_DOWNLOAD_RETRIES:
                # re-fire with --continue so wget resumes from current byte offset
                try:
                    client2 = sshManager(state.target_manager)
                    client2.fire_background_wget(state.vmanage_ip, state.software_filename)
                    state.update(status=DownloadStatus.DOWNLOADING,
                                 error=f"retry {retries}/{MAX_DOWNLOAD_RETRIES} — resuming from {pct:.0f}% | {log_tail}")
                except Exception as re_exc:
                    state.update(status=DownloadStatus.FAILED,
                                 error=f"retry {retries} fire failed: {re_exc}")
            else:
                state.update(status=DownloadStatus.FAILED, progress_pct=pct,
                             bytes_downloaded=bytes_dl,
                             error=f"gave up after {retries} retries at {pct:.0f}% | {log_tail}")
        elif bytes_dl == 0 and log_pct < 0:
            with state._lock:
                state._stall_polls += 1
                stall = state._stall_polls
            if stall >= 2:
                state.update(status=DownloadStatus.FAILED,
                             error=f"wget stalled (log: {log_tail or 'no log'})")
            else:
                state.update(status=DownloadStatus.DOWNLOADING, progress_pct=0.0,
                             bytes_downloaded=0, error=log_tail)
        else:
            with state._lock:
                state._stall_polls = 0
            state.update(status=DownloadStatus.DOWNLOADING, progress_pct=pct,
                         bytes_downloaded=bytes_dl, error=log_tail)
    except Exception as exc:
        state.update(error=f"poll: {exc}")


def _verify_install_site(state: SiteState):
    import re as _re
    try:
        state.update(status=DownloadStatus.VERIFYING)
        if not run_verify_iosxe_image(state.target_manager,
                                      filename=state.software_filename).get("success"):
            state.update(status=DownloadStatus.FAILED, error="verify failed")
            return
        state.update(status=DownloadStatus.INSTALLING)
        if not run_install_iosxe_image(state.target_manager,
                                       filename=state.software_filename).get("success"):
            state.update(status=DownloadStatus.FAILED, error="install failed")
            return

        state.update(status=DownloadStatus.CONFIRMING)
        client = sshManager(state.target_manager)
        result = client.send_command_on_edge_cli("show sdwan software", custom_timeout=30)
        output = result.get("output", "")
        # extract version key from filename: 17.09.05f or 17.12.08
        m = _re.search(r'universalk9\.(\d+(?:\.\d+)+[a-z]?)', state.software_filename)
        version_key = m.group(1) if m else ""
        if version_key and version_key in output:
            state.update(status=DownloadStatus.DONE, error=f"confirmed: {version_key} listed")
        elif not version_key:
            state.update(status=DownloadStatus.DONE, error="confirm skipped: version key not parsed")
        else:
            state.update(status=DownloadStatus.FAILED,
                         error=f"version {version_key} NOT found in show software")
    except Exception as exc:
        state.update(status=DownloadStatus.FAILED, error=f"verify/install: {exc}")


# ---------------------------------------------------------------------------
# Rich display helpers
# ---------------------------------------------------------------------------

def _bar(pct: float, width: int = 18) -> str:
    filled = int(pct / 100 * width)
    return "█" * filled + "░" * (width - filled)


def _build_table(states: list[SiteState], next_poll_in: int, polling: bool) -> Table:
    table = Table(
        title=f"IOS XE Bulk Download  —  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        box=box.ROUNDED,
        expand=True,
    )
    table.add_column("Hostname",   style="bold white", no_wrap=True)
    table.add_column("Model",      style="dim",        no_wrap=True)
    table.add_column("vManage",    style="dim",        no_wrap=True)
    table.add_column("Status",     no_wrap=True)
    table.add_column("Progress",   no_wrap=True, min_width=24)
    table.add_column("ETA",        no_wrap=True)
    table.add_column("Note",       style="dim",        max_width=32)

    counts: dict[DownloadStatus, int] = {s: 0 for s in DownloadStatus}
    for st in states:
        counts[st.status] += 1
        color = _STATUS_COLOR.get(st.status.value, "white")
        icon  = _STATUS_ICON.get(st.status.value, "?")
        status_cell = f"[{color}]{icon} {st.status.value}[/{color}]"

        if st.status in (DownloadStatus.DOWNLOADING,):
            bar_cell = f"[cyan]{_bar(st.progress_pct)}[/cyan] {st.progress_pct:.1f}%"
            eta_cell = st.eta_str
        elif st.status in (DownloadStatus.COMPLETE, DownloadStatus.VERIFYING,
                           DownloadStatus.INSTALLING, DownloadStatus.DONE):
            bar_cell = f"[green]{_bar(100.0)}[/green] 100.0%"
            eta_cell = "—"
        elif st.status == DownloadStatus.FAILED and st.progress_pct > 0:
            bar_cell = f"[red]{_bar(st.progress_pct)}[/red] {st.progress_pct:.1f}%"
            eta_cell = "—"
        else:
            bar_cell = "—"
            eta_cell = "—"

        table.add_row(
            st.hostname,
            st.device_model.replace("vedge-", "") or "—",
            st.vmanage_ip or "—",
            status_cell,
            bar_cell,
            eta_cell,
            st.error or "",
        )

    parts = [f"[bold]{len(states)} site{'s' if len(states) != 1 else ''}[/bold]"]
    for status, label, color in [
        (DownloadStatus.DOWNLOADING, "downloading", "cyan"),
        (DownloadStatus.VERIFYING,   "verifying",   "magenta"),
        (DownloadStatus.INSTALLING,  "installing",  "magenta"),
        (DownloadStatus.DONE,        "done",        "green"),
        (DownloadStatus.FAILED,      "failed",      "red"),
    ]:
        if counts[status]:
            parts.append(f"[{color}]{counts[status]} {label}[/{color}]")

    if polling:
        parts.append("[yellow]polling…[/yellow]")
    elif next_poll_in > 0:
        parts.append(f"next poll: [yellow]{next_poll_in}s[/yellow]")

    table.caption = "  |  ".join(parts)
    return table


def _all_terminal(states: list[SiteState]) -> bool:
    terminal = {DownloadStatus.DONE, DownloadStatus.FAILED}
    return all(s.status in terminal for s in states)


# ---------------------------------------------------------------------------
# Main orchestrator
# ---------------------------------------------------------------------------

def run_bulk_download(
    hostnames_file: str,
    poll_interval: int = POLL_INTERVAL_DEFAULT,
    max_workers: int = MAX_WORKERS,
    cleanup: bool = False,
):
    hostnames = _read_hostnames(hostnames_file)
    if not hostnames:
        print("No hostnames found in file.")
        return

    states = [SiteState(hostname=h) for h in hostnames]
    console = Console()

    # ---- resolve managers -----------------------------------------------
    console.print(f"\n[bold]Resolving managers for {len(states)} sites…[/bold]")
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        list(as_completed({pool.submit(_resolve_site, s): s for s in states}))

    # ---- resolve image sizes --------------------------------------------
    console.print("[bold]Resolving image sizes from the vManage software repository…[/bold]")
    _resolve_image_sizes(states, max_workers)

    # ---- fire downloads --------------------------------------------------
    fireable = [s for s in states if s.status != DownloadStatus.FAILED]
    console.print(f"[bold]{'Cleaning up + firing' if cleanup else 'Firing'} downloads on {len(fireable)} sites…[/bold]")
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        list(as_completed({pool.submit(_fire_site, s, cleanup): s for s in fireable}))

    # ---- monitor loop ----------------------------------------------------
    vi_executor  = ThreadPoolExecutor(max_workers=max_workers)
    vi_submitted: set[str] = set()   # hostnames already handed to verify/install

    next_poll_at   = time.time() + poll_interval
    polling_flag   = threading.Event()

    def _run_poll_cycle():
        pollable = [s for s in states if s.status == DownloadStatus.DOWNLOADING]
        if pollable:
            with ThreadPoolExecutor(max_workers=max_workers) as pool:
                list(as_completed({pool.submit(_poll_site, s): s for s in pollable}))
        polling_flag.clear()

    with Live(console=console, refresh_per_second=2) as live:
        while not _all_terminal(states):
            now = time.time()

            # kick off verify/install for any newly completed downloads
            for s in states:
                if s.status == DownloadStatus.COMPLETE and s.hostname not in vi_submitted:
                    vi_submitted.add(s.hostname)
                    vi_executor.submit(_verify_install_site, s)

            next_poll_in = max(0, int(next_poll_at - now))
            live.update(_build_table(states, next_poll_in, polling_flag.is_set()))

            if now >= next_poll_at and not polling_flag.is_set():
                polling_flag.set()
                next_poll_at = time.time() + poll_interval
                threading.Thread(target=_run_poll_cycle, daemon=True).start()

            time.sleep(0.5)

        live.update(_build_table(states, 0, False))

    vi_executor.shutdown(wait=True)

    done  = sum(1 for s in states if s.status == DownloadStatus.DONE)
    failed = sum(1 for s in states if s.status == DownloadStatus.FAILED)
    console.print(
        f"\n[bold]Finished.[/bold]  "
        f"[green]{done} done[/green]  |  [red]{failed} failed[/red]"
    )


def _read_hostnames(hostnames_file: str) -> list[str]:
    return [
        ln.strip()
        for ln in Path(hostnames_file).read_text().splitlines()
        if ln.strip() and not ln.startswith("#")
    ]


def _target_version(filename: str) -> str:
    """Extract the version key from an image filename: 17.09.05f or 17.12.08."""
    import re as _re
    m = _re.search(r'universalk9\.(\d+(?:\.\d+)+[a-z]?)', filename or "")
    return m.group(1) if m else ""


_PREFLIGHT_COLOR = {
    "READY":             "green",
    "ALREADY CURRENT":   "blue",
    "ALREADY DOWNLOADED": "cyan",
    "UNREACHABLE":       "yellow",
    "NOT IN INVENTORY":  "red",
    "NO IMAGE MAPPED":   "red",
    "IMAGE NOT IN REPO": "red",
}


def _run_preflight(hostnames_file: str):
    """Read-only fleet readiness check. API only — no SSH, no device changes."""
    console = Console()

    hostnames = _read_hostnames(hostnames_file)
    if not hostnames:
        console.print("No hostnames found in file.")
        return

    console.print(f"\n[bold yellow]PRE-FLIGHT: {len(hostnames)} sites[/bold yellow]\n")

    manager = sdwanManager()
    live_by_host = {d.get("host-name"): d for d in (manager.devices or [])}
    cfg_by_host = {
        d.get("host-name"): d
        for d in (manager.get_devices(config_base_override=True) or [])
        if d.get("host-name")
    }
    repo_images = manager.get_software_images()
    manager.close_session()

    table = Table(title="Upgrade Pre-Flight", box=box.ROUNDED, expand=True)
    table.add_column("Hostname", style="bold white", no_wrap=True)
    table.add_column("Model",    style="dim",        no_wrap=True)
    table.add_column("Current",  no_wrap=True)
    table.add_column("Target",   no_wrap=True)
    table.add_column("Image",    style="dim",        max_width=40)
    table.add_column("Status",   no_wrap=True)

    counts: dict[str, int] = {}

    for hostname in hostnames:
        device = live_by_host.get(hostname)
        cfg = cfg_by_host.get(hostname, {})

        model    = (device or {}).get("device-model", "") or cfg.get("deviceModel", "")
        current  = (device or {}).get("version", "") or cfg.get("version", "")
        software = find_software_for_model(model) if model else None
        filename = software["filename"] if software else ""
        target   = _target_version(filename)

        available = cfg.get("availableVersions") or []
        reachable = (device or {}).get("reachability") == "reachable"

        if not device and not cfg:
            status = "NOT IN INVENTORY"
        elif not software:
            status = "NO IMAGE MAPPED"
        elif repo_images and filename not in repo_images:
            status = "IMAGE NOT IN REPO"
        elif target and current.startswith(target):
            status = "ALREADY CURRENT"
        elif target and any(str(v).startswith(target) for v in available):
            status = "ALREADY DOWNLOADED"
        elif not reachable:
            status = "UNREACHABLE"
        else:
            status = "READY"

        counts[status] = counts.get(status, 0) + 1
        color = _PREFLIGHT_COLOR.get(status, "white")

        table.add_row(
            hostname,
            model.replace("vedge-", "") or "—",
            current or "—",
            target or "—",
            filename or "—",
            f"[{color}]{status}[/{color}]",
        )

    console.print(table)

    summary = "  |  ".join(
        f"[{_PREFLIGHT_COLOR.get(s, 'white')}]{n} {s.lower()}[/{_PREFLIGHT_COLOR.get(s, 'white')}]"
        for s, n in sorted(counts.items(), key=lambda x: -x[1])
    )
    console.print(f"\n{summary}\n")


def _run_diagnose(hostname: str):
    """Walk every SSH hop for one device and print raw output at each step."""
    import re as _re

    from unicon.eal.expect import Spawn

    console = Console()
    console.print(f"\n[bold yellow]DIAGNOSE: {hostname}[/bold yellow]\n")

    console.print("[bold]Step 1:[/bold] Resolving manager via API…")
    target_manager = run_find_manager(hostname)
    if not target_manager:
        console.print("[red]Manager not found. Check hostname spelling and API connectivity.[/red]")
        return
    console.print(f"  manager     : [cyan]{target_manager.get('hostname')}[/cyan]")
    console.print(f"  public IP   : [cyan]{target_manager.get('manager_public_ip')}[/cyan]")
    console.print(f"  system IP   : [cyan]{target_manager.get('manager_system_ip')}[/cyan]")
    console.print(f"  edge sys IP : [cyan]{target_manager.get('edge_system_ip')}[/cyan]")

    device_model = target_manager.get("edge_device_model", "")
    software = find_software_for_model(device_model)
    if not software:
        console.print(f"  model       : [cyan]{device_model or 'unknown'}[/cyan]")
        console.print("[red]No image mapped for this model in constants/SOFTWARE_MAP.yaml[/red]")
        return
    software_filename = software["filename"]
    console.print(f"  model       : [cyan]{device_model}[/cyan]")
    console.print(f"  image       : [cyan]{software_filename}[/cyan]")

    client = sshManager(target_manager)
    creds  = client.credentials
    mgr_ip   = creds["ip"]
    mgr_user = creds["user"]
    mgr_pwd  = creds["pwd"]
    mgr_port = creds["port"]
    edge_ip  = creds["edge_system_ip"]
    edge_pwd = creds["edge_admin_pwd"]

    try:
        console.print(f"\n[bold]Step 2:[/bold] SSH to vManage {mgr_ip}:{mgr_port}…")
        spawn = Spawn(f"ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null "
                      f"{mgr_user}@{mgr_ip} -p {mgr_port}")
        spawn.expect(['Password:', 'password:'], timeout=10)
        spawn.sendline(mgr_pwd)
        m = spawn.expect(['vmanage#', '#', '>'], timeout=10)
        console.print(f"  [green]OK[/green] — prompt: {repr(client._extract_expect_output(m)[-40:])}")

        console.print(f"\n[bold]Step 3:[/bold] vshell on vManage…")
        spawn.sendline("vshell")
        m = spawn.expect([r'.*~\$'], timeout=10)
        console.print(f"  [green]OK[/green] — prompt: {repr(client._extract_expect_output(m)[-40:])}")

        console.print(f"\n[bold]Step 4:[/bold] SSH to cEdge {edge_ip}:{client.edge_ssh_port}…")
        spawn.sendline(f"ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null "
                       f"-p {client.edge_ssh_port} -l admin {edge_ip}")

        connected = False
        password_prompts = 0
        raw_prompt = ""

        # cEdge prompts for the password more than once through the jump host
        for _ in range(8):
            m = spawn.expect([
                r'.*continue connecting \(yes/no(?:/\[fingerprint\])?\)\?.*',
                r'.*[Pp]assword:\s*$',
                r'.*[#>]\s*$',
                r'.*\$\s*$',
            ], timeout=30)
            raw_prompt = client._extract_expect_output(m)

            if "continue connecting" in raw_prompt:
                console.print("  host key prompt → sending 'yes'")
                spawn.sendline("yes")
                continue

            if "password:" in raw_prompt.lower():
                password_prompts += 1
                console.print(f"  password prompt #{password_prompts} → sending edge password")
                spawn.sendline(edge_pwd)
                continue

            if raw_prompt.strip().endswith("#"):
                connected = True
                break

        if not connected:
            console.print(f"  [red]Never reached cEdge prompt[/red] — last output: {repr(raw_prompt[-120:])}")
            return

        console.print(f"  [green]OK[/green] — prompt: {repr(raw_prompt[-40:])} "
                      f"(password prompts handled: {password_prompts})")

        def _cli(command, timeout=30):
            """Run an IOS XE exec command and return its output."""
            spawn.sendline(command)
            m = spawn.expect([r'.*#\s*$'], timeout=timeout)
            return client._extract_expect_output(m)

        spawn.sendline("terminal length 0")
        spawn.expect([r'.*#\s*$'], timeout=10)

        console.print(f"\n[bold]Step 5:[/bold] CLI check — show version…")
        out = _cli("show version | include uptime|Version")
        console.print(f"[dim]{out.strip()[:400]}[/dim]")

        console.print(f"\n[bold]Step 6:[/bold] bootflash capacity vs image size…")
        dir_out = _cli("dir bootflash:", timeout=60)

        free_m = _re.search(r'(\d+)\s+bytes free', dir_out)
        free_bytes = int(free_m.group(1)) if free_m else None
        needed = software.get("bytes") or 0

        if free_bytes is None:
            console.print(f"  [yellow]Could not parse free space[/yellow] — raw tail: "
                          f"{repr(dir_out.strip()[-200:])}")
        else:
            console.print(f"  free        : [cyan]{free_bytes:,} bytes "
                          f"({free_bytes / 1048576:.1f} MiB)[/cyan]")
            if needed:
                console.print(f"  image needs : [cyan]{needed:,} bytes "
                              f"({needed / 1048576:.1f} MiB)[/cyan]")
                if free_bytes >= needed:
                    console.print(f"  [green]Sufficient space[/green] — "
                                  f"{(free_bytes - needed) / 1048576:.1f} MiB would remain")
                else:
                    console.print(f"  [red]INSUFFICIENT SPACE[/red] — short by "
                                  f"{(needed - free_bytes) / 1048576:.1f} MiB")
            else:
                console.print("  [yellow]No size in SOFTWARE_MAP.yaml — cannot compare[/yellow]")

        if software_filename in dir_out:
            console.print(f"  [blue]Image already present on bootflash:[/blue] {software_filename}")
        else:
            console.print("  image not yet on bootflash:")

        console.print(f"\n[bold]Step 7:[/bold] Installed software inventory…")
        sw_out = _cli("show sdwan software", timeout=60)
        if "Unknown command" in sw_out or "Invalid input" in sw_out:
            console.print("  [yellow]'show sdwan software' not supported — trying 'show software'[/yellow]")
            sw_out = _cli("show software", timeout=60)
        console.print(f"[dim]{sw_out.strip()[:600]}[/dim]")

        target_version = _target_version(software_filename)
        if target_version and target_version in sw_out:
            console.print(f"  [blue]Target version {target_version} already listed[/blue]")
        elif target_version:
            console.print(f"  target version {target_version} not yet installed")

        spawn.sendline("exit")
        spawn.close()

    except Exception as exc:
        console.print(f"\n[red]FAILED at this step: {exc}[/red]")

    console.print("\n[bold]Diagnose complete.[/bold]\n")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Bulk nohup wget download + verify + install across multiple cEdge sites."
    )
    parser.add_argument("--file",     "-f",
                        help="Text file with one hostname per line")
    parser.add_argument("--interval", "-i", type=int, default=POLL_INTERVAL_DEFAULT,
                        help=f"Poll interval in seconds (default: {POLL_INTERVAL_DEFAULT})")
    parser.add_argument("--workers",  "-w", type=int, default=MAX_WORKERS,
                        help=f"Max parallel SSH workers (default: {MAX_WORKERS})")
    parser.add_argument("--diagnose", "-d", metavar="HOSTNAME", nargs="?", const="",
                        help="With a hostname: walk each SSH hop for that device and dump raw "
                             "output. Bare, with --file: read-only fleet pre-flight over the API")
    parser.add_argument("--cleanup", "-c", action="store_true",
                        help="Remove existing image file(s) and wget logs before firing downloads")
    args = parser.parse_args()

    if args.diagnose:
        _run_diagnose(args.diagnose)
    elif args.diagnose == "" and args.file:
        _run_preflight(args.file)
    elif args.diagnose == "":
        parser.error("--diagnose without a hostname requires --file")
    elif args.file:
        run_bulk_download(args.file, poll_interval=args.interval,
                          max_workers=args.workers, cleanup=args.cleanup)
    else:
        parser.error("provide --file or --diagnose")
