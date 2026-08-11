#!/usr/bin/env python3
"""
TCPdump / MCTP log parser with PLDM sensor decoding.

Parses a tcpdump hex dump log and produces:
  1. Timing report – how many requests were sent to each address and the
     average / min / max response time (ms) per address.  (original feature)
  2. Sensor-value table – one row per sensor, one column (C1 … CN) per
     polling cycle, containing the decoded physical value.  A cycle is
     delimited by the 2-byte sequence counter embedded in every request
     (bytes immediately after 0111 0ac8): it runs from 0180 (cycle start)
     to 019f (cycle end).  Each new 0180 seen after a 019f increments the
     cycle number.
     (new feature — integrates the PLDM GetSensorReading decoder)

Transaction formats
-------------------
Request:  first line of 0x0000 starts with  0111
Response: first line of 0x0000 starts with  010a

The target address of a request is the 2-byte word immediately after
the 2-byte marker 0211 in the hex payload.

PLDM GetSensorReading response layout (after the 02 11 marker)
--------------------------------------------------------------
  Byte 0 : completion_code   (0x00 = success)
  Byte 1 : sensor_data_size  (0x04 = sint16 throughout this log)
  Bytes 2-6 : state fields (operational / event states)
  Bytes 7+ : presentReading  (little-endian, width = sensor_data_size)

Conversion formula
------------------
  physical_value = raw_reading * resolution

  Temperature sensors (°C, raw value is integer degrees):
    - Family 7xx  (700–799)
    - Suffix  11  (x11: 211, 311, …, 611) — excludes 811 (voltage)
    - Sensor  916

  Electrical sensors (resolution = 0.001 → mV/mA/mW → V/A/W):
    - All other sensors (8xx, x03, x04, 10xx, 101, …)

  Special case: if sensor_data_size == sint16 but the raw sint16 value is
  negative (unsigned overflow, e.g. sensor 101 at 0xC5FB), the bit pattern
  is reinterpreted as uint16 before applying the resolution.

Algorithm (timing analysis)
---------------------------
Walk transactions sequentially (already time-ordered in the file).
  - If current = request  AND  next = response  → record delta, consume both.
  - If current = request  AND  next = request   → count the request, skip delta,
    advance by 1 (the unanswered request is discarded).
  - If current = response (orphan)              → skip, advance by 1.
"""

import re
import struct
import sys
import pandas as pd
from datetime import datetime
from collections import defaultdict
from pathlib import Path

import matplotlib
matplotlib.use("Agg")          # non-interactive backend; no display needed
import matplotlib.pyplot as plt


# ── Address lookup ───────────────────────────────────────────────────────────
#
# Keys are decimal integers (little-endian 2-byte address converted to dec).
# Values are human-readable names.
# Add your own entries here following the same pattern.
#
# Example: raw bytes 37 01  →  little-endian 0x0137  →  311 decimal  →  "Sensor_311"
#
ADDRESS_NAMES: dict[int, str] = {
        101: "POWER",
    203: "VCPU_VOLT",
    204: "VCPU_CUR",
    211: "VCPU_TMP",
    303: "VSYS_VOLT",
    304: "VSYS_CUR",
    311: "VSYS_TMP",
    403: "VSOC_VOLT",
    404: "VSOC_CUR",
    411: "VSOC_TMP",
    503: "VA0P85_VOLT",
    504: "VA0P85_CUR",
    511: "VA0P85_TMP",
    603: "VDDQ_VOLT",
    604: "VDDQ_CUR",
    611: "VDDQ_TMP",
    700: "DIMM_A_S0_TMP",
    701: "DIMM_A_S1_TMP",
    702: "DIMM_G_S0_TMP",
    703: "DIMM_G_S1_TMP",
    704: "DIMM_D_S0_TMP",
    705: "DIMM_D_S1_TMP",
    706: "DIMM_K_S0_TMP",
    707: "DIMM_K_S1_TMP",
    708: "DIMM_E_S0_TMP",
    709: "DIMM_E_S1_TMP",
    710: "DIMM_L_S0_TMP",
    711: "DIMM_L_S1_TMP",
    712: "DIMM_F_S0_TMP",
    713: "DIMM_F_S1_TMP",
    714: "DIMM_M_S0_TMP",
    715: "DIMM_M_S1_TMP",
    716: "DIMM_C_S0_TMP",
    717: "DIMM_C_S1_TMP",
    718: "DIMM_J_S0_TMP",
    719: "DIMM_J_S1_TMP",
    720: "DIMM_B_S0_TMP",
    721: "DIMM_B_S1_TMP",
    722: "DIMM_H_S0_TMP",
    723: "DIMM_H_S1_TMP",
    800: "DIMM_A_PWR",
    801: "DIMM_G_PWR",
    802: "DIMM_D_PWR",
    803: "DIMM_K_PWR",
    804: "DIMM_E_PWR",
    805: "DIMM_L_PWR",
    806: "DIMM_F_PWR",
    807: "DIMM_M_PWR",
    808: "DIMM_C_PWR",
    809: "DIMM_J_PWR",
    810: "DIMM_B_PWR",
    811: "DIMM_H_PWR",
    916: "CORE_TMP_MAX",
    1000: "FREQ_NOM",
    1001: "FREQ_MAX",
    1002: "FREQ_AVG"

}


def resolve_address(raw_hex: str) -> str:
    """
    Convert a 2-byte little-endian hex string (e.g. '3701') to a name.

    Steps:
      1. Interpret the two bytes in little-endian order:
             '3701'  →  byte0=0x37, byte1=0x01  →  0x0137  →  311
      2. Look up the decimal value in ADDRESS_NAMES.
      3. Return the name if found, otherwise 'UNKNOWN_<dec>'.

    Args:
        raw_hex: 4-character uppercase hex string, e.g. '3701'.

    Returns:
        A string label for the address.
    """
    if len(raw_hex) != 4:
        return f"UNKNOWN_{raw_hex}"

    byte0 = int(raw_hex[0:2], 16)   # low byte  (little-endian)
    byte1 = int(raw_hex[2:4], 16)   # high byte
    dec_value = (byte1 << 8) | byte0

    if dec_value in ADDRESS_NAMES:
        return ADDRESS_NAMES[dec_value]

    return f"UNKNOWN_{dec_value}"


# ── Regex patterns ──────────────────────────────────────────────────────────

# Timestamp: HH:MM:SS.ffffff  (with optional leading date YYYY-MM-DD)
TS_RE = re.compile(
    r"^(?:\d{4}-\d{2}-\d{2}\s+)?(\d{2}:\d{2}:\d{2}\.\d+)\s*$"
)

# Any hex line of a packet:  "0x0000:  <hex bytes>  <ascii>"
HEX_LINE_RE = re.compile(r"^\s*0x[0-9a-fA-F]+:\s+((?:[0-9a-fA-F]{4}\s*)+)")

# Convenience alias kept for classify_and_extract (matches the first hex line)
FIRST_LINE_RE = HEX_LINE_RE

TS_FMT = "%H:%M:%S.%f"


def parse_timestamp(ts_str: str) -> datetime:
    return datetime.strptime(ts_str.strip(), TS_FMT)


def hex_bytes(raw: str) -> list[str]:
    """Return a list of 2-char hex byte strings from a run of hex groups."""
    # raw looks like "0111 0ac8 0180 0211 c202 00"
    groups = raw.split()
    out = []
    for g in groups:
        # each group is 4 hex chars = 2 bytes
        for i in range(0, len(g), 2):
            out.append(g[i:i+2])
    return out


def hex_lines_to_bytes(hex_lines: list[str]) -> bytes:
    """
    Convert all captured hex dump lines for a packet into a bytes object.

    Each element of hex_lines is a raw log line like:
        '0x0000:  010a 11c0 0100 0211 0004 0000 0100 01dc  ................'
    """
    all_bytes: list[int] = []
    for line in hex_lines:
        m = HEX_LINE_RE.match(line)
        if m:
            for byte_str in hex_bytes(m.group(1)):
                if byte_str:
                    all_bytes.append(int(byte_str, 16))
    return bytes(all_bytes)


# ── Cycle counter ────────────────────────────────────────────────────────────
#
# Every request contains a 2-byte sequence counter immediately after the fixed
# prefix  01 11 0a c8.  The counter runs 01 80, 01 81, …, 01 9f and then
# wraps back to 01 80 on the next cycle.
#
# The two bytes are stored in the packet as a single big-endian 16-bit word
# (the log groups bytes in pairs).  In the hex dump they appear as the word
# right after "0ac8", e.g.:
#
#   0x0000:  0111 0ac8 0180 0211 …
#                       ^^^^
#                       cycle counter word = 0x0180
#
CYCLE_START: int = 0x0180   # counter value that opens a new cycle
CYCLE_END:   int = 0x019F   # counter value that closes a cycle


def extract_cycle_counter(b: list[str]) -> int | None:
    """
    Given the byte list of a request's first hex line, return the 16-bit
    cycle counter (big-endian) found at bytes [4:6] (after 01 11 0a c8),
    or None if the packet is too short or the prefix doesn't match.
    """
    # Expect at least 6 bytes: 01 11 0a c8 <hi> <lo>
    if len(b) < 6:
        return None
    if b[0] != "01" or b[1] != "11" or b[2] != "0a" or b[3] != "c8":
        return None
    return (int(b[4], 16) << 8) | int(b[5], 16)


def classify_and_extract(first_hex_line: str):
    """
    Returns (kind, address, cycle_counter) where
      kind          : 'request' | 'response' | 'unknown'
      address       : resolved label string, or None
      cycle_counter : int (CYCLE_START … CYCLE_END) for requests, else None
    """
    raw = FIRST_LINE_RE.match(first_hex_line)
    if not raw:
        return "unknown", None, None

    b = hex_bytes(raw.group(1))

    if len(b) < 2:
        return "unknown", None, None

    kind = None
    if b[0] == "01" and b[1] == "11":
        kind = "request"
    elif b[0] == "01" and b[1] == "0a":
        kind = "response"
    else:
        return "unknown", None, None

    if kind == "request":
        cycle_ctr = extract_cycle_counter(b)

        # Find marker 02 11 and take the next 2 bytes as sensor address
        for i in range(len(b) - 3):
            if b[i] == "02" and b[i+1] == "11":
                raw_addr = (b[i+2] + b[i+3]).upper()   # e.g. "3701"
                label = resolve_address(raw_addr)
                return kind, label, cycle_ctr
        # marker not found (address-less request)
        return kind, None, cycle_ctr

    return kind, None, None


# ── File parser ──────────────────────────────────────────────────────────────

def parse_transactions(path: str) -> list[dict]:
    """
    Returns list of dicts:
      {
        'ts':            datetime,
        'kind':          str,          # 'request' | 'response' | 'unknown'
        'addr':          str | None,   # resolved sensor label (requests only)
        'cycle_counter': int | None,   # raw counter value 0x0180–0x019F (requests only)
        'hex_lines':     list[str],    # ALL raw hex dump lines of this packet
      }

    hex_lines is stored for every packet so the PLDM decoder can reconstruct
    the full payload of responses.  cycle_counter is extracted from requests
    and used by analyse() to assign each pair to its polling cycle.
    """
    transactions: list[dict] = []
    current_ts: datetime | None = None
    current_hex_lines: list[str] = []
    in_packet = False

    def _flush():
        """Commit the current packet to transactions."""
        if current_ts is None or not current_hex_lines:
            return
        kind, addr, cycle_ctr = classify_and_extract(current_hex_lines[0])
        transactions.append({
            "ts":            current_ts,
            "kind":          kind,
            "addr":          addr,
            "cycle_counter": cycle_ctr,
            "hex_lines":     list(current_hex_lines),
        })

    with open(path, "r", errors="replace") as fh:
        for raw_line in fh:
            line = raw_line.rstrip("\r\n")

            ts_match = TS_RE.match(line)
            if ts_match:
                _flush()
                current_ts = parse_timestamp(ts_match.group(1))
                current_hex_lines = []
                in_packet = True
                continue

            if in_packet and "0x" in line and HEX_LINE_RE.match(line):
                current_hex_lines.append(line)

    _flush()  # don't forget the last packet
    return transactions


# ── PLDM sensor decoder ───────────────────────────────────────────────────────

# sensor_data_size byte → (struct format char, byte width)
_DATA_SIZE_FMT: dict[int, tuple[str, int]] = {
    0x01: ("B", 1),   # uint8
    0x02: ("b", 1),   # sint8
    0x03: ("H", 2),   # uint16
    0x04: ("h", 2),   # sint16   ← used throughout this log
    0x05: ("I", 4),   # uint32
    0x06: ("i", 4),   # sint32
}


def get_sensor_resolution(sensor_id: int) -> float:
    """
    Return the resolution (scale factor) for converting the raw reading to a
    physical value, based on the sensor's ID family/suffix.

    Temperature sensors → resolution = 1.0   (raw value is integer °C)
      - Family 7xx  (700–799)
      - Suffix  11  (x11: 211, 311, 411, 511, 611) — excludes 811 (voltage)
      - Sensor  916

    Electrical sensors → resolution = 0.001  (raw in milli-units → V / A / W)
      - All other sensors
    """
    family = sensor_id // 100
    suffix = sensor_id % 100

    if family == 7:                   # board/chip temperature sensors
        return 1.0
    if suffix == 11 and family < 8:  # inlet/outlet temp sensors (not 811)
        return 1.0
    if sensor_id == 916:             # special temperature sensor
        return 1.0

    return 0.001                      # electrical (mV / mA / mW → V / A / W)


def decode_response_payload(resp_bytes: bytes) -> float | None:
    """
    Decode a PLDM GetSensorReading response payload.

    Locates the 02 11 marker, reads the completion code, sensor_data_size,
    and presentReading; applies the per-sensor resolution; returns the
    physical value, or None if decoding fails.

    The sensor_id is derived from the same resp_bytes by inspecting bytes
    after the marker — this is only valid because the response mirrors the
    sensor_id from the request, but we extract it from the request address
    instead (passed separately to decode_pair).
    """
    # Locate 02 11 marker
    payload_start: int | None = None
    for i in range(len(resp_bytes) - 1):
        if resp_bytes[i] == 0x02 and resp_bytes[i + 1] in (0x11, 0x21):
            payload_start = i + 2
            break
    if payload_start is None:
        return None

    if len(resp_bytes) < payload_start + 8:
        return None

    completion_code = resp_bytes[payload_start]
    if completion_code != 0x00:
        return None   # error response

    sensor_data_size = resp_bytes[payload_start + 1]
    if sensor_data_size not in _DATA_SIZE_FMT:
        return None

    fmt, nbytes = _DATA_SIZE_FMT[sensor_data_size]
    reading_offset = payload_start + 7   # skip 5 state bytes

    if len(resp_bytes) < reading_offset + nbytes:
        return None

    raw_value: int = struct.unpack(
        "<" + fmt, resp_bytes[reading_offset:reading_offset + nbytes]
    )[0]

    return raw_value, sensor_data_size


def decode_pair(req_addr: str, resp_hex_lines: list[str]) -> float | None:
    """
    Given the resolved request address label and all hex lines of the response,
    return the decoded physical value (float) or None on failure.

    The sensor_id is inferred back from the address label:
      - 'UNKNOWN_<dec>' → decimal id = <dec>
      - anything in ADDRESS_NAMES → look up by value
      - otherwise try parsing trailing digits

    Args:
        req_addr:       resolved label, e.g. 'UNKNOWN_800' or 'Sensor_800'
        resp_hex_lines: list of raw hex dump lines for the response packet
    """
    resp_bytes = hex_lines_to_bytes(resp_hex_lines)
    result = decode_response_payload(resp_bytes)
    if result is None:
        return None

    raw_value, sensor_data_size = result

    # Recover the numeric sensor_id from the address label
    sensor_id: int | None = None

    # Case 1: label is 'UNKNOWN_<dec>'
    m = re.match(r"UNKNOWN_(\d+)$", req_addr)
    if m:
        sensor_id = int(m.group(1))
    else:
        # Case 2: label is a user-supplied name — reverse-lookup ADDRESS_NAMES
        for dec_id, name in ADDRESS_NAMES.items():
            if name == req_addr:
                sensor_id = dec_id
                break
        # Case 3: fall back to trailing integer in the label
        if sensor_id is None:
            m2 = re.search(r"(\d+)$", req_addr)
            if m2:
                sensor_id = int(m2.group(1))

    if sensor_id is None:
        return None

    resolution = get_sensor_resolution(sensor_id)

    # Handle unsigned overflow: sint16 declared but value physically cannot
    # be negative (sensor 101 etc.)
    if sensor_data_size == 0x04 and raw_value < 0:
        raw_value = raw_value + 65536   # reinterpret as uint16

    return round(raw_value * resolution, 4)


# ── Main analysis ─────────────────────────────────────────────────────────────

def analyse(transactions: list[dict]) -> tuple[dict, list[dict]]:
    """
    Walk the transaction list with the two-pointer algorithm.

    Cycle numbering
    ---------------
    Each request carries a cycle counter (CYCLE_START=0x0180 … CYCLE_END=0x019F).
    The cycle number increments every time the counter transitions from CYCLE_END
    back to CYCLE_START, i.e. every time we see 0x0180 after having previously
    seen 0x019F.  The very first 0x0180 encountered opens cycle 1.

    Returns
    -------
    stats : dict keyed by address label
        { 'count': int, 'time_deltas_ms': [float, ...] }

    pairs : list of dicts – one per completed request/response pair
        {
          'cycle':    int,          # 1-based polling cycle number (C1, C2, …)
          'addr':     str,          # sensor label
          'ts_req':   datetime,
          'ts_resp':  datetime,
          'delta_ms': float,
          'value':    float|None,   # decoded physical value
        }
    """
    stats = defaultdict(lambda: {"count": 0, "time_deltas_ms": []})
    pairs: list[dict] = []

    cycle_number  = 0      # 0 = before the first cycle starts
    saw_cycle_end = False  # True once we've seen at least one CYCLE_END

    i = 0
    n = len(transactions)

    while i < n:
        tx = transactions[i]

        if tx["kind"] != "request":
            # Orphan response or unknown → skip
            i += 1
            continue

        addr = tx["addr"]
        if addr is None:
            i += 1
            continue

        # ── Advance cycle counter based on embedded sequence number ──────────
        ctr = tx.get("cycle_counter")
        if ctr is not None:
            if ctr == CYCLE_START:
                # Either the very first cycle or a wrap-around from the previous.
                # Only increment when we've actually completed at least one cycle
                # (i.e. we've seen a CYCLE_END before).
                if cycle_number == 0 or saw_cycle_end:
                    cycle_number += 1
                    saw_cycle_end = False
            elif ctr == CYCLE_END:
                saw_cycle_end = True

        # Fall back to cycle 1 for any request that arrives before the first
        # CYCLE_START (shouldn't normally happen with well-formed captures).
        effective_cycle = max(cycle_number, 1)

        stats[addr]["count"] += 1

        if i + 1 < n and transactions[i + 1]["kind"] == "response":
            resp_tx  = transactions[i + 1]
            delta_ms = (resp_tx["ts"] - tx["ts"]).total_seconds() * 1000.0
            stats[addr]["time_deltas_ms"].append(delta_ms)

            value = decode_pair(addr, resp_tx["hex_lines"])

            pairs.append({
                "cycle":    effective_cycle,
                "addr":     addr,
                "ts_req":   tx["ts"],
                "ts_resp":  resp_tx["ts"],
                "delta_ms": delta_ms,
                "value":    value,
            })

            i += 2   # consume request + response
        else:
            i += 1   # unanswered request; no delta recorded

    return stats, pairs


# ── Timing report ─────────────────────────────────────────────────────────────

def print_report(stats: dict, csv_path: str = "report.csv") -> None:
    if not stats:
        print("No requests found.")
        return

    rows = []
    sorted_addrs = sorted(stats.keys())

    col_w = max(len(a) for a in sorted_addrs)
    header = (
        f"{'Name / Address':<{col_w}}  {'Requests':>10}  "
        f"{'Paired resp':>12}  {'Avg resp (ms)':>14}  "
        f"{'Min (ms)':>10}  {'Max (ms)':>10}"
    )
    sep = "-" * len(header)
    print(sep)
    print(header)
    print(sep)

    for addr in sorted_addrs:
        d = stats[addr]
        cnt = d["count"]
        deltas = d["time_deltas_ms"]
        paired = len(deltas)

        if deltas:
            avg = sum(deltas) / paired
            mn  = min(deltas)
            mx  = max(deltas)
            print(
                f"{addr:<{col_w}}  {cnt:>10}  {paired:>12}  "
                f"{avg:>14.3f}  {mn:>10.3f}  {mx:>10.3f}"
            )
            rows.append({
                "Name / Address": addr,
                "Requests": cnt,
                "Paired resp": paired,
                "Avg resp (ms)": round(avg, 3),
                "Min (ms)": round(mn, 3),
                "Max (ms)": round(mx, 3),
            })
        else:
            print(
                f"{addr:<{col_w}}  {cnt:>10}  {paired:>12}  "
                f"{'N/A':>14}  {'N/A':>10}  {'N/A':>10}"
            )
            rows.append({
                "Name / Address": addr,
                "Requests": cnt,
                "Paired resp": paired,
                "Avg resp (ms)": None,
                "Min (ms)": None,
                "Max (ms)": None,
            })

    print(sep)

    total_req    = sum(d["count"] for d in stats.values())
    total_paired = sum(len(d["time_deltas_ms"]) for d in stats.values())
    all_deltas   = [x for d in stats.values() for x in d["time_deltas_ms"]]
    overall_avg  = sum(all_deltas) / len(all_deltas) if all_deltas else float("nan")

    print(
        f"\nTotals  →  addresses: {len(stats)}  |  "
        f"requests: {total_req}  |  "
        f"paired (req+resp): {total_paired}  |  "
        f"overall avg resp: {overall_avg:.3f} ms"
    )

    Path(csv_path).parent.mkdir(parents=True, exist_ok=True)
    df = pd.DataFrame(rows)
    df.to_csv(csv_path, index=False)
    print(f"\nTiming report saved to: {csv_path}")


# ── Sensor value table ────────────────────────────────────────────────────────

def save_sensor_table(pairs: list[dict], csv_path: str = "sensor_values.csv") -> None:
    """
    Build and save a wide-format table:

      Sensor  | T1    | T2    | T3    | … | TN
      --------|-------|-------|-------|---|------
      UNKNOWN_800 | 1.5 | 1.5 | 1.625 | … | …
      UNKNOWN_211 | 48  | 48  | 48    | … | …
      …

    Rows are sensors (sorted).
    Columns T1…TN correspond to completed transaction pairs in time order.
    A cell is empty (NaN) when that sensor had no completed pair in that round.

    The pair index (T1, T2, …) is global across all sensors — it counts every
    completed request/response pair in the log, regardless of which sensor it
    belongs to.  This means sensors polled less frequently will have sparse
    rows, and sensors polled in every round will be dense.
    """
    if not pairs:
        print("No completed pairs found — sensor table not written.")
        return

    # Determine the full set of T-column names
    max_index = max(p["cycle"] for p in pairs)
    t_cols = [f"T{k}" for k in range(1, max_index + 1)]

    # Collect all sensor labels
    all_sensors = sorted({p["addr"] for p in pairs})

    # Build a lookup: (addr, pair_index) → value
    cell: dict[tuple[str, int], float | None] = {}
    for p in pairs:
        key = (p["addr"], p["cycle"])
        # If same sensor appears twice in the same pair slot (shouldn't happen),
        # keep the latest.
        cell[key] = p["value"]

    # Assemble rows
    rows = []
    for sensor in all_sensors:
        row: dict = {"Sensor": sensor}
        for k in range(1, max_index + 1):
            row[f"T{k}"] = cell.get((sensor, k), None)
        rows.append(row)

    df = pd.DataFrame(rows, columns=["Sensor"] + t_cols)

    Path(csv_path).parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(csv_path, index=False)

    print(f"\nSensor value table saved to: {csv_path}")
    print(f"  Sensors : {len(all_sensors)}")
    print(f"  Columns : Sensor + T1 … T{max_index}  ({max_index} transaction pairs)")

    # Also print a compact preview (first 5 sensors, first 8 T-columns)
    preview_cols = ["Sensor"] + t_cols[:8]
    preview_df   = df[preview_cols].head(5)
    print("\nPreview (first 5 sensors, up to T8):")
    print(preview_df.to_string(index=False))
    if len(all_sensors) > 5 or max_index > 8:
        print("  … (truncated — see full CSV)")


# ── Plotting ──────────────────────────────────────────────────────────────────

def save_plots(stats: dict, output_dir: str = "plots") -> None:
    """
    Save one line plot per address into output_dir.

    Each plot shows:
      - X axis: sample index (0, 1, 2, ...)
      - Y axis: response time in ms
      - A dashed horizontal line for the average
      - Title and filename equal to the address label

    Addresses with no recorded deltas are skipped.
    """
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    saved   = 0
    skipped = 0
    for label, data in sorted(stats.items()):
        deltas = data["time_deltas_ms"]
        if not deltas:
            skipped += 1
            continue

        avg     = sum(deltas) / len(deltas)
        indices = list(range(len(deltas)))

        fig, ax = plt.subplots(figsize=(10, 4))

        ax.plot(indices, deltas, linewidth=1.2, color="#2563eb", label="Response time")
        ax.axhline(avg, color="#dc2626", linewidth=1, linestyle="--",
                   label=f"Avg: {avg:.2f} ms")

        ax.set_title(label, fontsize=13, fontweight="bold")
        ax.set_xlabel("Sample index", fontsize=11)
        ax.set_ylabel("Response time (ms)", fontsize=11)
        ax.legend(fontsize=9)
        ax.grid(True, linestyle="--", alpha=0.4)
        fig.tight_layout()

        safe_name = "".join(c if c.isalnum() or c in "-_" else "_" for c in label)
        fig.savefig(out / f"{safe_name}.png", dpi=120)
        plt.close(fig)
        saved += 1

    print(f"\nPlots saved to '{out.resolve()}'  ({saved} files, {skipped} skipped — no deltas)")


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    path       = sys.argv[1] if len(sys.argv) > 1 else "node4_traffictofailure_session4.log"
    output_dir = sys.argv[2] if len(sys.argv) > 2 else "output"

    print(f"Parsing: {path}\n")
    transactions = parse_transactions(path)
    print(f"Transactions found: {len(transactions)}")

    req_count  = sum(1 for t in transactions if t["kind"] == "request")
    resp_count = sum(1 for t in transactions if t["kind"] == "response")
    unk_count  = sum(1 for t in transactions if t["kind"] == "unknown")
    print(f"  requests : {req_count}")
    print(f"  responses: {resp_count}")
    print(f"  unknown  : {unk_count}\n")

    stats, pairs = analyse(transactions)

    # ── Timing report (original) ──
    print("\n── Timing Report ────────────────────────────────────────────────")
    print_report(stats, f"{output_dir}/report/report.csv")

    # ── Sensor value table (new) ──
    print("\n── Sensor Value Table ───────────────────────────────────────────")
    save_sensor_table(pairs, f"{output_dir}/report/sensor_values.csv")

    # ── Response-time plots (original) ──
    save_plots(stats, f"{output_dir}/plots")


if __name__ == "__main__":
    main()