from datetime import datetime


def tprint(message):
    """Print with timestamp prefix in format [HH:MM:SS DDMMYYYY]"""
    timestamp = datetime.now().strftime("[%H:%M:%S %d%m%Y]")
    print(f"{timestamp} {message}")
