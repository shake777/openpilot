#!/usr/bin/env python3
"""Fake SLCAN-over-TCP adapter for bench-testing AA Probe's M-CAN recorder (stdlib only).

Acts like a WiCAN in slcan mode: acknowledges commands with CR, streams frames only after the
channel is opened at --bitrate (other speeds stay silent so the app's speed scan is exercised),
and fails loudly if the app ever sends a data frame (t/T/r/R), which a listen-only recorder must not.

usage: python3 fake_slcan.py [--port 3333] [--bitrate 100000] [--reject-listen]
"""
import argparse
import socket
import threading
import time

CODES = {0: 10_000, 1: 20_000, 2: 50_000, 3: 100_000, 4: 125_000, 5: 250_000, 6: 500_000, 7: 800_000, 8: 1_000_000}


def serve(conn: socket.socket, bitrate: int, reject_listen: bool) -> None:
  state = {"open": False, "rate": 0}
  stop = threading.Event()

  def stream() -> None:
    step = 0
    while not stop.is_set():
      if state["open"] and state["rate"] == bitrate:
        step += 1
        frames = [
          f"t4448{step & 0xFF:02X}00000000000000",
          f"t1148{(step // 10) & 0xFF:02X}11223344556677",
        ]
        if step % 5 == 0:
          # Changing TBT payload so the app logs mcan_frame payload changes.
          dist = (500 - (step // 5) % 50 * 10) & 0xFFFF
          frames.append(f"t4BB8{0x10 + (step // 50) % 4:02X}{(step // 250) % 8:02X}{dist >> 8:02X}{dist & 0xFF:02X}00000000")
        for f in frames:
          conn.sendall((f + "\r").encode())
      time.sleep(0.01)

  threading.Thread(target=stream, daemon=True).start()
  buf = b""
  try:
    while True:
      data = conn.recv(1024)
      if not data:
        break
      buf += data
      while b"\r" in buf:
        cmd, buf = buf.split(b"\r", 1)
        c = cmd.decode(errors="replace")
        print("cmd", repr(c))
        if not c:
          continue
        if c[0] in "tTrR":
          print("!!! DATA FRAME SENT BY APP — listen-only violated:", c)
          conn.sendall(b"\x07")
        elif c[0] == "S" and c[1:].isdigit() and int(c[1:]) in CODES:
          state["rate"] = CODES[int(c[1:])]
          conn.sendall(b"\r")
        elif c == "L":
          if reject_listen:
            conn.sendall(b"\x07")
          else:
            state["open"] = True
            conn.sendall(b"\r")
        elif c == "O":
          state["open"] = True
          conn.sendall(b"\r")
        elif c == "C":
          state["open"] = False
          conn.sendall(b"\r")
        else:
          conn.sendall(b"\r")
  finally:
    stop.set()
    conn.close()


def main() -> None:
  ap = argparse.ArgumentParser()
  ap.add_argument("--port", type=int, default=3333)
  ap.add_argument("--bitrate", type=int, default=100_000)
  ap.add_argument("--reject-listen", action="store_true")
  args = ap.parse_args()
  srv = socket.socket()
  srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
  srv.bind(("0.0.0.0", args.port))
  srv.listen(1)
  print(f"fake slcan on tcp/{args.port}, frames at {args.bitrate} bit/s")
  while True:
    conn, addr = srv.accept()
    print("client", addr)
    threading.Thread(target=serve, args=(conn, args.bitrate, args.reject_listen), daemon=True).start()


if __name__ == "__main__":
  main()
