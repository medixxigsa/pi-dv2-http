# medixxi-pi-dv2-http

Lee la consola **Davis Vantage Pro2 / Vantage Vue** conectada por USB a una
Raspberry Pi (datalogger WeatherLink USB o serie + adaptador USB) y envía los
datos meteorológicos por HTTP POST en JSON al destino que configures.

## Cómo funciona

1. Abre el puerto serie (19200 baudios), despierta la consola y pide paquetes `LOOP`
   (la consola manda uno cada ~2 s). Comprueba el CRC de cada paquete.
2. Lee de la EEPROM de la consola qué pluviómetro tiene (0.2 mm / 0.1 mm / 0.01").
3. Cada `interval` segundos envía la última lectura convertida a unidades métricas,
   con la **racha máxima** de viento vista en ese intervalo y el punto de rocío calculado.
4. Si el destino no responde, guarda las lecturas en una cola en memoria y reintenta
   (5 s, 10 s, 20 s… hasta 5 min). Si se desconecta el USB, reconecta solo.

## Instalación en la Raspberry

```bash
git clone https://github.com/medixxigsa/pi-dv2-http.git
```
```bash
cd pi-dv2-http && sudo ./install.sh
```

Busca el puerto de la consola (la ruta de `by-id` no cambia al reiniciar):

```bash
ls -l /dev/serial/by-id/
```

Edita la configuración (puerto, URL de destino, token…):

```bash
sudo nano /etc/medixxi-pi-dv2-http/config.ini
```

Prueba sin enviar nada (muestra el JSON por pantalla):

```bash
sudo -u medixxi python3 /opt/medixxi-pi-dv2-http/medixxi_pi_dv2_http.py -c /etc/medixxi-pi-dv2-http/config.ini --once --dry-run
```

Arranca el servicio y mira el log:

```bash
sudo systemctl start medixxi-pi-dv2-http
```
```bash
journalctl -u medixxi-pi-dv2-http -f
```

## Arranque automático al encender la Raspberry

`install.sh` instala el servicio systemd `medixxi-pi-dv2-http` y lo activa (`systemctl enable`),
así que arranca solo en cada encendido, espera a tener red y hora sincronizada, y se
reinicia solo si falla. Si la consola aún no está lista, el script reintenta hasta encontrarla.

| Acción | Comando |
|---|---|
| ¿Arrancará al iniciar? | `systemctl is-enabled medixxi-pi-dv2-http` |
| Activar / desactivar el arranque | `sudo systemctl enable medixxi-pi-dv2-http` / `sudo systemctl disable medixxi-pi-dv2-http` |
| Arrancar / parar ahora | `sudo systemctl start medixxi-pi-dv2-http` / `sudo systemctl stop medixxi-pi-dv2-http` |
| Estado | `systemctl status medixxi-pi-dv2-http` |
| Log en directo / desde el último arranque | `journalctl -u medixxi-pi-dv2-http -f` / `journalctl -u medixxi-pi-dv2-http -b` |

## Fichero de configuración

Todo se configura en `/etc/medixxi-pi-dv2-http/config.ini` (plantilla: `config.example.ini`).
Si no existe, el script busca `config.ini` en su misma carpeta, o se le indica con `-c`.

| Sección | Clave | Para qué sirve |
|---|---|---|
| `[estacion]` | `id` | Identificador de la estación (`auto` = serie de la Raspberry Pi) |
| | `nombre`, `ubicacion` | Texto descriptivo |
| | `latitud`, `longitud`, `altitud` | Coordenadas (grados decimales) y altitud (m). Vacío = las de la consola |
| | `zona_horaria` | Para `local_time`, p.ej. `Europe/Madrid` |
| | `unidades` | `metric` o `imperial` |
| | `pluviometro` | `auto`, `0.2mm`, `0.1mm` o `0.01in` |
| `[consola]` | `puerto`, `velocidad` | Puerto USB de la consola y baudios (19200) |
| `[destino]` | `url` | Dirección donde se hace el POST |
| | `token` | Se envía como `Authorization: Bearer <token>` |
| | `intervalo` | Segundos entre envíos |
| | `timeout`, `max_cola`, `verificar_tls` | Ajustes de la conexión |
| `[cabeceras]` | *cualquiera* | Cabeceras HTTP extra (`X-Api-Key = ...`) |

Al arrancar se valida el fichero; si algo está mal, el log dice qué clave falla
(p.ej. `[estacion] latitud = 123.0 fuera de rango`). Tras cambiarlo:
`sudo systemctl restart medixxi-pi-dv2-http`.

## Ejemplo de lo que se envía (POST, `Content-Type: application/json`)

```json
{
  "id": "estacion-01",
  "station": {
    "name": "Estación meteorológica",
    "location": "Reus (Tarragona)",
    "latitude": 41.1, "longitude": 1.2, "elevation_m": 128,
    "timezone": "Europe/Madrid",
    "id_source": "config",
    "console": {"model": "Vantage Pro/Pro2", "firmware": "3.15", "firmware_date": "Jan 10 2020",
                "latitude": 41.1, "longitude": 1.2, "elevation_m": 25}
  },
  "timestamp": "2026-11-16T08:55:21+00:00",
  "local_time": "2026-11-16T09:55:21+01:00",
  "units": {"temp": "C", "pressure": "hPa", "wind": "km/h", "rain": "mm", "rain_rate": "mm/h"},
  "data": {
    "temp_out": 15.1, "temp_in": 21.9, "dewpoint": 11.2, "humidity_out": 78, "humidity_in": 45,
    "barometer": 1016.3, "bar_trend": "rising_slowly",
    "wind_speed": 16.1, "wind_avg10": 9.7, "wind_gust": 20.9, "wind_dir": 214,
    "rain_rate": 0.0, "rain_day": 2.4, "rain_storm": 2.4, "rain_month": 28.0, "rain_year": 460.0,
    "uv_index": 2.5, "solar_radiation": 410, "et_day": 2.16,
    "console_battery_v": 4.69, "transmitter_battery_low": [false, "..."],
    "sunrise": "07:44", "sunset": "17:58"
  }
}
```

### El campo `id`

La consola Davis **no tiene un número de serie** que se pueda leer por el puerto serie,
así que el `id` se elige en este orden (`id_source` indica cuál se ha usado):

1. `[estacion] id` del config.ini, si no es `auto` → `config`
2. Número de serie de la Raspberry Pi (único por placa) → `rpi-<serie>` / `raspberry-pi-serial`
3. `/etc/machine-id` → `host-<12 caracteres>` / `machine-id`
4. Nombre del host → `hostname`

En `station` van los datos del config.ini; si las coordenadas o la altitud están vacías
se toman de la consola. `station.console` contiene lo que da la propia consola
(modelo, firmware y su ubicación configurada).

Los sensores que no existen (UV, solar, temperaturas extra…) llegan como `null`.
Si el servidor responde 4xx (salvo 408/429) la lectura se descarta; con 5xx o sin
conexión se reintenta.

## Probar sin estación

`tools/simulator.py` crea una consola falsa en un pseudo-terminal:

```bash
python3 tools/simulator.py
```

Pon la ruta que muestra (`/dev/pts/N`) en `[consola] puerto` y ejecuta `medixxi_pi_dv2_http.py` con `--dry-run`.

## Problemas habituales

- **Permiso denegado en /dev/ttyUSB0**: el usuario tiene que estar en el grupo `dialout`.
- **"La consola no responde"**: comprueba que el datalogger está bien insertado en la
  consola, y que nada más (WeeWX, WeatherLink…) tiene el puerto abierto.
- **Lluvia con valores raros**: fija `pluviometro` a mano en lugar de `auto`.
