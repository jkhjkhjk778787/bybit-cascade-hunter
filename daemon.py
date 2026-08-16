import subprocess
import sys
import os

log_file = "/home/jph/bybit_trade_collector/collector.log"
with open(log_file, "a") as f:
    process = subprocess.Popen(
        [
            "/home/jph/bybit_trade_collector/venv/bin/python",
            "-u",
            "/home/jph/bybit_trade_collector/collector.py",
            "--symbol", "AKEUSDT"
        ],
        cwd="/home/jph/bybit_trade_collector",
        stdout=f,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        start_new_session=True
    )
print(f"Daemon started with PID: {process.pid}")
