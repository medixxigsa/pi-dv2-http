#!/bin/sh
# Instalación de medixxi-pi-dv2-http en Raspberry Pi OS / Debian. Ejecutar con sudo.
set -e
NAME=medixxi-pi-dv2-http
SVC_USER=medixxi

apt-get update
apt-get install -y python3 python3-serial

id $SVC_USER >/dev/null 2>&1 || useradd --system --no-create-home --shell /usr/sbin/nologin $SVC_USER
usermod -aG dialout $SVC_USER

# Migración desde el nombre anterior (davis-http), si estaba instalado
if [ -f /etc/systemd/system/davis-http.service ]; then
    echo ">> Encontrada instalación anterior davis-http: se desactiva"
    systemctl disable --now davis-http 2>/dev/null || true
    rm -f /etc/systemd/system/davis-http.service
    rm -rf /opt/davis-http
    if [ -f /etc/davis-http/config.ini ] && [ ! -f /etc/$NAME/config.ini ]; then
        install -d /etc/$NAME
        mv /etc/davis-http/config.ini /etc/$NAME/config.ini
        rmdir /etc/davis-http 2>/dev/null || true
        echo ">> Configuración movida a /etc/$NAME/config.ini (revisa que tenga el formato nuevo)"
    fi
fi

install -d /opt/$NAME /etc/$NAME
install -m 755 medixxi_pi_dv2_http.py /opt/$NAME/medixxi_pi_dv2_http.py
if [ ! -f /etc/$NAME/config.ini ]; then
    install -m 640 config.example.ini /etc/$NAME/config.ini
    echo ">> Edita /etc/$NAME/config.ini antes de arrancar el servicio"
fi
# Permisos: la carpeta y el config.ini deben poder leerse por el usuario del servicio
chown root:$SVC_USER /etc/$NAME /etc/$NAME/config.ini
chmod 750 /etc/$NAME
chmod 640 /etc/$NAME/config.ini

install -m 644 $NAME.service /etc/systemd/system/$NAME.service
systemctl daemon-reload
# Comprobar que el usuario del servicio puede leer la configuración
if ! runuser -u $SVC_USER -- test -r /etc/$NAME/config.ini; then
    echo "!! ATENCIÓN: $SVC_USER no puede leer /etc/$NAME/config.ini"
    ls -ld /etc/$NAME /etc/$NAME/config.ini
fi
systemctl enable systemd-time-wait-sync.service 2>/dev/null || true
systemctl enable $NAME
if systemctl is-active --quiet $NAME; then systemctl restart $NAME; fi
echo ">> Listo. Prueba:  sudo -u $SVC_USER python3 /opt/$NAME/medixxi_pi_dv2_http.py -c /etc/$NAME/config.ini --once --dry-run"
echo ">> Arranca:        sudo systemctl start $NAME && journalctl -u $NAME -f"
