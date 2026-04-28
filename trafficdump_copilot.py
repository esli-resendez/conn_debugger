import re
from datetime import datetime
from collections import defaultdict

INPUT_FILE = "tcpdump.txt"

# Regex helpers
TIMESTAMP_RE = re.compile(r"^(\d{2}:\d{2}:\d{2}\.\d+)")
HEX_RE = re.compile(r"([0-9a-fA-F]{4})")

def parse_timestamp(line):
    m = TIMESTAMP_RE.match(line.strip())
    if m:
        return datetime.strptime(m.group(1), "%H:%M:%S.%f")
    return None

def extract_bytes(line):
    matches = HEX_RE.findall(line)
    bytes_list = []
    for m in matches:
        bytes_list.append(int(m[0:2], 16))
        bytes_list.append(int(m[2:4], 16))
    return bytes_list

def classify_transaction(bytes_list):
    # First 2 bytes define request/response
    if len(bytes_list) < 2:
        return None

    prefix = (bytes_list[0] << 8) | bytes_list[1]

    if prefix == 0x0111:
        return "request"
    elif prefix == 0x010a:
        return "response"
    else:
        return None

def extract_address(bytes_list):
    """
    Address rule:
    Find sequence 02 11, take next 2 bytes (little endian)
    """
    for i in range(len(bytes_list) - 3):
        if bytes_list[i] == 0x02 and bytes_list[i + 1] == 0x11:
            # Address is next 2 bytes (little endian)
            addr = bytes_list[i + 2] | (bytes_list[i + 3] << 8)
            return addr
    return None

def parse_transactions(filename):
    transactions = []

    with open(filename, "r") as f:
        current_ts = None
        current_bytes = []

        for line in f:
            ts = parse_timestamp(line)

            if ts:
                # Commit previous transaction
                if current_ts is not None:
                    transactions.append((current_ts, current_bytes))

                current_ts = ts
                current_bytes = []
            else:
                bytes_list = extract_bytes(line)
                current_bytes.extend(bytes_list)

        # last one
        if current_ts is not None:
            transactions.append((current_ts, current_bytes))

    return transactions

def analyze(transactions):
    stats = defaultdict(lambda: {"count": 0, "deltas": []})

    i = 0
    n = len(transactions)

    while i < n - 1:
        ts1, bytes1 = transactions[i]
        ts2, bytes2 = transactions[i + 1]

        type1 = classify_transaction(bytes1)
        type2 = classify_transaction(bytes2)

        # Only process request → response pairs
        if type1 == "request" and type2 == "response":
            addr = extract_address(bytes1)

            if addr is not None:
                delta = (ts2 - ts1).total_seconds()

                stats[addr]["count"] += 1
                stats[addr]["deltas"].append(delta)

            # Remove the pair (skip both)
            i += 2
        else:
            # Skip only the first
            i += 1

    return stats

def print_summary(stats):
    print("\n=== Summary ===\n")
    for addr, data in sorted(stats.items()):
        count = data["count"]
        deltas = data["deltas"]

        avg = sum(deltas) / len(deltas) if deltas else 0

        print(f"Address 0x{addr:04x}")
        print(f"  Requests: {count}")
        print(f"  Avg response time: {avg*1000:.3f} ms")
        print()

# ---- MAIN ----

if __name__ == "__main__":
    transactions = parse_transactions(INPUT_FILE)
    stats = analyze(transactions)
    print_summary(stats)