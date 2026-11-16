#!/usr/bin/env python3
"""
Simulador de consola Davis Vantage sobre un pseudo-terminal (pty), para probar
medixxi_pi_dv2_http.py sin hardware. Muestra la ruta del puerto a usar en [consola] puerto.

    python3 tools/simulator.py
"""
import os
import pty
import random
import select
import struct
import sys
import time
import tty

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from medixxi_pi_dv2_http import crc16  # noqa: E402


def with_crc(data: bytes) -> bytes:
    return data + struct.pack(">H", crc16(data))


def make_loop() -> bytes:
    p = bytearray(97)
    p[0:3] = b"LOO"
    p[3] = 20                       # barómetro subiendo despacio
    p[4] = 0                        # tipo LOOP
    struct.pack_into("<H", p, 5, 0)
    struct.pack_into("<H", p, 7, 30012 + random.randint(-5, 5))     # inHg/1000
    struct.pack_into("<h", p, 9, 715)                               # 71.5 °F dentro
    p[11] = 45
    struct.pack_into("<h", p, 12, 590 + random.randint(-3, 3))      # ~59 °F fuera
    p[14] = random.randint(0, 15)                                   # mph
    p[15] = 6
    struct.pack_into("<H", p, 16, random.randint(180, 240))
    p[18:25] = bytes([255] * 7)
    p[25:33] = bytes([255] * 8)
    p[33] = 78
    p[34:41] = bytes([255] * 7)
    struct.pack_into("<H", p, 41, 0)                                # rain rate clicks/h
    p[43] = 25                                                      # UV 2.5
    struct.pack_into("<H", p, 44, 410)                              # W/m2
    struct.pack_into("<H", p, 46, 12)
    struct.pack_into("<H", p, 48, 0xFFFF)
    struct.pack_into("<H", p, 50, 12)                               # 12 clicks hoy
    struct.pack_into("<H", p, 52, 140)
    struct.pack_into("<H", p, 54, 2300)
    struct.pack_into("<H", p, 56, 85)
    struct.pack_into("<H", p, 58, 120)
    struct.pack_into("<H", p, 60, 3400)
    p[62:70] = bytes([255] * 8)
    p[86] = 0
    struct.pack_into("<H", p, 87, 800)                              # ~4.7 V
    p[89] = 6
    p[90] = 44
    struct.pack_into("<H", p, 91, 744)
    struct.pack_into("<H", p, 93, 1758)
    p[95:97] = b"\n\r"
    return with_crc(bytes(p))


def main():
    master, slave = pty.openpty()
    tty.setraw(slave)
    print(f"Consola simulada en: {os.ttyname(slave)}", flush=True)
    buf = b""
    loops_left = 0
    next_loop = 0.0
    while True:
        r, _, _ = select.select([master], [], [], 0.1)
        if r:
            buf += os.read(master, 1024)
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                cmd = line.strip().upper()
                if loops_left and cmd == b"":
                    loops_left = 0           # '\n' cancela el LOOP
                if cmd == b"":
                    os.write(master, b"\n\r")
                elif cmd.startswith(b"LOOP"):
                    loops_left = int(cmd.split()[1])
                    os.write(master, b"\x06")
                    next_loop = time.monotonic()
                elif cmd.startswith(b"WRD"):
                    os.write(master, b"\x06\x10")                       # Vantage Pro2
                elif cmd == b"NVER":
                    os.write(master, b"\n\rOK\n\r3.15\n\r")
                elif cmd == b"VER":
                    os.write(master, b"\n\rOK\n\rJan 10 2020\n\r")
                elif cmd.startswith(b"EEBRD 0B"):
                    # lat 41.1, lon 1.2, altitud 82 pies
                    os.write(master, b"\x06" + with_crc(struct.pack("<hhh", 411, 12, 82)))
                elif cmd.startswith(b"EEBRD 2B"):
                    os.write(master, b"\x06" + with_crc(bytes([0x10])))  # 0.2 mm
                else:
                    os.write(master, b"\x21")
        if loops_left and time.monotonic() >= next_loop:
            os.write(master, make_loop())
            loops_left -= 1
            next_loop = time.monotonic() + 2.0


if __name__ == "__main__":
    main()
