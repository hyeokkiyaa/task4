const state = {
    networks: [],
    selectedSsid: "",
    lastPing: null,
};

const els = {
    apStatus: document.getElementById("apStatus"),
    uplinkStatus: document.getElementById("uplinkStatus"),
    ipStatus: document.getElementById("ipStatus"),
    scanMeta: document.getElementById("scanMeta"),
    networkList: document.getElementById("networkList"),
    refreshBtn: document.getElementById("refreshBtn"),
    connectForm: document.getElementById("connectForm"),
    ssidInput: document.getElementById("ssidInput"),
    passwordInput: document.getElementById("passwordInput"),
    togglePassword: document.getElementById("togglePassword"),
    connectBtn: document.getElementById("connectBtn"),
    pingBtn: document.getElementById("pingBtn"),
    statusLog: document.getElementById("statusLog"),
};

async function api(path, options = {}) {
    const response = await fetch(path, {
        headers: { "Content-Type": "application/json" },
        ...options,
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok && !payload.error) {
        payload.error = `HTTP ${response.status}`;
    }
    return payload;
}

function signalLevel(signal) {
    const text = String(signal || "");
    const value = Number.parseFloat(text);
    if (Number.isNaN(value)) return 1;
    if (text.includes("%")) {
        if (value >= 75) return 4;
        if (value >= 50) return 3;
        if (value >= 25) return 2;
        return 1;
    }
    if (value >= -55) return 4;
    if (value >= -67) return 3;
    if (value >= -75) return 2;
    return 1;
}

function bars(level) {
    return `<span class="bars level-${level}" aria-hidden="true"><span></span><span></span><span></span><span></span></span>`;
}

function selectNetwork(ssid) {
    state.selectedSsid = ssid;
    els.ssidInput.value = ssid;
    document.querySelectorAll(".network-row").forEach((row) => {
        row.classList.toggle("selected", row.dataset.ssid === ssid);
    });
}

function renderNetworks(networks) {
    state.networks = networks || [];
    if (!state.networks.length) {
        els.networkList.innerHTML = `<div class="empty">No WiFi APs found. Move closer to the router and refresh again.</div>`;
        return;
    }

    els.networkList.innerHTML = state.networks.map((ap) => {
        const ssid = escapeHtml(ap.ssid || "");
        const signal = escapeHtml(ap.signal || "n/a");
        const channel = escapeHtml(ap.channel || "n/a");
        const security = escapeHtml(ap.security || "Unknown");
        const level = signalLevel(ap.signal);
        return `
            <button class="network-row" type="button" data-ssid="${ssid}">
                <span class="ssid">${ssid}</span>
                <span class="signal">${bars(level)}<span class="meta">${signal}</span></span>
                <span class="meta channel">CH ${channel}</span>
                <span class="meta security">${security}</span>
            </button>
        `;
    }).join("");

    document.querySelectorAll(".network-row").forEach((row) => {
        row.addEventListener("click", () => selectNetwork(row.dataset.ssid));
    });

    const previous = state.networks.find((ap) => ap.ssid === state.selectedSsid);
    selectNetwork(previous ? previous.ssid : state.networks[0].ssid);
}

function escapeHtml(value) {
    return String(value)
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;")
        .replaceAll('"', "&quot;")
        .replaceAll("'", "&#039;");
}

function updateStatusTiles(status) {
    const ap = status?.ap || {};
    const uplink = status?.uplink || {};
    const link = uplink.link || {};

    const apOk = ap.hostapd && ap.dnsmasq;
    els.apStatus.textContent = apOk ? `${ap.ssid} ready` : "Not running";
    els.apStatus.className = apOk ? "ok" : "bad";

    if (link.connected) {
        els.uplinkStatus.textContent = link.ssid || "Connected";
        els.uplinkStatus.className = "ok";
    } else {
        els.uplinkStatus.textContent = "Not connected";
        els.uplinkStatus.className = "warn";
    }

    els.ipStatus.textContent = uplink.ipv4 || "No IP yet";
    els.ipStatus.className = uplink.ipv4 ? "ok" : "warn";

    if (status?.scan?.available) {
        els.scanMeta.textContent = `${status.scan.count} networks from last scan at ${status.scan.timestamp || "unknown time"}.`;
    }
}

function statusLines(payload) {
    const lines = [];
    const status = payload?.status || {};
    const job = payload?.job || {};

    if (!status.ok && status.error) {
        lines.push(`Status error: ${status.error}`);
        if (status.sudo_hint) lines.push(`Sudo hint: ${status.sudo_hint}`);
    }

    const link = status.uplink?.link || {};
    lines.push(`Setup AP: ${status.ap?.hostapd && status.ap?.dnsmasq ? "running" : "not running"}`);
    lines.push(`Uplink: ${link.connected ? link.ssid || "connected" : "not connected"}`);
    lines.push(`Device IP: ${status.uplink?.ipv4 || "none"}`);

    if (job.running) {
        lines.push("");
        lines.push(`Connecting to ${job.ssid}...`);
        lines.push(job.message || "The AP can restart now. Reconnect your phone if the page drops.");
    } else if (job.result) {
        lines.push("");
        lines.push(`Last connection: ${job.result.ok ? "success" : "failed"}`);
        if (job.result.error) lines.push(`Error: ${job.result.error}`);
        if (job.result.detail) lines.push(`Detail: ${job.result.detail}`);
        const events = job.result.events || [];
        events.slice(-8).forEach((event) => lines.push(`${event.time} ${event.level}: ${event.message}`));
    }

    if (state.lastPing) {
        lines.push("");
        lines.push(`Ping 8.8.8.8: ${state.lastPing.ip_ping?.ok ? "ok" : "failed"}`);
        lines.push(`Ping google.com: ${state.lastPing.dns_ping?.ok ? "ok" : "failed"}`);
        if (state.lastPing.dns_ping?.output) {
            lines.push(state.lastPing.dns_ping.output.split("\n").slice(-2).join("\n"));
        }
    }

    return lines.join("\n");
}

async function refreshScan() {
    els.refreshBtn.disabled = true;
    els.refreshBtn.textContent = "Scanning";
    els.scanMeta.textContent = "Scanning nearby WiFi networks from the Raspberry Pi.";
    try {
        const payload = await api("/api/scan", { method: "POST", body: "{}" });
        if (!payload.ok) {
            throw new Error(payload.error || payload.detail || "Scan failed");
        }
        renderNetworks(payload.networks);
        els.scanMeta.textContent = `${payload.networks.length} networks found via ${payload.source}.`;
    } catch (error) {
        els.scanMeta.textContent = error.message;
        els.networkList.innerHTML = `<div class="empty">${escapeHtml(error.message)}</div>`;
    } finally {
        els.refreshBtn.disabled = false;
        els.refreshBtn.textContent = "Refresh";
    }
}

async function loadStatus() {
    try {
        const payload = await api("/api/status");
        updateStatusTiles(payload.status);
        els.statusLog.textContent = statusLines(payload);
    } catch (error) {
        els.statusLog.textContent = `The setup AP may be restarting.\nReconnect to ${window.IOT_WIFI_AP_SSID || "the setup AP"} and reopen http://192.168.100.1:5000.`;
    }
}

async function connect(event) {
    event.preventDefault();
    const ssid = els.ssidInput.value.trim();
    const password = els.passwordInput.value;

    if (!ssid) {
        els.statusLog.textContent = "Select or type an SSID first.";
        els.ssidInput.focus();
        return;
    }

    els.connectBtn.disabled = true;
    els.connectBtn.textContent = "Connecting";
    els.statusLog.textContent = `Connecting to ${ssid}.\nThe AP can restart after this request is accepted.`;

    try {
        const payload = await api("/api/connect", {
            method: "POST",
            body: JSON.stringify({ ssid, password }),
        });
        if (!payload.ok) throw new Error(payload.error || "Connection request failed");
        els.statusLog.textContent = `${payload.message}\n\nWait 10-30 seconds, then reconnect to ${window.IOT_WIFI_AP_SSID}.`;
        window.setTimeout(loadStatus, 2000);
    } catch (error) {
        els.statusLog.textContent = error.message;
    } finally {
        els.connectBtn.disabled = false;
        els.connectBtn.textContent = "Connect";
    }
}

async function ping() {
    els.pingBtn.disabled = true;
    els.pingBtn.textContent = "Pinging";
    try {
        const payload = await api("/api/ping", { method: "POST", body: "{}" });
        state.lastPing = payload;
        await loadStatus();
    } catch (error) {
        els.statusLog.textContent = `Ping failed: ${error.message}`;
    } finally {
        els.pingBtn.disabled = false;
        els.pingBtn.textContent = "Ping Test";
    }
}

els.refreshBtn.addEventListener("click", refreshScan);
els.connectForm.addEventListener("submit", connect);
els.pingBtn.addEventListener("click", ping);
els.togglePassword.addEventListener("click", () => {
    const show = els.passwordInput.type === "password";
    els.passwordInput.type = show ? "text" : "password";
    els.togglePassword.textContent = show ? "Hide" : "Show";
});

loadStatus();
window.setInterval(loadStatus, 4000);
