import os
import sys
import time
import subprocess

if os.fork() > 0:
    sys.exit(0)

os.setsid()

if os.fork() > 0:
    sys.exit(0)

# Supervisor Loop (자동 복구 데몬)
log_file = "/home/jph/bybit_trade_collector/collector.log"

while True:
    with open(log_file, "a") as f:
        f.write(f"\n[{time.strftime('%Y-%m-%d %H:%M:%S')}] [SUPERVISOR] 수집 엔진 프로세스 기동\n")
        f.flush()
        process = subprocess.Popen(
            [
                "/home/jph/bybit_trade_collector/venv/bin/python",
                "-u",
                "/home/jph/bybit_trade_collector/collector.py"
            ],
            cwd="/home/jph/bybit_trade_collector",
            stdout=f,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL
        )
        process.wait()
        
    time.sleep(2)
