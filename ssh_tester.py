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

SENSOR_CMD = "ipmitool sdr"
FAN_MANUAL = "ipmitool raw 0x32 0x90 0x01"
FAN_AUTO = "ipmitool raw 0x32 0x90 0x00"
FAN_LOW_SPEED = "ipmitool raw 0x32 0x91 0x01 0x10"
FAN_HIGH_SPEED = "ipmitool raw 0x32 0x91 0x01 0x5F"


def print_w_ts(text):
    print(f"{datetime.now().strftime("%y/%m/%d %H:%M:%S")}\t{text}")
    return

def pull_journal_log(node:SSHClientWrapper, node_pos):

    journal_log = Logger("journal_log", node_pos)
    journal_txt = node.query("journalctl -b 0 --output=short-iso-precise --no-pager", 300, False)
    journal_log.log("journalctl", journal_txt)
    journal_log.close()

    return


def adjust_node_time(node:SSHClientWrapper):
    print_w_ts('adjusting hostOS time')
    r = node.query(f'date -s "{datetime.now().strftime("%y/%m/%d %H:%M:%S")}"')
    r = node.query('hwclock --systohc')
    return

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

def power_cycle_node_wait(node:SSHClientWrapper, rm:SSHClientWrapper, logger:Logger, node_pos, node_port, ac_reboot:bool) -> SSHClientWrapper:

    if ac_reboot:
        pwr_off = f"set manager port off -i {node_pos}"
        pwr_on = f"set manager port on -i {node_pos}"
        d_time = 400
    else:
        pwr_off = f"set system off -i {node_pos}"
        pwr_on = f"set system on -i {node_pos}"
        d_time = 300

    # Commands to send to RM
    node.close()
    system_rdy = False
    ready_count = 0
    
    print_w_ts(f"Powering off node: {node_pos}:")
    if ac_reboot:
        print_w_ts("Using AC Cycle via RM")
    r = rm.query(pwr_off)
    print(r)
    time.sleep(10)
    print_w_ts(f"Send system on to node {node_pos}:")
    r = rm.query(pwr_on)
    print_w_ts(f"Wait plain {d_time}s for node {node_pos} to come back on")
    time.sleep(d_time)
    print(f"Wait done, checking SSH")
    for _ in range(3):
        if (wait_for_ssh(port=node_port, timeout=300, logger=logger)):
            node = SSHClientWrapper(port=node_port, logger=logger)
            node.connect()
            return node

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


def check_for_events(logger, node_port, node_pos, delay):
    rm = SSHClientWrapper(port=40050, password="$pl3nd1D", logger=logger)
    node = SSHClientWrapper(port=node_port, logger=logger)
    count = 0
    rm.connect()
    node.connect()
    SENSOR_CMD = "ipmitool sdr"
    print_w_ts("Setting fans to high speed")
    output = node.query(FAN_MANUAL)
    output = node.query(FAN_HIGH_SPEED)
    # put system into current time
    adjust_node_time(node) 
    # Main task waiting forever
    print_w_ts("Fan ctrl done. Start the collection of data")
    try:
        while True:
            output = node.query(SENSOR_CMD)
            uptime = node.query("uptime")
            if check_no_reading(output):
                print_w_ts("no reading detected in output")
                raise KeyboardInterrupt
                node = power_cycle_node_wait(node, rm, logger, node_pos, node_port, ac_reboot=False)
                print_w_ts("Cycle done, checking again")
                uptime = node.query("uptime")
                print_w_ts(f"Uptime is: {uptime}")
                print_w_ts("Setting fans to high speed after reboot")
                output = node.query(FAN_MANUAL)
                output = node.query(FAN_HIGH_SPEED)
            else:
                print_w_ts(f"Output completed, waiting for: {delay}")
                time.sleep(delay)
                print_w_ts("Wait done, checking again")
            

    except KeyboardInterrupt:
        print_w_ts(f"\n[+] Interrupted Task#6 for node {node_pos}")
    finally:
        rm.close()
        node.close()
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
            node = power_cycle_node_wait(node, rm, logger, node_pos, node_port, ac_reboot)


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
                # Commands to send to RM to reboot system
                power_cycle_node_wait(node, rm, logger, node_pos, node_port, ac_reboot)

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
    print(f"{datetime.now().strftime("%y/%m/%d %H:%M:%S")}\tSlowing down fans")
    r = node.query("ipmitool raw 0x32 0x90 0x01") # enable manual ctrl
    r = node.query("ipmitool raw 0x32 0x91 0x01 0x10") # low speed")

    try:
        while True:
            try:
                output = node.query(SENSOR_CMD)
                triggered = check_overtemp(sensor_output=output, threshold=120)
                no_reading = check_no_reading(output)
                
                if no_reading:
                    print(f"{datetime.now().strftime("%y/%m/%d %H:%M:%S")}\tNo reading was detected, waiting for node to cool down")
                    time.sleep(60)
                    continue
                # system is stable, set fans to low mode
                r = node.query(FAN_MANUAL) # enable manual ctrl
                r = node.query(FAN_LOW_SPEED) # low speed")

                if triggered:
                    print("[!] Overtemperature detected:", triggered)
                    logger.log("ERROR", f"Overtemp Detected at sensor {triggered}")
                    # shut down the node now via AC cycle
                    node = power_cycle_node_wait(node, rm, logger, node_pos, node_port, ac_reboot=True)
                    print(f"{datetime.now().strftime("%y/%m/%d %H:%M:%S")}\tSystem is back on")
                print(f"{datetime.now().strftime("%y/%m/%d %H:%M:%S")}\tipmitool cmd completed")
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


def fan_policy_check(logger, node_port, node_pos):

    rm = SSHClientWrapper(port=40050, password="$pl3nd1D", logger=logger)
    node = SSHClientWrapper(port=node_port, logger=logger)
    fans_manual = False

    rm.connect()
    node.connect()

    try:
        while True:
            try:
                print_w_ts("Checking node")
                output = node.query(SENSOR_CMD)
                print_w_ts("ipmi command completed, checking overtemp and no reading")
                triggered = check_overtemp(sensor_output=output, threshold=85)
                no_reading = check_no_reading(output)
                if no_reading:
                    print_w_ts("No reading was detected, waiting for node to cool down")
                    r = node.query(FAN_AUTO) # enable Auto mode
                    time.sleep(60)
                    continue
                # system is overheating, return to control
                if triggered:
                    print_w_ts(f"[!] Overtemperature detected: {triggered}")
                    logger.log("ERROR", f"Overtemp Detected at sensor {triggered}")
                    # shut down the node now via AC cycle
                    if fans_manual:
                        r = node.query(FAN_AUTO) # low speed")
                        print_w_ts("fans now back to Auto mode...")
                        fans_manual = False
                else:
                    if not fans_manual:
                        print_w_ts("No overtemp, setting fans to slow speed")
                        r = node.query(FAN_MANUAL)
                        r = node.query(FAN_LOW_SPEED)
                        fans_manual = True
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


def fan_control_algorithm_check(logger, rm_ip, rm_port, node_ip, node_port, node_pos, stress_duration, stress_interval):

    rm = SSHClientWrapper(host=rm_ip, port=rm_port, password="$pl3nd1D", logger=logger)
    node = SSHClientWrapper(host=node_ip, port=node_port, logger=logger)
    elapsed = 0

    rm.connect()
    node.connect()

    try:
        while elapsed < stress_duration:
            try:
                print_w_ts("Checking node")
                output = node.query(SENSOR_CMD)
                print_w_ts("ipmi command completed, checking overtemp and no reading")
                triggered = check_overtemp(sensor_output=output, threshold=95)
                no_reading = check_no_reading(output)
                if no_reading:
                    print_w_ts("No reading was detected, waiting for node to cool down")
                    r = node.query(FAN_AUTO) # enable Auto mode
                    time.sleep(60)
                    continue
                # One or more sensors reporting overheating, exit loop
                if triggered:
                    print_w_ts(f"[!] Overtemperature detected: {triggered}")
                    logger.log("ERROR", f"Overtemp Detected at sensor {triggered}")
                    raise KeyboardInterrupt
                time.sleep(stress_interval)
                elapsed = elapsed + 1
            # System became unresponsive, power cycle it with a DC reset see how that goes
            except Exception as e:
                logger.log("ERROR",f"Exception created, details:\n{e}\nTrying to DC reset the node now")
                r = rm.query(f"set system off -i {node_pos}")
                print(f"Trying to set system off node: {node_pos}:\n{r}")
                time.sleep(10)
                r = rm.query(f"set system on -i {node_pos}")
                print(f"Trying to set system on to node {node_pos}:\n{r}")
                raise e

            time.sleep(2)

    except KeyboardInterrupt:
        print_w_ts("\n[+] Interrupted task")
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
    parser.add_argument("-t", type=int, choices=[1, 2, 3, 4, 5, 6, 7, 8], required=True, help="Task number to be Executed")
    parser.add_argument("-rmip", type=str, default="127.0.0.1", help="Rack Manager IP")
    parser.add_argument("-rmp", type=int, default=22, help="Rack manager SSH Port (default 22)")
    parser.add_argument("-nip", type=str, default="127.0.0.1", help="Host Node IP")
    parser.add_argument("-npo", type=int, default=22, help="Host Node SSH Port (default 22)")
    parser.add_argument("-n", type=str, default="4", help="Node position in a Rack Manager")
    parser.add_argument("-d", type=float, default=1.0, help="Delay Time")
    parser.add_argument("-dst", type=float, default=1.0, help="Delay stress interval Time")
    parser.add_argument("-a", action="store_true")

    args = parser.parse_args()

    task_id = args.t
    port = args.npo # node port
    delay = args.d
    node = args.n
    ac_cycle = args.a
    rm_ip = args.rmip # rack manager IP
    rm_port = args.rmp # rack manager's SSH port
    node_ip = args.nip # host node IP address
    delay_interval = args.dst

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
    elif task_id == 6:
        check_for_events(logger, port, node, delay)
    elif task_id == 7:
        fan_policy_check(logger, port, node)
    elif task_id == 8:
        fan_control_algorithm_check(logger, rm_ip, rm_port, node_ip, port, node, delay, delay_interval)

if __name__ == "__main__":
    main()