# SD-WAN Bulk Software Downloader

Standalone tool to bulk-push, verify, and install an IOS XE software image
onto multiple Cisco SD-WAN cEdge (IOS XE) devices in parallel, via SSH through
their managing vManage node.

Extracted from an internal AutoZone migration toolkit so it can be reused
against other SD-WAN customer environments.

## What it does

For each hostname listed in an input file, the tool:

1. Looks up the device's managing vManage node via the vManage API, and the
   image mapped to that device's model.
2. SSHes to that vManage node and fires a detached `sshpass`/`scp` push of the
   image into the device's `bootflash:vmanage-admin/` staging directory.
3. Polls transfer progress from the device until the byte count matches the
   expected image size (restarting the transfer if it stalls — scp cannot
   resume, so a retry starts from zero).
4. Copies the image from the staging directory to the `bootflash:` root.
5. Runs `verify bootflash:<image>` and
   `request platform software sdwan software install bootflash:<image>`.
6. Confirms the new version is listed in `show sdwan software`, then deletes
   the staging copy.

The install only *adds* the image — it does not activate or reboot.

Progress for all sites is shown live in a terminal table (via `rich`).

## Requirements

- Python 3.9+
- Network/SSH reachability from this machine to the customer's vManage nodes
- A vManage API user with read access to device inventory
- `sshpass` available on the vManage (used for the non-interactive scp push)
- SSH admin credentials for the cEdge devices (or a shared admin password)

Install dependencies:

```bash
pip install -r requirements.txt
```

`pyats[full]` pulls in `unicon`, which is used for SSH session handling.

## One-time setup

1. Copy the credentials template and fill in the target customer's values:

   ```bash
   cp sample_credentials.sh credentials.sh
   source ./credentials.sh
   ```

   `credentials.sh` is gitignored — never commit real credentials.

2. Copy the manager map template and edit it with one entry per vManage node in
   the customer's cluster (system IP + reachable public IP/hostname). This is
   how the tool resolves which vManage node owns a given device.

   ```bash
   cp core/constants/sample_MANAGER_MAP.yaml core/constants/MANAGER_MAP.yaml
   ```

3. Copy the software map template and edit it with one entry per device model,
   keyed by the raw `device-model` value vManage reports for that platform:

   ```bash
   cp core/constants/sample_SOFTWARE_MAP.yaml core/constants/SOFTWARE_MAP.yaml
   ```

   ```yaml
   vedge-C8000V:
     filename: "c8000v-universalk9.17.12.08.SPA.bin"
     bytes: 863114363
   ```

   `filename` must match the image staged on the customer's vManage software
   repository. Leave `bytes: 0` to have the tool read the real size from the
   repository at run time (one lookup per distinct image); set a non-zero value
   to skip that lookup. A device whose model has no entry fails immediately
   rather than downloading a wrong image.

   Both files are gitignored — they hold environment-specific data and are
   never committed.

   To list the models present in an environment, and the images staged in the
   repository:

   ```bash
   curl -k -u "$VMANAGE_USER" "https://$VMANAGE_IP/dataservice/device"
   curl -k -u "$VMANAGE_USER" "https://$VMANAGE_IP/dataservice/device/action/software"
   ```

## Usage

Copy the hostname list template and list the devices to act on, one per line
(blank lines and lines starting with `#` are ignored):

```bash
cp sample_hostnames.txt hostnames.txt
```

`hostnames.txt` is gitignored — it holds environment-specific data.

Run a read-only pre-flight first (API only, no SSH, no device changes):

```bash
python tool_bulk_download_software.py --diagnose --file hostnames.txt
```

Run the bulk download:

```bash
python tool_bulk_download_software.py --file hostnames.txt
```

Options:

```bash
--file, -f       Text file with one hostname per line (required, unless --diagnose)
--interval, -i   Poll interval in seconds (default: 60)
--workers, -w    Max parallel SSH workers (default: 20)
--cleanup, -c    Delete any partial/stale staged image on the edge before pushing
--diagnose, -d   With a hostname: walk each SSH hop and print raw output.
                 Bare, with --file: read-only fleet pre-flight over the API
```

Diagnose a single device (useful when troubleshooting connectivity before a
bulk run):

```bash
python tool_bulk_download_software.py --diagnose S10712-HUB
```

## Project layout

```bash
tool_bulk_download_software.py   root wrapper (entry point)
core/
  bulk_download_software.py      orchestrator: parallel resolve/fire/poll/verify/install + rich UI
  find_manager.py                resolves which vManage node owns a device
  install_iosxe_image.py         runs "request software install-image"
  verify_iosxe_image.py          runs "request software verify-image"
  constants/
    MANAGER_MAP.yaml             customer-specific vManage cluster map (edit this)
    testbed_manager.j2           pyATS testbed template for the vManage node
    testbed_cedge.j2             pyATS testbed template for cEdge (IOS XE) proxy hop
  utils/
    api_manager.py                vManage API session/device lookup (catalystwan)
    ssh_manager.py                 SSH/pyATS session handling, scp push/poll, install/verify calls
    additional_functions.py        small shared helpers (arg parsing, yaml/json helpers, manager map lookup)
```

## Environment variables

|Variable|Required|Description|
|---|---|---|
|`VMANAGE_IP`|yes|vManage API host/IP|
|`VMANAGE_PORT`|no|vManage API port (default 443)|
|`VMANAGE_USER`|yes|vManage username (API + SSH)|
|`VMANAGE_PASSWORD`|yes|vManage password (API + SSH)|
|`EDGE_ADMIN_PASSWORD`|yes|Admin password on the cEdge device|
|`EDGE_SSH_PORT`|no|SSH port used for the cEdge jump hop (default 830)|
