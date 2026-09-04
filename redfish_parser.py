#!/usr/bin/env python3
"""
parse_sensors.py

Parses a log file containing repeated [timestamp] COMMAND / OUTPUT blocks,
where each OUTPUT block lists sensor readings as:

    <sensor name...> <float value | null>

Builds a table with one row per timestamp (each OUTPUT block) and one
column per sensor, then writes it out as a CSV.

Usage:
    python3 parse_sensors.py input.log output.csv
"""

import csv
import re
import sys

# ---------------------------------------------------------------------------
# Filter: only sensors listed here will appear as columns in the output CSV.
# Leave the list empty ( SENSOR_FILTER = [] ) to include every sensor found
# in the file instead.
# ---------------------------------------------------------------------------
SENSOR_FILTER = [
    "DIMM TMP MAX",
    "DIMM A TMP",
    "DIMM B TMP",
    "DIMM C TMP",
    "DIMM D TMP",
    "DIMM E TMP",
    "DIMM F TMP",
    "DIMM G TMP",
    "DIMM H TMP",
    "DIMM J TMP",
    "DIMM K TMP",
    "DIMM L TMP",
    "DIMM M TMP",
    "SOC TD1 TMP",
    "PWM 1",
    

]

# Value written to the CSV in place of a "null" reading.
NULL_VALUE = "NULL"

# Matches a block header line, e.g.:
#   [2026-08-20 13:14:10] COMMAND: curl -k ...
#   [2026-08-20 13:14:10] OUTPUT:
HEADER_RE = re.compile(
    r"^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]\s+(COMMAND|OUTPUT):\s*(.*)$"
)

# Matches a sensor data line, e.g.:
#   DIMM A TMP 67.0
#   SoC 0 VRHOT null
# The sensor name is everything except the trailing numeric/null value.
SENSOR_RE = re.compile(r"^(.+?)\s+(-?\d+(?:\.\d+)?|null)\s*$", re.IGNORECASE)


def parse_file(path):
    """Returns (rows, all_sensor_names_in_order_seen)."""
    rows = []
    seen_sensors = []
    seen_sensors_set = set()

    current_ts = None
    current_row = {}
    in_output = False

    def flush():
        if current_ts is not None and current_row:
            rows.append((current_ts, dict(current_row)))

    with open(path, "r", errors="replace") as f:
        for raw_line in f:
            line = raw_line.rstrip("\n")

            header_match = HEADER_RE.match(line)
            if header_match:
                ts, tag, _rest = header_match.groups()
                if tag.upper() == "COMMAND":
                    # New block starting -> save the previous OUTPUT block (if any)
                    flush()
                    current_row = {}
                    current_ts = None
                    in_output = False
                else:  # OUTPUT
                    current_ts = ts
                    in_output = True
                continue

            if not in_output or not line.strip():
                continue

            sensor_match = SENSOR_RE.match(line.strip())
            if not sensor_match:
                # Not a sensor line (e.g. curl progress meter output) -> skip
                continue

            name, value = sensor_match.groups()
            name = name.strip()

            if value.lower() == "null":
                value = NULL_VALUE

            if SENSOR_FILTER and name not in SENSOR_FILTER:
                continue

            current_row[name] = value

            if name not in seen_sensors_set:
                seen_sensors_set.add(name)
                seen_sensors.append(name)

    # flush the last block in the file
    flush()

    return rows, seen_sensors


def write_csv(rows, sensors, out_path):
    columns = SENSOR_FILTER if SENSOR_FILTER else sensors

    with open(out_path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["timestamp"] + columns)
        for ts, values in rows:
            writer.writerow([ts] + [values.get(col, "") for col in columns])


def main():
    if len(sys.argv) != 3:
        print("Usage: python3 parse_sensors.py <input_log> <output_csv>")
        sys.exit(1)

    in_path, out_path = sys.argv[1], sys.argv[2]
    rows, sensors = parse_file(in_path)

    if not rows:
        print("No OUTPUT blocks with matching sensor data were found.")
        sys.exit(1)

    write_csv(rows, sensors, out_path)
    print(f"Wrote {len(rows)} row(s) to {out_path}")


if __name__ == "__main__":
    main()