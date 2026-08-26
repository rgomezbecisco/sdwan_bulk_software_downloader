from utils import tprint
import sys
import time
from jinja2 import Environment, FileSystemLoader

# Comprehensive warning suppression - must be done before any pyATS imports
import warnings
warnings.filterwarnings("ignore")
warnings.filterwarnings("ignore", category=DeprecationWarning)
warnings.filterwarnings("ignore", category=UserWarning)
warnings.filterwarnings("ignore", category=FutureWarning)
warnings.filterwarnings("ignore", category=PendingDeprecationWarning)
warnings.filterwarnings("ignore", message=".*pkg_resources.*")
warnings.filterwarnings("ignore", message=".*deprecated.*")
warnings.filterwarnings("ignore", message=".*setuptools.*")

# Set environment variables to suppress pyATS logging and warnings
import os
os.environ["PYATS_LOGS_DIR"] = "/dev/null"
os.environ["PYATS_ARCHIVE_DIR"] = "/dev/null"
os.environ["UNICON_LOG_LEVEL"] = "CRITICAL"
os.environ["UNICON_DEFAULT_LEARN_OS"] = "FALSE"
os.environ["PYATS_LIBS_EXTERNAL_LIBS_LOG"] = "CRITICAL"
os.environ["PYTHONWARNINGS"] = "ignore"

# Import pyats modules after warning suppression
from pyats.topology import loader

class sshManager():

    def __init__(self, target_manager):

        self.target_manager = target_manager
        self.credentials = self.load_credentials()
        self.rendered_template_manager = self.render_testbed('testbed_manager.j2')
        # edge access through the SDWAN Manager jump host uses port 830 in this environment
        self.edge_ssh_port = int(os.getenv("EDGE_SSH_PORT", "830"))
        self.rendered_template_cedge = self.render_testbed('testbed_cedge.j2', ssh_port=self.edge_ssh_port)
        self._edge_spawn = None

    def load_credentials(self):
        """Load vManage credentials from environment variables."""
    
        if os.getenv("VMANAGE_USER") and os.getenv("VMANAGE_PASSWORD"):

            credentials = {
            "hostname": self.target_manager.get("hostname"),
            "ip" : self.target_manager.get("manager_public_ip"),
            "port": 22,
            "user": os.getenv("VMANAGE_USER"),
            "pwd": os.getenv("VMANAGE_PASSWORD"),
            "edge_hostname": self.target_manager.get("edge_hostname"),
            "edge_system_ip": self.target_manager.get("edge_system_ip"),
            "edge_admin_pwd": os.getenv("EDGE_ADMIN_PASSWORD")
            }

            tprint("Manager credentials loaded")

            return credentials

        else:
        
            tprint("Environment variables not loaded. Locally: source credentials.sh. In CI: set VMANAGE_USER, VMANAGE_PASSWORD as GitLab CI variables")
            sys.exit(1)

    def render_testbed(self,template_name,ssh_port=22):
        """Render pyATS testbed for the target vManage."""

        testbedpath = os.path.join('constants')
    
        file_loader = FileSystemLoader(testbedpath)
        env = Environment(loader=file_loader)
        template = env.get_template(template_name)

        # Render the template with target_manager data
        rendered_template = template.render(
            manager_hostname = self.credentials.get("hostname"),
            manager_ip = self.credentials.get("ip"),
            manager_port = self.credentials.get("port"),
            manager_username = self.credentials.get("user"),
            manager_password = self.credentials.get("pwd"),
            edge_hostname = self.credentials.get("edge_hostname"),
            edge_system_ip = self.credentials.get("edge_system_ip"),
            edge_port = ssh_port,
            edge_admin_password = self.credentials.get("edge_admin_pwd"),
        )
        
        return rendered_template

    @staticmethod
    def _extract_expect_output(match):
        """Return best-effort text from unicon expect match object."""
        if hasattr(match, "match_output") and match.match_output:
            return match.match_output
        if hasattr(match, "match") and hasattr(match.match, "group"):
            try:
                return match.match.group(0)
            except Exception:
                return str(match)
        return str(match)

    def _run_command_on_spawn(self, spawn, command, custom_timeout, password=None, stream_output=False):
        """Run a command in an existing shell and handle interactive confirmations."""
        import re
        prompt_patterns = [r'.*[#>]\s*$']
        interactive_patterns = [
            r'.*continue connecting \(yes/no(?:/\[fingerprint\])?\)\?.*',
            r'.*\[yes/no\].*',
            r'.*\(yes/no\).*',
            r'.*Do you want to continue.*',
            r'.*Proceed.*',
            r'.*Are you sure.*',
            r'.*[Pp]assword:\s*$',
        ]
        # Matches a real password prompt at end of received text (not embedded in output)
        _password_prompt_re = re.compile(r'[Pp]assword:\s*$')

        patterns = interactive_patterns + prompt_patterns
        output_chunks = []

        spawn.sendline(command)

        deadline = time.time() + max(1, int(custom_timeout))
        while time.time() < deadline:
            remaining = max(1, int(deadline - time.time()))
            # Use full remaining time as step timeout so slow commands don't get cut off
            step_timeout = remaining

            match = spawn.expect(patterns, timeout=step_timeout)
            text = self._extract_expect_output(match)
            output_chunks.append(text)
            if stream_output and text.strip():
                print(text, end="", flush=True)

            lowered = text.lower()
            stripped = text.strip()

            # Only send password if the output ends with an actual password prompt
            if password and _password_prompt_re.search(stripped):
                spawn.sendline(password)
                continue

            if (
                "yes/no" in lowered
                or "do you want to continue" in lowered
                or "proceed" in lowered
                or "are you sure" in lowered
                or "continue connecting" in lowered
            ):
                spawn.sendline("yes")
                continue

            if stripped.endswith("#") or stripped.endswith(">"):
                return "\n".join(output_chunks)

        raise TimeoutError(f"Timed out waiting for command completion: {command}")

    def _open_edge_spawn(self):
        """Open SSH manager->cEdge jump session and return active spawn at the IOS XE prompt."""
        from unicon.eal.expect import Spawn

        manager_ip = self.credentials.get("ip")
        manager_user = self.credentials.get("user")
        manager_pwd = self.credentials.get("pwd")
        manager_port = self.credentials.get("port")
        edge_ip = self.credentials.get("edge_system_ip")
        edge_pwd = self.credentials.get("edge_admin_pwd")
        edge_port = self.edge_ssh_port

        spawn = Spawn(
            f"ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null "
            f"{manager_user}@{manager_ip} -p {manager_port}"
        )

        spawn.expect(['Password:', 'password:'], timeout=10)
        spawn.sendline(manager_pwd)
        spawn.expect(['vmanage#', '#', '>'], timeout=10)
        tprint(f"Connected to {self.credentials.get('hostname')} (manager)")

        # vshell on the manager — `request execute` is reserved for TAC
        spawn.sendline("vshell")
        spawn.expect([r'.*~\$'], timeout=10)
        tprint(f"Entered vshell on {self.credentials.get('hostname')}")

        spawn.sendline(
            f"ssh -o StrictHostKeyChecking=no "
            f"-o UserKnownHostsFile=/dev/null -p {edge_port} -l admin {edge_ip}"
        )

        connected = False
        password_prompts = 0

        for _ in range(8):
            match = spawn.expect([
                r'.*continue connecting \(yes/no(?:/\[fingerprint\])?\)\?.*',
                r'.*[Pp]assword:\s*$',
                r'.*[#>]\s*$',
                r'.*\$\s*$',
            ], timeout=30)
            output = self._extract_expect_output(match)

            if "continue connecting" in output:
                spawn.sendline("yes")
                continue

            if "password:" in output.lower():
                spawn.sendline(edge_pwd)
                password_prompts += 1
                continue

            if output.strip().endswith("#"):
                connected = True
                break

        if not connected:
            raise Exception("Could not reach cEdge prompt after jump-host SSH authentication")

        tprint(f"Connected to cEdge {edge_ip}:{edge_port}. Password prompts handled: {password_prompts}")

        spawn.sendline("terminal length 0")
        spawn.expect([r'.*[#>]\s*$'], timeout=10)

        return spawn

    def open_edge_session(self):
        """Open and keep an edge shell session for subsequent commands."""
        if self._edge_spawn is None:
            self._edge_spawn = self._open_edge_spawn()
        return self._edge_spawn

    def close_edge_session(self):
        """Close a previously opened reusable edge shell session."""
        if self._edge_spawn is not None:
            try:
                self._edge_spawn.sendline("exit")
                self._edge_spawn.close()
            except Exception:
                pass
            self._edge_spawn = None

    def _open_edge_vshell_spawn(self):
        """Extend _open_edge_spawn by entering a bash shell on the edge.

        Viptela-only — IOS XE cEdge has no `vshell`. Pending replacement in the
        cEdge download engine rework.
        """
        spawn = self._open_edge_spawn()
        spawn.sendline("vshell")
        # match the full prompt line so match_output includes it; Viptela prompt: hostname:~$
        spawn.expect([r'.*~?\$\s*$', r'.*~\$'], timeout=10)
        tprint(f"Entered vshell on {self.credentials.get('edge_hostname')}")
        return spawn

    def _vshell_run(self, spawn, command, marker, timeout=15):
        """Send a command that embeds its result in an echo marker, return matched text."""
        spawn.sendline(command)
        match = spawn.expect([marker], timeout=timeout)
        raw = self._extract_expect_output(match)
        # consume the trailing prompt so the buffer is clean for the next command
        spawn.expect([r'.*~?\$\s*$', r'.*\$\s*$'], timeout=5)
        return raw

    def fire_background_wget(self, vmanage_ip, filename):
        """Launch nohup wget on the edge bash shell and disconnect. Returns PID string or None."""
        import re
        spawn = self._open_edge_vshell_spawn()
        # --continue resumes a partial file; keep flags minimal for Viptela wget compatibility
        cmd = (
            f"nohup wget --continue"
            f" http://{vmanage_ip}:8080/software/package/{filename}"
            f" -O /home/admin/{filename} >/tmp/wget_dl.log 2>&1 &"
            f" echo BGPID:$!"
        )
        spawn.sendline(cmd)
        match = spawn.expect([r'BGPID:\d+', r'BGPID:'], timeout=15)
        raw = self._extract_expect_output(match)
        # consume prompt
        try:
            spawn.expect([r'.*~?\$\s*$', r'.*\$\s*$'], timeout=5)
        except Exception:
            pass
        pid = None
        m = re.search(r'BGPID:(\d+)', raw)
        if m:
            pid = m.group(1)
        else:
            # fallback: bash job notification [1] 12345 in leftover buffer
            m = re.search(r'\[\d+\]\s+(\d+)', raw)
            if m:
                pid = m.group(1)
        try:
            spawn.sendline("exit")
            spawn.close()
        except Exception:
            pass
        tprint(f"nohup wget fired on {self.credentials.get('edge_hostname')} — PID: {pid}")
        if pid is None:
            tprint(f"[!] Could not parse PID. Raw output: {repr(raw)}")
        return pid

    def get_remote_file_size(self, vmanage_ip, filename):
        """Return the staged image size in bytes via wget --spider, or None if unavailable."""
        import re as _re

        spawn = self._open_edge_vshell_spawn()
        raw = self._vshell_run(
            spawn,
            f"echo SPIDER:$(wget --spider"
            f" http://{vmanage_ip}:8080/software/package/{filename} 2>&1"
            f" | grep -oE 'Length: [0-9]+' | head -1 | tr -dc '0-9')",
            r'SPIDER:\d*',
            timeout=30,
        )

        try:
            spawn.sendline("exit")
            spawn.close()
        except Exception:
            pass

        m = _re.search(r'SPIDER:(\d+)', raw)
        size = int(m.group(1)) if m else None

        if size:
            tprint(f"Image '{filename}' is {size} bytes on {vmanage_ip}")
        else:
            tprint(f"[!] Could not determine size of '{filename}' on {vmanage_ip}")

        return size

    def cleanup_before_download(self, filename):
        """Remove stale download file(s) and old wget logs from the edge before a fresh run."""
        spawn = self._open_edge_vshell_spawn()
        # remove versioned copies wget creates when target file already exists (.1, .2, …)
        self._vshell_run(spawn,
            f"rm -f /home/admin/{filename} /home/admin/{filename}.* 2>/dev/null; echo CLEAN:done",
            r'CLEAN:done')
        self._vshell_run(spawn,
            "rm -f /tmp/wget_dl.log /tmp/wget_dl.log.* 2>/dev/null; echo LOGCLEAN:done",
            r'LOGCLEAN:done')
        tprint(f"Cleaned up old download files on {self.credentials.get('edge_hostname')}")
        try:
            spawn.sendline("exit")
            spawn.close()
        except Exception:
            pass

    def poll_download_progress(self, filename, expected_bytes):
        """Check download progress via edge bash shell.

        Returns dict with keys:
          bytes_downloaded, process_alive, progress_pct, wget_speed, wget_eta, log_tail
        """
        import re as _re

        spawn = self._open_edge_vshell_spawn()

        # embed result in marker — bypasses match_output capture issues
        raw_size = self._vshell_run(
            spawn,
            f"echo SIZE:$(wc -c < /home/admin/{filename} 2>/dev/null || echo 0)",
            r'SIZE:\d+',
        )
        m = _re.search(r'SIZE:(\d+)', raw_size)
        bytes_downloaded = int(m.group(1)) if m else 0

        raw_proc = self._vshell_run(
            spawn,
            "echo PROC:$(ps 2>/dev/null | grep wget | grep -v grep | wc -l || echo 0)",
            r'PROC:\S+',
        )
        m = _re.search(r'PROC:(\S+)', raw_proc)
        proc_val = m.group(1) if m else "0"
        process_alive = proc_val.isdigit() and int(proc_val) > 0

        # extract percentage from last wget progress line in the log
        raw_pct = self._vshell_run(
            spawn,
            "echo PCT:$(grep -oE '[0-9]+%' /tmp/wget_dl.log 2>/dev/null | tail -1 | tr -d '%' || echo NONE)",
            r'PCT:\S+',
        )
        m = _re.search(r'PCT:(\S+)', raw_pct)
        pct_val = m.group(1) if m else "NONE"
        progress_pct = float(pct_val) if pct_val.isdigit() else -1.0

        # speed and ETA from the last full progress line
        raw_log = self._vshell_run(
            spawn,
            "echo LOGLINE:$(grep -E '[0-9]+%' /tmp/wget_dl.log 2>/dev/null | tail -1 | tr -s ' ' | cut -d' ' -f7,8 || echo NONE)",
            r'LOGLINE:\S+',
        )
        wget_speed = ""
        wget_eta   = ""
        log_tail   = ""
        m = _re.search(r'LOGLINE:(\S+)\s*(\S*)', raw_log)
        if m and m.group(1) != "NONE":
            wget_speed = m.group(1)
            wget_eta   = m.group(2)
            log_tail   = f"{pct_val}% @ {wget_speed} ETA {wget_eta}"
        elif "NO_LOG" in raw_log or pct_val == "NONE":
            log_tail = "no wget log — wget may not have launched"

        try:
            spawn.sendline("exit")
            spawn.close()
        except Exception:
            pass

        return {
            "bytes_downloaded": bytes_downloaded,
            "process_alive":    process_alive,
            "progress_pct":     progress_pct,
            "wget_speed":       wget_speed,
            "wget_eta":         wget_eta,
            "log_tail":         log_tail,
        }

    def send_command_on_manager_cli(self, command):
        """Send a command on the manager CLI via pyATS."""

        result = {
            "success": False,
            "output": ""
        }

        testbed = loader.load(self.rendered_template_manager)

        device = testbed.devices[self.credentials.get("hostname")]

        device.connect(
            init_exec_commands=[],
            init_config_commands=[],
            log_stdout=False,
            logfile=None,
        )

        tprint(f"Successfully connected to {self.credentials.get('hostname')} via pyATS")

        tprint(f"Sending command on Manager CLI: {command}")
        output = device.execute(command, timeout=60)
        result["output"] = output

        if output:
            result["success"] = True
            tprint(f"Command executed successfully on Manager CLI")

        else:
            tprint(f"Command execution on Manager CLI may have failed. Output: {output}")

        return result

    def send_file_on_manager_cli(self,manager_path, remote_filename):

        result = {
            "success": False,
            "command": "",
        }

        ip = self.credentials.get('edge_system_ip')
        edge_pwd = self.credentials.get('edge_admin_pwd')
        # Remove stale host keys on the manager shell in case edge IPs are reused/reimaged.
        cleanup_known_host_command = f"ssh-keygen -R {ip} >/dev/null 2>&1 || true"
        
        scp_command = (
            f"scp -o StrictHostKeyChecking=no "
            f"-o UserKnownHostsFile=/dev/null "
            f"-o GlobalKnownHostsFile=/dev/null "
            f"-P 22 {manager_path} admin@{ip}:/home/admin/{remote_filename}"
        )
        result["command"] = scp_command
        
        tprint(f"Uploading file to Edge {ip} via SCP")

        try:
            # Use Unicon's EAL (expectation and action language) for raw SSH
            from unicon.eal.expect import Spawn
            
            # Spawn a raw SSH session (bypasses device discovery)
            spawn = Spawn(
                f"ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null "
                f"{self.credentials.get('user')}@{self.credentials.get('ip')} -p {self.credentials.get('port')}"
            )
            
            # Wait for password prompt and authenticate
            spawn.expect(['Password:', 'password:'], timeout=10)
            spawn.sendline(self.credentials.get("pwd"))
            
            # Wait for shell prompt
            spawn.expect(['vmanage#', '#', '>'], timeout=10)
            tprint(f"Successfully connected to {self.credentials.get('hostname')} via SSH")

            # Enter vshell to access scp (request execute is reserved for TAC)
            spawn.sendline("vshell")
            spawn.expect([r'.*~\$'], timeout=10)
            tprint(f"Entered vshell on {self.credentials.get('hostname')}")

            # Best-effort cleanup before SCP to avoid "REMOTE HOST IDENTIFICATION HAS CHANGED" failures.
            spawn.sendline(cleanup_known_host_command)
            spawn.expect([r'.*~\$'], timeout=15)
            
            # Send the SCP command
            spawn.sendline(scp_command)
            
            # Wait for edge password prompt
            spawn.expect(['password:', 'Password:'], timeout=30)
            spawn.sendline(edge_pwd)
            
            # Wait for completion
            spawn.expect(['100%', '#'], timeout=300)
            output = spawn.match.group(0) if hasattr(spawn.match, 'group') else str(spawn.match)
            
            if '100%' in output:
                result["success"] = True
                tprint(f"File uploaded to Edge {ip} successfully")
            else:
                tprint(f"File upload to Edge {ip} may have failed")
            
            spawn.close()
                
        except Exception as e:
            result["output"] = str(e)
            tprint(f"SCP upload failed: {str(e)}")

        return result

    def send_command_on_edge_cli(self, command, custom_timeout=60, reuse_session=False, stream_output=False):
        """Send a command on the cEdge CLI."""

        result = {
            "success": False,
            "output": ""
        }

        edge_pwd = self.credentials.get("edge_admin_pwd")
        owns_spawn = False
        try:
            if reuse_session:
                spawn = self.open_edge_session()
            else:
                spawn = self._open_edge_spawn()
                owns_spawn = True

            # Send the actual command
            tprint(f"Sending command on cEdge CLI: {command}")
            output = self._run_command_on_spawn(
                spawn,
                command,
                custom_timeout=custom_timeout,
                password=edge_pwd,
                stream_output=stream_output,
            )
            result["output"] = output

            if output:
                result["success"] = True
                tprint(f"Command executed successfully on cEdge CLI")
            else:
                tprint(f"Command execution on cEdge CLI may have failed. Output: {output}")

            if owns_spawn:
                try:
                    spawn.sendline("exit")
                except Exception:
                    pass
                spawn.close()
            
        except Exception as e:
            tprint(f"cEdge connection/command failed: {str(e)}")
            result["output"] = str(e)

        return result
    
    def send_command_list_on_edge_cli(self, commands, custom_timeout=60):
        """Send a list of commands on the cEdge CLI."""

        result = {}
        
        try:
            # Use Unicon's EAL Spawn for raw SSH through manager proxy
            from unicon.eal.expect import Spawn
            
            manager_ip = self.credentials.get("ip")
            manager_user = self.credentials.get("user")
            manager_pwd = self.credentials.get("pwd")
            manager_port = self.credentials.get("port")
            edge_ip = self.credentials.get("edge_system_ip")
            edge_pwd = self.credentials.get("edge_admin_pwd")
            edge_port = self.edge_ssh_port

            # First, SSH into the manager
            spawn = Spawn(
                f"ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null "
                f"{manager_user}@{manager_ip} -p {manager_port}"
            )
            
            # Authenticate to manager
            spawn.expect(['Password:', 'password:'], timeout=10)
            spawn.sendline(manager_pwd)
            spawn.expect(['vmanage#', '#', '>'], timeout=10)
            
            tprint(f"Connected to manager {self.credentials.get('hostname')} for cEdge jump session")
            
            # Enter vshell to access SSH (request execute is reserved for TAC)
            spawn.sendline("vshell")
            spawn.expect([r'.*~\$'], timeout=10)
            tprint(f"Entered vshell on {self.credentials.get('hostname')}")

            # Now SSH from manager to cEdge
            ssh_command = (
                f"ssh -o StrictHostKeyChecking=no "
                f"-o UserKnownHostsFile=/dev/null -p {edge_port} -l admin {edge_ip}"
            )
            spawn.sendline(ssh_command)

            password_prompts = 0
            connected_to_edge = False

            for _ in range(8):
                match = spawn.expect([
                    r'.*continue connecting \(yes/no(?:/\[fingerprint\])?\)\?.*',
                    r'.*[Pp]assword:\s*$',
                    r'.*Password:\s*$',
                    r'.*[#>]\s*$',
                    r'.*\$\s*$'
                ], timeout=30)

                output = match.match_output or "" if hasattr(match, 'match_output') else str(match)

                if "continue connecting" in output:
                    spawn.sendline("yes")
                    continue

                if "password:" in output.lower():
                    spawn.sendline(edge_pwd)
                    password_prompts += 1
                    continue

                stripped = output.strip()
                if stripped.endswith("#"):
                    connected_to_edge = True
                    break

            if not connected_to_edge:
                raise Exception("Could not reach cEdge prompt after jump-host SSH authentication")

            tprint(f"Connected to cEdge {edge_ip}:{edge_port}. Password prompts handled: {password_prompts}")

            # Disable paging once in cEdge shell
            spawn.sendline("terminal length 0")
            spawn.expect([r'.*[#>]\s*$'], timeout=10)

            for command in commands:
                tprint(f"Sending command on cEdge CLI: {command}")
                spawn.sendline(command)
                cmd_match = spawn.expect([r'.*[#>]\s*$'], timeout=custom_timeout)
                result[command] = cmd_match.match_output or "" if hasattr(cmd_match, 'match_output') else str(cmd_match)

            # Exit cEdge shell back to manager shell
            spawn.sendline("exit")
            spawn.close()

        except Exception as e:
            tprint(f"cEdge connection/command failed: {str(e)}")
            result["error"] = str(e)

        return result