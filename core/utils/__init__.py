from datetime import datetime
from rich.console import Console

# Shared with bulk_download_software's Live table so threaded prints from
# verify/install workers are interleaved safely instead of being clobbered
# by the live redraw (never use bare print() for status/debug output).
console = Console()


def tprint(message):
    """Print with timestamp prefix in format [HH:MM:SS DDMMYYYY]"""
    timestamp = datetime.now().strftime("[%H:%M:%S %d%m%Y]")
    console.print(f"{timestamp} {message}")
