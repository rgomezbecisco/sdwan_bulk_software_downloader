import argparse
import json
import os
import sys
from pygments import highlight, lexers, formatters
import yaml
from utils import tprint


def argument_parser():
    """Parse command line arguments"""

    parser = argparse.ArgumentParser(description="Resolve and act on a single cEdge hostname via its managing vManage node.")
    parser.add_argument("--hostname", "-s",type=str, required=True, help="cEdge hostname to target")
    parser.add_argument("--verbose", "-v", action="store_true", help="Display data in terminal")
    parser.add_argument("--showsupport", "-ss", action="store_true", help="Display supported templates")
    parser.add_argument("--sitetype", "-t", type=str, default="STORE_ST6", help="Site type suffix for variable files (e.g., STORE_ST6, STORE_ST5, etc)")
    parser.add_argument("--mhub", action="store_true", help="Append MHUB-specific verification show commands")

    return parser.parse_args()


def pretty_print_dict_as_json(data):
    """Print dictionary as colored JSON format."""

    formatted_json = json.dumps(data, indent=4)
    colorful_json = highlight(
        formatted_json, lexers.JsonLexer(), formatters.TerminalFormatter()
    )

    print(colorful_json)


def read_yaml_file(file_path):
    """Read a YAML file and return its contents as a dictionary"""

    if not os.path.exists(file_path):
        sample = os.path.join(os.path.dirname(file_path), f"sample_{os.path.basename(file_path)}")
        tprint(f"[!] Missing config file '{file_path}'. Copy the template first: cp {sample} {file_path}")
        sys.exit(1)

    with open(file_path, "r") as file:
        data = yaml.safe_load(file)

    return data


def find_target_manager(target_device):

    target_manager = None

    manager_system_ip = target_device.get("connectedVManages")[0]
    manager_map_file = os.path.join("constants", "MANAGER_MAP.yaml")
    manager_map = read_yaml_file(manager_map_file)

    for manager in manager_map["manager_map"]:
        if manager.get("manager_system_ip") == manager_system_ip:
            manager["edge_hostname"] = target_device.get("host-name")
            tprint(f"Target Manager ({manager.get('hostname')}) found!")
            return manager

    if not target_manager:
        tprint(f"[!] Target Manager NOT found")

    return target_manager


def find_software_for_model(device_model):
    """Look up the image mapped to a raw vManage `device-model` value."""

    software_map_file = os.path.join("constants", "SOFTWARE_MAP.yaml")
    software_map = read_yaml_file(software_map_file) or {}
    entry = software_map.get(device_model) or {}
    filename = entry.get("filename")

    if not filename:
        tprint(f"[!] No image mapped for device model '{device_model or 'unknown'}'")
        return None

    tprint(f"Model '{device_model}' maps to image '{filename}'")

    return {"filename": filename, "bytes": int(entry.get("bytes") or 0)}


def resolve_software_filename(target_manager):
    """Return the mapped image filename for the edge behind this manager entry."""

    software = find_software_for_model((target_manager or {}).get("edge_device_model", ""))

    return software.get("filename") if software else None


def confirm_iosxe_image_verification(output):
    text = str(output or "").lower()
    success_markers = [
        "digital signature successfully verified",
        "signature successfully verified",
    ]
    return any(marker in text for marker in success_markers)


def confirm_iosxe_image_installation(output):
    text = str(output or "").lower()
    success_markers = [
        "success: install_add",
        "image added. version",
        "found installed ios xe image",   # image already installed - treat as success
    ]
    success = any(marker in text for marker in success_markers)
    if not success:
        tprint(f"[!] Detailed output: {output}")
    return success
