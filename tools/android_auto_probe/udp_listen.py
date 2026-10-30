#!/usr/bin/env python3
"""Receive AA Probe UDP log lines and print/save them (PC or C4, stdlib only).

usage: python3 udp_listen.py [--port 7799] [--out aaprobe_udp.jsonl]
"""
import argparse
import json
import socket
import time


def main() -> None:
  parser = argparse.ArgumentParser()
  parser.add_argument("--port", type=int, default=7799)
  parser.add_argument("--out", default="aaprobe_udp.jsonl")
  args = parser.parse_args()

  sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
  sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
  sock.bind(("0.0.0.0", args.port))
  print(f"listening udp/{args.port} -> {args.out}")
  with open(args.out, "a", encoding="utf-8") as f:
    while True:
      data, addr = sock.recvfrom(65535)
      line = data.decode("utf-8", errors="replace")
      f.write(line + "\n")
      f.flush()
      try:
        msg = json.loads(line)
        kind = msg.get("kind")
        name = msg.get("name", "")
        rest = {k: v for k, v in msg.items() if k not in ("t", "mono_ms", "kind", "name")}
        print(f"{time.strftime('%H:%M:%S')} {addr[0]} {kind} {name} {json.dumps(rest, ensure_ascii=False)[:200]}")
      except ValueError:
        print(f"{addr[0]} {line[:200]}")


if __name__ == "__main__":
  main()
