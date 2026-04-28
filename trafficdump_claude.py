#!/usr/bin/env python3
"""
TCPdump log parser.

Parses a tcpdump hex dump log and summarizes:
  1. How many requests were sent to each address.
  2. The average response time (ms) per address.

Transaction formats
-------------------
Request:  first line of 0x0000 starts with  0111
Response: first line of 0x0000 starts with  010a

The target address of a request is the 2-byte word immediately after
the 2-byte marker 0211 in the hex payload.

Algorithm
---------
Walk transactions sequentially (already time-ordered in the file).
  - If current = request  AND  next = response  → record delta, consume both.
  - If current = request  AND  next = request   → count the request, skip delta,
    advance by 1 (the unanswered request is discarded).
  - If current = response (orphan)              → skip, advance by 1.
"""

import re
import sys
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
    1002: "FREQ_AVG",
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

# First hex line of a packet:  "        0x0000:  <hex bytes>  <ascii>"
FIRST_LINE_RE = re.compile(r"^\s+0x0000:\s+((?:[0-9a-fA-F]{4}\s*)+)")

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


def classify_and_extract(first_hex_line: str):
    """
    Returns (kind, address) where
      kind    : 'request' | 'response' | 'unknown'
      address : uppercase hex string like '37 01', or None
    """
    raw = FIRST_LINE_RE.match(first_hex_line)
    if not raw:
        return "unknown", None

    b = hex_bytes(raw.group(1))

    if len(b) < 2:
        return "unknown", None

    kind = None
    if b[0] == "01" and b[1] == "11":
        kind = "request"
    elif b[0] == "01" and b[1] == "0a":
        kind = "response"
    else:
        return "unknown", None

    if kind == "request":
        # Find marker 02 11 and take the next 2 bytes as address
        for i in range(len(b) - 3):
            if b[i] == "02" and b[i+1] == "11":
                raw_addr = (b[i+2] + b[i+3]).upper()   # e.g. "3701"
                label = resolve_address(raw_addr)
                return kind, label
        # marker not found
        return kind, None

    return kind, None


# ── File parser ──────────────────────────────────────────────────────────────

def parse_transactions(path: str):
    """
    Returns list of dicts: {'ts': datetime, 'kind': str, 'addr': str|None}
    """
    transactions = []
    current_ts = None
    first_hex_line = None
    in_packet = False

    with open(path, "r", errors="replace") as fh:
        for raw_line in fh:
            line = raw_line.rstrip("\r\n")

            ts_match = TS_RE.match(line)
            if ts_match:
                # Save the previous packet if we have one
                if current_ts is not None and first_hex_line is not None:
                    kind, addr = classify_and_extract(first_hex_line)
                    transactions.append({
                        "ts":   current_ts,
                        "kind": kind,
                        "addr": addr,
                    })
                # Start a new packet
                current_ts = parse_timestamp(ts_match.group(1))
                first_hex_line = None
                in_packet = True
                continue

            if in_packet and first_hex_line is None:
                if "0x0000:" in line:
                    first_hex_line = line

        # Don't forget the last packet
        if current_ts is not None and first_hex_line is not None:
            kind, addr = classify_and_extract(first_hex_line)
            transactions.append({
                "ts":   current_ts,
                "kind": kind,
                "addr": addr,
            })

    return transactions


# ── Main analysis ─────────────────────────────────────────────────────────────

def analyse(transactions):
    """
    Walk the transaction list with the two-pointer algorithm described
    in the spec. Returns a dict keyed by address:
      { 'count': int, 'time_deltas_ms': [float, ...] }
    """
    stats = defaultdict(lambda: {"count": 0, "time_deltas_ms": []})
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
            # Request with unparseable address → skip
            i += 1
            continue

        stats[addr]["count"] += 1

        # Check if the very next transaction is a response
        if i + 1 < n and transactions[i + 1]["kind"] == "response":
            delta_ms = (
                transactions[i + 1]["ts"] - tx["ts"]
            ).total_seconds() * 1000.0
            stats[addr]["time_deltas_ms"].append(delta_ms)
            i += 2   # consume request + response
        else:
            i += 1   # unanswered request; no delta recorded

    return stats


def print_report(stats):
    if not stats:
        print("No requests found.")
        return

    # Sort by label name for readability
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
        else:
            print(
                f"{addr:<{col_w}}  {cnt:>10}  {paired:>12}  "
                f"{'N/A':>14}  {'N/A':>10}  {'N/A':>10}"
            )

    print(sep)
    total_req = sum(d["count"] for d in stats.values())
    total_paired = sum(len(d["time_deltas_ms"]) for d in stats.values())
    all_deltas = [x for d in stats.values() for x in d["time_deltas_ms"]]
    overall_avg = sum(all_deltas) / len(all_deltas) if all_deltas else float("nan")
    print(
        f"\nTotals  →  addresses: {len(stats)}  |  "
        f"requests: {total_req}  |  "
        f"paired (req+resp): {total_paired}  |  "
        f"overall avg resp: {overall_avg:.3f} ms"
    )



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

    saved = 0
    skipped = 0
    for label, data in sorted(stats.items()):
        deltas = data["time_deltas_ms"]
        if not deltas:
            skipped += 1
            continue

        avg = sum(deltas) / len(deltas)
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

        # Sanitise the label so it is a safe filename
        safe_name = "".join(c if c.isalnum() or c in "-_" else "_" for c in label)
        fig.savefig(out / f"{safe_name}.png", dpi=120)
        plt.close(fig)
        saved += 1

    print(f"\nPlots saved to '{out.resolve()}'  ({saved} files, {skipped} skipped — no deltas)")

# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    path       = sys.argv[1] if len(sys.argv) > 1 else "node4_traffictofailure_session4.log"
    output_dir = sys.argv[2] if len(sys.argv) > 2 else "plots"

    print(f"Parsing: {path}\n")
    transactions = parse_transactions(path)
    print(f"Transactions found: {len(transactions)}")

    req_count  = sum(1 for t in transactions if t["kind"] == "request")
    resp_count = sum(1 for t in transactions if t["kind"] == "response")
    unk_count  = sum(1 for t in transactions if t["kind"] == "unknown")
    print(f"  requests : {req_count}")
    print(f"  responses: {resp_count}")
    print(f"  unknown  : {unk_count}\n")

    stats = analyse(transactions)
    print_report(stats)
    save_plots(stats, output_dir)


if __name__ == "__main__":
    main()