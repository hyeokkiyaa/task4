#!/usr/bin/env python3
import json
import os
import subprocess
import threading
from pathlib import Path

from flask import Flask, jsonify, render_template, request


BASE_DIR = Path(__file__).resolve().parent
HELPER = Path(os.environ.get("IOT_WIFI_HELPER", BASE_DIR / "iot_wifi_control.py"))
HELPER_PYTHON = os.environ.get("IOT_WIFI_HELPER_PYTHON", "/usr/bin/python3")
WEB_STATE_DIR = Path(os.environ.get("IOT_WIFI_WEB_STATE_DIR", BASE_DIR / "web_state"))
JOB_FILE = WEB_STATE_DIR / "connect_job.json"

APP_HOST = os.environ.get("IOT_WIFI_WEB_HOST", "0.0.0.0")
APP_PORT = int(os.environ.get("IOT_WIFI_WEB_PORT", "5000"))
AP_SSID = os.environ.get("IOT_WIFI_AP_SSID", "HYEOKMIN_AP")


app = Flask(__name__)
job_lock = threading.Lock()


def ensure_web_state():
    WEB_STATE_DIR.mkdir(parents=True, exist_ok=True)


def read_job():
    ensure_web_state()
    if not JOB_FILE.exists():
        return {"running": False, "message": "No connection job has run yet."}
    try:
        return json.loads(JOB_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"running": False, "message": "Job status is unavailable."}


def write_job(payload):
    ensure_web_state()
    JOB_FILE.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def helper_command(action, *args, timeout=30):
    cmd = [HELPER_PYTHON, str(HELPER), action, *args]
    if os.geteuid() != 0:
        cmd = ["sudo", "-n", *cmd]

    result = subprocess.run(
        cmd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
    )

    payload = None
    stdout = result.stdout.strip()
    if stdout:
        try:
            payload = json.loads(stdout.splitlines()[-1])
        except json.JSONDecodeError:
            payload = None

    if payload is None:
        payload = {
            "ok": False,
            "error": "Helper did not return JSON.",
            "stdout": stdout[-1200:],
            "stderr": result.stderr.strip()[-1200:],
        }

    if result.returncode != 0:
        payload["ok"] = False
        payload.setdefault("error", "Helper command failed.")
        payload["stderr"] = result.stderr.strip()[-1200:]
        if "a password is required" in result.stderr or "sudo:" in result.stderr:
            payload["sudo_hint"] = "Run sudo ./install.sh once, or run the Flask service as root."

    return payload


def connect_worker(ssid, password):
    with job_lock:
        write_job(
            {
                "running": True,
                "ssid": ssid,
                "message": "Connecting. The setup AP can restart during this step.",
                "result": None,
            }
        )

    try:
        result = helper_command("connect", "--ssid", ssid, "--password", password, timeout=120)
    except subprocess.TimeoutExpired as exc:
        result = {"ok": False, "error": f"Connection timed out: {exc}"}
    except Exception as exc:
        result = {"ok": False, "error": str(exc)}

    with job_lock:
        write_job(
            {
                "running": False,
                "ssid": ssid,
                "message": "Connection job finished.",
                "result": result,
            }
        )


@app.route("/")
def index():
    return render_template("index.html", ap_ssid=AP_SSID)


@app.route("/api/scan", methods=["POST"])
def api_scan():
    try:
        payload = helper_command("scan", timeout=45)
    except subprocess.TimeoutExpired:
        payload = {"ok": False, "error": "WiFi scan timed out. Try refresh again."}
    return jsonify(payload), 200 if payload.get("ok") else 500


@app.route("/api/status")
def api_status():
    try:
        status = helper_command("status", timeout=15)
    except subprocess.TimeoutExpired:
        status = {"ok": False, "error": "Status command timed out."}
    return jsonify({"ok": status.get("ok", False), "status": status, "job": read_job()})


@app.route("/api/connect", methods=["POST"])
def api_connect():
    data = request.get_json(silent=True) or request.form
    ssid = (data.get("ssid") or "").strip()
    password = data.get("password") or ""

    if not ssid:
        return jsonify({"ok": False, "error": "Select an SSID first."}), 400

    with job_lock:
        current = read_job()
        if current.get("running"):
            return jsonify({"ok": False, "error": "A connection attempt is already running."}), 409
        write_job(
            {
                "running": True,
                "ssid": ssid,
                "message": "Connection request accepted.",
                "result": None,
            }
        )

    thread = threading.Thread(target=connect_worker, args=(ssid, password), daemon=True)
    thread.start()
    return jsonify(
        {
            "ok": True,
            "message": f"Connecting to {ssid}. Reconnect your phone to {AP_SSID} if the AP restarts.",
        }
    )


@app.route("/connect", methods=["POST"])
def connect_form_fallback():
    return api_connect()


@app.route("/api/ping", methods=["POST"])
def api_ping():
    try:
        payload = helper_command("ping", timeout=20)
    except subprocess.TimeoutExpired:
        payload = {"ok": False, "error": "Ping timed out."}
    return jsonify(payload), 200 if payload.get("ok") else 500


@app.route("/api/doctor")
def api_doctor():
    try:
        payload = helper_command("doctor", timeout=20)
    except subprocess.TimeoutExpired:
        payload = {"ok": False, "error": "Doctor command timed out."}
    return jsonify(payload), 200 if payload.get("ok") else 500


if __name__ == "__main__":
    ensure_web_state()
    app.run(host=APP_HOST, port=APP_PORT)
