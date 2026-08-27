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

# Image source on the vManage, and the only directory an SD-WAN edge accepts
# remote writes into. The staged filename must be the real image name — the
# edge rejects arbitrary names.
#
# MANAGER_IMAGE_DIR is expanded by the vManage shell, so the default resolves to
# the home of whichever user VMANAGE_USER is — not necessarily /home/admin.
MANAGER_IMAGE_DIR = os.getenv("MANAGER_IMAGE_DIR", "$HOME")
EDGE_STAGING_DIR = "/bootflash/vmanage-admin"
EDGE_STAGING_DIR_CLI = "bootflash:vmanage-admin/"

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
        disconnect_patterns = [
            r'.*closed by remote host.*',
            r'.*Connection to .* closed.*',
        ]
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

        patterns = interactive_patterns + disconnect_patterns + prompt_patterns
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

            # The vManage drops the session under load; fail now rather than
            # waiting out the remaining timeout for a prompt that never arrives.
            if "closed by remote host" in lowered or "connection to" in lowered and "closed" in lowered:
                raise ConnectionError(f"session closed by remote host during: {command}")

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

    def _open_manager_vshell_spawn(self):
        """Open SSH to the vManage and drop into its vshell (bash). Returns active spawn."""
        from unicon.eal.expect import Spawn

        manager_ip = self.credentials.get("ip")
        manager_user = self.credentials.get("user")
        manager_pwd = self.credentials.get("pwd")
        manager_port = self.credentials.get("port")

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

        return spawn

    def _open_edge_spawn(self):
        """Open SSH manager->cEdge jump session and return active spawn at the IOS XE prompt."""
        edge_ip = self.credentials.get("edge_system_ip")
        edge_pwd = self.credentials.get("edge_admin_pwd")
        edge_port = self.edge_ssh_port

        spawn = self._open_manager_vshell_spawn()

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

    def _vshell_run(self, spawn, command, marker, timeout=15):
        """Send a command that embeds its result in an echo marker, return matched text."""
        spawn.sendline(command)
        match = spawn.expect([marker], timeout=timeout)
        raw = self._extract_expect_output(match)
        # consume the trailing prompt so the buffer is clean for the next command
        spawn.expect([r'.*~?\$\s*$', r'.*\$\s*$'], timeout=5)
        return raw

    def fire_background_scp(self, filename):
        """Launch a detached sshpass+scp push from the vManage to the edge. Returns PID or None."""
        import re

        edge_ip = self.credentials.get("edge_system_ip")
        edge_pwd = self.credentials.get("edge_admin_pwd")

        spawn = self._open_manager_vshell_spawn()

        # Log beside the image so it lands somewhere this user can write.
        log_path = f"{MANAGER_IMAGE_DIR}/scp_{edge_ip}.log"

        # Fail early and clearly if the image is not readable by this user.
        check = self._vshell_run(
            spawn,
            f"test -r {MANAGER_IMAGE_DIR}/{filename} && echo IMG:ok || echo IMG:missing",
            r'IMG:\S+',
        )
        if "IMG:ok" not in check:
            try:
                spawn.sendline("exit")
                spawn.close()
            except Exception:
                pass
            raise Exception(
                f"image not readable at {MANAGER_IMAGE_DIR}/{filename} "
                f"as user {self.credentials.get('user')}")

        # SSHPASS is passed via the environment so the password never lands in the process list.
        # scp cannot resume, so a retry always restarts from zero.
        cmd = (
            f"SSHPASS='{edge_pwd}' nohup sshpass -e scp -P {self.edge_ssh_port}"
            f" -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null"
            f" {MANAGER_IMAGE_DIR}/{filename}"
            f" admin@{edge_ip}:{EDGE_STAGING_DIR}/{filename}"
            f" >{log_path} 2>&1 &"
            f" echo BGPID:$!"
        )
        spawn.sendline(cmd)
        match = spawn.expect(
            [r'BGPID:\d+', r'Permission denied', r'No such file or directory',
             r'command not found', r'BGPID:'],
            timeout=15,
        )
        raw = self._extract_expect_output(match)
        try:
            spawn.expect([r'.*~?\$\s*$', r'.*\$\s*$'], timeout=5)
        except Exception:
            pass

        try:
            spawn.sendline("exit")
            spawn.close()
        except Exception:
            pass

        # A failed redirect or missing binary still yields a PID for the dead subshell,
        # so treat any shell error as a launch failure rather than a running transfer.
        for marker in ("Permission denied", "No such file or directory", "command not found"):
            if marker in raw:
                raise Exception(f"scp launch failed on the vManage shell: {marker} — {raw.strip()[-160:]}")

        pid = None
        m = re.search(r'BGPID:(\d+)', raw)
        if m:
            pid = m.group(1)
        else:
            # fallback: bash job notification [1] 12345 in leftover buffer
            m = re.search(r'\[\d+\]\s+(\d+)', raw)
            if m:
                pid = m.group(1)

        tprint(f"scp push fired to {self.credentials.get('edge_hostname')} — PID: {pid}")
        if pid is None:
            tprint(f"[!] Could not parse PID. Raw output: {repr(raw)}")

        return pid

    def cleanup_before_transfer(self, filename):
        """Remove any partial/stale staged image from the edge before a fresh push."""
        result = self.send_command_on_edge_cli(
            f"delete /force {EDGE_STAGING_DIR_CLI}{filename}", custom_timeout=60)
        tprint(f"Cleaned staged image on {self.credentials.get('edge_hostname')}")
        return result

    def poll_transfer_progress(self, filename):
        """Return bytes written so far to the edge staging directory.

        `ps` is unreliable on the vManage, so liveness is inferred by the caller
        from byte growth across polls rather than from process state.
        """
        spawn = self._open_edge_spawn()

        spawn.sendline(f"dir {EDGE_STAGING_DIR_CLI}{filename}")
        match = spawn.expect([r'.*#\s*$'], timeout=60)
        out = self._extract_expect_output(match)

        try:
            spawn.sendline("exit")
            spawn.close()
        except Exception:
            pass

        bytes_transferred = 0
        note = ""

        if "No such file" in out or "%Error" in out:
            note = "not yet created on the edge"
        else:
            for line in out.splitlines():
                # the "Directory of <path>" header also contains the filename — skip it
                if filename not in line or line.strip().startswith("Directory of"):
                    continue
                # dir entry: <index>  -rw-  <bytes>  <date>  <name>
                for token in line.split()[1:]:
                    if token.isdigit():
                        bytes_transferred = int(token)
                        break
                if bytes_transferred:
                    break

        return {
            "bytes_transferred": bytes_transferred,
            "note":              note,
        }

    def stage_image(self, filename, custom_timeout=600, reuse_session=False):
        """Copy the pushed image from the staging directory to the bootflash root."""
        result = {"success": False, "output": ""}
        owns_spawn = False

        try:
            if reuse_session:
                spawn = self.open_edge_session()
            else:
                spawn = self._open_edge_spawn()
                owns_spawn = True

            spawn.sendline(
                f"copy {EDGE_STAGING_DIR_CLI}{filename} bootflash:{filename}")

            out = ""
            for _ in range(4):
                match = spawn.expect([
                    r'.*Destination filename.*\?\s*$',
                    r'.*over ?write.*\[confirm\].*',
                    r'.*\[confirm\].*',
                    r'.*#\s*$',
                ], timeout=custom_timeout)
                chunk = self._extract_expect_output(match)
                out += chunk

                if "Destination filename" in chunk or "confirm" in chunk.lower():
                    spawn.sendline("")
                    continue
                break

            result["output"] = out
            result["success"] = "bytes copied in" in out

            if owns_spawn:
                try:
                    spawn.sendline("exit")
                    spawn.close()
                except Exception:
                    pass

            if result["success"]:
                tprint(f"Staged image to bootflash: on {self.credentials.get('edge_hostname')}")
            else:
                tprint(f"[!] Staging copy did not confirm. Output tail: {repr(out[-200:])}")

        except Exception as e:
            tprint(f"Staging copy failed: {e}")
            result["output"] = str(e)

        return result

    def remove_staged_image(self, filename, reuse_session=False):
        """Delete the staging-directory copy once the install is confirmed."""
        result = self.send_command_on_edge_cli(
            f"delete /force {EDGE_STAGING_DIR_CLI}{filename}",
            custom_timeout=60, reuse_session=reuse_session)
        tprint(f"Removed staged image on {self.credentials.get('edge_hostname')}")
        return result

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
            
        except ConnectionError:
            # let the caller reconnect and retry rather than reporting a command failure
            raise
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