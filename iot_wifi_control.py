#!/usr/bin/env python3
import argparse
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path


WLAN_IFACE = os.environ.get("IOT_WIFI_WLAN", "wlan0")
AP_IFACE = os.environ.get("IOT_WIFI_AP_IFACE", "uap0")
AP_SSID = os.environ.get("IOT_WIFI_AP_SSID", "HYEOKMIN_AP")
AP_PASSWORD = os.environ.get("IOT_WIFI_AP_PASSWORD", "12345678")
AP_IP = os.environ.get("IOT_WIFI_AP_IP", "192.168.100.1")
AP_CIDR = os.environ.get("IOT_WIFI_AP_CIDR", "192.168.100.1/24")
DHCP_START = os.environ.get("IOT_WIFI_DHCP_START", "192.168.100.10")
DHCP_END = os.environ.get("IOT_WIFI_DHCP_END", "192.168.100.80")
COUNTRY_CODE = os.environ.get("IOT_WIFI_COUNTRY", "KR")
DEFAULT_AP_CHANNEL = int(os.environ.get("IOT_WIFI_AP_CHANNEL", "6"))

STATE_DIR = Path(os.environ.get("IOT_WIFI_STATE_DIR", "/var/lib/iot-wifi-setup"))
RUN_DIR = Path(os.environ.get("IOT_WIFI_RUN_DIR", "/run/iot-wifi-setup"))
ETC_DIR = Path(os.environ.get("IOT_WIFI_ETC_DIR", "/etc/iot-wifi-setup"))

SCAN_FILE = STATE_DIR / "wifi_scan.json"
SELECTED_FILE = STATE_DIR / "selected_wifi.json"
LOG_FILE = STATE_DIR / "connect_log.txt"
HOSTAPD_CONF = ETC_DIR / "hostapd.conf"
DNSMASQ_CONF = ETC_DIR / "dnsmasq.conf"
HOSTAPD_PID = RUN_DIR / "hostapd.pid"
DNSMASQ_PID = RUN_DIR / "dnsmasq.pid"


class CommandError(RuntimeError):
    def __init__(self, message, result=None):
        super().__init__(message)
        self.result = result


def now():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def json_print(payload, code=0):
    print(json.dumps(payload, ensure_ascii=False))
    return code


def run(cmd, check=True, timeout=30):
    try:
        result = subprocess.run(
            cmd,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        result = subprocess.CompletedProcess(cmd, 127, "", str(exc))
    if check and result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        raise CommandError(f"{cmd[0]} failed: {detail}", result)
    return result


def command_exists(name):
    return shutil.which(name) is not None


def ensure_dirs():
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    ETC_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(STATE_DIR, 0o700)
    os.chmod(RUN_DIR, 0o755)
    os.chmod(ETC_DIR, 0o755)


def require_root():
    if os.geteuid() != 0:
        raise CommandError("This command must run as root. Use install.sh to enable passwordless sudo for the helper.")


def add_event(events, message, level="info"):
    entry = {"time": now(), "level": level, "message": message}
    events.append(entry)
    try:
        ensure_dirs()
        with LOG_FILE.open("a", encoding="utf-8") as fp:
            fp.write(f"[{entry['time']}] {level.upper()}: {message}\n")
    except OSError:
        pass


def ip_link_exists(interface):
    return run(["ip", "link", "show", interface], check=False).returncode == 0


def stop_pid_file(pid_file, events, label):
    if not pid_file.exists():
        return
    try:
        pid = int(pid_file.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        pid_file.unlink(missing_ok=True)
        return

    try:
        os.kill(pid, signal.SIGTERM)
        for _ in range(20):
            try:
                os.kill(pid, 0)
                time.sleep(0.1)
            except ProcessLookupError:
                break
    except ProcessLookupError:
        pass
    except PermissionError:
        add_event(events, f"Could not stop {label}: permission denied", "warning")
    finally:
        pid_file.unlink(missing_ok=True)
        add_event(events, f"Stopped {label}")


def stop_ap(events):
    stop_pid_file(HOSTAPD_PID, events, "hostapd")
    stop_pid_file(DNSMASQ_PID, events, "dnsmasq")


def ensure_ap_interface(events):
    if not ip_link_exists(AP_IFACE):
        result = run(["iw", "dev", WLAN_IFACE, "interface", "add", AP_IFACE, "type", "__ap"], check=False)
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip()
            raise CommandError(
                f"Could not create {AP_IFACE}. Your WiFi adapter may not support AP+STA mode: {detail}",
                result,
            )
        add_event(events, f"Created AP interface {AP_IFACE}")

    run(["nmcli", "device", "set", AP_IFACE, "managed", "no"], check=False)
    run(["ip", "addr", "flush", "dev", AP_IFACE], check=False)
    run(["ip", "addr", "add", AP_CIDR, "dev", AP_IFACE], check=True)
    run(["ip", "link", "set", AP_IFACE, "up"], check=True)
    add_event(events, f"{AP_IFACE} is up at {AP_CIDR}")


def freq_to_channel(freq):
    if not freq:
        return None
    freq = int(float(freq))
    if freq == 2484:
        return 14
    if 2412 <= freq <= 2472:
        return (freq - 2407) // 5
    if 5000 <= freq <= 5900:
        return (freq - 5000) // 5
    return None


def channel_to_freq(channel):
    if not channel:
        return ""
    channel = int(channel)
    if channel == 14:
        return "2484"
    if 1 <= channel <= 13:
        return str(2407 + channel * 5)
    if channel > 14:
        return str(5000 + channel * 5)
    return ""


def parse_iw_link(output):
    if "Not connected" in output:
        return {"connected": False}

    link = {"connected": True}
    for line in output.splitlines():
        line = line.strip()
        if line.startswith("SSID:"):
            link["ssid"] = line.split(":", 1)[1].strip()
        elif line.startswith("freq:"):
            link["frequency"] = line.split(":", 1)[1].strip()
            link["channel"] = freq_to_channel(link["frequency"])
        elif line.startswith("signal:"):
            link["signal"] = line.split(":", 1)[1].strip()
        elif line.startswith("Connected to"):
            parts = line.split()
            if len(parts) >= 3:
                link["bssid"] = parts[2]
    return link


def get_wlan_link():
    result = run(["iw", "dev", WLAN_IFACE, "link"], check=False)
    if result.returncode != 0:
        return {"connected": False, "error": result.stderr.strip() or result.stdout.strip()}
    return parse_iw_link(result.stdout)


def choose_ap_channel():
    link = get_wlan_link()
    channel = link.get("channel")
    if isinstance(channel, int) and 1 <= channel <= 165:
        return channel
    return DEFAULT_AP_CHANNEL


def write_hostapd_conf(channel):
    hw_mode = "g" if channel <= 14 else "a"
    extra = ""
    if channel <= 14:
        extra = "ieee80211n=1\n"
    else:
        extra = "ieee80211n=1\nieee80211ac=1\n"

    HOSTAPD_CONF.write_text(
        "\n".join(
            [
                f"interface={AP_IFACE}",
                "driver=nl80211",
                f"ssid={AP_SSID}",
                f"country_code={COUNTRY_CODE}",
                f"hw_mode={hw_mode}",
                f"channel={channel}",
                "wmm_enabled=1",
                extra.rstrip(),
                "auth_algs=1",
                "wpa=2",
                f"wpa_passphrase={AP_PASSWORD}",
                "wpa_key_mgmt=WPA-PSK",
                "rsn_pairwise=CCMP",
                "",
            ]
        ),
        encoding="utf-8",
    )


def write_dnsmasq_conf():
    DNSMASQ_CONF.write_text(
        "\n".join(
            [
                f"interface={AP_IFACE}",
                "bind-interfaces",
                "domain-needed",
                "bogus-priv",
                "port=0",
                f"dhcp-range={DHCP_START},{DHCP_END},255.255.255.0,12h",
                f"dhcp-option=option:router,{AP_IP}",
                "dhcp-option=option:dns-server,8.8.8.8,1.1.1.1",
                "",
            ]
        ),
        encoding="utf-8",
    )


def start_ap(args, events):
    channel = args.channel if getattr(args, "channel", None) else choose_ap_channel()
    ensure_dirs()
    run(["systemctl", "stop", "hostapd"], check=False)
    run(["systemctl", "stop", "dnsmasq"], check=False)
    ensure_ap_interface(events)
    write_hostapd_conf(channel)
    write_dnsmasq_conf()
    stop_ap(events)

    run(["dnsmasq", f"--conf-file={DNSMASQ_CONF}", f"--pid-file={DNSMASQ_PID}"], check=True)
    add_event(events, f"Started dnsmasq DHCP on {AP_IFACE}")
    run(["hostapd", "-B", "-P", str(HOSTAPD_PID), str(HOSTAPD_CONF)], check=True)
    add_event(events, f"Started AP {AP_SSID} on channel {channel}")
    enable_nat(events)
    return status_payload()


def ensure_rule(check_cmd, add_cmd):
    result = run(check_cmd, check=False)
    if result.returncode != 0:
        run(add_cmd, check=True)


def enable_nat(events):
    run(["sysctl", "-w", "net.ipv4.ip_forward=1"], check=False)
    ensure_rule(
        ["iptables", "-t", "nat", "-C", "POSTROUTING", "-o", WLAN_IFACE, "-j", "MASQUERADE"],
        ["iptables", "-t", "nat", "-A", "POSTROUTING", "-o", WLAN_IFACE, "-j", "MASQUERADE"],
    )
    ensure_rule(
        ["iptables", "-C", "FORWARD", "-i", AP_IFACE, "-o", WLAN_IFACE, "-j", "ACCEPT"],
        ["iptables", "-A", "FORWARD", "-i", AP_IFACE, "-o", WLAN_IFACE, "-j", "ACCEPT"],
    )
    ensure_rule(
        [
            "iptables",
            "-C",
            "FORWARD",
            "-i",
            WLAN_IFACE,
            "-o",
            AP_IFACE,
            "-m",
            "conntrack",
            "--ctstate",
            "RELATED,ESTABLISHED",
            "-j",
            "ACCEPT",
        ],
        [
            "iptables",
            "-A",
            "FORWARD",
            "-i",
            WLAN_IFACE,
            "-o",
            AP_IFACE,
            "-m",
            "conntrack",
            "--ctstate",
            "RELATED,ESTABLISHED",
            "-j",
            "ACCEPT",
        ],
    )
    add_event(events, f"Enabled NAT from {AP_IFACE} to {WLAN_IFACE}")


def parse_signal_dbm(value):
    if not value:
        return -999
    match = re.search(r"-?\d+(?:\.\d+)?", value)
    if not match:
        return -999
    return float(match.group(0))


def normalize_security(security):
    labels = []
    if "WPA3" in security:
        labels.append("WPA3")
    if "WPA2" in security or "RSN" in security:
        labels.append("WPA2")
    if "WPA" in security and "WPA2" not in labels:
        labels.append("WPA")
    if "WEP" in security:
        labels.append("WEP")
    if not labels:
        return "Open"
    return "/".join(dict.fromkeys(labels))


def save_network(networks, current):
    ssid = current.get("ssid", "")
    if not ssid:
        return
    current["security"] = normalize_security(current.get("security", ""))
    if current.get("channel") and not current.get("frequency"):
        current["frequency"] = channel_to_freq(current["channel"])
    networks.append(current.copy())


def parse_iw_scan(output):
    networks = []
    current = {}

    for raw in output.splitlines():
        line = raw.strip()
        if line.startswith("BSS "):
            save_network(networks, current)
            current = {
                "bssid": line.split()[1].split("(")[0],
                "ssid": "",
                "signal": "",
                "channel": "",
                "frequency": "",
                "security": "",
            }
        elif line.startswith("freq:"):
            current["frequency"] = line.split(":", 1)[1].strip()
            channel = freq_to_channel(current["frequency"])
            if channel:
                current["channel"] = str(channel)
        elif line.startswith("signal:"):
            current["signal"] = line.split(":", 1)[1].strip()
        elif line.startswith("SSID:"):
            current["ssid"] = line.split(":", 1)[1].strip()
        elif "primary channel:" in line:
            match = re.search(r"primary channel:\s*(\d+)", line)
            if match:
                current["channel"] = match.group(1)
        elif line.startswith("DS Parameter set: channel"):
            match = re.search(r"channel\s+(\d+)", line)
            if match:
                current["channel"] = match.group(1)
        elif line.startswith("capability:") and "Privacy" in line:
            current["security"] += " WEP"
        elif line.startswith("RSN:"):
            current["security"] += " RSN"
        elif line.startswith("WPA:"):
            current["security"] += " WPA"

    save_network(networks, current)
    return deduplicate_networks(networks)


def split_nmcli_line(line):
    fields = []
    buf = []
    escaped = False
    for char in line:
        if escaped:
            buf.append(char)
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == ":":
            fields.append("".join(buf))
            buf = []
        else:
            buf.append(char)
    fields.append("".join(buf))
    return fields


def parse_nmcli_scan(output):
    networks = []
    for line in output.splitlines():
        parts = split_nmcli_line(line)
        if len(parts) < 4:
            continue
        ssid, signal, channel, security = parts[:4]
        if not ssid:
            continue
        networks.append(
            {
                "ssid": ssid,
                "signal": f"{signal}%",
                "channel": channel,
                "frequency": channel_to_freq(channel) if channel else "",
                "security": normalize_security(security),
                "bssid": "",
            }
        )
    return deduplicate_networks(networks)


def deduplicate_networks(networks):
    best = {}
    for network in networks:
        ssid = network.get("ssid")
        if not ssid:
            continue
        signal = parse_signal_dbm(network.get("signal", ""))
        if "%" in network.get("signal", ""):
            try:
                signal = float(network["signal"].replace("%", ""))
            except ValueError:
                signal = -999
        old = best.get(ssid)
        if not old or signal > old["_score"]:
            item = network.copy()
            item["_score"] = signal
            best[ssid] = item

    result = []
    for item in best.values():
        item.pop("_score", None)
        result.append(item)
    return sorted(result, key=lambda ap: parse_signal_dbm(ap.get("signal", "0")), reverse=True)


def scan_wifi():
    ensure_dirs()
    run(["rfkill", "unblock", "wifi"], check=False)
    run(["nmcli", "radio", "wifi", "on"], check=False)
    run(["nmcli", "device", "wifi", "rescan", "ifname", WLAN_IFACE], check=False, timeout=15)
    time.sleep(1)

    result = run(["iw", "dev", WLAN_IFACE, "scan"], check=False, timeout=30)
    if result.returncode == 0 and result.stdout.strip():
        networks = parse_iw_scan(result.stdout)
        source = "iw"
    else:
        nmcli = run(
            [
                "nmcli",
                "-t",
                "--escape",
                "yes",
                "-f",
                "SSID,SIGNAL,CHAN,SECURITY",
                "device",
                "wifi",
                "list",
                "ifname",
                WLAN_IFACE,
                "--rescan",
                "yes",
            ],
            check=True,
            timeout=30,
        )
        networks = parse_nmcli_scan(nmcli.stdout)
        source = "nmcli"

    payload = {"ok": True, "timestamp": now(), "source": source, "networks": networks}
    SCAN_FILE.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return payload


def wait_for_ip(timeout=25):
    deadline = time.time() + timeout
    while time.time() < deadline:
        ip = get_ipv4(WLAN_IFACE)
        if ip:
            return ip
        time.sleep(1)
    return ""


def connect_wifi(args):
    require_root()
    ensure_dirs()
    events = []
    ssid = args.ssid.strip()
    password = args.password or ""

    if not ssid:
        raise CommandError("SSID is required")

    SELECTED_FILE.write_text(
        json.dumps({"ssid": ssid, "password": password, "saved_at": now()}, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    os.chmod(SELECTED_FILE, 0o600)

    add_event(events, f"Saved credentials for SSID '{ssid}'")
    add_event(events, "Keeping setup AP running while connecting the upstream WiFi")

    if not pid_is_running(HOSTAPD_PID) or not pid_is_running(DNSMASQ_PID):
        add_event(events, "Setup AP was not fully running; starting it before uplink connection", "warning")
        start_ap(argparse.Namespace(channel=None), events)

    try:
        run(["rfkill", "unblock", "wifi"], check=False)
        run(["nmcli", "device", "set", WLAN_IFACE, "managed", "yes"], check=False)
        run(["nmcli", "radio", "wifi", "on"], check=True)
        run(["nmcli", "connection", "delete", "iot-uplink"], check=False)
        run(["nmcli", "device", "wifi", "rescan", "ifname", WLAN_IFACE], check=False, timeout=15)

        cmd = [
            "nmcli",
            "--wait",
            "45",
            "device",
            "wifi",
            "connect",
            ssid,
            "ifname",
            WLAN_IFACE,
            "name",
            "iot-uplink",
        ]
        if password:
            cmd.extend(["password", password])
        run(cmd, check=True, timeout=60)
        run(["nmcli", "connection", "modify", "iot-uplink", "connection.autoconnect", "yes"], check=False)
        add_event(events, f"{WLAN_IFACE} connected to '{ssid}'")

        ip = wait_for_ip()
        if ip:
            add_event(events, f"{WLAN_IFACE} received IP address {ip}")
        else:
            add_event(events, "Connected but no IPv4 address was received yet", "warning")

        if pid_is_running(HOSTAPD_PID) and pid_is_running(DNSMASQ_PID):
            add_event(events, "Setup AP stayed running during uplink connection")
            enable_nat(events)
        else:
            add_event(events, "Setup AP was interrupted by the WiFi driver; restarting it now", "warning")
            start_ap(argparse.Namespace(channel=None), events)

        ping_result = ping_test()
        if ping_result["internet_ok"]:
            add_event(events, "Internet ping succeeded")
        else:
            add_event(events, "Internet ping failed; check upstream password, DHCP, DNS, or routing", "warning")
        return {"ok": ping_result["internet_ok"], "timestamp": now(), "events": events, "status": status_payload(), "ping": ping_result}
    except Exception:
        add_event(events, "Connection failed; starting setup AP again so the phone can retry", "warning")
        try:
            start_ap(argparse.Namespace(channel=DEFAULT_AP_CHANNEL), events)
        except Exception as start_error:
            add_event(events, f"Could not restart setup AP: {start_error}", "error")
        raise


def get_ipv4(interface):
    result = run(["ip", "-4", "-o", "addr", "show", "dev", interface], check=False)
    if result.returncode != 0:
        return ""
    match = re.search(r"inet\s+(\d+\.\d+\.\d+\.\d+/\d+)", result.stdout)
    return match.group(1) if match else ""


def get_default_route():
    result = run(["ip", "route", "show", "default"], check=False)
    return result.stdout.strip()


def pid_is_running(pid_file):
    if not pid_file.exists():
        return False
    try:
        pid = int(pid_file.read_text(encoding="utf-8").strip())
        os.kill(pid, 0)
        return True
    except (OSError, ValueError, ProcessLookupError):
        return False


def status_payload():
    scan_summary = {"available": False}
    if SCAN_FILE.exists():
        try:
            scan_data = json.loads(SCAN_FILE.read_text(encoding="utf-8"))
            scan_summary = {
                "available": True,
                "timestamp": scan_data.get("timestamp"),
                "count": len(scan_data.get("networks", [])),
            }
        except (OSError, json.JSONDecodeError):
            pass

    return {
        "ok": True,
        "timestamp": now(),
        "interfaces": {
            "wlan": WLAN_IFACE,
            "ap": AP_IFACE,
        },
        "ap": {
            "ssid": AP_SSID,
            "ip": AP_CIDR,
            "hostapd": pid_is_running(HOSTAPD_PID),
            "dnsmasq": pid_is_running(DNSMASQ_PID),
            "interface_exists": ip_link_exists(AP_IFACE),
            "ipv4": get_ipv4(AP_IFACE),
        },
        "uplink": {
            "link": get_wlan_link(),
            "ipv4": get_ipv4(WLAN_IFACE),
            "default_route": get_default_route(),
        },
        "scan": scan_summary,
    }


def ping_host(host, count="2", timeout="3"):
    result = run(["ping", "-c", count, "-W", timeout, host], check=False, timeout=10)
    return {
        "host": host,
        "ok": result.returncode == 0,
        "output": (result.stdout.strip() or result.stderr.strip())[-1200:],
    }


def ping_test():
    ip_ping = ping_host("8.8.8.8")
    dns_ping = ping_host("google.com")
    return {
        "ok": True,
        "timestamp": now(),
        "internet_ok": ip_ping["ok"] and dns_ping["ok"],
        "ip_ping": ip_ping,
        "dns_ping": dns_ping,
    }


def doctor_payload():
    checks = []
    for name in ["iw", "ip", "nmcli", "hostapd", "dnsmasq", "iptables", "ping", "rfkill"]:
        checks.append({"name": name, "ok": command_exists(name)})

    iw_list = run(["iw", "list"], check=False, timeout=10)
    text = iw_list.stdout
    supports_ap = "* AP" in text
    supports_managed = "* managed" in text
    has_combinations = "valid interface combinations" in text.lower()
    return {
        "ok": True,
        "timestamp": now(),
        "commands": checks,
        "wifi_capability": {
            "managed": supports_managed,
            "ap": supports_ap,
            "interface_combinations_reported": has_combinations,
            "note": "For phone internet through the setup AP, the adapter must support managed+AP concurrency.",
        },
        "status": status_payload(),
    }


def main():
    parser = argparse.ArgumentParser(description="Privileged WiFi/AP helper for IoT setup portal")
    sub = parser.add_subparsers(dest="command", required=True)

    start = sub.add_parser("start-ap")
    start.add_argument("--channel", type=int)
    sub.add_parser("stop-ap")
    sub.add_parser("scan")
    sub.add_parser("status")
    sub.add_parser("ping")
    sub.add_parser("doctor")
    connect = sub.add_parser("connect")
    connect.add_argument("--ssid", required=True)
    connect.add_argument("--password", default="")

    args = parser.parse_args()

    try:
        if args.command == "start-ap":
            require_root()
            events = []
            payload = start_ap(args, events)
            payload["events"] = events
            return json_print(payload)
        if args.command == "stop-ap":
            require_root()
            events = []
            stop_ap(events)
            payload = status_payload()
            payload["events"] = events
            return json_print(payload)
        if args.command == "scan":
            require_root()
            return json_print(scan_wifi())
        if args.command == "status":
            return json_print(status_payload())
        if args.command == "ping":
            return json_print(ping_test())
        if args.command == "doctor":
            return json_print(doctor_payload())
        if args.command == "connect":
            return json_print(connect_wifi(args), code=0)
    except subprocess.TimeoutExpired as exc:
        return json_print({"ok": False, "timestamp": now(), "error": f"Command timed out: {exc}"}, code=2)
    except CommandError as exc:
        detail = ""
        if exc.result is not None:
            detail = exc.result.stderr.strip() or exc.result.stdout.strip()
        return json_print({"ok": False, "timestamp": now(), "error": str(exc), "detail": detail}, code=1)
    except Exception as exc:
        return json_print({"ok": False, "timestamp": now(), "error": str(exc)}, code=1)

    return json_print({"ok": False, "timestamp": now(), "error": "Unknown command"}, code=1)


if __name__ == "__main__":
    sys.exit(main())
