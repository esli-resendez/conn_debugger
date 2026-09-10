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

# Commands for rsmcli
RM_FRU = "show manager fru"
PSF_FRU = "show powershelf fru"
C13_FRU = "show powershelf c13 fru"
TELEMETRY = "show manager telemetry"
HUM = "show manager hsc -b 0 reading"
VOLT = "show manager voltage -b 0 status"
PMR = "show manager powermeter reading"
HLT_PWR = "show manager health --power"
C13_5 = "show powershelf c13 reading -c 5"
C13_READING_ALL = "show powershelf c13 reading" # all c13 modules
# TASK 9 list
T9_LIST = [RM_FRU, HUM, VOLT, PSF_FRU, C13_FRU, C13_READING_ALL]

# show versions
RM_VER = "show manager version"
SUP_VER = "show powershelf psu version"

# Powershelf commands
C1_STAT = [f"show powershelf c13 status -c {x+1}" for x in range(4)]
C13_STATUS_ALL = "show powershelf c13 status"
C13_READING = [f"show powershelf c13 reading -c {x+1}" for x in range(4)]

# List of commands to check


PULL_VIA_REDFISH = r"""curl -k -H "Content-Type: application/json" -X GET -u admin:admin "https://127.0.0.1/redfish/v1/Chassis/System/Sensors/?\$expand=." | jq -r '.Members[] | "\(.Name) \(.Reading)"'"""


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

def check_error_found(output):
    return "Failure" in output

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


def reset_c13_module(rm:SSHClientWrapper, logger:Logger):
    '''
        Reset the c13 module powering it OFF, read the i2c bus
        Power it back on
        Inputs: takes RM object
        Output: Returns FALSE if the bus did not recover
    '''
    print_w_ts("Resetting the C13 module see if that works")
    logger.log("RECOVERY", "Reseting C13")
    rm.query("set powershelf c13 off -c 5")
    rm.query("show powershelf c13 status")
    time.sleep(5)
    rm.query(RM_FRU)
    time.sleep(5)
    rm.query(PSF_FRU)
    time.sleep(5)
    rm.query(C13_FRU)
    time.sleep(5)
    rm.query("set powershelf c13 on -c 5")
    out = rm.query(RM_FRU)
    return not("Failure" in out)

def check_c13_status(rm:SSHClientWrapper):
    s = rm.query(C13_STATUS_ALL)
    print_w_ts(f"Status:\n{s}")
    fru = rm.query(C13_FRU)
    print_w_ts(f"FRU Content:\n{fru}")
    r = rm.query(C13_READING_ALL)
    print_w_ts(f"Reading\n{r}")
    print_w_ts("Completed checking PSHELF Status")
    return

def test_c13_modules(rm:SSHClientWrapper, logger:Logger):

    # Shut down all C13 modules
    for i in range(8):
        print_w_ts(f"Turn off {i+1}")
        rm.query(f"set powershelf c13 off -c {i+1}")
    
    check_c13_status(rm)
    c13_arrangement = [[1,2, 3,4], [5,6, 7,8]]

    for c13 in c13_arrangement:
        logger.log("TEST_MODULE", f"Turning ON {c13}")
        print_w_ts(f"Turning on modules {c13}")
        for module in c13:
            rm.query(f"set powershelf c13 on -c {module}")
        print_w_ts("Waiting for 2 min for C13 to Init...")
        time.sleep(120)
        check_c13_status(rm)
        logger.log("TEST_MODULE", f"Turning OFF modules {c13}")
        for module in c13:
            rm.query(f"set powershelf c13 off -c {module}")
        print_w_ts(f"Turn off modules {c13}")
        check_c13_status(rm)

    return


def find_psu_fw_mismatches(output_str: str, expected_version: str) -> list[int]:
    """
    Parse PSU firmware output and return a list of socket numbers whose
    ImageA Version does not match the expected version.

    """

    mismatches = []
    # Match each psu socket block (01:, 02:, ..., 12:)
    pattern = re.compile(
        r'^\s*(\d{2}):\s*\n(.*?)(?=^\s*\d{2}:|\Z)',
        re.MULTILINE | re.DOTALL
    )

    for match in pattern.finditer(output_str):
        socket_num = int(match.group(1))
        block = match.group(2)

        # Ignore PSU not present
        if "Status Description: PSU not present" in block:
            print_w_ts(f"Ignoring Block: {block} since is not present")
            continue
        # Extract ImageA Version
        version_match = re.search(
            r'ImageA Version:\s*([0-9A-Fa-f]+)',
            block
        )
        if version_match:
            actual_version = version_match.group(1)
            if actual_version.upper() != expected_version.upper():
                print_w_ts(f"Found {block} FW mismatch")
                mismatches.append(socket_num)

    return mismatches

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


def fan_policy_check(logger, node_port, node_pos, test_time=300, test_delay=0.5):

    rm = SSHClientWrapper(port=40050, password="$pl3nd1D", logger=logger)
    node = SSHClientWrapper(port=node_port, logger=logger)
    fans_manual = False
    elapsed = 0

    rm.connect()
    node.connect()

    try:
        while elapsed < test_time:
            try:
                print_w_ts("Collecting sensor from node")
                output = node.query(SENSOR_CMD)
                print_w_ts("ipmi command completed, checking overtemp and no readings")
                triggered = check_overtemp(sensor_output=output, threshold=85)
                if triggered:
                    print_w_ts('[+++] Overheating found')
                

            # System became unresponsive, power cycle it with a DC reset see how that goes
            except Exception as e:
                print(f"Exception created, details:\n{e}")
                #r = rm.query(f"set system off -i {node_pos}")
                #print(f"Trying to set system off node: {node_pos}:\n{r}")
                #time.sleep(10)
                #r = rm.query(f"set system on -i {node_pos}")
                #print(f"Trying to set system on to node {node_pos}:\n{r}")
                raise e
            elapsed = elapsed + 1
            time.sleep(test_delay)
        print_w_ts('[*] Completed task')


    except KeyboardInterrupt:
        print("\n[+] Interrupted power cycle task")
    finally:
        rm.close()
        node.close()
        logger.close()

    return


def fan_control_algorithm_check(logger, rm_ip, rm_port, node_ip, node_port, node_pos, stress_duration, stress_interval, node_pw='admin', node_user='admin'):

    rm = SSHClientWrapper(host=rm_ip, port=rm_port, password="$pl3nd1D", logger=logger)
    node = SSHClientWrapper(host=node_ip, port=node_port, logger=logger, username=node_user, password=node_pw)
    elapsed = 0
    power_check = 0

    rm.connect()
    node.connect()

    try:
        while elapsed < stress_duration:
            try:
                print_w_ts("Checking node")
                output = node.query(PULL_VIA_REDFISH)
                if output == "":
                    print_w_ts("[----] No output found, node likely shut down")
                    raise KeyboardInterrupt
                time.sleep(stress_interval)
                elapsed = elapsed + 1
                power_check = power_check + 1
                if power_check > 5:
                    print_w_ts("Checking SDR sensors for Power Level")
                    output = node.query(SENSOR_CMD)
                    power_check = 0

            # System became unresponsive, power cycle it with a DC reset see how that goes
            except Exception as e:
                logger.log("ERROR",f"Exception created, details:\n{e}\nTrying to DC reset the node now")
                r = rm.query(f"set system off -i {node_pos}")
                print(f"Trying to set system off node: {node_pos}:\n{r}")
                time.sleep(10)
                r = rm.query(f"set system on -i {node_pos}")
                print(f"Trying to set system on to node {node_pos}:\n{r}")
                raise e

            time.sleep(0.5)
        print_w_ts("[++] Completed the task without interruptions")

    except KeyboardInterrupt:
        print_w_ts("[+] Interrupted task... exiting now")
    finally:
        rm.close()
        node.close()
        logger.close()

    return 



def check_rscm(logger, rm_ip, rm_port, iterations, check_c13):

    rm = SSHClientWrapper(host=rm_ip, port=rm_port, password="$pl3nd1D", logger=logger)
    #node = SSHClientWrapper(host=node_ip, port=node_port, logger=logger, username=node_user, password=node_pw)
    elapsed = 0
    e_count = 0
    rm.connect()
    #node.connect()

    print_w_ts("Display version and FRU")
    v = rm.query(RM_VER)
    fru = rm.query(RM_FRU)
    ps = rm.query(SUP_VER)

    print_w_ts(f"System:\n{fru}\nVersion:\n{v}")

    # single read of fw ver and abort if not working
    if check_c13:
        test_c13_modules(rm, logger)
        return

    try:
        while elapsed < iterations:
            print_w_ts(f"Checking CYCLE: {elapsed+1}")
            for cmd in T9_LIST:
                try:
                    print_w_ts(f"Checking R-SCM Cli - {elapsed} cmd: {cmd}")
                    output = rm.query(cmd)
                    if check_error_found(output):
                        print_w_ts("[----] Error found in command, RM failure")
                        logger.log("error", f"Error found in command: {cmd}")
                        e_count = e_count+1
                        if e_count > 10:
                            logger.log("error", f"10 or more consecutive errors found")
                            print_w_ts("Consecutive error count reached, attempt a reset in c13 c5 module")
                            # Attempt to reset the C13 module 
                            if reset_c13_module(rm, logger):
                                print_w_ts("recovery successful")
                            else:
                                print_w_ts('Recovery unsuccessful, bus still having trouble')
                                raise KeyboardInterrupt
                    else:
                        time.sleep(0.2)
                        e_count = 0 # This is to count consecutive errors
                # Handle exception to close the log file
                except Exception as e:
                    logger.log("ERROR",f"Exception created, details:\n{e}\n")
                    raise e
                time.sleep(0.2)
            elapsed = elapsed+1
        print_w_ts("[++] Completed all commands in main task without interruptions")

    except KeyboardInterrupt:
        print_w_ts("[+] Interrupted task... exiting now")
    finally:
        rm.close()
        #node.close()
        logger.close()

    return


def rscm_psu_fw_upgrade(logger, rm_ip, rm_port, expected_ver):

    timeout = 100
    rm = SSHClientWrapper(host=rm_ip, port=rm_port, password="$pl3nd1D", logger=logger)
    rm.connect()
    psu_status = rm.query(SUP_VER)
    upgradeable_psu = find_psu_fw_mismatches(psu_status, expected_ver)
    
    # Upgrade one by one the upgradeable PSUs
    
    for psu in upgradeable_psu:
        # Report current version
        print_w_ts(rm.query(f"show powershelf psu version -s {psu}"))
        print_w_ts("f[++] Will try to upgrade now PSU: {psu}")
        r= rm.query(f"set powershelf psu update -s {psu} -f Flex_M1279207-001_P4020_V000F0E00.hex")
        # check if cmd went ok
        if "PSU firmware update started" in r:
            for i in range(timeout):
                status = rm.query(f"show powershelf psu update -s {psu}")
                print_w_ts(f"PSU update status:\n{status}\n")
                if "Update completed" in status:
                    print_w_ts("Done, moving to next")
                    break
                else:
                    print_w_ts("Waiting 60 sec")
                    time.sleep(60)

    print_w_ts("All PSU upgraded")
    psu_status = rm.query(SUP_VER)
    print_w_ts(psu_status)

    return




# =========================
# MAIN
# =========================
def main():
    parser = argparse.ArgumentParser(description="SSH Task Runner")
    parser.add_argument("-t", type=int, choices=[1, 2, 3, 4, 5, 6, 7, 8, 9, 10], required=True, help="Task number to be Executed")
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
        fan_policy_check(logger, port, node, delay, delay_interval)
    elif task_id == 8:
        fan_control_algorithm_check(logger, rm_ip, rm_port, node_ip, port, node, delay, delay_interval)
    elif task_id==9:
        check_rscm(logger, rm_ip, rm_port, delay_interval, ac_cycle)
    elif task_id==10:
        rscm_psu_fw_upgrade(logger, rm_ip, rm_port, node)

if __name__ == "__main__":
    main()