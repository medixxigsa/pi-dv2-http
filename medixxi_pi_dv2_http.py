#!/usr/bin/env python3
"""
medixxi-pi-dv2-http: lee datos de una consola Davis Vantage (Pro2 / Vue) conectada por
USB (datalogger WeatherLink USB o serie) y los envía por HTTP a un destino.

Protocolo: "Vantage Serial Communication Reference Manual" de Davis
(comandos de despertar, LOOP y EEBRD).

Dependencias: python3 + pyserial (apt install python3-serial)
"""

import argparse
import configparser
import json
import logging
import math
import queue
import os
import signal
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

import serial

LOG = logging.getLogger("medixxi-pi-dv2-http")

ACK = b"\x06"
LOOP_SIZE = 99

# Tamaño del "click" del pluviómetro en mm según el tipo configurado en la consola
RAIN_COLLECTORS_MM = {
    "0.01in": 0.254,
    "0.2mm": 0.2,
    "0.1mm": 0.1,
}

STATION_TYPES = {
    0: "Wizard III", 1: "Wizard II", 2: "Monitor", 3: "Perception",
    4: "GroWeather", 5: "Energy Enviromonitor", 6: "Health Enviromonitor",
    16: "Vantage Pro/Pro2", 17: "Vantage Vue",
}

BAR_TREND = {
    -60: "falling_rapidly",
    -20: "falling_slowly",
    0: "steady",
    20: "rising_slowly",
    60: "rising_rapidly",
}


# --------------------------------------------------------------------------
# CRC-CCITT (XMODEM) usado por Davis. El CRC de un paquete completo
# (datos + 2 bytes de CRC) debe dar 0.
# --------------------------------------------------------------------------
def _crc_table():
    table = []
    for i in range(256):
        crc = i << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) if crc & 0x8000 else (crc << 1)
        table.append(crc & 0xFFFF)
    return table


_CRC_TABLE = _crc_table()


def crc16(data: bytes) -> int:
    crc = 0
    for b in data:
        crc = (_CRC_TABLE[((crc >> 8) ^ b) & 0xFF] ^ (crc << 8)) & 0xFFFF
    return crc


# --------------------------------------------------------------------------
# Comunicación con la consola
# --------------------------------------------------------------------------
class DavisError(Exception):
    pass


class DavisConsole:
    def __init__(self, port: str, baudrate: int = 19200, timeout: float = 3.0):
        self.port = port
        self.baudrate = baudrate
        self.timeout = timeout
        self.ser = None

    def open(self):
        LOG.info("Abriendo %s a %d baudios", self.port, self.baudrate)
        self.ser = serial.Serial(self.port, self.baudrate, timeout=self.timeout)

    def close(self):
        if self.ser:
            try:
                self.ser.close()
            except Exception:
                pass
            self.ser = None

    def wakeup(self, tries: int = 3):
        """Despierta la consola: envía '\\n' y espera '\\n\\r'."""
        for i in range(tries):
            self.ser.reset_input_buffer()
            self.ser.write(b"\n")
            self.ser.flush()
            old = self.ser.timeout
            self.ser.timeout = 1.2
            resp = self.ser.read(2)
            self.ser.timeout = old
            if resp == b"\n\r":
                return
            LOG.debug("Wakeup intento %d sin respuesta (%r)", i + 1, resp)
        raise DavisError("La consola no responde al despertar")

    def _wait_ack(self, timeout: float = 2.0):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            b = self.ser.read(1)
            if b == ACK:
                return
            if b in (b"!", b"\x21"):
                raise DavisError("La consola rechazó el comando (NAK)")
        raise DavisError("No se recibió ACK de la consola")

    def read_eeprom(self, address: int, length: int) -> bytes:
        self.wakeup()
        self.ser.write(f"EEBRD {address:02X} {length:02X}\n".encode())
        self._wait_ack()
        data = self.ser.read(length + 2)
        if len(data) != length + 2 or crc16(data) != 0:
            raise DavisError("Error de CRC leyendo EEPROM")
        return data[:length]

    def read_station_type(self):
        """WRD 0x12 0x4D -> tipo de estación (16 = Vantage Pro/Pro2, 17 = Vantage Vue)."""
        self.wakeup()
        self.ser.write(b"WRD\x12\x4d\n")
        self._wait_ack()
        b = self.ser.read(1)
        if not b:
            raise DavisError("Sin respuesta a WRD")
        return STATION_TYPES.get(b[0], f"desconocido ({b[0]})")

    def read_text(self, cmd: str):
        """Comandos de texto (NVER, VER): respuesta '\\n\\rOK\\n\\r<valor>\\n\\r'."""
        self.wakeup()
        self.ser.write(cmd.encode() + b"\n")
        old = self.ser.timeout
        self.ser.timeout = 1.0
        resp = self.ser.read(64)
        self.ser.timeout = old
        lines = [x for x in resp.decode("ascii", "replace").replace("\r", "").split("\n") if x]
        if len(lines) >= 2 and lines[0] == "OK":
            return lines[1].strip()
        raise DavisError(f"Respuesta inesperada a {cmd}: {resp!r}")

    def read_station_info(self) -> dict:
        """Información que la consola puede dar sobre sí misma. La consola Davis
        no tiene número de serie legible por el protocolo serie."""
        info = {}
        for key, fn in (
            ("model", self.read_station_type),
            ("firmware", lambda: self.read_text("NVER")),
            ("firmware_date", lambda: self.read_text("VER")),
        ):
            try:
                info[key] = fn()
            except DavisError as e:
                LOG.debug("No se pudo leer %s: %s", key, e)
        try:
            ee = self.read_eeprom(0x0B, 6)   # latitud, longitud (0.1°), altitud (pies)
            lat, lon, elev = (_s16(ee, 0) / 10.0, _s16(ee, 2) / 10.0, _s16(ee, 4))
            info["latitude"] = lat
            info["longitude"] = lon
            info["elevation_m"] = round(elev * 0.3048)
        except DavisError as e:
            LOG.debug("No se pudo leer la ubicación: %s", e)
        try:
            from serial.tools import list_ports
            real = os.path.realpath(self.port)
            for pinfo in list_ports.comports():
                if os.path.realpath(pinfo.device) == real and pinfo.serial_number:
                    info["usb_serial"] = pinfo.serial_number
        except Exception:
            pass
        return info

    def detect_rain_collector(self) -> str:
        """Lee el tipo de pluviómetro configurado en la consola (EEPROM 0x2B)."""
        setup = self.read_eeprom(0x2B, 1)[0]
        kind = (setup >> 4) & 0x03
        return {0: "0.01in", 1: "0.2mm", 2: "0.1mm"}.get(kind, "0.01in")

    def loop(self, count: int):
        """Generador de paquetes LOOP crudos (99 bytes, CRC verificado)."""
        self.wakeup()
        self.ser.write(f"LOOP {count}\n".encode())
        self._wait_ack()
        for _ in range(count):
            pkt = self.ser.read(LOOP_SIZE)
            if len(pkt) != LOOP_SIZE:
                raise DavisError(f"Paquete LOOP incompleto ({len(pkt)} bytes)")
            if not pkt.startswith(b"LOO"):
                raise DavisError("Cabecera LOOP inválida (desincronizado)")
            if crc16(pkt) != 0:
                raise DavisError("CRC inválido en paquete LOOP")
            yield pkt

    def cancel(self):
        """Interrumpe un LOOP en curso."""
        try:
            self.ser.write(b"\n")
            time.sleep(0.5)
            self.ser.reset_input_buffer()
        except Exception:
            pass


# --------------------------------------------------------------------------
# Decodificación del paquete LOOP (tipo 0, "LOOP1")
# --------------------------------------------------------------------------
def _u8(p, o):
    return p[o]


def _s8(p, o):
    v = p[o]
    return v - 256 if v > 127 else v


def _u16(p, o):
    return p[o] | (p[o + 1] << 8)


def _s16(p, o):
    v = _u16(p, o)
    return v - 65536 if v > 32767 else v


def f_to_c(f):
    return (f - 32.0) * 5.0 / 9.0


def dewpoint_c(t_c, rh):
    if t_c is None or not rh:
        return None
    a, b = 17.62, 243.12
    g = math.log(rh / 100.0) + a * t_c / (b + t_c)
    return b * g / (a - g)


def _hhmm(v):
    if v in (0xFFFF, 32767) or v > 2359:
        return None
    return f"{v // 100:02d}:{v % 100:02d}"


def decode_loop(p: bytes, rain_click_mm: float) -> dict:
    """Devuelve los valores en unidades imperiales originales de la consola
    (temperaturas °F, presión inHg, viento mph, lluvia en clicks) normalizados,
    con None cuando el sensor no da datos."""
    d = {}

    bt = _s8(p, 3)
    d["bar_trend"] = BAR_TREND.get(bt) if p[3] != ord("P") else None

    bar = _u16(p, 7)
    d["barometer_inhg"] = bar / 1000.0 if bar else None

    t = _s16(p, 9)
    d["temp_in_f"] = t / 10.0 if t != 32767 else None
    h = _u8(p, 11)
    d["humidity_in"] = h if h != 255 else None

    t = _s16(p, 12)
    d["temp_out_f"] = t / 10.0 if t != 32767 else None
    h = _u8(p, 33)
    d["humidity_out"] = h if h != 255 else None

    ws = _u8(p, 14)
    d["wind_speed_mph"] = ws if ws != 255 else None
    ws10 = _u8(p, 15)
    d["wind_avg10_mph"] = ws10 if ws10 != 255 else None
    wd = _u16(p, 16)
    d["wind_dir"] = (wd % 360) if 0 < wd <= 360 else None

    d["extra_temps_f"] = [(p[18 + i] - 90) if p[18 + i] != 255 else None for i in range(7)]
    d["soil_temps_f"] = [(p[25 + i] - 90) if p[25 + i] != 255 else None for i in range(4)]
    d["leaf_temps_f"] = [(p[29 + i] - 90) if p[29 + i] != 255 else None for i in range(4)]
    d["extra_humidities"] = [p[34 + i] if p[34 + i] != 255 else None for i in range(7)]

    rr = _u16(p, 41)
    d["rain_rate_mm_h"] = rr * rain_click_mm if rr != 65535 else None

    uv = _u8(p, 43)
    d["uv_index"] = uv / 10.0 if uv != 255 else None
    sr = _u16(p, 44)
    d["solar_radiation"] = sr if sr not in (32767, 65535) else None

    d["rain_storm_mm"] = _u16(p, 46) * rain_click_mm
    d["rain_day_mm"] = _u16(p, 50) * rain_click_mm
    d["rain_month_mm"] = _u16(p, 52) * rain_click_mm
    d["rain_year_mm"] = _u16(p, 54) * rain_click_mm

    d["et_day_in"] = _u16(p, 56) / 1000.0
    d["et_month_in"] = _u16(p, 58) / 100.0
    d["et_year_in"] = _u16(p, 60) / 100.0

    d["soil_moistures"] = [p[62 + i] if p[62 + i] != 255 else None for i in range(4)]
    d["leaf_wetnesses"] = [p[66 + i] if p[66 + i] != 255 else None for i in range(4)]

    d["transmitter_battery_low"] = [bool(p[86] & (1 << i)) for i in range(8)]
    d["console_battery_v"] = round(_u16(p, 87) * 300 / 512 / 100.0, 2)
    d["forecast_icons"] = _u8(p, 89)
    d["forecast_rule"] = _u8(p, 90)
    d["sunrise"] = _hhmm(_u16(p, 91))
    d["sunset"] = _hhmm(_u16(p, 93))
    return d


def to_output(raw: dict, gust_mph, units: str) -> dict:
    """Convierte a las unidades de salida ('metric' o 'imperial')."""
    def r(v, n=1):
        return round(v, n) if v is not None else None

    def lst(values, fn):
        return [fn(v) if v is not None else None for v in values]

    metric = units == "metric"
    t_out_c = f_to_c(raw["temp_out_f"]) if raw["temp_out_f"] is not None else None
    dp_c = dewpoint_c(t_out_c, raw["humidity_out"])

    if metric:
        temp = lambda f: r(f_to_c(f))                    # °C
        wind = lambda mph: r(mph * 1.609344)             # km/h
        out = {
            "temp_out": temp(raw["temp_out_f"]) if raw["temp_out_f"] is not None else None,
            "temp_in": temp(raw["temp_in_f"]) if raw["temp_in_f"] is not None else None,
            "dewpoint": r(dp_c),
            "barometer": r(raw["barometer_inhg"] * 33.8639) if raw["barometer_inhg"] else None,  # hPa
            "wind_speed": wind(raw["wind_speed_mph"]) if raw["wind_speed_mph"] is not None else None,
            "wind_avg10": wind(raw["wind_avg10_mph"]) if raw["wind_avg10_mph"] is not None else None,
            "wind_gust": wind(gust_mph) if gust_mph is not None else None,
            "rain_rate": r(raw["rain_rate_mm_h"]),       # mm/h
            "rain_day": r(raw["rain_day_mm"]),           # mm
            "rain_storm": r(raw["rain_storm_mm"]),
            "rain_month": r(raw["rain_month_mm"]),
            "rain_year": r(raw["rain_year_mm"]),
            "et_day": r(raw["et_day_in"] * 25.4, 2),     # mm
            "et_month": r(raw["et_month_in"] * 25.4),
            "et_year": r(raw["et_year_in"] * 25.4),
            "extra_temps": lst(raw["extra_temps_f"], temp),
            "soil_temps": lst(raw["soil_temps_f"], temp),
            "leaf_temps": lst(raw["leaf_temps_f"], temp),
        }
        unit_info = {"temp": "C", "pressure": "hPa", "wind": "km/h", "rain": "mm", "rain_rate": "mm/h"}
    else:
        mm_in = lambda v: r(v / 25.4, 2) if v is not None else None
        dp_f = dp_c * 9 / 5 + 32 if dp_c is not None else None
        out = {
            "temp_out": raw["temp_out_f"],
            "temp_in": raw["temp_in_f"],
            "dewpoint": r(dp_f),
            "barometer": raw["barometer_inhg"],
            "wind_speed": raw["wind_speed_mph"],
            "wind_avg10": raw["wind_avg10_mph"],
            "wind_gust": gust_mph,
            "rain_rate": mm_in(raw["rain_rate_mm_h"]),
            "rain_day": mm_in(raw["rain_day_mm"]),
            "rain_storm": mm_in(raw["rain_storm_mm"]),
            "rain_month": mm_in(raw["rain_month_mm"]),
            "rain_year": mm_in(raw["rain_year_mm"]),
            "et_day": raw["et_day_in"],
            "et_month": raw["et_month_in"],
            "et_year": raw["et_year_in"],
            "extra_temps": raw["extra_temps_f"],
            "soil_temps": raw["soil_temps_f"],
            "leaf_temps": raw["leaf_temps_f"],
        }
        unit_info = {"temp": "F", "pressure": "inHg", "wind": "mph", "rain": "in", "rain_rate": "in/h"}

    out.update({
        "humidity_out": raw["humidity_out"],
        "humidity_in": raw["humidity_in"],
        "wind_dir": raw["wind_dir"],
        "bar_trend": raw["bar_trend"],
        "uv_index": raw["uv_index"],
        "solar_radiation": raw["solar_radiation"],   # W/m²
        "extra_humidities": raw["extra_humidities"],
        "soil_moistures": raw["soil_moistures"],     # cb
        "leaf_wetnesses": raw["leaf_wetnesses"],     # 0-15
        "console_battery_v": raw["console_battery_v"],
        "transmitter_battery_low": raw["transmitter_battery_low"],
        "forecast_icons": raw["forecast_icons"],
        "forecast_rule": raw["forecast_rule"],
        "sunrise": raw["sunrise"],
        "sunset": raw["sunset"],
    })
    return out, unit_info


# --------------------------------------------------------------------------
# Configuración
# --------------------------------------------------------------------------
class ConfigError(Exception):
    pass


def _access_report(path: str) -> str:
    """Explica quién es el dueño del fichero y con qué usuario/grupos se ejecuta el proceso."""
    import grp
    import pwd
    import stat

    def uname(uid):
        try:
            return pwd.getpwuid(uid).pw_name
        except KeyError:
            return str(uid)

    def gname(gid):
        try:
            return grp.getgrgid(gid).gr_name
        except KeyError:
            return str(gid)

    parts = []
    for p in (os.path.dirname(path) or ".", path):
        try:
            st = os.stat(p)
            parts.append(f"{p}: {uname(st.st_uid)}:{gname(st.st_gid)} {stat.filemode(st.st_mode)}")
        except OSError as e:
            parts.append(f"{p}: {e.strerror}")
    groups = ",".join(sorted({gname(g) for g in os.getgroups()} | {gname(os.getgid())}))
    parts.append(f"proceso: usuario={uname(os.getuid())} grupos={groups}")
    return " | ".join(parts)


class Settings:
    """Lee y valida config.ini (secciones [estacion], [consola], [destino], [cabeceras])."""

    def __init__(self, path: str, need_url: bool = True):
        cfg = configparser.ConfigParser()
        cfg.optionxform = str  # respetar mayúsculas en [cabeceras]
        try:
            with open(path, encoding="utf-8") as f:
                cfg.read_file(f, source=path)
        except FileNotFoundError:
            raise ConfigError(f"No existe el fichero de configuración {path}")
        except PermissionError:
            raise ConfigError(f"Sin permiso para leer {path}. {_access_report(path)}")
        except configparser.Error as e:
            raise ConfigError(f"Formato incorrecto en {path}: {e}")
        self.path = path

        def get(sec, key, default=""):
            return cfg.get(sec, key, fallback=default).strip()

        def num(sec, key, kind=float, default=None, lo=None, hi=None):
            v = get(sec, key)
            if v == "":
                return default
            try:
                v = kind(v.replace(",", "."))
            except ValueError:
                raise ConfigError(f"[{sec}] {key} = {v!r} no es un número válido")
            if (lo is not None and v < lo) or (hi is not None and v > hi):
                raise ConfigError(f"[{sec}] {key} = {v} fuera de rango ({lo}..{hi})")
            return v

        # [estacion]
        self.id = get("estacion", "id", "auto")
        self.name = get("estacion", "nombre") or None
        self.location = get("estacion", "ubicacion") or None
        self.latitude = num("estacion", "latitud", lo=-90, hi=90)
        self.longitude = num("estacion", "longitud", lo=-180, hi=180)
        self.altitude = num("estacion", "altitud", lo=-500, hi=9000)
        self.timezone = get("estacion", "zona_horaria", "UTC") or "UTC"
        try:
            from zoneinfo import ZoneInfo
            self.tz = ZoneInfo(self.timezone)
        except Exception:
            raise ConfigError(f"[estacion] zona_horaria = {self.timezone!r} no es válida")
        self.units = get("estacion", "unidades", "metric").lower()
        if self.units not in ("metric", "imperial"):
            raise ConfigError("[estacion] unidades debe ser metric o imperial")
        self.rain_collector = get("estacion", "pluviometro", "auto").lower()
        if self.rain_collector != "auto" and self.rain_collector not in RAIN_COLLECTORS_MM:
            raise ConfigError("[estacion] pluviometro debe ser auto, 0.2mm, 0.1mm o 0.01in")

        # [consola]
        self.port = get("consola", "puerto", "/dev/ttyUSB0")
        self.baudrate = num("consola", "velocidad", int, 19200)

        # [destino]
        self.url = get("destino", "url")
        if need_url and not self.url.lower().startswith(("http://", "https://")):
            raise ConfigError("[destino] url debe empezar por http:// o https://")
        self.token = get("destino", "token")
        self.interval = num("destino", "intervalo", int, 60, lo=2)
        self.timeout = num("destino", "timeout", float, 10, lo=1)
        self.max_queue = num("destino", "max_cola", int, 1000, lo=1)
        try:
            self.verify_tls = cfg.getboolean("destino", "verificar_tls", fallback=True)
        except ValueError:
            raise ConfigError("[destino] verificar_tls debe ser true o false")

        # [cabeceras]
        self.headers = dict(cfg.items("cabeceras")) if cfg.has_section("cabeceras") else {}
        if self.token:
            self.headers["Authorization"] = f"Bearer {self.token}"


class HttpSender(threading.Thread):
    """Envía los datos por POST en JSON desde un hilo aparte, con cola y reintentos."""

    def __init__(self, settings: Settings, stop_event):
        super().__init__(daemon=True, name="http-sender")
        self.url = settings.url
        self.timeout = settings.timeout
        self.max_queue = settings.max_queue
        self.headers = {
            "User-Agent": "medixxi-pi-dv2-http/1.0",
            "Content-Type": "application/json",
            **settings.headers,
        }
        self.ssl_ctx = None
        if not settings.verify_tls:
            import ssl
            self.ssl_ctx = ssl._create_unverified_context()
        self.q = queue.Queue()
        self.pending = []
        self.stop_event = stop_event

    def submit(self, payload: dict):
        self.q.put(payload)

    def _send(self, payload) -> bool:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(self.url, data=body, headers=self.headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout, context=self.ssl_ctx) as resp:
                LOG.info("Enviado (%s) -> HTTP %d", payload["timestamp"], resp.status)
                return True
        except urllib.error.HTTPError as e:
            LOG.warning("El destino respondió HTTP %d: %s", e.code, e.reason)
            # 4xx (excepto 408/429) no se reintenta: el dato es rechazado
            if 400 <= e.code < 500 and e.code not in (408, 429):
                return True
            return False
        except Exception as e:
            LOG.warning("Error enviando: %s", e)
            return False

    def run(self):
        backoff = 5
        while not self.stop_event.is_set():
            try:
                while True:
                    self.pending.append(self.q.get_nowait())
            except queue.Empty:
                pass
            if len(self.pending) > self.max_queue:
                dropped = len(self.pending) - self.max_queue
                self.pending = self.pending[dropped:]
                LOG.warning("Cola llena: descartadas %d lecturas antiguas", dropped)
            if not self.pending:
                try:
                    self.pending.append(self.q.get(timeout=1))
                except queue.Empty:
                    continue
            if self._send(self.pending[0]):
                self.pending.pop(0)
                backoff = 5
            else:
                LOG.info("Reintento en %ds (%d en cola)", backoff, len(self.pending))
                self.stop_event.wait(backoff)
                backoff = min(backoff * 2, 300)


# --------------------------------------------------------------------------
# Bucle principal
# --------------------------------------------------------------------------
def _read_first(*paths):
    for path in paths:
        try:
            with open(path, "rb") as f:
                v = f.read().replace(b"\x00", b"").decode().strip()
            if v:
                return v
        except OSError:
            pass
    return None


def resolve_station_id(settings) -> (str, str):
    """Devuelve (id, origen). Orden: config -> serie Raspberry Pi -> machine-id -> hostname."""
    cid = settings.id
    if cid and cid.lower() != "auto":
        return cid, "config"
    serial_no = _read_first("/sys/firmware/devicetree/base/serial-number")
    if not serial_no:
        try:
            with open("/proc/cpuinfo") as f:
                for line in f:
                    if line.lower().startswith("serial"):
                        serial_no = line.split(":", 1)[1].strip()
        except OSError:
            pass
    if serial_no and serial_no.strip("0"):
        return f"rpi-{serial_no.lstrip('0') or serial_no}", "raspberry-pi-serial"
    mid = _read_first("/etc/machine-id")
    if mid:
        return f"host-{mid[:12]}", "machine-id"
    return socket.gethostname(), "hostname"


def build_station_info(settings, id_source, console_info) -> dict:
    """Bloque "station": datos del config.ini; lo que falte se completa con la consola."""
    def pick(cfg_value, key):
        return cfg_value if cfg_value is not None else console_info.get(key)

    info = {
        "name": settings.name,
        "location": settings.location,
        "latitude": pick(settings.latitude, "latitude"),
        "longitude": pick(settings.longitude, "longitude"),
        "elevation_m": pick(settings.altitude, "elevation_m"),
        "timezone": settings.timezone,
        "id_source": id_source,
        "console": console_info,
    }
    return {k: v for k, v in info.items() if v is not None}


def build_payload(settings, raw, gust_mph, station_id, station_info):
    data, unit_info = to_output(raw, gust_mph, settings.units)
    now = datetime.now(timezone.utc)
    return {
        "id": station_id,
        "station": station_info,
        "timestamp": now.isoformat(timespec="seconds"),
        "local_time": now.astimezone(settings.tz).isoformat(timespec="seconds"),
        "units": unit_info,
        "data": data,
    }


def run(settings, once=False, dry_run=False):
    stop = threading.Event()

    def _sig(*_):
        LOG.info("Parando...")
        stop.set()

    signal.signal(signal.SIGTERM, _sig)
    signal.signal(signal.SIGINT, _sig)

    interval = settings.interval
    sender = None
    if not dry_run:
        sender = HttpSender(settings, stop)
        sender.start()
        LOG.info("Destino: POST %s cada %ds", settings.url, interval)

    console = DavisConsole(settings.port, settings.baudrate)
    rain_cfg = settings.rain_collector
    rain_mm = None
    station_info = None
    station_id, id_source = resolve_station_id(settings)
    LOG.info("Identificador de estación: %s (origen: %s)", station_id, id_source)
    retry = 5

    while not stop.is_set():
        try:
            console.open()
            if station_info is None:
                console_info = console.read_station_info()
                LOG.info("Consola: %s", console_info)
                station_info = build_station_info(settings, id_source, console_info)
            if rain_mm is None:
                if rain_cfg == "auto":
                    try:
                        kind = console.detect_rain_collector()
                        LOG.info("Pluviómetro detectado en la consola: %s", kind)
                    except DavisError as e:
                        kind = "0.2mm"
                        LOG.warning("No se pudo detectar el pluviómetro (%s); uso %s", e, kind)
                else:
                    kind = rain_cfg
                rain_mm = RAIN_COLLECTORS_MM[kind]

            last_sent = 0.0
            gust = None
            retry = 5
            while not stop.is_set():
                # La consola envía un LOOP cada ~2 s; pedimos tandas largas
                for pkt in console.loop(200):
                    raw = decode_loop(pkt, rain_mm)
                    ws = raw["wind_speed_mph"]
                    if ws is not None:
                        gust = ws if gust is None else max(gust, ws)
                    now = time.monotonic()
                    if once or now - last_sent >= interval:
                        payload = build_payload(settings, raw, gust, station_id, station_info)
                        gust = None
                        last_sent = now
                        if dry_run:
                            print(json.dumps(payload, indent=2, ensure_ascii=False), flush=True)
                        else:
                            sender.submit(payload)
                        if once:
                            stop.set()
                    if stop.is_set():
                        console.cancel()
                        break
        except (DavisError, serial.SerialException, OSError) as e:
            LOG.error("Error con la consola: %s. Reintentando en %ds", e, retry)
            console.cancel()
            console.close()
            stop.wait(retry)
            retry = min(retry * 2, 120)
        finally:
            if stop.is_set():
                console.close()

    if sender and once:
        # dar tiempo a que salga el envío en modo --once
        deadline = time.monotonic() + 15
        while (sender.pending or not sender.q.empty()) and time.monotonic() < deadline:
            time.sleep(0.2)


def main():
    ap = argparse.ArgumentParser(description="Consola Davis -> HTTP")
    ap.add_argument("-c", "--config", default=None,
                    help="fichero de configuración (por defecto /etc/medixxi-pi-dv2-http/config.ini "
                         "o config.ini junto al script)")
    ap.add_argument("--once", action="store_true", help="leer una vez, enviar y salir")
    ap.add_argument("--dry-run", action="store_true", help="no enviar, mostrar JSON por pantalla")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    path = args.config
    if path is None:
        local = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.ini")
        path = "/etc/medixxi-pi-dv2-http/config.ini" if os.path.exists("/etc/medixxi-pi-dv2-http/config.ini") else local
    try:
        settings = Settings(path, need_url=not args.dry_run)
    except ConfigError as e:
        LOG.error("Configuración (%s): %s", path, e)
        sys.exit(1)
    LOG.info("Configuración: %s", path)

    run(settings, once=args.once, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
