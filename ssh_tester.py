#!/usr/bin/env python3

import argparse
import paramiko
import sys
import time
import socket
import re
from datetime import datetime

# =========================
# TASK DEFINITIONS
# =========================
TASK_1 = [
    "ipmitool sdr"
]

TASK_2 = [
    "ipmitool raw 0x32 0x90 0x01",
    "ipmitool raw 0x32 0x91 0x01 0x10",
    "ipmitool raw 0x32 0x90 0x01",
    "ipmitool raw 0x32 0x91 0x01 0x5F"
]

# =========================
# LOGGER
# =========================
class Logger:
    def __init__(self, task_id):
        timestamp = datetime.now().strftime("%y%m%d%H%M%S")
        self.filename = f"ssh_conn_task{task_id}_{timestamp}.log"
        self.file = open(self.filename, "a", encoding="utf-8")

    def log(self, command, output):
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.file.write(f"[{ts}] COMMAND: {command}\n")
        self.file.write(f"[{ts}] OUTPUT:\n{output}\n")
        self.file.write("=" * 60 + "\n")
        self.file.flush()

    def close(self):
        if not self.file.closed:
            self.file.close()

# =========================
# SSH CLIENT CLASS
# =========================
class SSHClientWrapper:
    def __init__(self, host="localhost", port=22, username="root", password="msft", logger=None):
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.logger = logger
        self.client = paramiko.SSHClient()
        self.client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    def connect(self):
        try:
            self.client.connect(
                hostname=self.host,
                port=self.port,
                username=self.username,
                password=self.password,
                timeout=10,
                banner_timeout=10,
                auth_timeout=10
            )
            print(f"[+] Connected to {self.host}:{self.port}")
        except (paramiko.SSHException, socket.error, socket.timeout) as e:
            if self.logger:
                self.logger.log("CONNECTION", f"ERROR: {str(e)}")
                self.logger.close()
            print(f"[!] Connection failed: {e}")
            sys.exit(1)

    def write(self, command):
        try:
            self.client.exec_command(command)
        except Exception as e:
            if self.logger:
                self.logger.log(command, f"WRITE ERROR: {str(e)}")

    def read(self, stdout, stderr):
        output = stdout.read().decode(errors="ignore")
        error = stderr.read().decode(errors="ignore")
        return output + error

    def query(self, command, timeout=60):
        try:
            stdin, stdout, stderr = self.client.exec_command(command, timeout=timeout)
            result = self.read(stdout, stderr)
            if self.logger:
                self.logger.log(command, result)
            return result
        except Exception as e:
            if self.logger:
                self.logger.log(command, f"QUERY ERROR: {str(e)}")
            return ""

    def close(self):
        self.client.close()

def wait_for_ssh(host="127.0.0.1", port=22, timeout=300, interval=2, logger=None):
    """
    Wait until SSH port is reachable.
    timeout: total seconds to wait
    interval: seconds between retries
    """
    start = time.time()

    while True:
        try:
            with socket.create_connection((host, port), timeout=5):
                if logger:
                    logger.log("WAIT_FOR_SSH", f"{host}:{port} is reachable")
                return True
        except (socket.timeout, socket.error):
            if time.time() - start > timeout:
                if logger:
                    logger.log("WAIT_FOR_SSH", f"Timeout after {timeout}s")
                return False
            time.sleep(interval)

# =========================
# REGEX FOR TMP > 80
# =========================
TMP_REGEX = re.compile(
    r"(?P<name>\S+_TMP\S*)\s*\|\s*(?P<value>\d+(\.\d+)?)\s*degrees C",
    re.IGNORECASE
)

def check_overtemp(sensor_output, threshold=85.0):
    matches = TMP_REGEX.finditer(sensor_output)
    triggered = []
    for m in matches:
        temp = float(m.group("value"))
        if temp > threshold:
            triggered.append((m.group("name"), temp))
    return triggered

# =========================
# TASK EXECUTION
# =========================
def execute_task(commands, logger, port, delay):
    client = SSHClientWrapper(port=port, logger=logger)
    client.connect()

    try:
        while True:
            for cmd in commands:
                client.query(cmd)
                time.sleep(delay)
    except KeyboardInterrupt:
        print("\n[+] Interrupted by user")
    finally:
        client.close()
        logger.close()

# =========================
# POWER CYCLE TASK
# =========================
def power_cycle_task(logger, node_port, node_pos):

    rm = SSHClientWrapper(port=40050, password="$pl3nd1D", logger=logger)
    node = SSHClientWrapper(port=node_port, logger=logger)

    rm.connect()
    node.connect()

    SENSOR_CMD = "ipmitool sdr"

    try:
        while True:
            output = node.query(SENSOR_CMD)
            triggered = check_overtemp(output)

            if triggered:
                print("[!] Overtemperature detected:", triggered)
                logger.log("ERROR", "Overtemp Detected")
                # Commands to send to RM
                node.close()
                r = rm.query(f"set system off -i {node_pos}")
                print(f"Sending System off node: {node_pos}:\n{r}")
                time.sleep(10)
                r = rm.query(f"set system on -i {node_pos}")
                print(f"Send system on to node {node_pos}:\n{r}")
                print(f"Wait 300s for node {node_pos} to come back on")
                wait_for_ssh(port=node_port, logger=logger)
                node.connect()

            time.sleep(2)

    except KeyboardInterrupt:
        print("\n[+] Interrupted power cycle task")
    finally:
        rm.close()
        node.close()
        logger.close()

# =========================
# MAIN
# =========================
def main():
    parser = argparse.ArgumentParser(description="SSH Task Runner")
    parser.add_argument("-t", type=int, choices=[1, 2, 3], required=True, help="Task number")
    parser.add_argument("-a", type=str, default="127.0.0.1")
    parser.add_argument("-p", type=int, default=40004)
    parser.add_argument("-n", type=str, default="4", help="Node number")
    parser.add_argument("-d", type=float, default=1.0)

    args = parser.parse_args()

    task_id = args.t
    port = args.p
    delay = args.d
    node = args.n

    logger = Logger(task_id)

    if task_id == 1:
        execute_task(TASK_1, logger, port, delay)
    elif task_id == 2:
        execute_task(TASK_2, logger, port, delay)
    elif task_id == 3:
        power_cycle_task(logger, port, node)

if __name__ == "__main__":
    main()