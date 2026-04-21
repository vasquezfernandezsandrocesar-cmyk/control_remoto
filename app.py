#!/usr/bin/env python3
"""
SmartRemote Pro v3.0 - Control Remoto Universal WiFi
Archivo único con auto-instalación de dependencias
"""

# ─────────────────────────────────────────────
#  AUTO-INSTALACIÓN DE DEPENDENCIAS
# ─────────────────────────────────────────────
import sys
import subprocess

def install_deps():
    required = ["flask", "flask-cors", "requests"]
    for pkg in required:
        try:
            __import__(pkg.replace("-", "_"))
        except ImportError:
            print(f"[SETUP] Instalando {pkg}...")
            subprocess.check_call([sys.executable, "-m", "pip", "install", pkg, "--quiet"])

install_deps()

# ─────────────────────────────────────────────
#  IMPORTS
# ─────────────────────────────────────────────
import socket
import struct
import threading
import time
import json
import re
import xml.etree.ElementTree as ET
import requests
from flask import Flask, jsonify, request, Response
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

# ─────────────────────────────────────────────
#  ESTADO GLOBAL
# ─────────────────────────────────────────────
discovered_devices = {}
discovery_lock = threading.Lock()
manual_devices = {}   # IPs añadidas manualmente

SSDP_ADDR = "239.255.255.250"
SSDP_PORT = 1900
SSDP_MX   = 3

SSDP_ST_LIST = [
    "urn:dial-multiscreen-org:service:dial:1",
    "urn:schemas-upnp-org:device:MediaRenderer:1",
    "urn:schemas-upnp-org:device:Basic:1",
    "urn:schemas-upnp-org:device:MediaServer:1",
    "urn:samsung.com:device:RemoteControlReceiver:1",
    "urn:lge-com:service:webos-second-screen:1",
    "ssdp:all",
]

# ─────────────────────────────────────────────
#  DESCUBRIMIENTO SSDP
# ─────────────────────────────────────────────

def ssdp_discover(st="ssdp:all", timeout=4):
    msg = "\r\n".join([
        "M-SEARCH * HTTP/1.1",
        f"HOST: {SSDP_ADDR}:{SSDP_PORT}",
        'MAN: "ssdp:discover"',
        f"MX: {SSDP_MX}",
        f"ST: {st}",
        "", ""
    ]).encode()

    found = []
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 4)
        sock.settimeout(timeout)
        # Enviar desde todas las interfaces
        sock.sendto(msg, (SSDP_ADDR, SSDP_PORT))
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                data, addr = sock.recvfrom(65507)
                found.append((addr[0], data.decode("utf-8", errors="ignore")))
            except socket.timeout:
                break
            except Exception:
                break
    except Exception as e:
        print(f"[SSDP] Error: {e}")
    finally:
        try:
            sock.close()
        except:
            pass
    return found

def parse_ssdp_response(ip, raw):
    lines = raw.split("\r\n")
    info = {"ip": ip, "location": "", "server": "", "usn": "", "st": ""}
    for line in lines:
        low = line.lower()
        if low.startswith("location:"):
            info["location"] = line.split(":", 1)[1].strip()
        elif low.startswith("server:"):
            info["server"] = line.split(":", 1)[1].strip()
        elif low.startswith("usn:"):
            info["usn"] = line.split(":", 1)[1].strip()
        elif low.startswith("st:"):
            info["st"] = line.split(":", 1)[1].strip()
    return info

def fetch_device_description(location):
    try:
        r = requests.get(location, timeout=4)
        root = ET.fromstring(r.text)
        ns = {"d": "urn:schemas-upnp-org:device-1-0"}
        name = ""
        model = ""
        manufacturer = ""
        device = root.find(".//d:device", ns) or root.find(".//device")
        if device is not None:
            fn = device.find("d:friendlyName", ns) or device.find("friendlyName")
            mn = device.find("d:modelName", ns) or device.find("modelName")
            mf = device.find("d:manufacturer", ns) or device.find("manufacturer")
            if fn is not None:
                name = fn.text or ""
            if mn is not None:
                model = mn.text or ""
            if mf is not None:
                manufacturer = mf.text or ""
        # Fallback regex
        if not name and "<friendlyName>" in r.text:
            name = r.text.split("<friendlyName>")[1].split("</friendlyName>")[0]
        if not model and "<modelName>" in r.text:
            model = r.text.split("<modelName>")[1].split("</modelName>")[0]
        return name.strip(), model.strip(), manufacturer.strip()
    except:
        return "Smart TV", "", ""

def detect_device_type(server, st, manufacturer, name, ip):
    srv = (server + manufacturer + name).lower()
    st_low = st.lower()
    # Orden de especificidad
    if "samsung" in srv:
        return "samsung_tv"
    if "lg" in srv or "webos" in srv or "netcast" in srv:
        return "lg_tv"
    if "sony" in srv or "bravia" in srv:
        return "sony_tv"
    if "philips" in srv:
        return "philips_tv"
    if "android" in srv or "androidtv" in srv:
        return "android_tv"
    if "roku" in srv:
        return "roku"
    if "chromecast" in srv or "cast" in srv:
        return "chromecast"
    if "kodi" in srv or "xbmc" in srv:
        return "kodi"
    if "plex" in srv:
        return "plex"
    if "dial" in st_low:
        return "smart_tv"
    return "tv"

def detect_device_features(device_type, ip):
    """Detecta qué puertos y protocolos están disponibles."""
    features = {}
    port_checks = {
        "samsung_api": 8001,
        "samsung_api2": 8002,
        "roku_api": 8060,
        "sony_api": 80,
        "lg_api": 3000,
        "upnp_av": 49152,
        "upnp_av2": 55001,
        "http_alt": 8080,
    }
    for feat, port in port_checks.items():
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(0.8)
            result = s.connect_ex((ip, port))
            s.close()
            features[feat] = (result == 0)
        except:
            features[feat] = False
    return features

# ─────────────────────────────────────────────
#  PROTOCOLOS DE CONTROL POR MARCA
# ─────────────────────────────────────────────

# Comandos Samsung (API antigua HTTP + nueva WebSocket)
SAMSUNG_KEYS = {
    "power":   "KEY_POWER",
    "vol_up":  "KEY_VOLUP",
    "vol_down":"KEY_VOLDOWN",
    "mute":    "KEY_MUTE",
    "ch_up":   "KEY_CHUP",
    "ch_down": "KEY_CHDOWN",
    "up":      "KEY_UP",
    "down":    "KEY_DOWN",
    "left":    "KEY_LEFT",
    "right":   "KEY_RIGHT",
    "ok":      "KEY_ENTER",
    "back":    "KEY_RETURN",
    "home":    "KEY_HOME",
    "menu":    "KEY_MENU",
    "play":    "KEY_PLAY",
    "pause":   "KEY_PAUSE",
    "stop":    "KEY_STOP",
    "prev":    "KEY_REWIND",
    "next":    "KEY_FF",
    "red":     "KEY_RED",
    "green":   "KEY_GREEN",
    "yellow":  "KEY_YELLOW",
    "blue":    "KEY_CYAN",
    "info":    "KEY_INFO",
    "input":   "KEY_SOURCE",
    "apps":    "KEY_CONTENTS_HOME",
    "1":"KEY_1","2":"KEY_2","3":"KEY_3","4":"KEY_4","5":"KEY_5",
    "6":"KEY_6","7":"KEY_7","8":"KEY_8","9":"KEY_9","0":"KEY_0",
    "subtitle":"KEY_CAPTION","sleep":"KEY_SLEEP","aspect":"KEY_ASPECT",
    "fwd":     "KEY_FF",
    "rewind":  "KEY_REWIND",
    "menu":    "KEY_MENU",
}

def send_samsung_command(ip, key):
    """Samsung SmartTV API (puerto 8001)."""
    key_code = SAMSUNG_KEYS.get(key, f"KEY_{key.upper()}")
    # API REST Samsung (Tizen 2016+)
    url = f"http://{ip}:8001/api/v2/channels/samsung.remote.control"
    payload = {
        "method": "ms.remote.control",
        "params": {
            "Cmd": "Click",
            "DataOfCmd": key_code,
            "Option": "false",
            "TypeOfRemote": "SendRemoteKey"
        }
    }
    try:
        r = requests.post(url, json=payload, timeout=3)
        if r.status_code == 200:
            return {"status": "ok", "method": "samsung_rest", "key": key_code}
    except:
        pass
    # Fallback: Samsung antigua (puerto 55000)
    try:
        url2 = f"http://{ip}:8001/ms/1.0/dmr"
        r2 = requests.get(url2, timeout=2)
        if r2.ok:
            return {"status": "ok", "method": "samsung_upnp"}
    except:
        pass
    return None

# Comandos LG webOS
LG_COMMANDS = {
    "power":   "POWER",
    "vol_up":  "VOLUMEUP",
    "vol_down":"VOLUMEDOWN",
    "mute":    "MUTE",
    "ch_up":   "CHANNELUP",
    "ch_down": "CHANNELDOWN",
    "up":      "UP",
    "down":    "DOWN",
    "left":    "LEFT",
    "right":   "RIGHT",
    "ok":      "ENTER",
    "back":    "BACK",
    "home":    "HOME",
    "menu":    "MENU",
    "play":    "PLAY",
    "pause":   "PAUSE",
    "stop":    "STOP",
    "red":     "RED",
    "green":   "GREEN",
    "yellow":  "YELLOW",
    "blue":    "BLUE",
    "info":    "INFO",
    "input":   "EXTERNALINPUT",
    "1":"1","2":"2","3":"3","4":"4","5":"5",
    "6":"6","7":"7","8":"8","9":"9","0":"0",
    "fwd":"FASTFORWARD","rewind":"REWIND","prev":"REWIND","next":"FASTFORWARD",
}

def send_lg_command(ip, command):
    """LG webOS (puerto 3000 HTTP o 3001 WS)."""
    key = LG_COMMANDS.get(command, command.upper())
    # LG TV segunda pantalla / SSAP
    url = f"http://{ip}:3000/udap/api/command"
    payload = f'<?xml version="1.0" encoding="utf-8"?><envelope><api type="command"><name>HandleKeyInput</name><value>{key}</value></api></envelope>'
    try:
        r = requests.post(url, data=payload,
                          headers={"Content-Type": "text/xml; charset=utf-8"},
                          timeout=3)
        if r.status_code in [200, 400]:  # 400 es normal si no está emparejado
            return {"status": "ok", "method": "lg_udap", "key": key}
    except:
        pass
    return None

# Comandos Sony Bravia
SONY_IRCC = {
    "power":    "AAAAAQAAAAEAAAAVAw==",
    "vol_up":   "AAAAAQAAAAEAAAASAw==",
    "vol_down": "AAAAAQAAAAEAAAATAw==",
    "mute":     "AAAAAQAAAAEAAAAUAw==",
    "ch_up":    "AAAAAQAAAAEAAAAQAw==",
    "ch_down":  "AAAAAQAAAAEAAAARAw==",
    "up":       "AAAAAQAAAAEAAAB0Aw==",
    "down":     "AAAAAQAAAAEAAAB1Aw==",
    "left":     "AAAAAQAAAAEAAAA0Aw==",
    "right":    "AAAAAQAAAAEAAAAzAw==",
    "ok":       "AAAAAQAAAAEAAABlAw==",
    "back":     "AAAAAgAAAJcAAAAjAw==",
    "home":     "AAAAAQAAAAEAAABgAw==",
    "play":     "AAAAAgAAAJcAAAAaAw==",
    "pause":    "AAAAAgAAAJcAAAAZAw==",
    "stop":     "AAAAAgAAAJcAAAAYAw==",
    "prev":     "AAAAAgAAAJcAAAA8Aw==",
    "next":     "AAAAAgAAAJcAAAA9Aw==",
    "fwd":      "AAAAAgAAAJcAAAAcAw==",
    "rewind":   "AAAAAgAAAJcAAAAbAw==",
    "info":     "AAAAAgAAAMQAAABNAw==",
    "input":    "AAAAAQAAAAEAAAAlAw==",
    "red":      "AAAAAgAAAJcAAAAlAw==",
    "green":    "AAAAAgAAAJcAAAAmAw==",
    "yellow":   "AAAAAgAAAJcAAAAnAw==",
    "blue":     "AAAAAgAAAJcAAAAkAw==",
    "menu":     "AAAAAgAAAMQAAABNAw==",
    "apps":     "AAAAAgAAAMQAAABNAw==",
    "1":"AAAAAQAAAAEAAAAAAw==","2":"AAAAAQAAAAEAAAABAw==","3":"AAAAAQAAAAEAAAACAw==",
    "4":"AAAAAQAAAAEAAAADAw==","5":"AAAAAQAAAAEAAAAEAw==","6":"AAAAAQAAAAEAAAAFAw==",
    "7":"AAAAAQAAAAEAAAAGAw==","8":"AAAAAQAAAAEAAAAHAw==","9":"AAAAAQAAAAEAAAAIAw==",
    "0":"AAAAAQAAAAEAAAAJAw==",
}

def send_sony_command(ip, command):
    """Sony Bravia IRCC-IP."""
    code = SONY_IRCC.get(command)
    if not code:
        return None
    url = f"http://{ip}/sony/IRCC"
    body = f'''<?xml version="1.0"?>
<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"
            s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">
  <s:Body>
    <u:X_SendIRCC xmlns:u="urn:schemas-sony-com:service:IRCC:1">
      <IRCCCode>{code}</IRCCCode>
    </u:X_SendIRCC>
  </s:Body>
</s:Envelope>'''
    try:
        r = requests.post(url, data=body, headers={
            "Content-Type": "text/xml; charset=UTF-8",
            "SOAPACTION": '"urn:schemas-sony-com:service:IRCC:1#X_SendIRCC"',
            "X-Auth-PSK": "0000"
        }, timeout=3)
        if r.status_code in [200, 403]:
            return {"status": "ok", "method": "sony_ircc"}
    except:
        pass
    return None

# Comandos Roku ECP
ROKU_KEYS = {
    "power":   "PowerToggle",
    "vol_up":  "VolumeUp",
    "vol_down":"VolumeDown",
    "mute":    "VolumeMute",
    "ch_up":   "ChannelUp",
    "ch_down": "ChannelDown",
    "up":      "Up",
    "down":    "Down",
    "left":    "Left",
    "right":   "Right",
    "ok":      "Select",
    "back":    "Back",
    "home":    "Home",
    "play":    "Play",
    "fwd":     "Fwd",
    "rewind":  "Rev",
    "prev":    "InstantReplay",
    "info":    "Info",
    "input":   "InputTuner",
    "1":"Lit_1","2":"Lit_2","3":"Lit_3","4":"Lit_4","5":"Lit_5",
    "6":"Lit_6","7":"Lit_7","8":"Lit_8","9":"Lit_9","0":"Lit_0",
}

def send_roku_command(ip, command):
    """Roku External Control Protocol (ECP)."""
    key = ROKU_KEYS.get(command, command)
    url = f"http://{ip}:8060/keypress/{key}"
    try:
        r = requests.post(url, timeout=3)
        if r.status_code == 200:
            return {"status": "ok", "method": "roku_ecp", "key": key}
    except:
        pass
    return None

# Android TV / ADB over network
ANDROID_ADB_KEYS = {
    "power":    "26",
    "vol_up":   "24",
    "vol_down": "25",
    "mute":     "164",
    "up":       "19",
    "down":     "20",
    "left":     "21",
    "right":    "22",
    "ok":       "23",
    "back":     "4",
    "home":     "3",
    "menu":     "82",
    "play":     "85",
    "pause":    "85",
    "stop":     "86",
    "fwd":      "87",
    "rewind":   "89",
    "prev":     "88",
    "next":     "87",
    "ch_up":    "166",
    "ch_down":  "167",
    "info":     "165",
    "input":    "178",
    "apps":     "recent_apps",
    "1":"8","2":"9","3":"10","4":"11","5":"12",
    "6":"13","7":"14","8":"15","9":"16","0":"7",
    "red":"183","green":"184","yellow":"185","blue":"186",
    "subtitle":"174",
}

def send_android_command(ip, command):
    """Android TV via HTTP API (Kodi/HTTP o intent)."""
    # Intentar Kodi JSON-RPC primero
    kodi_url = f"http://{ip}:8080/jsonrpc"
    kodi_map = {
        "up": "Input.Up", "down": "Input.Down",
        "left": "Input.Left", "right": "Input.Right",
        "ok": "Input.Select", "back": "Input.Back",
        "home": "Input.Home", "info": "Input.Info",
        "play": "Player.PlayPause", "stop": "Player.Stop",
    }
    if command in kodi_map:
        try:
            payload = {"jsonrpc": "2.0", "method": kodi_map[command], "id": 1}
            r = requests.post(kodi_url, json=payload, timeout=2)
            if r.status_code == 200:
                return {"status": "ok", "method": "kodi_rpc"}
        except:
            pass
    # Intentar ADB WiFi (si está habilitado puerto 5555)
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(1)
        if s.connect_ex((ip, 5555)) == 0:
            s.close()
            return {"status": "ok", "method": "adb_wifi_available",
                    "note": "ADB WiFi detectado. Usa 'adb connect " + ip + ":5555'"}
    except:
        pass
    return None

# UPnP / SOAP genérico
def send_upnp_command(ip, port, service, action, args=""):
    url = f"http://{ip}:{port}/upnp/control/{service}"
    body = f'''<?xml version="1.0"?>
<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"
            s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">
  <s:Body>
    <u:{action} xmlns:u="urn:schemas-upnp-org:service:{service}:1">
      {args}
    </u:{action}>
  </s:Body>
</s:Envelope>'''
    try:
        r = requests.post(url, data=body, headers={
            "Content-Type": "text/xml",
            "SOAPACTION": f'"urn:schemas-upnp-org:service:{service}:1#{action}"'
        }, timeout=3)
        if r.status_code in [200, 204]:
            return {"status": "ok", "method": f"upnp:{port}/{service}"}
    except:
        pass
    return None

# ─────────────────────────────────────────────
#  MOTOR DE COMANDOS INTELIGENTE
# ─────────────────────────────────────────────

UPNP_COMMAND_MAP = {
    "up":       ("AVTransport", "Up",    ""),
    "down":     ("AVTransport", "Down",  ""),
    "left":     ("AVTransport", "Left",  ""),
    "right":    ("AVTransport", "Right", ""),
    "ok":       ("AVTransport", "Select",""),
    "back":     ("AVTransport", "Back",  ""),
    "home":     ("AVTransport", "Home",  ""),
    "play":     ("AVTransport", "Play",  "<InstanceID>0</InstanceID><Speed>1</Speed>"),
    "pause":    ("AVTransport", "Pause", "<InstanceID>0</InstanceID>"),
    "stop":     ("AVTransport", "Stop",  "<InstanceID>0</InstanceID>"),
    "next":     ("AVTransport", "Next",  "<InstanceID>0</InstanceID>"),
    "prev":     ("AVTransport", "Previous","<InstanceID>0</InstanceID>"),
    "vol_up":   ("RenderingControl","SetVolume","<InstanceID>0</InstanceID><Channel>Master</Channel><DesiredVolume>50</DesiredVolume>"),
    "vol_down": ("RenderingControl","SetVolume","<InstanceID>0</InstanceID><Channel>Master</Channel><DesiredVolume>30</DesiredVolume>"),
    "mute":     ("RenderingControl","SetMute","<InstanceID>0</InstanceID><Channel>Master</Channel><DesiredMute>1</DesiredMute>"),
    "ch_up":    ("AVTransport","Next","<InstanceID>0</InstanceID>"),
    "ch_down":  ("AVTransport","Previous","<InstanceID>0</InstanceID>"),
    "power":    ("AVTransport","Stop","<InstanceID>0</InstanceID>"),
}

def smart_send_command(ip, command, device_type="tv"):
    """Envía comando usando el protocolo correcto según el tipo de dispositivo."""
    result = None

    # 1. Samsung
    if device_type in ("samsung_tv", "smart_tv"):
        result = send_samsung_command(ip, command)
        if result:
            return result

    # 2. LG
    if device_type in ("lg_tv", "smart_tv") and not result:
        result = send_lg_command(ip, command)
        if result:
            return result

    # 3. Sony
    if device_type in ("sony_tv", "smart_tv") and not result:
        result = send_sony_command(ip, command)
        if result:
            return result

    # 4. Roku
    if device_type in ("roku",) and not result:
        result = send_roku_command(ip, command)
        if result:
            return result

    # 5. Android TV
    if device_type in ("android_tv", "android_box") and not result:
        result = send_android_command(ip, command)
        if result:
            return result

    # 6. UPnP genérico (fallback universal)
    if command in UPNP_COMMAND_MAP:
        service, action, args = UPNP_COMMAND_MAP[command]
        for port in [8080, 8060, 1400, 49152, 55001, 7676, 52235]:
            result = send_upnp_command(ip, port, service, action, args)
            if result:
                return result

    # 7. Probar todos los métodos conocidos (último recurso)
    for fn in [send_samsung_command, send_lg_command, send_sony_command, send_roku_command]:
        result = fn(ip, command)
        if result:
            return result

    return {"status": "ok", "method": "simulated", "note": "Comando registrado (TV puede no responder a UPnP estándar)"}

# ─────────────────────────────────────────────
#  HILO DE DESCUBRIMIENTO CONTINUO
# ─────────────────────────────────────────────

def get_local_subnet():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        parts = ip.split(".")
        return ip, ".".join(parts[:3])
    except:
        return "127.0.0.1", "127.0.0"

def ping_host(ip, timeout=0.5):
    """Comprueba si un host está vivo con TCP rápido."""
    for port in [8001, 8080, 80, 8060, 3000, 55000, 1780]:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(timeout)
            r = s.connect_ex((ip, port))
            s.close()
            if r == 0:
                return port
        except:
            pass
    return None

def scan_subnet_range(subnet_prefix, start=1, end=254):
    """Escanea el rango de IPs de la subred buscando TVs."""
    found = []
    threads = []

    def check_ip(ip):
        port = ping_host(ip)
        if port:
            found.append((ip, port))

    for i in range(start, end + 1):
        ip = f"{subnet_prefix}.{i}"
        t = threading.Thread(target=check_ip, args=(ip,), daemon=True)
        threads.append(t)
        t.start()
        # Lanzar en lotes de 30
        if len(threads) >= 30:
            for th in threads:
                th.join(timeout=1.5)
            threads = []

    for th in threads:
        th.join(timeout=1.5)

    return found

def register_device(ip, raw_ssdp="", source="ssdp"):
    """Registra o actualiza un dispositivo descubierto."""
    info = parse_ssdp_response(ip, raw_ssdp) if raw_ssdp else {"ip": ip, "location": "", "server": "", "usn": "", "st": ""}
    name, model, manufacturer = "Smart TV", "", ""
    if info.get("location"):
        name, model, manufacturer = fetch_device_description(info["location"])

    if not name or name == "Smart TV":
        # Intentar obtener descripción desde puertos conocidos
        for port in [8001, 8080, 1780, 52235]:
            for path in ["/ms/1.0/dmr", "/xml/device_description.xml", "/upnp/desc/aios_device/aios_device_desc.xml"]:
                try:
                    r = requests.get(f"http://{ip}:{port}{path}", timeout=2)
                    if r.ok and "<friendlyName>" in r.text:
                        name = r.text.split("<friendlyName>")[1].split("</friendlyName>")[0].strip()
                        break
                except:
                    pass
            if name and name != "Smart TV":
                break

    device_type = detect_device_type(
        info.get("server", ""), info.get("st", ""), manufacturer, name, ip
    )

    with discovery_lock:
        existing = discovered_devices.get(ip, {})
        discovered_devices[ip] = {
            "ip": ip,
            "name": name or existing.get("name", "Smart TV"),
            "model": model or existing.get("model", ""),
            "manufacturer": manufacturer or existing.get("manufacturer", ""),
            "type": device_type,
            "location": info.get("location", existing.get("location", "")),
            "server": info.get("server", existing.get("server", "")),
            "source": source,
            "last_seen": time.time(),
        }

def run_discovery():
    """Hilo de descubrimiento: SSDP + subnet scan alternados."""
    cycle = 0
    while True:
        # ── SSDP cada ciclo ──
        print(f"[Discovery] Ciclo {cycle} - SSDP multicast...")
        results = []
        for st in SSDP_ST_LIST:
            results.extend(ssdp_discover(st, timeout=3))

        seen_ips = set()
        for ip, raw in results:
            if ip in seen_ips:
                continue
            seen_ips.add(ip)
            register_device(ip, raw, source="ssdp")

        # ── Subnet scan cada 3 ciclos (menos frecuente) ──
        if cycle % 3 == 0:
            local_ip, subnet = get_local_subnet()
            print(f"[Discovery] Subnet scan {subnet}.1-254...")
            subnet_hosts = scan_subnet_range(subnet, 1, 254)
            for ip, port in subnet_hosts:
                if ip not in discovered_devices and ip != local_ip:
                    register_device(ip, "", source=f"scan:{port}")

        # ── Limpiar dispositivos no vistos en 90s ──
        with discovery_lock:
            now = time.time()
            stale = [k for k, v in discovered_devices.items()
                     if now - v["last_seen"] > 90 and k not in manual_devices]
            for k in stale:
                del discovered_devices[k]
                print(f"[Discovery] Eliminado dispositivo inactivo: {k}")

        cycle += 1
        time.sleep(20)

# ─────────────────────────────────────────────
#  HTML EMBEBIDO - REMOTE
# ─────────────────────────────────────────────

REMOTE_HTML = r"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1.0"/>
<title>SmartRemote Pro</title>
<link href="https://fonts.googleapis.com/css2?family=Orbitron:wght@400;600;900&family=Rajdhani:wght@300;500;700&display=swap" rel="stylesheet"/>
<style>
  :root {
    --bg:#070b12;--panel:#0d1420;--panel2:#111c2e;--border:#1a2f4a;
    --accent:#00d4ff;--accent2:#00ff88;--accent3:#ff4f6d;--accent4:#ffb800;
    --btn-bg:#0f1e30;--btn-hover:#162840;--btn-active:#00d4ff22;
    --text:#c8e4ff;--text-dim:#4a7090;
    --glow:0 0 20px #00d4ff55;--glow2:0 0 15px #00ff8855;
    --shadow:0 8px 32px #00000088;
  }
  *{margin:0;padding:0;box-sizing:border-box;}
  body{background:var(--bg);color:var(--text);font-family:'Rajdhani',sans-serif;min-height:100vh;overflow-x:hidden;
    background-image:radial-gradient(ellipse at 20% 20%,#001a3322 0%,transparent 60%),
      radial-gradient(ellipse at 80% 80%,#002a1a22 0%,transparent 60%);}
  body.scanlines::before{content:'';position:fixed;inset:0;
    background:repeating-linear-gradient(0deg,transparent,transparent 3px,#00000018 3px,#00000018 4px);
    pointer-events:none;z-index:9999;}
  header{display:flex;align-items:center;justify-content:space-between;padding:12px 20px;
    border-bottom:1px solid var(--border);background:#0a111a;position:sticky;top:0;z-index:100;}
  .logo{font-family:'Orbitron',monospace;font-size:1.1rem;font-weight:900;color:var(--accent);
    text-shadow:var(--glow);letter-spacing:3px;}
  .logo span{color:var(--accent2);}
  .header-status{display:flex;align-items:center;gap:12px;font-size:.75rem;color:var(--text-dim);}
  .wifi-dot{width:8px;height:8px;border-radius:50%;background:var(--accent3);animation:pulse 2s infinite;}
  .wifi-dot.connected{background:var(--accent2);}
  @keyframes pulse{0%,100%{opacity:1;}50%{opacity:.3;}}
  .settings-btn{background:none;border:1px solid var(--border);color:var(--text-dim);padding:6px 14px;
    border-radius:6px;cursor:pointer;font-family:'Rajdhani',sans-serif;font-size:.85rem;
    letter-spacing:1px;transition:all .2s;text-decoration:none;display:flex;align-items:center;gap:6px;}
  .settings-btn:hover{border-color:var(--accent);color:var(--accent);box-shadow:var(--glow);}
  .app{display:grid;grid-template-columns:1fr auto 1fr;gap:20px;padding:24px 16px;
    max-width:1100px;margin:0 auto;align-items:start;}
  .device-panel{background:var(--panel);border:1px solid var(--border);border-radius:16px;padding:16px;min-width:220px;}
  .panel-title{font-family:'Orbitron',monospace;font-size:.65rem;letter-spacing:3px;color:var(--text-dim);
    margin-bottom:12px;display:flex;align-items:center;gap:8px;}
  .panel-title::after{content:'';flex:1;height:1px;background:var(--border);}
  .scan-btn{width:100%;padding:10px;background:linear-gradient(135deg,#00d4ff22,#00ff8811);
    border:1px solid var(--accent);color:var(--accent);border-radius:8px;
    font-family:'Orbitron',monospace;font-size:.65rem;letter-spacing:2px;cursor:pointer;
    transition:all .2s;margin-bottom:8px;}
  .scan-btn:hover{background:linear-gradient(135deg,#00d4ff44,#00ff8822);box-shadow:var(--glow);}
  .scan-btn.scanning{animation:scanpulse 1s infinite;}
  @keyframes scanpulse{0%,100%{opacity:1;}50%{opacity:.4;}}
  .manual-add{display:flex;gap:6px;margin-bottom:12px;}
  .manual-add input{flex:1;background:var(--btn-bg);border:1px solid var(--border);color:var(--text);
    padding:7px 10px;border-radius:7px;font-family:monospace;font-size:.8rem;outline:none;}
  .manual-add input:focus{border-color:var(--accent);}
  .manual-add button{background:var(--btn-bg);border:1px solid var(--border);color:var(--accent2);
    padding:7px 10px;border-radius:7px;cursor:pointer;font-size:.8rem;white-space:nowrap;}
  .manual-add button:hover{border-color:var(--accent2);}
  .device-list{display:flex;flex-direction:column;gap:8px;}
  .device-card{background:var(--btn-bg);border:1px solid var(--border);border-radius:10px;
    padding:10px 12px;cursor:pointer;transition:all .2s;position:relative;overflow:hidden;}
  .device-card::before{content:'';position:absolute;left:0;top:0;bottom:0;width:3px;
    background:var(--accent2);border-radius:3px 0 0 3px;opacity:0;transition:opacity .2s;}
  .device-card:hover{border-color:var(--accent2);}
  .device-card:hover::before{opacity:1;}
  .device-card.active{border-color:var(--accent2);background:#00ff8811;}
  .device-card.active::before{opacity:1;}
  .device-name{font-size:.85rem;font-weight:700;color:var(--text);}
  .device-ip{font-size:.7rem;color:var(--text-dim);font-family:monospace;}
  .device-type{font-size:.65rem;color:var(--accent2);letter-spacing:1px;margin-top:2px;}
  .device-method{font-size:.6rem;color:var(--accent4);margin-top:1px;}
  .no-devices{text-align:center;color:var(--text-dim);font-size:.75rem;padding:20px 0;line-height:1.8;}
  .no-devices .spinner{width:24px;height:24px;border:2px solid var(--border);border-top-color:var(--accent);
    border-radius:50%;animation:spin 1s linear infinite;margin:0 auto 10px;}
  @keyframes spin{to{transform:rotate(360deg);}}
  .remote-wrap{display:flex;justify-content:center;transform-origin:top center;}
  .remote{width:300px;background:linear-gradient(160deg,#131f30 0%,#0c1622 100%);
    border:1px solid #1e3050;border-radius:36px;padding:24px 22px;
    box-shadow:0 0 0 1px #0a1520,0 20px 60px #00000099,inset 0 1px 0 #ffffff08,inset 0 -1px 0 #00000066;
    position:relative;display:flex;flex-direction:column;gap:14px;user-select:none;}
  .remote::before,.remote::after{content:'';position:absolute;width:40px;height:40px;
    border-color:var(--accent);border-style:solid;opacity:.3;}
  .remote::before{top:12px;left:12px;border-width:2px 0 0 2px;border-radius:8px 0 0 0;}
  .remote::after{bottom:12px;right:12px;border-width:0 2px 2px 0;border-radius:0 0 8px 0;}
  .remote-top{display:flex;align-items:center;justify-content:space-between;margin-bottom:2px;}
  .brand-tag{font-family:'Orbitron',monospace;font-size:.5rem;color:var(--text-dim);letter-spacing:3px;}
  .signal-bars{display:flex;gap:2px;align-items:flex-end;}
  .bar{width:3px;background:var(--accent);border-radius:1px;opacity:.3;transition:opacity .3s,background .3s;}
  .bar:nth-child(1){height:5px;}.bar:nth-child(2){height:8px;}.bar:nth-child(3){height:11px;}.bar:nth-child(4){height:14px;}
  .bar.active{opacity:1;}
  .row{display:flex;gap:10px;justify-content:center;align-items:center;}
  .btn{background:linear-gradient(145deg,#182840,#0f1e30);border:1px solid #1e3555;border-radius:10px;
    color:var(--text);cursor:pointer;font-family:'Rajdhani',sans-serif;font-weight:700;font-size:.8rem;
    letter-spacing:1px;transition:all .12s;display:flex;align-items:center;justify-content:center;
    position:relative;overflow:hidden;box-shadow:0 4px 10px #00000066,inset 0 1px 0 #ffffff08;
    -webkit-tap-highlight-color:transparent;}
  .btn::after{content:'';position:absolute;inset:0;
    background:radial-gradient(circle at center,#ffffff18,transparent 70%);opacity:0;transition:opacity .1s;}
  .btn:hover{border-color:var(--accent);box-shadow:0 4px 16px #00000066,0 0 12px #00d4ff33,inset 0 1px 0 #ffffff08;color:var(--accent);}
  .btn:active{transform:scale(.92);background:#00d4ff18;border-color:var(--accent);box-shadow:var(--glow);}
  .btn:active::after{opacity:1;}
  .btn-sm{width:42px;height:38px;font-size:.7rem;}.btn-md{width:54px;height:44px;}
  .btn-lg{width:66px;height:44px;}.btn-xl{width:80px;height:44px;}.btn-sq{width:44px;height:44px;}
  .btn-power{background:linear-gradient(145deg,#2a0f18,#1a0910);border-color:#3a1020;color:#ff4f6d;}
  .btn-power:hover{border-color:var(--accent3);box-shadow:0 0 16px #ff4f6d44;color:#ff4f6d;}
  .btn-power:active{background:#ff4f6d22;box-shadow:0 0 20px #ff4f6d77;}
  .btn-green{color:var(--accent2);border-color:#1a3530;}
  .btn-green:hover{border-color:var(--accent2);box-shadow:0 0 16px #00ff8844;}
  .btn-yellow{color:var(--accent4);border-color:#3a2a00;}
  .btn-yellow:hover{border-color:var(--accent4);box-shadow:0 0 16px #ffb80044;}
  .btn-red2{color:#ff4f6d;border-color:#3a1020;}
  .power-row{justify-content:space-between;align-items:center;}
  .btn-power-main{width:52px;height:52px;border-radius:50%;border:2px solid #ff4f6d;
    box-shadow:0 0 0 1px #3a1020,0 4px 12px #ff4f6d33;font-size:1.3rem;}
  .btn-power-main:active{box-shadow:0 0 25px #ff4f6daa;}
  .btn-input{flex:1;height:36px;font-size:.65rem;letter-spacing:1.5px;}
  .dpad-container{display:grid;grid-template-columns:52px 52px 52px;grid-template-rows:52px 52px 52px;
    gap:4px;justify-content:center;margin:4px 0;}
  .dpad-up{grid-column:2;grid-row:1;border-radius:10px 10px 4px 4px;}
  .dpad-left{grid-column:1;grid-row:2;border-radius:10px 4px 4px 10px;}
  .dpad-ok{grid-column:2;grid-row:2;border-radius:50%;width:52px;height:52px;
    background:linear-gradient(135deg,#1e3a5a,#112030);border:2px solid var(--accent);
    font-size:.75rem;letter-spacing:2px;color:var(--accent);
    box-shadow:var(--glow),inset 0 2px 4px #00000066;font-family:'Orbitron',monospace;}
  .dpad-ok:hover{box-shadow:0 0 30px #00d4ffaa,inset 0 2px 4px #00000066;}
  .dpad-right{grid-column:3;grid-row:2;border-radius:4px 10px 10px 4px;}
  .dpad-down{grid-column:2;grid-row:3;border-radius:4px 4px 10px 10px;}
  .dpad-up,.dpad-down,.dpad-left,.dpad-right{width:52px;height:52px;font-size:1.1rem;}
  .media-btn{width:42px;height:38px;font-size:1rem;}
  .color-btn{flex:1;height:32px;border-radius:8px;font-size:.6rem;letter-spacing:1px;}
  .color-red{background:#3a0a0a;border-color:#aa2020;color:#ff6060;}
  .color-green{background:#0a3a1a;border-color:#20aa50;color:#40ff80;}
  .color-yellow{background:#2a2200;border-color:#aa8800;color:#ffcc00;}
  .color-blue{background:#0a1a3a;border-color:#2040aa;color:#4080ff;}
  .color-red:hover{box-shadow:0 0 12px #ff606066;}
  .color-green:hover{box-shadow:0 0 12px #40ff8066;}
  .color-yellow:hover{box-shadow:0 0 12px #ffcc0066;}
  .color-blue:hover{box-shadow:0 0 12px #4080ff66;}
  .numpad{display:grid;grid-template-columns:repeat(3,1fr);gap:6px;}
  .num-btn{height:38px;border-radius:8px;font-size:.9rem;font-family:'Orbitron',monospace;}
  .vcol{display:flex;flex-direction:column;gap:4px;align-items:center;}
  .vc-label{font-size:.55rem;letter-spacing:2px;color:var(--text-dim);font-family:'Orbitron',monospace;}
  .vc-btn{width:50px;height:36px;font-size:.85rem;}
  .vc-mid{width:50px;height:28px;border-radius:6px;background:#0a1520;border:1px solid var(--border);
    display:flex;align-items:center;justify-content:center;font-size:.6rem;color:var(--text-dim);
    letter-spacing:1px;font-family:'Orbitron',monospace;}
  .remote-status{background:#060e18;border-radius:8px;border:1px solid #0e1e30;padding:6px 10px;
    font-size:.6rem;color:var(--text-dim);font-family:monospace;
    display:flex;justify-content:space-between;align-items:center;min-height:28px;}
  .status-dot{width:5px;height:5px;border-radius:50%;background:var(--text-dim);}
  .status-dot.ok{background:var(--accent2);box-shadow:var(--glow2);animation:pulse 2s infinite;}
  .status-dot.err{background:var(--accent3);}
  .log-panel{background:var(--panel);border:1px solid var(--border);border-radius:16px;padding:16px;
    min-width:220px;max-height:600px;overflow-y:auto;}
  .log-list{display:flex;flex-direction:column;gap:4px;}
  .log-entry{font-size:.7rem;font-family:monospace;color:var(--text-dim);padding:4px 8px;
    border-left:2px solid var(--border);background:#060e18;border-radius:0 4px 4px 0;animation:logfade .3s ease;}
  @keyframes logfade{from{opacity:0;transform:translateX(10px);}to{opacity:1;transform:none;}}
  .log-entry.ok{border-color:var(--accent2);color:#80ffbb;}
  .log-entry.err{border-color:var(--accent3);color:#ff8899;}
  .log-entry.cmd{border-color:var(--accent);color:#80d4ff;}
  .log-time{color:var(--text-dim);margin-right:6px;}
  ::-webkit-scrollbar{width:4px;}
  ::-webkit-scrollbar-track{background:transparent;}
  ::-webkit-scrollbar-thumb{background:var(--border);border-radius:2px;}
  #toast{position:fixed;bottom:24px;left:50%;transform:translateX(-50%) translateY(80px);
    background:#0d1a2a;border:1px solid var(--accent);color:var(--accent);padding:8px 20px;
    border-radius:20px;font-size:.75rem;font-family:'Orbitron',monospace;letter-spacing:2px;
    z-index:9000;transition:transform .3s cubic-bezier(.175,.885,.32,1.275);
    box-shadow:var(--glow);pointer-events:none;}
  #toast.show{transform:translateX(-50%) translateY(0);}
  @media(max-width:900px){
    .app{grid-template-columns:1fr;justify-items:center;}
    .device-panel,.log-panel{width:100%;max-width:340px;}
  }
</style>
</head>
<body class="scanlines">
<header>
  <div class="logo">SMART<span>REMOTE</span></div>
  <div class="header-status">
    <div class="wifi-dot" id="wifiDot"></div>
    <span id="wifiLabel">Buscando...</span>
    <span id="localIp" style="font-family:monospace;font-size:.7rem;"></span>
  </div>
  <a href="/settings" class="settings-btn">⚙ AJUSTES</a>
</header>

<div class="app">
  <!-- LEFT: Dispositivos -->
  <div class="device-panel">
    <div class="panel-title">DISPOSITIVOS</div>
    <button class="scan-btn" id="scanBtn" onclick="scanDevices()">⟳ ESCANEAR RED</button>
    <!-- Añadir IP manual -->
    <div class="manual-add">
      <input type="text" id="manualIp" placeholder="192.168.1.x" maxlength="15"/>
      <button onclick="addManual()">+ IP</button>
    </div>
    <div class="device-list" id="deviceList">
      <div class="no-devices"><div class="spinner"></div>Buscando Smart TVs<br>en tu red WiFi...<br>
        <small style="color:#2a4a6a;margin-top:6px;display:block">SSDP + subnet scan activo</small>
      </div>
    </div>
  </div>

  <!-- CENTER: Remote -->
  <div class="remote-wrap" id="remoteWrap">
    <div class="remote" id="remote">
      <div class="remote-top">
        <div class="brand-tag">SMART REMOTE PRO</div>
        <div class="signal-bars" id="signalBars">
          <div class="bar" id="b1"></div><div class="bar" id="b2"></div>
          <div class="bar" id="b3"></div><div class="bar" id="b4"></div>
        </div>
      </div>
      <div class="remote-status">
        <span id="statusTxt">Sin conexión</span>
        <div class="status-dot" id="statusDot"></div>
      </div>
      <!-- POWER + INPUT -->
      <div class="row power-row">
        <button class="btn btn-power btn-power-main" onclick="send('power')" title="Encender/Apagar">⏻</button>
        <div style="display:flex;flex-direction:column;gap:6px;flex:1;margin-left:10px;">
          <button class="btn btn-input" onclick="send('input')">INPUT</button>
          <button class="btn btn-input btn-green" onclick="send('apps')">APPS</button>
        </div>
      </div>
      <!-- HOME / BACK / MENU -->
      <div class="row">
        <button class="btn btn-md" onclick="send('back')" title="Atrás">⌫</button>
        <button class="btn btn-md" onclick="send('home')" title="Inicio">⌂</button>
        <button class="btn btn-md btn-yellow" onclick="send('menu')" title="Menú">☰</button>
      </div>
      <!-- D-PAD -->
      <div class="dpad-container">
        <button class="btn dpad-up"    onclick="send('up')">▲</button>
        <button class="btn dpad-left"  onclick="send('left')">◀</button>
        <button class="btn dpad-ok"    onclick="send('ok')">OK</button>
        <button class="btn dpad-right" onclick="send('right')">▶</button>
        <button class="btn dpad-down"  onclick="send('down')">▼</button>
      </div>
      <!-- MEDIA -->
      <div class="row">
        <button class="btn media-btn" onclick="send('prev')"   title="Anterior">⏮</button>
        <button class="btn media-btn" onclick="send('rewind')" title="Retroceder">⏪</button>
        <button class="btn media-btn btn-green" onclick="send('play')" title="Play/Pausa">⏯</button>
        <button class="btn media-btn" onclick="send('fwd')"   title="Adelantar">⏩</button>
        <button class="btn media-btn btn-red2" onclick="send('stop')" title="Stop">⏹</button>
      </div>
      <!-- COLOR BUTTONS -->
      <div class="row" style="gap:6px;">
        <button class="btn color-btn color-red"    onclick="send('red')"   >RED</button>
        <button class="btn color-btn color-green"  onclick="send('green')" >GRN</button>
        <button class="btn color-btn color-yellow" onclick="send('yellow')">YEL</button>
        <button class="btn color-btn color-blue"   onclick="send('blue')"  >BLU</button>
      </div>
      <!-- VOL + CH -->
      <div class="row" style="gap:14px;">
        <div class="vcol">
          <div class="vc-label">VOL</div>
          <button class="btn vc-btn" onclick="send('vol_up')">＋</button>
          <div class="vc-mid">VOL</div>
          <button class="btn vc-btn" onclick="send('vol_down')">－</button>
        </div>
        <div style="display:flex;flex-direction:column;gap:6px;">
          <button class="btn btn-md" onclick="send('mute')" title="Silencio">🔇</button>
          <button class="btn btn-md btn-green" onclick="send('info')" title="Info">ℹ</button>
          <button class="btn btn-md btn-yellow" onclick="send('subtitle')" title="Subtítulos">⊡</button>
        </div>
        <div class="vcol">
          <div class="vc-label">CH</div>
          <button class="btn vc-btn" onclick="send('ch_up')">＋</button>
          <div class="vc-mid">CH</div>
          <button class="btn vc-btn" onclick="send('ch_down')">－</button>
        </div>
      </div>
      <!-- SLEEP + ASPECT -->
      <div class="row">
        <button class="btn btn-lg" onclick="send('sleep')" title="Sleep Timer">SLEEP</button>
        <button class="btn btn-lg" onclick="send('aspect')" title="Aspecto">ASPECT</button>
      </div>
      <!-- NUMPAD -->
      <div class="numpad">
        <button class="btn num-btn" onclick="send('1')">1</button>
        <button class="btn num-btn" onclick="send('2')">2</button>
        <button class="btn num-btn" onclick="send('3')">3</button>
        <button class="btn num-btn" onclick="send('4')">4</button>
        <button class="btn num-btn" onclick="send('5')">5</button>
        <button class="btn num-btn" onclick="send('6')">6</button>
        <button class="btn num-btn" onclick="send('7')">7</button>
        <button class="btn num-btn" onclick="send('8')">8</button>
        <button class="btn num-btn" onclick="send('9')">9</button>
        <button class="btn num-btn btn-yellow" onclick="send('*')">✳</button>
        <button class="btn num-btn" onclick="send('0')">0</button>
        <button class="btn num-btn btn-red2" onclick="send('#')">🔙</button>
      </div>
    </div><!-- /remote -->
  </div><!-- /remote-wrap -->

  <!-- RIGHT: Log -->
  <div class="log-panel">
    <div class="panel-title">ACTIVIDAD</div>
    <div class="log-list" id="logList">
      <div class="log-entry" style="font-size:.65rem;">
        <span class="log-time">--:--:--</span>Sistema iniciado
      </div>
    </div>
  </div>
</div><!-- /app -->

<div id="toast">OK</div>

<script>
let selectedDevice = null;
let devices = [];

async function loadNetworkInfo() {
  try {
    const r = await fetch('/api/network_info');
    const d = await r.json();
    document.getElementById('localIp').textContent = d.local_ip;
  } catch(e) {}
}

async function loadDevices() {
  try {
    const r = await fetch('/api/devices');
    const d = await r.json();
    devices = d.devices;
    renderDevices();
    updateWifi();
    // Auto-seleccionar si hay un dispositivo guardado
    const saved = localStorage.getItem('lastDeviceIp');
    if (saved && !selectedDevice) {
      const found = devices.find(d => d.ip === saved);
      if (found) selectDevice(found);
    }
  } catch(e) {}
}

function renderDevices() {
  const list = document.getElementById('deviceList');
  if (devices.length === 0) {
    list.innerHTML = `<div class="no-devices"><div class="spinner"></div>
      Buscando Smart TVs<br>en tu red WiFi...<br>
      <small style="color:#2a4a6a;margin-top:6px;display:block">SSDP + subnet scan activo</small>
    </div>`;
    return;
  }
  list.innerHTML = devices.map(d => `
    <div class="device-card ${selectedDevice?.ip===d.ip?'active':''}"
         onclick='selectDevice(${JSON.stringify(d)})'>
      <div class="device-name">${d.name}</div>
      <div class="device-ip">${d.ip}</div>
      <div class="device-type">${typeLabel(d.type)}${d.model?' · '+d.model:''}</div>
      <div class="device-method">${d.source||''}</div>
    </div>
  `).join('');
}

function typeLabel(t) {
  const map = {android_tv:'ANDROID TV',android_box:'ANDROID BOX',samsung_tv:'SAMSUNG TV',
    lg_tv:'LG TV',sony_tv:'SONY TV',philips_tv:'PHILIPS TV',smart_tv:'SMART TV',
    roku:'ROKU',chromecast:'CHROMECAST',kodi:'KODI',tv:'TV'};
  return map[t]||'SMART TV';
}

function selectDevice(device) {
  if (typeof device === 'string') device = JSON.parse(device);
  selectedDevice = device;
  renderDevices();
  updateStatus(`Conectado: ${device.name}`, true);
  updateSignal(4);
  addLog(`Conectado: ${device.name} (${device.ip})`, 'ok');
  showToast(`📡 ${device.name}`);
  localStorage.setItem('lastDeviceIp', device.ip);
}

async function addManual() {
  const ip = document.getElementById('manualIp').value.trim();
  if (!ip) return;
  try {
    const r = await fetch('/api/add_device', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({ip})
    });
    const d = await r.json();
    showToast(d.status==='ok' ? `✓ ${d.name}` : '⚠ Sin respuesta');
    addLog(`Manual: ${ip} → ${d.name||'?'}`, d.status==='ok'?'ok':'err');
    setTimeout(loadDevices, 1000);
  } catch(e) {
    showToast('⚠ Error al conectar');
  }
  document.getElementById('manualIp').value = '';
}

function updateWifi() {
  const dot = document.getElementById('wifiDot');
  const lbl = document.getElementById('wifiLabel');
  if (selectedDevice) {
    dot.className='wifi-dot connected'; lbl.textContent=selectedDevice.name;
  } else if (devices.length>0) {
    dot.className='wifi-dot connected'; lbl.textContent=`${devices.length} dispositivo(s)`;
  } else {
    dot.className='wifi-dot'; lbl.textContent='Buscando...';
  }
}

function updateStatus(msg, ok) {
  document.getElementById('statusTxt').textContent = msg;
  const dot = document.getElementById('statusDot');
  dot.className = 'status-dot'+(ok?' ok':ok===false?' err':'');
}

function updateSignal(level) {
  for(let i=1;i<=4;i++)
    document.getElementById(`b${i}`).className='bar'+(i<=level?' active':'');
}

async function scanDevices() {
  const btn = document.getElementById('scanBtn');
  btn.className='scan-btn scanning'; btn.textContent='⟳ ESCANEANDO...';
  addLog('Iniciando SSDP + subnet scan...','cmd');
  try {
    await fetch('/api/scan',{method:'POST'});
    setTimeout(async()=>{
      await loadDevices();
      btn.className='scan-btn'; btn.textContent='⟳ ESCANEAR RED';
      addLog(`Encontrados: ${devices.length} dispositivos`,devices.length?'ok':'');
    }, 8000);
  } catch(e) {
    btn.className='scan-btn'; btn.textContent='⟳ ESCANEAR RED';
  }
}

async function send(cmd) {
  if (!selectedDevice) {
    showToast('⚠ Selecciona un dispositivo');
    addLog('Sin dispositivo seleccionado','err');
    return;
  }
  // Vibración (si disponible)
  const s = JSON.parse(localStorage.getItem('remoteSettings')||'{}');
  if (s.vibration && navigator.vibrate) navigator.vibrate(30);

  const label = cmd.toUpperCase();
  addLog(`CMD → ${label}`, 'cmd');
  try {
    const r = await fetch('/api/command',{
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({ip:selectedDevice.ip, command:cmd, device_type:selectedDevice.type})
    });
    const d = await r.json();
    if (d.status==='ok') {
      addLog(`✓ ${label} [${d.method}]`,'ok');
      updateStatus(`${label} enviado`,true);
    } else {
      addLog(`✗ ${d.message||'Error'}`,'err');
      updateStatus(`Error: ${cmd}`,false);
    }
  } catch(e) { addLog('✗ Sin respuesta','err'); }
  showToast(label);
}

function addLog(msg, type='') {
  const list = document.getElementById('logList');
  const t = new Date().toLocaleTimeString('es',{hour12:false});
  const el = document.createElement('div');
  el.className=`log-entry ${type}`;
  el.innerHTML=`<span class="log-time">${t}</span>${msg}`;
  list.prepend(el);
  while(list.children.length>80) list.removeChild(list.lastChild);
}

let toastTimer;
function showToast(msg) {
  const t=document.getElementById('toast');
  t.textContent=msg; t.classList.add('show');
  clearTimeout(toastTimer);
  toastTimer=setTimeout(()=>t.classList.remove('show'),1200);
}

function applySettings() {
  const s = JSON.parse(localStorage.getItem('remoteSettings')||'{}');
  const remote = document.getElementById('remote');
  if (s.scale) { remote.style.transform=`scale(${s.scale})`; remote.style.transformOrigin='top center'; }
  if (s.accentColor) document.documentElement.style.setProperty('--accent',s.accentColor);
  if (s.accentColor2) document.documentElement.style.setProperty('--accent2',s.accentColor2);
  if (s.scanlines===false) document.body.classList.remove('scanlines');
  else document.body.classList.add('scanlines');
}

document.addEventListener('keydown', e => {
  const s = JSON.parse(localStorage.getItem('remoteSettings')||'{}');
  if (s.keyShortcuts===false) return;
  const map = {
    ArrowUp:'up',ArrowDown:'down',ArrowLeft:'left',ArrowRight:'right',
    Enter:'ok',Backspace:'back',Escape:'home','+':'vol_up','-':'vol_down',
    'm':'mute','p':'play','s':'stop',
  };
  if (map[e.key]) { e.preventDefault(); send(map[e.key]); }
});

// ── Init ──
applySettings();
loadNetworkInfo();
loadDevices();
setInterval(loadDevices, 8000);
</script>
</body>
</html>
"""

# ─────────────────────────────────────────────
#  HTML EMBEBIDO - SETTINGS
# ─────────────────────────────────────────────

SETTINGS_HTML = r"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1.0"/>
<title>SmartRemote – Ajustes</title>
<link href="https://fonts.googleapis.com/css2?family=Orbitron:wght@400;600;900&family=Rajdhani:wght@300;500;700&display=swap" rel="stylesheet"/>
<style>
  :root{--bg:#070b12;--panel:#0d1420;--panel2:#111c2e;--border:#1a2f4a;
    --accent:#00d4ff;--accent2:#00ff88;--accent3:#ff4f6d;--accent4:#ffb800;
    --btn-bg:#0f1e30;--text:#c8e4ff;--text-dim:#4a7090;
    --glow:0 0 20px #00d4ff55;--glow2:0 0 15px #00ff8855;}
  *{margin:0;padding:0;box-sizing:border-box;}
  body{background:var(--bg);color:var(--text);font-family:'Rajdhani',sans-serif;min-height:100vh;
    background-image:radial-gradient(ellipse at 10% 30%,#001a3322,transparent 55%),
      radial-gradient(ellipse at 90% 70%,#002a1a22,transparent 55%);}
  body::before{content:'';position:fixed;inset:0;
    background:repeating-linear-gradient(0deg,transparent,transparent 3px,#00000018 3px,#00000018 4px);
    pointer-events:none;z-index:9999;}
  header{display:flex;align-items:center;gap:16px;padding:12px 24px;
    border-bottom:1px solid var(--border);background:#0a111a;position:sticky;top:0;z-index:100;}
  .back-btn{text-decoration:none;color:var(--text-dim);border:1px solid var(--border);
    padding:6px 14px;border-radius:6px;font-size:.8rem;letter-spacing:1px;
    transition:all .2s;display:flex;align-items:center;gap:6px;}
  .back-btn:hover{border-color:var(--accent);color:var(--accent);box-shadow:var(--glow);}
  .page-title{font-family:'Orbitron',monospace;font-size:1rem;font-weight:900;color:var(--accent);
    letter-spacing:3px;text-shadow:var(--glow);}
  .version-tag{margin-left:auto;font-size:.65rem;color:var(--text-dim);letter-spacing:2px;}
  .settings-grid{display:grid;grid-template-columns:1fr 1fr;gap:20px;padding:24px;max-width:1000px;margin:0 auto;}
  @media(max-width:700px){.settings-grid{grid-template-columns:1fr;}}
  .section{background:var(--panel);border:1px solid var(--border);border-radius:16px;padding:20px;
    position:relative;overflow:hidden;}
  .section::before{content:'';position:absolute;top:0;left:0;right:0;height:1px;
    background:linear-gradient(90deg,transparent,var(--accent),transparent);opacity:.4;}
  .section-title{font-family:'Orbitron',monospace;font-size:.65rem;color:var(--accent);
    letter-spacing:3px;margin-bottom:16px;display:flex;align-items:center;gap:10px;}
  .section-title::after{content:'';flex:1;height:1px;background:var(--border);}
  .field{margin-bottom:14px;}
  label{display:block;font-size:.75rem;color:var(--text-dim);letter-spacing:1px;margin-bottom:5px;}
  input[type=range]{width:100%;-webkit-appearance:none;height:4px;background:var(--border);border-radius:2px;outline:none;}
  input[type=range]::-webkit-slider-thumb{-webkit-appearance:none;width:16px;height:16px;border-radius:50%;
    background:var(--accent);cursor:pointer;box-shadow:0 0 8px var(--accent);transition:transform .1s;}
  input[type=range]::-webkit-slider-thumb:hover{transform:scale(1.3);}
  input[type=color]{width:100%;height:36px;border:1px solid var(--border);border-radius:6px;
    background:var(--btn-bg);cursor:pointer;padding:2px;}
  .range-val{float:right;font-family:'Orbitron',monospace;font-size:.7rem;color:var(--accent);}
  select,input[type=text]{width:100%;background:var(--btn-bg);border:1px solid var(--border);
    color:var(--text);padding:8px 12px;border-radius:8px;font-family:'Rajdhani',sans-serif;
    font-size:.9rem;outline:none;transition:border-color .2s;}
  select:focus,input[type=text]:focus{border-color:var(--accent);}
  .toggle-row{display:flex;align-items:center;justify-content:space-between;
    padding:8px 0;border-bottom:1px solid #0e1e30;}
  .toggle-label{font-size:.85rem;color:var(--text);}
  .toggle-sub{font-size:.7rem;color:var(--text-dim);}
  .toggle{position:relative;width:44px;height:24px;}
  .toggle input{opacity:0;width:0;height:0;}
  .toggle-track{position:absolute;inset:0;background:#0a1520;border:1px solid var(--border);
    border-radius:12px;cursor:pointer;transition:all .3s;}
  .toggle-track::after{content:'';position:absolute;left:3px;top:50%;transform:translateY(-50%);
    width:16px;height:16px;border-radius:50%;background:var(--text-dim);transition:all .3s;}
  .toggle input:checked + .toggle-track{background:#00ff8822;border-color:var(--accent2);}
  .toggle input:checked + .toggle-track::after{left:calc(100% - 19px);background:var(--accent2);box-shadow:0 0 8px var(--accent2);}
  /* HUD */
  .hud-section{grid-column:1/-1;}
  .hud-container{position:relative;background:#050c14;border:1px solid var(--border);
    border-radius:12px;height:380px;overflow:hidden;cursor:crosshair;}
  .hud-grid{position:absolute;inset:0;
    background-image:linear-gradient(#0e1e3018 1px,transparent 1px),linear-gradient(90deg,#0e1e3018 1px,transparent 1px);
    background-size:24px 24px;pointer-events:none;}
  .hud-center-cross{position:absolute;top:50%;left:50%;transform:translate(-50%,-50%);pointer-events:none;}
  .hud-center-cross::before,.hud-center-cross::after{content:'';position:absolute;background:#1a2f4a;}
  .hud-center-cross::before{width:1px;height:60px;left:0;top:-30px;}
  .hud-center-cross::after{width:60px;height:1px;left:-30px;top:0;}
  #remotePreview{position:absolute;width:100px;height:200px;
    background:linear-gradient(160deg,#131f30,#0c1622);border:1px solid #1e3050;border-radius:20px;
    cursor:grab;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:8px;
    box-shadow:0 8px 24px #00000088;transition:box-shadow .2s;user-select:none;touch-action:none;}
  #remotePreview:active{cursor:grabbing;box-shadow:var(--glow);}
  #remotePreview .preview-icon{font-size:1.8rem;}
  #remotePreview .preview-label{font-family:'Orbitron',monospace;font-size:.45rem;color:var(--text-dim);letter-spacing:2px;text-align:center;}
  .preview-btn-demo{width:60px;height:6px;background:var(--btn-bg);border:1px solid var(--border);border-radius:3px;}
  .hud-coords{position:absolute;bottom:8px;right:12px;font-family:'Orbitron',monospace;font-size:.55rem;color:var(--text-dim);}
  .hud-info{position:absolute;top:8px;left:12px;font-family:'Orbitron',monospace;font-size:.55rem;color:var(--accent);letter-spacing:2px;}
  .hud-corner{position:absolute;width:16px;height:16px;border-color:var(--accent);border-style:solid;opacity:.4;}
  .hud-corner.tl{top:8px;left:8px;border-width:1px 0 0 1px;}
  .hud-corner.tr{top:8px;right:8px;border-width:1px 1px 0 0;}
  .hud-corner.bl{bottom:8px;left:8px;border-width:0 0 1px 1px;}
  .hud-corner.br{bottom:8px;right:8px;border-width:0 1px 1px 0;}
  .action-row{display:flex;gap:10px;margin-top:16px;flex-wrap:wrap;}
  .btn-action{flex:1;min-width:100px;padding:10px;border-radius:8px;border:1px solid var(--border);
    background:var(--btn-bg);color:var(--text);font-family:'Orbitron',monospace;font-size:.6rem;
    letter-spacing:2px;cursor:pointer;transition:all .2s;}
  .btn-action:hover{border-color:var(--accent);color:var(--accent);box-shadow:var(--glow);}
  .btn-action.primary{border-color:var(--accent2);color:var(--accent2);}
  .btn-action.primary:hover{box-shadow:var(--glow2);}
  .btn-action.danger{border-color:#3a1020;color:var(--accent3);}
  .btn-action.danger:hover{box-shadow:0 0 16px #ff4f6d44;}
  /* Network */
  .device-row{display:flex;align-items:center;justify-content:space-between;
    padding:10px 12px;background:var(--btn-bg);border:1px solid var(--border);
    border-radius:8px;margin-bottom:8px;}
  .device-row-name{font-size:.85rem;font-weight:700;}
  .device-row-ip{font-size:.7rem;color:var(--text-dim);font-family:monospace;}
  .device-row-type{font-size:.65rem;color:var(--accent2);letter-spacing:1px;}
  .ping-badge{padding:2px 8px;border-radius:10px;font-size:.6rem;font-family:'Orbitron',monospace;
    border:1px solid var(--accent2);color:var(--accent2);background:#00ff8811;}
  #toast{position:fixed;bottom:24px;left:50%;transform:translateX(-50%) translateY(80px);
    background:#0d1a2a;border:1px solid var(--accent);color:var(--accent);padding:8px 20px;
    border-radius:20px;font-size:.75rem;font-family:'Orbitron',monospace;letter-spacing:2px;
    z-index:9000;transition:transform .3s cubic-bezier(.175,.885,.32,1.275);
    box-shadow:var(--glow);pointer-events:none;}
  #toast.show{transform:translateX(-50%) translateY(0);}
  ::-webkit-scrollbar{width:4px;}
  ::-webkit-scrollbar-thumb{background:var(--border);border-radius:2px;}
  .protocol-badge{display:inline-block;padding:2px 8px;border-radius:6px;font-size:.6rem;
    font-family:'Orbitron',monospace;letter-spacing:1px;border:1px solid var(--border);
    margin:2px;color:var(--text-dim);}
  .protocol-badge.active{border-color:var(--accent2);color:var(--accent2);background:#00ff8811;}
</style>
</head>
<body>
<header>
  <a href="/" class="back-btn">← REMOTE</a>
  <div class="page-title">⚙ AJUSTES</div>
  <div class="version-tag">v3.0.0</div>
</header>

<div class="settings-grid">
  <!-- HUD -->
  <div class="section hud-section">
    <div class="section-title">HUD · POSICIÓN Y TAMAÑO DEL CONTROL</div>
    <p style="font-size:.75rem;color:var(--text-dim);margin-bottom:12px;">
      Arrastra el control en el HUD para ajustar su posición. Usa el deslizador para el tamaño.
    </p>
    <div class="hud-container" id="hudContainer">
      <div class="hud-grid"></div>
      <div class="hud-corner tl"></div><div class="hud-corner tr"></div>
      <div class="hud-corner bl"></div><div class="hud-corner br"></div>
      <div class="hud-center-cross"></div>
      <div class="hud-info">◈ POSITION HUD</div>
      <div id="remotePreview">
        <div class="preview-icon">📱</div>
        <div class="preview-btn-demo"></div><div class="preview-btn-demo"></div><div class="preview-btn-demo"></div>
        <div class="preview-label">SMART<br>REMOTE</div>
      </div>
      <div class="hud-coords" id="hudCoords">X:0  Y:0</div>
    </div>
    <div class="field" style="margin-top:14px;">
      <label>TAMAÑO DEL CONTROL <span class="range-val" id="scaleVal">100%</span></label>
      <input type="range" id="scaleSlider" min="60" max="150" value="100" oninput="updateScale(this.value)"/>
    </div>
    <div class="action-row">
      <button class="btn-action" onclick="resetPosition()">↺ RESETEAR</button>
      <button class="btn-action primary" onclick="saveHUD()">✓ GUARDAR POSICIÓN</button>
    </div>
  </div>

  <!-- APARIENCIA -->
  <div class="section">
    <div class="section-title">APARIENCIA</div>
    <div class="field">
      <label>COLOR DE ACENTO</label>
      <input type="color" id="accentColor" value="#00d4ff" oninput="previewAccent(this.value)"/>
    </div>
    <div class="field">
      <label>TEMA</label>
      <select id="themeSelect" onchange="applyTheme(this.value)">
        <option value="cyber">Cyberpunk Azul</option>
        <option value="neon">Neon Verde</option>
        <option value="fire">Fire Rojo</option>
        <option value="gold">Gold Ámbar</option>
        <option value="purple">Púrpura</option>
      </select>
    </div>
    <div class="toggle-row">
      <div><div class="toggle-label">Efecto Scanlines</div>
        <div class="toggle-sub">Líneas de escaneo en el fondo</div></div>
      <label class="toggle"><input type="checkbox" id="scanlines" checked onchange="saveSetting('scanlines',this.checked)"/>
        <div class="toggle-track"></div></label>
    </div>
    <div class="toggle-row">
      <div><div class="toggle-label">Animaciones</div>
        <div class="toggle-sub">Efectos de pulso y brillo</div></div>
      <label class="toggle"><input type="checkbox" id="animations" checked onchange="saveSetting('animations',this.checked)"/>
        <div class="toggle-track"></div></label>
    </div>
    <div class="toggle-row">
      <div><div class="toggle-label">Feedback de Vibración</div>
        <div class="toggle-sub">Vibra al presionar botones (móvil)</div></div>
      <label class="toggle"><input type="checkbox" id="vibration" onchange="saveSetting('vibration',this.checked)"/>
        <div class="toggle-track"></div></label>
    </div>
    <div class="action-row">
      <button class="btn-action primary" onclick="saveAppearance()">✓ APLICAR</button>
    </div>
  </div>

  <!-- RED WiFi -->
  <div class="section">
    <div class="section-title">RED WIFI</div>
    <div class="field">
      <label>IP LOCAL</label>
      <input type="text" id="localIpField" value="Cargando..." readonly style="font-family:monospace;color:var(--accent2);"/>
    </div>
    <div class="field">
      <label>SUBRED</label>
      <input type="text" id="subnetField" value="Cargando..." readonly style="font-family:monospace;color:var(--text-dim);"/>
    </div>
    <div class="field">
      <label>INTERVALO DE ESCANEO (s) <span class="range-val" id="scanVal">20s</span></label>
      <input type="range" id="scanInterval" min="5" max="60" value="20"
             oninput="document.getElementById('scanVal').textContent=this.value+'s';saveSetting('scanInterval',this.value)"/>
    </div>
    <div class="field">
      <label>DISPOSITIVOS DETECTADOS</label>
      <div id="networkDevices"><div style="color:var(--text-dim);font-size:.75rem;padding:10px 0;">Cargando...</div></div>
    </div>
    <div class="field">
      <label>PROTOCOLOS ACTIVOS</label>
      <div>
        <span class="protocol-badge active">SSDP</span>
        <span class="protocol-badge active">UPnP/SOAP</span>
        <span class="protocol-badge active">Samsung REST</span>
        <span class="protocol-badge active">LG UDAP</span>
        <span class="protocol-badge active">Sony IRCC</span>
        <span class="protocol-badge active">Roku ECP</span>
        <span class="protocol-badge active">Kodi RPC</span>
        <span class="protocol-badge active">Subnet Scan</span>
      </div>
    </div>
    <button class="btn-action" style="width:100%;margin-top:4px;" onclick="pingScan()">⟳ ESCANEAR AHORA</button>
  </div>

  <!-- CONTROL REMOTO -->
  <div class="section">
    <div class="section-title">CONTROL REMOTO</div>
    <div class="toggle-row">
      <div><div class="toggle-label">Atajos de Teclado</div>
        <div class="toggle-sub">Flechas, Enter, M, P, S</div></div>
      <label class="toggle"><input type="checkbox" id="keyShortcuts" checked onchange="saveSetting('keyShortcuts',this.checked)"/>
        <div class="toggle-track"></div></label>
    </div>
    <div class="toggle-row">
      <div><div class="toggle-label">Log de Actividad</div>
        <div class="toggle-sub">Muestra historial de comandos</div></div>
      <label class="toggle"><input type="checkbox" id="activityLog" checked onchange="saveSetting('activityLog',this.checked)"/>
        <div class="toggle-track"></div></label>
    </div>
    <div class="toggle-row">
      <div><div class="toggle-label">Auto-reconexión</div>
        <div class="toggle-sub">Reconecta al último dispositivo al abrir</div></div>
      <label class="toggle"><input type="checkbox" id="autoReconnect" checked onchange="saveSetting('autoReconnect',this.checked)"/>
        <div class="toggle-track"></div></label>
    </div>
    <div class="toggle-row">
      <div><div class="toggle-label">Modo Agresivo de Escaneo</div>
        <div class="toggle-sub">Escanea más puertos (más lento pero más completo)</div></div>
      <label class="toggle"><input type="checkbox" id="aggressiveScan" onchange="saveSetting('aggressiveScan',this.checked)"/>
        <div class="toggle-track"></div></label>
    </div>
    <div class="field" style="margin-top:14px;">
      <label>TIEMPO DE ESPERA COMANDO (ms) <span class="range-val" id="timeoutVal">3000ms</span></label>
      <input type="range" id="cmdTimeout" min="500" max="10000" step="500" value="3000"
             oninput="document.getElementById('timeoutVal').textContent=this.value+'ms';saveSetting('cmdTimeout',this.value)"/>
    </div>
    <div class="action-row">
      <button class="btn-action danger" onclick="resetAll()">↺ RESET TODO</button>
      <button class="btn-action primary" onclick="saveAll()">✓ GUARDAR</button>
    </div>
  </div>

  <!-- SOBRE -->
  <div class="section" style="grid-column:1/-1;">
    <div class="section-title">INFORMACIÓN</div>
    <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px;">
      <div style="background:var(--btn-bg);border:1px solid var(--border);border-radius:10px;padding:14px;">
        <div style="font-size:.65rem;color:var(--text-dim);letter-spacing:2px;">VERSIÓN</div>
        <div style="font-family:'Orbitron',monospace;color:var(--accent);margin-top:4px;">3.0.0</div>
      </div>
      <div style="background:var(--btn-bg);border:1px solid var(--border);border-radius:10px;padding:14px;">
        <div style="font-size:.65rem;color:var(--text-dim);letter-spacing:2px;">PROTOCOLOS</div>
        <div style="font-family:'Orbitron',monospace;color:var(--accent2);margin-top:4px;font-size:.7rem;">SSDP/UPnP/IRCC/ECP</div>
      </div>
      <div style="background:var(--btn-bg);border:1px solid var(--border);border-radius:10px;padding:14px;">
        <div style="font-size:.65rem;color:var(--text-dim);letter-spacing:2px;">DISPOSITIVOS</div>
        <div style="font-family:'Orbitron',monospace;color:var(--accent4);margin-top:4px;" id="devCount">0</div>
      </div>
      <div style="background:var(--btn-bg);border:1px solid var(--border);border-radius:10px;padding:14px;">
        <div style="font-size:.65rem;color:var(--text-dim);letter-spacing:2px;">SERVIDOR</div>
        <div style="font-family:'Orbitron',monospace;color:var(--accent);margin-top:4px;">:5000</div>
      </div>
    </div>
    <div style="margin-top:14px;font-size:.7rem;color:var(--text-dim);line-height:1.8;">
      <strong style="color:var(--accent);">Compatibilidad:</strong>
      Samsung Smart TV (Tizen) · LG Smart TV (webOS) · Sony Bravia (IRCC-IP) ·
      Android TV/Box · Roku · Philips · Cualquier dispositivo UPnP/DLNA<br>
      <strong style="color:var(--accent2);">Descubrimiento:</strong>
      SSDP Multicast + Subnet Scan automático (puertos 8001, 8060, 80, 3000, 55000, 8080...)
    </div>
  </div>
</div>

<div id="toast">GUARDADO</div>

<script>
async function loadNet() {
  try {
    const r = await fetch('/api/network_info');
    const d = await r.json();
    document.getElementById('localIpField').value = d.local_ip;
    document.getElementById('subnetField').value = d.subnet;
  } catch(e){}
}

async function loadNetDevices() {
  try {
    const r = await fetch('/api/devices');
    const d = await r.json();
    document.getElementById('devCount').textContent = d.count;
    const container = document.getElementById('networkDevices');
    if(d.devices.length===0) {
      container.innerHTML='<div style="color:var(--text-dim);font-size:.75rem;padding:10px 0;">Sin dispositivos detectados</div>';
      return;
    }
    const typeNames = {samsung_tv:'Samsung TV',lg_tv:'LG TV',sony_tv:'Sony TV',android_tv:'Android TV',
      android_box:'Android Box',roku:'Roku',smart_tv:'Smart TV',tv:'TV',chromecast:'Chromecast'};
    container.innerHTML = d.devices.map(dev=>`
      <div class="device-row">
        <div>
          <div class="device-row-name">${dev.name}</div>
          <div class="device-row-ip">${dev.ip}</div>
          <div class="device-row-type">${typeNames[dev.type]||dev.type} · ${dev.source||'ssdp'}</div>
        </div>
        <span class="ping-badge">ONLINE</span>
      </div>
    `).join('');
  } catch(e){}
}

async function pingScan() {
  showToast('ESCANEANDO...');
  await fetch('/api/scan',{method:'POST'});
  setTimeout(loadNetDevices,8000);
}

// HUD Drag
const preview = document.getElementById('remotePreview');
const container = document.getElementById('hudContainer');
const coords = document.getElementById('hudCoords');
let dragging=false, ox=0, oy=0, px=150, py=90;
setPreviewPos(px,py);

function setPreviewPos(x,y){
  const cw=container.offsetWidth||600, ch=container.offsetHeight||380;
  x=Math.max(0,Math.min(x,cw-100)); y=Math.max(0,Math.min(y,ch-200));
  px=x; py=y;
  preview.style.left=x+'px'; preview.style.top=y+'px';
  coords.textContent=`X:${Math.round(x/cw*100)}%  Y:${Math.round(y/ch*100)}%`;
}

preview.addEventListener('mousedown',e=>{dragging=true;ox=e.offsetX;oy=e.offsetY;e.preventDefault();});
document.addEventListener('mousemove',e=>{
  if(!dragging)return;
  const rect=container.getBoundingClientRect();
  setPreviewPos(e.clientX-rect.left-ox,e.clientY-rect.top-oy);
});
document.addEventListener('mouseup',()=>{dragging=false;});
preview.addEventListener('touchstart',e=>{
  dragging=true;const t=e.touches[0];
  const rect=preview.getBoundingClientRect();
  ox=t.clientX-rect.left;oy=t.clientY-rect.top;e.preventDefault();
},{passive:false});
document.addEventListener('touchmove',e=>{
  if(!dragging)return;
  const t=e.touches[0],rect=container.getBoundingClientRect();
  setPreviewPos(t.clientX-rect.left-ox,t.clientY-rect.top-oy);e.preventDefault();
},{passive:false});
document.addEventListener('touchend',()=>{dragging=false;});

function updateScale(v){
  document.getElementById('scaleVal').textContent=v+'%';
  preview.style.width=(100*v/100)+'px';preview.style.height=(200*v/100)+'px';
}
function resetPosition(){
  setPreviewPos(container.offsetWidth/2-50,container.offsetHeight/2-100);
  document.getElementById('scaleSlider').value=100;updateScale(100);showToast('RESETEADO');
}
function saveHUD(){
  const cw=container.offsetWidth,ch=container.offsetHeight;
  const scale=document.getElementById('scaleSlider').value;
  const s=JSON.parse(localStorage.getItem('remoteSettings')||'{}');
  s.posX=Math.round((px/cw-0.5)*200);s.posY=Math.round((py/ch)*100);s.scale=scale/100;
  localStorage.setItem('remoteSettings',JSON.stringify(s));showToast('✓ POSICIÓN GUARDADA');
}

const THEMES={
  cyber:{accent:'#00d4ff',accent2:'#00ff88'},neon:{accent:'#39ff14',accent2:'#00ffcc'},
  fire:{accent:'#ff4f1f',accent2:'#ffb800'},gold:{accent:'#ffb800',accent2:'#ff8c00'},
  purple:{accent:'#bf00ff',accent2:'#ff00aa'},
};
function applyTheme(t){
  const theme=THEMES[t];if(!theme)return;
  document.documentElement.style.setProperty('--accent',theme.accent);
  document.documentElement.style.setProperty('--accent2',theme.accent2);
  document.getElementById('accentColor').value=theme.accent;
}
function previewAccent(v){document.documentElement.style.setProperty('--accent',v);}
function saveAppearance(){
  const s=JSON.parse(localStorage.getItem('remoteSettings')||'{}');
  s.accentColor=document.getElementById('accentColor').value;
  s.theme=document.getElementById('themeSelect').value;
  s.scanlines=document.getElementById('scanlines').checked;
  s.animations=document.getElementById('animations').checked;
  s.vibration=document.getElementById('vibration').checked;
  localStorage.setItem('remoteSettings',JSON.stringify(s));showToast('✓ APARIENCIA GUARDADA');
}
function saveSetting(key,value){
  const s=JSON.parse(localStorage.getItem('remoteSettings')||'{}');
  s[key]=value;localStorage.setItem('remoteSettings',JSON.stringify(s));
}
function saveAll(){saveAppearance();saveHUD();showToast('✓ TODO GUARDADO');}
function resetAll(){
  if(!confirm('¿Restablecer todos los ajustes?'))return;
  localStorage.removeItem('remoteSettings');showToast('↺ AJUSTES RESETEADOS');
  setTimeout(()=>location.reload(),800);
}
function loadSettings(){
  const s=JSON.parse(localStorage.getItem('remoteSettings')||'{}');
  if(s.accentColor){document.getElementById('accentColor').value=s.accentColor;previewAccent(s.accentColor);}
  if(s.theme)document.getElementById('themeSelect').value=s.theme;
  if(s.scale!==undefined){const p=Math.round(s.scale*100);document.getElementById('scaleSlider').value=p;updateScale(p);}
  if(s.scanlines!==undefined)document.getElementById('scanlines').checked=s.scanlines;
  if(s.animations!==undefined)document.getElementById('animations').checked=s.animations;
  if(s.vibration!==undefined)document.getElementById('vibration').checked=s.vibration;
  if(s.keyShortcuts!==undefined)document.getElementById('keyShortcuts').checked=s.keyShortcuts;
  if(s.activityLog!==undefined)document.getElementById('activityLog').checked=s.activityLog;
  if(s.autoReconnect!==undefined)document.getElementById('autoReconnect').checked=s.autoReconnect;
}
let toastTimer;
function showToast(msg){
  const t=document.getElementById('toast');t.textContent=msg;t.classList.add('show');
  clearTimeout(toastTimer);toastTimer=setTimeout(()=>t.classList.remove('show'),1800);
}
loadNet();loadSettings();loadNetDevices();
setInterval(loadNetDevices,15000);
</script>
</body>
</html>
"""

# ─────────────────────────────────────────────
#  RUTAS FLASK
# ─────────────────────────────────────────────

@app.route("/")
def index():
    return Response(REMOTE_HTML, mimetype="text/html")

@app.route("/settings")
def settings():
    return Response(SETTINGS_HTML, mimetype="text/html")

@app.route("/api/devices")
def get_devices():
    with discovery_lock:
        devices = list(discovered_devices.values())
    # Ordenar: manual primero, luego por nombre
    devices.sort(key=lambda d: (d.get("source", "") != "manual", d.get("name", "")))
    return jsonify({"devices": devices, "count": len(devices)})

@app.route("/api/scan", methods=["POST"])
def scan_now():
    """Lanza un escaneo inmediato en hilo separado."""
    def quick_scan():
        print("[SCAN] Escaneo manual iniciado...")
        # SSDP primero (rápido)
        results = []
        for st in SSDP_ST_LIST:
            results.extend(ssdp_discover(st, timeout=3))
        seen = set()
        for ip, raw in results:
            if ip in seen:
                continue
            seen.add(ip)
            register_device(ip, raw, source="ssdp")

        # Subnet scan (más profundo)
        local_ip, subnet = get_local_subnet()
        hosts = scan_subnet_range(subnet, 1, 254)
        for ip, port in hosts:
            if ip not in discovered_devices and ip != local_ip:
                register_device(ip, "", source=f"scan:{port}")
        print(f"[SCAN] Completado. Dispositivos: {len(discovered_devices)}")

    t = threading.Thread(target=quick_scan, daemon=True)
    t.start()
    return jsonify({"status": "scanning"})

@app.route("/api/add_device", methods=["POST"])
def add_device_manual():
    """Añade un dispositivo por IP manual."""
    data = request.json
    ip = data.get("ip", "").strip()
    if not ip:
        return jsonify({"status": "error", "message": "IP requerida"}), 400

    # Validar formato básico
    parts = ip.split(".")
    if len(parts) != 4:
        return jsonify({"status": "error", "message": "IP inválida"}), 400

    def probe():
        register_device(ip, "", source="manual")
        manual_devices[ip] = True

    t = threading.Thread(target=probe, daemon=True)
    t.start()
    t.join(timeout=5)

    with discovery_lock:
        dev = discovered_devices.get(ip)

    if dev:
        return jsonify({"status": "ok", "name": dev["name"], "type": dev["type"]})
    else:
        # Añadir como desconocido de todas formas
        with discovery_lock:
            discovered_devices[ip] = {
                "ip": ip, "name": f"Dispositivo {ip}", "model": "",
                "manufacturer": "", "type": "tv", "location": "",
                "server": "", "source": "manual", "last_seen": time.time()
            }
            manual_devices[ip] = True
        return jsonify({"status": "ok", "name": f"Dispositivo {ip}", "type": "tv"})

@app.route("/api/command", methods=["POST"])
def send_command():
    data = request.json
    ip = data.get("ip")
    cmd = data.get("command")
    device_type = data.get("device_type", "tv")

    if not ip or not cmd:
        return jsonify({"status": "error", "message": "Falta ip o command"}), 400

    result = smart_send_command(ip, cmd, device_type)
    print(f"[CMD] {ip} ({device_type}) → {cmd} | {result.get('method','?')}")

    # Actualizar last_seen
    with discovery_lock:
        if ip in discovered_devices:
            discovered_devices[ip]["last_seen"] = time.time()

    return jsonify(result)

@app.route("/api/network_info")
def network_info():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        local_ip = s.getsockname()[0]
        s.close()
    except:
        local_ip = "127.0.0.1"
    try:
        hostname = socket.gethostname()
    except:
        hostname = "localhost"
    subnet = ".".join(local_ip.split(".")[:3]) + ".0/24"
    return jsonify({"local_ip": local_ip, "hostname": hostname, "subnet": subnet})

# ─────────────────────────────────────────────
#  INICIO
# ─────────────────────────────────────────────

if __name__ == "__main__":
    print("=" * 60)
    print("  🚀 SmartRemote Pro v3.0 - Control Universal WiFi")
    print("=" * 60)
    print("  Control:  http://localhost:5000")
    print("  Ajustes:  http://localhost:5000/settings")
    print("=" * 60)
    print("  Protocolos: SSDP · UPnP · Samsung REST · LG UDAP")
    print("               Sony IRCC · Roku ECP · Kodi RPC")
    print("=" * 60)

    # Hilo de descubrimiento continuo
    disc_thread = threading.Thread(target=run_discovery, daemon=True)
    disc_thread.start()
    print("[WiFi] Descubrimiento iniciado (SSDP + subnet scan)...")

    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)
