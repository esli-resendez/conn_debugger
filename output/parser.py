#!/usr/bin/env python3
"""
parse_sensor_log.py
Parses cyclically-repeating ipmitool sdr + uptime log entries into a wide-format CSV.

Each row = one sensor (or 'uptime').
Each column (after 'sensor') = one timestamp, with the sensor value at that time.

Usage:
    python parse_sensor_log.py <logfile> [-o output.csv]

Example:
    python parse_sensor_log.py system.log
    python parse_sensor_log.py system.log -o report.csv
"""

import re
import csv
import sys
import argparse
from collections import defaultdict

# ---------------------------------------------------------------------------
# Patterns
# ---------------------------------------------------------------------------
CMD_LINE   = re.compile(r'\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\] COMMAND: (.+)')
SDR_ROW    = re.compile(r'^([A-Za-z0-9_ ]+?)\s+\|\s+(.+?)\s+\|\s+\w+\s*$')
UPTIME_RE  = re.compile(r'up\s+(.*?),\s+\d+\s+user')

def parse_sdr_value(raw: str) -> str:
    """Strip the unit string and return just the numeric (or hex) value."""
    # e.g. "28 percent" -> "28", "12.24 Volts" -> "12.24", "0x00" -> "0x00"
    parts = raw.split()
    return parts[0] if parts else raw

def parse_uptime_value(line: str) -> str:
    """Extract the 'up X min/hours/days' portion."""
    m = UPTIME_RE.search(line)
    return m.group(1).strip() if m else line.strip()

# ---------------------------------------------------------------------------
# Main parser
# ---------------------------------------------------------------------------
def parse_log(path: str):
    """
    Returns:
        timestamps : list of str   – ordered list of all timestamps seen
        data       : dict[sensor -> dict[timestamp -> value]]
    """
    timestamps = []          # preserves order
    seen_ts    = set()
    data       = defaultdict(dict)   # sensor -> {ts: value}

    current_ts   = None
    current_cmd  = None
    in_output    = False

    with open(path, 'r', errors='replace') as fh:
        for raw_line in fh:
            line = raw_line.rstrip('\n')

            # ---- detect COMMAND lines ----------------------------------------
            m = CMD_LINE.match(line)
            if m:
                current_ts  = m.group(1)
                current_cmd = m.group(2).strip()
                in_output   = False

                if current_ts not in seen_ts:
                    timestamps.append(current_ts)
                    seen_ts.add(current_ts)
                continue

            # ---- detect OUTPUT: marker ----------------------------------------
            if re.match(r'\[\d{4}-\d{2}-\d{2}.*\] OUTPUT:', line):
                in_output = True
                continue

            # ---- separator lines (===...) ------------------------------------
            if line.startswith('='):
                in_output = False
                continue

            # ---- parse output lines ------------------------------------------
            if in_output and current_ts and current_cmd:

                if 'ipmitool sdr' in current_cmd:
                    m2 = SDR_ROW.match(line)
                    if m2:
                        sensor = m2.group(1).strip()
                        value  = parse_sdr_value(m2.group(2).strip())
                        data[sensor][current_ts] = value

                elif current_cmd == 'uptime':
                    value = parse_uptime_value(line)
                    if value:
                        data['uptime'][current_ts] = value

    return timestamps, data

# ---------------------------------------------------------------------------
# CSV writer
# ---------------------------------------------------------------------------
def write_csv(timestamps, data, out_path: str):
    sensors = sorted(data.keys())

    with open(out_path, 'w', newline='') as fh:
        writer = csv.writer(fh)

        # Header: sensor, ts1, ts2, ...
        writer.writerow(['sensor'] + timestamps)

        for sensor in sensors:
            row = [sensor] + [data[sensor].get(ts, '') for ts in timestamps]
            writer.writerow(row)

    print(f"Written {len(sensors)} sensors × {len(timestamps)} timestamps → {out_path}")

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description='Parse IPMI sensor log to CSV.')
    ap.add_argument('logfile', help='Path to the log file')
    ap.add_argument('-o', '--output', default='sensor_log.csv',
                    help='Output CSV path (default: sensor_log.csv)')
    args = ap.parse_args()

    print(f"Parsing {args.logfile} ...")
    timestamps, data = parse_log(args.logfile)

    if not timestamps:
        print("ERROR: No timestamped COMMAND blocks found. Check the log format.", file=sys.stderr)
        sys.exit(1)

    print(f"Found {len(timestamps)} timestamp(s), {len(data)} sensor(s).")
    write_csv(timestamps, data, args.output)

if __name__ == '__main__':
    main()