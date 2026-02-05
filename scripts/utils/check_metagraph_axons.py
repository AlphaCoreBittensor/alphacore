#!/usr/bin/env python3
"""
Print miner axon endpoints from the Bittensor metagraph.

Examples:
  python3 scripts/utils/check_metagraph_axons.py --netuid 66 --network finney
  python3 scripts/utils/check_metagraph_axons.py --netuid 66 --chain-endpoint wss://finney.opentensor.io:443
  python3 scripts/utils/check_metagraph_axons.py --netuid 66 --network finney --format json --miners-only
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any


def _is_zero_endpoint(ip: str | None, port: int | None) -> bool:
    if not ip or ip.strip() in {"0.0.0.0", "0", "none", "null"}:
        return True
    if port is None or int(port) == 0:
        return True
    return False


def _render_table(rows: list[dict[str, Any]]) -> str:
    headers = ["uid", "hotkey", "ip", "port", "validator_permit"]
    widths = {h: len(h) for h in headers}
    for row in rows:
        for h in headers:
            widths[h] = max(widths[h], len(str(row.get(h, ""))))
    line = "  ".join(h.ljust(widths[h]) for h in headers)
    sep = "  ".join("-" * widths[h] for h in headers)
    out = [line, sep]
    for row in rows:
        out.append("  ".join(str(row.get(h, "")).ljust(widths[h]) for h in headers))
    return "\n".join(out)


def main() -> int:
    parser = argparse.ArgumentParser(description="List miner axon endpoints from metagraph.")
    parser.add_argument("--netuid", type=int, required=True, help="Subnet netuid.")
    parser.add_argument("--network", default=None, help="Bittensor network (e.g., finney).")
    parser.add_argument("--chain-endpoint", default=None, help="Subtensor chain endpoint URL.")
    parser.add_argument(
        "--include-zero",
        action="store_true",
        help="Include axons with 0.0.0.0:0 (not served).",
    )
    parser.add_argument(
        "--miners-only",
        action="store_true",
        help="Exclude UIDs with validator_permit=true.",
    )
    parser.add_argument(
        "--format",
        choices=["table", "json", "csv"],
        default="table",
        help="Output format.",
    )
    args = parser.parse_args()

    try:
        import bittensor as bt
    except Exception as exc:
        print(f"ERROR: failed to import bittensor: {exc}", file=sys.stderr)
        return 2

    subtensor_ctor = getattr(bt, "subtensor", None) or getattr(bt, "Subtensor", None)
    if subtensor_ctor is None:
        print("ERROR: bittensor does not expose subtensor/Subtensor constructor", file=sys.stderr)
        return 2
    try:
        subtensor = subtensor_ctor(network=args.network, chain_endpoint=args.chain_endpoint)
    except TypeError:
        # Older bittensor versions don't accept chain_endpoint.
        if args.chain_endpoint:
            print(
                "ERROR: bittensor Subtensor does not accept --chain-endpoint in this version",
                file=sys.stderr,
            )
            return 2
        subtensor = subtensor_ctor(network=args.network)
    metagraph = subtensor.metagraph(args.netuid)
    metagraph.sync()

    rows: list[dict[str, Any]] = []
    uids = list(getattr(metagraph, "uids", []))
    for uid in uids:
        uid_int = int(uid)
        ax = metagraph.axons[uid_int] if uid_int < len(metagraph.axons) else None
        ip = getattr(ax, "ip", None) if ax is not None else None
        port = getattr(ax, "port", None) if ax is not None else None
        hotkey = getattr(ax, "hotkey", None) if ax is not None else None
        validator_permit = bool(getattr(metagraph, "validator_permit", [False] * (uid_int + 1))[uid_int])

        if args.miners_only and validator_permit:
            continue
        if not args.include_zero and _is_zero_endpoint(str(ip) if ip is not None else None, int(port) if port else None):
            continue

        rows.append(
            {
                "uid": uid_int,
                "hotkey": hotkey or "",
                "ip": ip or "",
                "port": int(port) if port is not None else "",
                "validator_permit": validator_permit,
            }
        )

    if args.format == "json":
        print(json.dumps(rows, indent=2, sort_keys=True))
        return 0
    if args.format == "csv":
        print("uid,hotkey,ip,port,validator_permit")
        for row in rows:
            print(f'{row["uid"]},{row["hotkey"]},{row["ip"]},{row["port"]},{row["validator_permit"]}')
        return 0

    print(_render_table(rows))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
