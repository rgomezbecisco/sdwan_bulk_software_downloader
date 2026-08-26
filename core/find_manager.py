from utils.api_manager import sdwanManager
from utils.additional_functions import (
    argument_parser,
    find_target_manager,
    pretty_print_dict_as_json
)
from utils import tprint

def run_find_manager(target_hostname, verbose=False, manager=None, target_edge=None):

    print("<< Find Manager - START >>")

    owns_manager = manager is None
    manager = manager or sdwanManager()
    target_edge = target_edge or manager.get_device_by_hostname(target_hostname)
    
    if target_edge:

        target_manager = find_target_manager(target_edge)

        if target_manager:

            target_manager["ssh_link"] = target_manager.get("ssh_link").replace("{{USERNAME}}", manager.credentials.get("user"))
            target_manager["edge_system_ip"] = target_edge.get("system-ip")
            target_manager["edge_device_model"] = target_edge.get("device-model")

        if verbose:
            pretty_print_dict_as_json(target_edge)
            pretty_print_dict_as_json(target_manager)

        if owns_manager:
            manager.close_session()
        print("<< Find Manager - END >>\n")

        return target_manager

    if owns_manager:
        manager.close_session()
    print("<< Find Manager - END >>\n")


if __name__ == "__main__":

    args = argument_parser()

    hostname = args.hostname
    verbose = args.verbose
    showsupport = args.showsupport
    sitetype = args.sitetype

    target_manager = run_find_manager(hostname, verbose=verbose)
    
    if not verbose and target_manager:
        pretty_print_dict_as_json(target_manager)