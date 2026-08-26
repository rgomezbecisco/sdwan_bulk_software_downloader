from utils.additional_functions import (
    argument_parser,
    pretty_print_dict_as_json,
    confirm_iosxe_image_installation,
    resolve_software_filename,
)
from utils.ssh_manager import sshManager
from find_manager import run_find_manager
from utils import tprint

def run_install_iosxe_image(target_manager, filename=None, custom_timeout=120, verbose=False, sshclient=None, reuse_session=False):

    print("<< Install IOS XE image - START >>")

    filename = filename or resolve_software_filename(target_manager)

    if not filename:
        tprint("IOS XE image installation FAILED! No image mapped for this device model")
        print("<< Install IOS XE image - END >>\n")
        return {"success": False, "output": "no image mapped for device model"}

    install_command = f"request software install-image {filename}"
        
    sshclient = sshclient or sshManager(target_manager)
    result = sshclient.send_command_on_edge_cli(
        install_command,
        custom_timeout=custom_timeout,
        reuse_session=reuse_session,
    )
    install_ok = result.get("success") and confirm_iosxe_image_installation(result.get("output"))
    if install_ok:
        tprint(f"IOS XE image installation successful!")

    else:
        tprint(f"IOS XE image installation FAILED!")

    # Propagate semantic validation so callers can make correct status decisions.
    result["success"] = bool(install_ok)

    if verbose:
        pretty_print_dict_as_json(result)
  
    print("<< Install IOS XE image - END >>\n")

    return result

if __name__ == "__main__":

    args = argument_parser()
    hostname = args.hostname
    verbose = args.verbose
    showsupport = args.showsupport
    sitetype = args.sitetype

    target_manager = run_find_manager(hostname, verbose=verbose)
    
    result = run_install_iosxe_image(target_manager, verbose=verbose)
    print(result.get("output"))
