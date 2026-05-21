#!/usr/bin/env bash
set -euo pipefail

INSTALL_DIR="/opt/iot-wifi-setup"
SERVICE_NAME="iot-wifi-setup"
AP_SERVICE_NAME="iot-wifi-ap"
RUN_USER="${SUDO_USER:-${USER}}"
RUN_GROUP="$(id -gn "${RUN_USER}")"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKIP_APT=0
START_AP=0

for arg in "$@"; do
    case "${arg}" in
        --skip-apt) SKIP_APT=1 ;;
        --start-ap) START_AP=1 ;;
        *)
            echo "Unknown option: ${arg}" >&2
            echo "Usage: sudo ./install.sh [--skip-apt] [--start-ap]" >&2
            exit 2
            ;;
    esac
done

if [[ "${EUID}" -ne 0 ]]; then
    echo "Run with sudo: sudo ./install.sh" >&2
    exit 1
fi

if [[ "${SKIP_APT}" -eq 0 ]] && command -v apt-get >/dev/null 2>&1; then
    apt-get update
    apt-get install -y python3 python3-flask network-manager hostapd dnsmasq iw iproute2 iptables rfkill
fi

install -d -m 755 "${INSTALL_DIR}" "${INSTALL_DIR}/templates" "${INSTALL_DIR}/static"
install -d -m 700 /var/lib/iot-wifi-setup
install -d -m 755 /etc/iot-wifi-setup

install -m 644 "${SRC_DIR}/app.py" "${INSTALL_DIR}/app.py"
install -m 755 "${SRC_DIR}/iot_wifi_control.py" "${INSTALL_DIR}/iot_wifi_control.py"
install -m 644 "${SRC_DIR}/templates/index.html" "${INSTALL_DIR}/templates/index.html"
install -m 644 "${SRC_DIR}/static/styles.css" "${INSTALL_DIR}/static/styles.css"
install -m 644 "${SRC_DIR}/static/app.js" "${INSTALL_DIR}/static/app.js"
chown -R "${RUN_USER}:${RUN_GROUP}" "${INSTALL_DIR}"
chown -R root:root /var/lib/iot-wifi-setup /etc/iot-wifi-setup

cat >/etc/default/iot-wifi-setup <<'DEFAULTS'
IOT_WIFI_WLAN=wlan0
IOT_WIFI_AP_IFACE=uap0
IOT_WIFI_AP_SSID=HYEOKMIN_AP
IOT_WIFI_AP_PASSWORD=12345678
IOT_WIFI_AP_IP=192.168.100.1
IOT_WIFI_AP_CIDR=192.168.100.1/24
IOT_WIFI_DHCP_START=192.168.100.10
IOT_WIFI_DHCP_END=192.168.100.80
IOT_WIFI_COUNTRY=KR
IOT_WIFI_WEB_PORT=5000
IOT_WIFI_HELPER=/opt/iot-wifi-setup/iot_wifi_control.py
IOT_WIFI_STATE_DIR=/var/lib/iot-wifi-setup
DEFAULTS

cat >/etc/systemd/system/${SERVICE_NAME}.service <<SERVICE
[Unit]
Description=IoT WiFi Setup Web Portal
After=network.target NetworkManager.service
Wants=NetworkManager.service

[Service]
Type=simple
User=${RUN_USER}
Group=${RUN_GROUP}
WorkingDirectory=${INSTALL_DIR}
EnvironmentFile=-/etc/default/iot-wifi-setup
ExecStart=/usr/bin/python3 ${INSTALL_DIR}/app.py
Restart=on-failure
RestartSec=2

[Install]
WantedBy=multi-user.target
SERVICE

cat >/etc/systemd/system/${AP_SERVICE_NAME}.service <<SERVICE
[Unit]
Description=IoT WiFi Setup Access Point
After=NetworkManager.service
Wants=NetworkManager.service

[Service]
Type=oneshot
RemainAfterExit=yes
EnvironmentFile=-/etc/default/iot-wifi-setup
ExecStart=/usr/bin/python3 ${INSTALL_DIR}/iot_wifi_control.py start-ap
ExecStop=/usr/bin/python3 ${INSTALL_DIR}/iot_wifi_control.py stop-ap

[Install]
WantedBy=multi-user.target
SERVICE

cat >/etc/sudoers.d/iot-wifi-setup <<SUDOERS
Defaults:${RUN_USER} env_keep += "IOT_WIFI_WLAN IOT_WIFI_AP_IFACE IOT_WIFI_AP_SSID IOT_WIFI_AP_PASSWORD IOT_WIFI_AP_IP IOT_WIFI_AP_CIDR IOT_WIFI_DHCP_START IOT_WIFI_DHCP_END IOT_WIFI_COUNTRY IOT_WIFI_STATE_DIR IOT_WIFI_RUN_DIR IOT_WIFI_ETC_DIR"
${RUN_USER} ALL=(root) NOPASSWD: /usr/bin/python3 ${INSTALL_DIR}/iot_wifi_control.py
${RUN_USER} ALL=(root) NOPASSWD: /usr/bin/python3 ${INSTALL_DIR}/iot_wifi_control.py *
SUDOERS
chmod 440 /etc/sudoers.d/iot-wifi-setup
visudo -cf /etc/sudoers.d/iot-wifi-setup >/dev/null

cat >/usr/local/bin/iot-wifi <<'WRAPPER'
#!/usr/bin/env bash
exec /usr/bin/python3 /opt/iot-wifi-setup/iot_wifi_control.py "$@"
WRAPPER
chmod 755 /usr/local/bin/iot-wifi

cat >/etc/sysctl.d/99-iot-wifi-setup.conf <<'SYSCTL'
net.ipv4.ip_forward=1
SYSCTL
sysctl --system >/dev/null || true

systemctl daemon-reload
systemctl enable --now ${SERVICE_NAME}.service
systemctl enable ${AP_SERVICE_NAME}.service

if [[ "${START_AP}" -eq 1 ]]; then
    systemctl restart ${AP_SERVICE_NAME}.service
fi

cat <<EOF
Installed ${SERVICE_NAME}.

Web service:
  sudo systemctl status ${SERVICE_NAME}

Start setup AP:
  sudo iot-wifi start-ap
  sudo systemctl restart ${AP_SERVICE_NAME}

Phone setup URL:
  http://192.168.100.1:5000

Run hardware checks:
  sudo iot-wifi doctor
EOF
