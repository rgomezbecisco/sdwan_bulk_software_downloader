import os
import urllib3
import sys
from catalystwan.session import create_manager_session
from utils import tprint

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

class sdwanManager:
    """Manages vManage API connections and device retrieval."""

    def __init__(self, config_devices_based=False):
        """Initialize API manager with vManage credentials."""
        
        self.config_based = config_devices_based
        self.credentials = self.get_manager_creds()
        self.session = self.get_manager_session()
        initial_context = "device config details" if self.config_based else "device details"
        self.devices = self.get_devices(context_label=initial_context)

    def get_manager_creds(self):
        """Retrieve vManage credentials from environment variables."""

        if os.getenv("VMANAGE_IP") and os.getenv("VMANAGE_USER") and os.getenv("VMANAGE_PASSWORD"):
            tprint("Manager credentials loaded")
           
            return {
                "ip": os.getenv("VMANAGE_IP"),
                "port": int(os.getenv("VMANAGE_PORT", 443)),
                "user": os.getenv("VMANAGE_USER"),
                "pwd": os.getenv("VMANAGE_PASSWORD")
            }
    
        else:
            tprint("Environment variables not loaded. Locally: source credentials.sh. In CI: set VMANAGE_IP, VMANAGE_USER, VMANAGE_PASSWORD as GitLab CI variables")
            sys.exit(1)

    def get_manager_session(self):
        """Create and return vManage API session."""
        
        device_ip = self.credentials.get("ip")
        device_port = int(self.credentials.get("port"))
        device_user = self.credentials.get("user")
        device_pwd = self.credentials.get("pwd")

        url = f"https://{device_ip}:{device_port}"

        session = create_manager_session(
            url=url,
            username=device_user,
            password=device_pwd,
        )

        tprint(f"Connected to SDWAN Manager API at {device_ip}:{device_port}")

        return session

    def get_devices(self,config_base_override=False, context_label=None):
        """Retrieve all devices from vManage inventory."""

        try:

            with self.session as session:
                endpoint = "dataservice/device"

                if self.config_based or config_base_override:
                    endpoint = "dataservice/system/device/vedges"

                if context_label:
                    tprint(f"Retrieving {context_label} from Manager")
                elif endpoint == "dataservice/system/device/vedges":
                    tprint("Retrieving device config details from Manager")
                else:
                    tprint("Retrieving device details from Manager")

                response = session.get(endpoint)
                devices = response.json()["data"]

                if len(devices) > 0:
                    return devices
                
                else:
                    tprint("No devices found in Manager")
                    return None

        except Exception as e:
            tprint(f"Error: {e}")
            return None
        
    def get_device_by_hostname(self, hostname, custom_devices=None):
        """Retrieve a device by its host name from vManage inventory."""

        try:

            devices = custom_devices if custom_devices else self.devices

            for device in devices:
                if device.get("host-name") == hostname:
                    tprint(f"Device '{hostname}' found in WAN edge inventory")
                    return device

            tprint(f"Device '{hostname}' not found in WAN edge inventory")

        except Exception as e:
            print(f"Error: {e}")
            return None
        
    def get_device_values(self, device):

        """Retrieve device values needed for template population."""

        try:

            endpoint = "/dataservice/template/device/config/input/"

            payload = {
                "templateId":device.get("templateId"),
                "deviceIds":[device.get("uuid")]}

            response = self.session.post(url=endpoint, json=payload)
            device_values = response.json()["data"]

            tprint(f"Retrieved device values for '{device.get('host-name')}'")

            return device_values[0]

        except Exception as e:
            tprint(f"Error: {e}")
            return None
        
    def get_serial_number(self, device):
        """Retrieve the serial number of a device by its system IP."""

        try:
            # https://18.235.222.158/dataservice/device?deviceId=10.255.213.25
            endpoint = f"/dataservice/device?deviceId={device.get('system-ip')}"

            response = self.session.get(endpoint)
            device_info = response.json()["data"][0]

            uuid = device_info.get("uuid")
            serial_number = uuid.split("-")[-1]

            return serial_number

        except Exception as e:
            tprint(f"Error: {e}")
            return None

    def get_software_images(self):
        """Return the set of image filenames staged in the Manager software repository."""

        try:
            response = self.session.get("dataservice/device/action/software")
            records = response.json().get("data", [])

            filenames = set()
            for record in records:
                # availableFiles arrives as "[a.bin]" or "a.bin, b.bin" depending on endpoint
                raw = str(record.get("availableFiles") or "").strip("[]")
                for name in raw.split(","):
                    name = name.strip()
                    if name:
                        filenames.add(name)

            tprint(f"Retrieved {len(filenames)} image(s) from the software repository")

            return filenames

        except Exception as e:
            tprint(f"Error retrieving software repository: {e}")
            return set()

    def close_session(self):
        """Close the Manager API session."""
        
        try:
            self.session.close()
            tprint("Manager API session closed")

        except Exception as e:
            tprint(f"Error closing session: {e}")