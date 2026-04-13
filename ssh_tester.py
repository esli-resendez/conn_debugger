#!/usr/bin/env python3

import argparse
import paramiko
import sys
import time
import socket
import re
from datetime import datetime
from logger_util import Logger
from ssh_client_mgr import SSHClientWrapper

# =========================
# TASK DEFINITIONS
# =========================
TASK_1 = [
    "ipmitool sdr"
]

TASK_2 = [
    "ipmitool raw 0x32 0x90 0x01", # manual ctrl
    "ipmitool raw 0x32 0x91 0x01 0x10", # low speed
    "ipmitool raw 0x32 0x90 0x01", # manual ctrl 
    "ipmitool raw 0x32 0x91 0x01 0x5F" # high speed
]

# =========================
# LOGGER
# =========================


# =========================
# SSH CLIENT CLASS
# =========================


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

def power_cycle_node_wait(node:SSHClientWrapper, rm:SSHClientWrapper, logger:Logger, node_pos, node_port, ac_reboot:bool):

    if ac_reboot:
        PWR_OFF = f"set manager port off -i {node_pos}"
        PWR_ON = f"set manager port on -i {node_pos}"
    else:
        PWR_OFF = f"set system off -i {node_pos}"
        PWR_ON = f"set system on -i {node_pos}"

    # Commands to send to RM
    node.close()
    r = rm.query()
    print(f"Sending System off node: {node_pos}:\n{r}")
    time.sleep(10)
    r = rm.query(f"set system on -i {node_pos}")
    print(f"Send system on to node {node_pos}:\n{r}")
    print(f"Wait 300s for node {node_pos} to come back on")
    time.sleep(300)
    print(f"Wait done, checking SSH")
    wait_for_ssh(port=node_port, logger=logger)
    node.connect()
    return


def check_no_reading(sensor_output):
    return"no reading" in sensor_output

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
def power_cycle_task(logger, node_port, node_pos, ac_reboot):

    rm = SSHClientWrapper(port=40050, password="$pl3nd1D", logger=logger)
    node = SSHClientWrapper(port=node_port, logger=logger)
    count = 0

    rm.connect()
    node.connect()

    SENSOR_CMD = "ipmitool sdr"

    try:
        while count < 20:
            print(f"Starting iteration: {count}")
            output = node.query(SENSOR_CMD)
            triggered = check_no_reading(output)
            if triggered:
                # No Reading detected, count one
                print("[!] Detected a no reading:")
                logger.log("ERROR", f"Overtemp Detected at iteration {count}")
            
            count = count+1
            power_cycle_node_wait(node, rm, logger, node_pos, ac_reboot)


    except KeyboardInterrupt:
        print("\n[+] Interrupted power cycle task")
    finally:
        rm.close()
        node.close()
        logger.close()



def detect_overheat_task(logger, node_port, node_pos, ac_reboot):

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
                power_cycle_node_wait(node, rm, logger, node_pos, ac_reboot)

            time.sleep(2)

    except KeyboardInterrupt:
        print("\n[+] Interrupted power cycle task")
    finally:
        rm.close()
        node.close()
        logger.close()




# Cause an overheat and then force the error
def cause_overheat_task(logger, node_port, node_pos):

    rm = SSHClientWrapper(port=40050, password="$pl3nd1D", logger=logger)
    node = SSHClientWrapper(port=node_port, logger=logger)

    sensors_overheating = set()

    rm.connect()
    node.connect()

    SENSOR_CMD = "ipmitool sdr"

    # Forcing fans to speed slow:
    r = node.query("ipmitool raw 0x32 0x90 0x01") # enable manual ctrl
    r = node.query("ipmitool raw 0x32 0x91 0x01 0x10") # low speed")

    try:
        while True:
            try:
                output = node.query(SENSOR_CMD)
                triggered = check_overtemp(output)

                if triggered:
                    print("[!] Overtemperature detected:", triggered)
                    logger.log("ERROR", f"Overtemp Detected at sensor {triggered}")
                print(f"{datetime.now().strftime("%y%m%d%H%M%S")}\tipmitool cmd completed")
            # System became unresponsive, power cycle it with a DC reset see how that goes
            except Exception as e:
                print(f"Exception created, details:\n{e}")
                r = rm.query(f"set system off -i {node_pos}")
                print(f"Trying to set system off node: {node_pos}:\n{r}")
                time.sleep(10)
                r = rm.query(f"set system on -i {node_pos}")
                print(f"Trying to set system on to node {node_pos}:\n{r}")
                raise e

            time.sleep(2)

    except KeyboardInterrupt:
        print("\n[+] Interrupted power cycle task")
    finally:
        rm.close()
        node.close()
        logger.close()

    return





# =========================
# MAIN
# =========================
def main():
    parser = argparse.ArgumentParser(description="SSH Task Runner")
    parser.add_argument("-t", type=int, choices=[1, 2, 3, 4, 5], required=True, help="Task number")
    parser.add_argument("-i", type=str, default="127.0.0.1")
    parser.add_argument("-p", type=int, default=40004)
    parser.add_argument("-n", type=str, default="4", help="Node number")
    parser.add_argument("-d", type=float, default=1.0)
    parser.add_argument("-a", action="store_true")

    args = parser.parse_args()

    task_id = args.t
    port = args.p
    delay = args.d
    node = args.n
    ac_cycle = args.a

    logger = Logger(task_id, node)

    if task_id == 1:
        execute_task(TASK_1, logger, port, delay)
    elif task_id == 2:
        execute_task(TASK_2, logger, port, delay)
    elif task_id == 3:
        detect_overheat_task(logger, port, node, ac_cycle)
    elif task_id == 4:
        cause_overheat_task(logger, port, node)
    elif task_id == 5:
        power_cycle_task(logger, port, node, ac_cycle)

if __name__ == "__main__":
    main()