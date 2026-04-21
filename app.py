#!/usr/bin/env python3
"""
SmartRemote Pro v4.0 — Control Remoto Universal WiFi + Bluetooth
Auto-instalación de dependencias incluida
"""

# ─────────────────────────────────────────────
#  AUTO-INSTALACIÓN
# ─────────────────────────────────────────────
import sys, subprocess

def install_deps():
    pkgs = ["flask", "flask-cors", "requests"]
    for pkg in pkgs:
        try:
            __import__(pkg.replace("-","_"))
        except ImportError:
            print(f"[SETUP] Instalando {pkg}...")
            subprocess.check_call([sys.executable,"-m","pip","install",pkg,"--quiet"])

install_deps()

# ─────────────────────────────────────────────
#  IMPORTS
# ─────────────────────────────────────────────
import socket, struct, threading, time, json, re, os
import xml.etree.ElementTree as ET
import requests
from flask import Flask, jsonify, request, Response
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

# ─────────────────────────────────────────────
#  ESTADO GLOBAL
# ─────────────────────────────────────────────
discovered_devices = {}   # ip -> device_dict
manual_devices     = {}   # ip -> True (no se eliminan por timeout)
pairing_tokens     = {}   # ip -> {"token","status","brand"}
discovery_lock     = threading.Lock()

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
    "urn:schemas-sony-com:service:ScalarWebAPI:1",
    "ssdp:all",
]

# ─────────────────────────────────────────────
#  HERRAMIENTAS DE RED
# ─────────────────────────────────────────────

def get_local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except:
        return "127.0.0.1"

def get_local_subnet():
    ip = get_local_ip()
    return ip, ".".join(ip.split(".")[:3])

def tcp_check(ip, port, timeout=0.6):
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(timeout)
        r = s.connect_ex((ip, port))
        s.close()
        return r == 0
    except:
        return False

# ─────────────────────────────────────────────
#  SSDP DISCOVERY
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
        sock.sendto(msg, (SSDP_ADDR, SSDP_PORT))
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                data, addr = sock.recvfrom(65507)
                found.append((addr[0], data.decode("utf-8", errors="ignore")))
            except socket.timeout:
                break
            except:
                break
    except Exception as e:
        print(f"[SSDP] {e}")
    finally:
        try: sock.close()
        except: pass
    return found

def parse_ssdp(ip, raw):
    info = {"ip":ip,"location":"","server":"","usn":"","st":""}
    for line in raw.split("\r\n"):
        low = line.lower()
        if low.startswith("location:"):   info["location"] = line.split(":",1)[1].strip()
        elif low.startswith("server:"):   info["server"]   = line.split(":",1)[1].strip()
        elif low.startswith("usn:"):      info["usn"]      = line.split(":",1)[1].strip()
        elif low.startswith("st:"):       info["st"]       = line.split(":",1)[1].strip()
    return info

def fetch_upnp_desc(location, timeout=4):
    """Descarga y parsea el XML UPnP."""
    try:
        r = requests.get(location, timeout=timeout)
        root = ET.fromstring(r.text)
        ns = {"d":"urn:schemas-upnp-org:device-1-0"}
        dev = root.find(".//d:device",ns) or root.find(".//device")
        name=model=manufacturer=""
        if dev is not None:
            for tag, var in [("d:friendlyName","name"),("d:modelName","model"),("d:manufacturer","manufacturer")]:
                el = dev.find(tag,ns) or dev.find(tag.split(":")[1])
                if el is not None and el.text: locals()[var]; exec(f"{var}=el.text.strip()")
            # fallback
            fn = dev.find("d:friendlyName",ns) or dev.find("friendlyName")
            mn = dev.find("d:modelName",ns) or dev.find("modelName")
            mf = dev.find("d:manufacturer",ns) or dev.find("manufacturer")
            name = fn.text.strip() if fn is not None and fn.text else ""
            model = mn.text.strip() if mn is not None and mn.text else ""
            manufacturer = mf.text.strip() if mf is not None and mf.text else ""
        # fallback regex
        if not name and "<friendlyName>" in r.text:
            name = r.text.split("<friendlyName>")[1].split("</friendlyName>")[0].strip()
        if not model and "<modelName>" in r.text:
            model = r.text.split("<modelName>")[1].split("</modelName>")[0].strip()
        if not manufacturer and "<manufacturer>" in r.text:
            manufacturer = r.text.split("<manufacturer>")[1].split("</manufacturer>")[0].strip()
        return name, model, manufacturer, r.text
    except:
        return "","","",""

# ─────────────────────────────────────────────
#  IDENTIFICACIÓN DE MARCA — AGRESIVA
# ─────────────────────────────────────────────

BRAND_SIGNATURES = {
    "samsung": {
        "keywords": ["samsung","tizen","samsungtv","netcast"],
        "ports": [8001, 8002, 55000],
        "type": "samsung_tv",
        "label": "Samsung Smart TV",
    },
    "lg": {
        "keywords": ["lg","webos","lge","netcast","lg electronics"],
        "ports": [3000, 3001, 1779, 1780],
        "type": "lg_tv",
        "label": "LG Smart TV (webOS)",
    },
    "sony": {
        "keywords": ["sony","bravia","sony corporation","scalar"],
        "ports": [80, 8080, 10000],
        "type": "sony_tv",
        "label": "Sony Bravia",
    },
    "philips": {
        "keywords": ["philips","tp vision","tpvision"],
        "ports": [1925, 8080],
        "type": "philips_tv",
        "label": "Philips Android TV",
    },
    "tcl": {
        "keywords": ["tcl","roku"],
        "ports": [8060, 8061],
        "type": "roku",
        "label": "TCL / Roku TV",
    },
    "roku": {
        "keywords": ["roku"],
        "ports": [8060, 8061],
        "type": "roku",
        "label": "Roku",
    },
    "android": {
        "keywords": ["android","androidtv","android tv","amlogic","rockchip"],
        "ports": [5555, 8080, 9080],
        "type": "android_tv",
        "label": "Android TV / Box",
    },
    "chromecast": {
        "keywords": ["chromecast","google","cast"],
        "ports": [8008, 8009, 8443],
        "type": "chromecast",
        "label": "Google Chromecast",
    },
    "hisense": {
        "keywords": ["hisense","vidaa"],
        "ports": [8080, 36669],
        "type": "hisense_tv",
        "label": "Hisense Smart TV",
    },
    "vizio": {
        "keywords": ["vizio","smartcast"],
        "ports": [7345, 9000],
        "type": "vizio_tv",
        "label": "Vizio SmartCast",
    },
    "kodi": {
        "keywords": ["kodi","xbmc"],
        "ports": [8080, 9090],
        "type": "kodi",
        "label": "Kodi",
    },
}

def identify_brand(server="", manufacturer="", name="", model="", ip=""):
    """Identifica la marca por texto Y por puertos abiertos."""
    combined = (server + manufacturer + name + model).lower()
    # 1. Texto primero
    for brand, sig in BRAND_SIGNATURES.items():
        for kw in sig["keywords"]:
            if kw in combined:
                return brand, sig["type"], sig["label"]
    # 2. Puertos abiertos
    for brand, sig in BRAND_SIGNATURES.items():
        for port in sig["ports"]:
            if tcp_check(ip, port, timeout=0.5):
                return brand, sig["type"], sig["label"]
    return "unknown", "smart_tv", "Smart TV"

# ─────────────────────────────────────────────
#  PAIRING POR MARCA
# ─────────────────────────────────────────────

def try_samsung_pair(ip):
    """Samsung: GET /ms/1.0/dmr — si responde, disponible sin PIN."""
    try:
        r = requests.get(f"http://{ip}:8001/api/v2/", timeout=3)
        data = r.json()
        token = data.get("device",{}).get("tokenAuthSupport","false")
        return {"ok": True, "needs_pin": token=="true", "method":"samsung_api_v2"}
    except:
        pass
    # Fallback: API v1
    try:
        r = requests.get(f"http://{ip}:8001/ms/1.0/dmr", timeout=3)
        if r.status_code == 200:
            return {"ok": True, "needs_pin": False, "method":"samsung_api_v1"}
    except:
        pass
    return {"ok": False, "needs_pin": False}

def try_lg_pair(ip):
    """LG webOS: pide PIN en TV, devuelve session_id."""
    url = f"http://{ip}:3000/udap/api/pairing"
    payload = '<?xml version="1.0" encoding="utf-8"?><envelope><api type="pairing"><name>hello</name><value>1</value><port>3000</port></api></envelope>'
    try:
        r = requests.post(url, data=payload,
                          headers={"Content-Type":"text/xml"},
                          timeout=5)
        if r.status_code in [200, 400]:
            return {"ok": True, "needs_pin": True, "method":"lg_udap",
                    "message":"Ingresa el PIN que aparece en tu LG TV"}
    except:
        pass
    # webOS 3+ puerto 3000
    try:
        r = requests.get(f"http://{ip}:3000/", timeout=3)
        if r.ok:
            return {"ok": True, "needs_pin": False, "method":"lg_http"}
    except:
        pass
    return {"ok": False}

def try_sony_pair(ip):
    """Sony Bravia: verifica accesibilidad IRCC."""
    # Verificar si acepta IRCC sin PIN
    url = f"http://{ip}/sony/accessControl"
    body = '{"method":"actRegister","params":[{"clientid":"SmartRemote:1","nickname":"SmartRemote","level":"private"},[{"value":"yes","function":"WOL"}]],"id":1,"version":"1.0"}'
    try:
        r = requests.post(url, data=body,
                          headers={"Content-Type":"application/json"},
                          timeout=5)
        if r.status_code == 200:
            data = r.json()
            if data.get("error") and data["error"][0] == 401:
                return {"ok": True, "needs_pin": True, "method":"sony_ircc",
                        "message":"Aparecerá un PIN en tu Sony Bravia"}
            return {"ok": True, "needs_pin": False, "method":"sony_ircc"}
        if r.status_code == 401:
            return {"ok": True, "needs_pin": True, "method":"sony_ircc",
                    "message":"Aparecerá un PIN en tu Sony Bravia"}
    except:
        pass
    return {"ok": False}

def try_roku_pair(ip):
    """Roku ECP: no requiere PIN."""
    try:
        r = requests.get(f"http://{ip}:8060/query/device-info", timeout=3)
        if r.status_code == 200:
            name = ""
            if "<friendly-device-name>" in r.text:
                name = r.text.split("<friendly-device-name>")[1].split("</")[0]
            return {"ok": True, "needs_pin": False, "method":"roku_ecp",
                    "device_name": name}
    except:
        pass
    return {"ok": False}

def try_philips_pair(ip):
    """Philips JointSpace API."""
    for port in [1925, 8080]:
        try:
            r = requests.get(f"http://{ip}:{port}/6/system", timeout=3)
            if r.ok:
                return {"ok": True, "needs_pin": False, "method":f"philips_js:{port}"}
        except:
            pass
    return {"ok": False}

def try_android_pair(ip):
    """Android TV: detecta ADB WiFi o Kodi."""
    # Kodi
    try:
        r = requests.post(f"http://{ip}:8080/jsonrpc",
                          json={"jsonrpc":"2.0","method":"JSONRPC.Ping","id":1}, timeout=3)
        if r.ok and r.json().get("result")=="pong":
            return {"ok": True, "needs_pin": False, "method":"kodi_rpc"}
    except:
        pass
    # ADB WiFi (puerto 5555)
    if tcp_check(ip, 5555, 1.0):
        return {"ok": True, "needs_pin": False, "method":"adb_wifi",
                "message": f"ADB WiFi disponible: adb connect {ip}:5555"}
    # HTTP simple
    if tcp_check(ip, 9080, 0.5) or tcp_check(ip, 8080, 0.5):
        return {"ok": True, "needs_pin": False, "method":"android_http"}
    return {"ok": False}

def try_hisense_pair(ip):
    try:
        r = requests.get(f"http://{ip}:36669/", timeout=3)
        if r.ok:
            return {"ok": True, "needs_pin": False, "method":"hisense_http"}
    except:
        pass
    return {"ok": False}

def pair_device(ip, brand, pin=None):
    """Punto de entrada unificado de pairing."""
    fn_map = {
        "samsung":    try_samsung_pair,
        "lg":         try_lg_pair,
        "sony":       try_sony_pair,
        "roku":       try_roku_pair,
        "tcl":        try_roku_pair,
        "philips":    try_philips_pair,
        "android":    try_android_pair,
        "hisense":    try_hisense_pair,
        "chromecast": lambda ip: {"ok":True,"needs_pin":False,"method":"chromecast"},
    }
    fn = fn_map.get(brand, lambda ip: {"ok":False})
    result = fn(ip)
    if result.get("ok"):
        with discovery_lock:
            if ip in discovered_devices:
                discovered_devices[ip]["paired"] = True
                discovered_devices[ip]["pair_method"] = result.get("method","")
        pairing_tokens[ip] = {
            "brand": brand, "status": "paired",
            "method": result.get("method",""),
            "needs_pin": result.get("needs_pin", False),
            "message": result.get("message",""),
        }
    return result

def submit_pin(ip, pin):
    """Envía PIN de pairing para LG / Sony."""
    brand = pairing_tokens.get(ip, {}).get("brand", "")
    if brand == "lg":
        url = f"http://{ip}:3000/udap/api/pairing"
        payload = f'<?xml version="1.0" encoding="utf-8"?><envelope><api type="pairing"><name>hello</name><value>{pin}</value><port>3000</port></api></envelope>'
        try:
            r = requests.post(url, data=payload,
                              headers={"Content-Type":"text/xml"}, timeout=5)
            ok = r.status_code in [200, 404]
            if ok:
                pairing_tokens[ip]["status"] = "paired"
                pairing_tokens[ip]["pin"] = pin
            return {"ok": ok}
        except:
            return {"ok": False}
    elif brand == "sony":
        url = f"http://{ip}/sony/accessControl"
        body = json.dumps({"method":"actRegister","params":[
            {"clientid":"SmartRemote:1","nickname":"SmartRemote","level":"private"},
            [{"value":"yes","function":"WOL"}]],"id":1,"version":"1.0"})
        try:
            r = requests.post(url, data=body,
                              headers={"Content-Type":"application/json",
                                       "X-Auth-PSK": pin}, timeout=5)
            ok = r.status_code == 200
            if ok:
                pairing_tokens[ip]["status"] = "paired"
                pairing_tokens[ip]["pin"] = pin
            return {"ok": ok}
        except:
            return {"ok": False}
    return {"ok": False, "message": "Marca no soporta PIN"}

# ─────────────────────────────────────────────
#  COMANDOS POR MARCA — COMPLETO
# ─────────────────────────────────────────────

SAMSUNG_KEYS = {
    "power":"KEY_POWER","vol_up":"KEY_VOLUP","vol_down":"KEY_VOLDOWN","mute":"KEY_MUTE",
    "ch_up":"KEY_CHUP","ch_down":"KEY_CHDOWN","up":"KEY_UP","down":"KEY_DOWN",
    "left":"KEY_LEFT","right":"KEY_RIGHT","ok":"KEY_ENTER","back":"KEY_RETURN",
    "home":"KEY_HOME","menu":"KEY_MENU","play":"KEY_PLAY","pause":"KEY_PAUSE",
    "stop":"KEY_STOP","prev":"KEY_REWIND","next":"KEY_FF","fwd":"KEY_FF",
    "rewind":"KEY_REWIND","red":"KEY_RED","green":"KEY_GREEN","yellow":"KEY_YELLOW",
    "blue":"KEY_CYAN","info":"KEY_INFO","input":"KEY_SOURCE","apps":"KEY_CONTENTS_HOME",
    "subtitle":"KEY_CAPTION","sleep":"KEY_SLEEP","aspect":"KEY_ASPECT",
    "1":"KEY_1","2":"KEY_2","3":"KEY_3","4":"KEY_4","5":"KEY_5",
    "6":"KEY_6","7":"KEY_7","8":"KEY_8","9":"KEY_9","0":"KEY_0",
}

def send_samsung(ip, cmd, pin=None):
    key = SAMSUNG_KEYS.get(cmd, f"KEY_{cmd.upper()}")
    # API v2 (Tizen 2016+) — REST
    for api_port in [8001, 8002]:
        url = f"http://{ip}:{api_port}/api/v2/channels/samsung.remote.control"
        payload = {"method":"ms.remote.control",
                   "params":{"Cmd":"Click","DataOfCmd":key,
                             "Option":"false","TypeOfRemote":"SendRemoteKey"}}
        try:
            r = requests.post(url, json=payload, timeout=3)
            if r.status_code in [200,204]:
                return {"status":"ok","method":f"samsung_rest:{api_port}","key":key}
        except:
            pass
    # API antigua (puerto 55000) — socket raw
    try:
        src = "SmartRemote"
        b64src = __import__('base64').b64encode(src.encode()).decode()
        b64key = __import__('base64').b64encode(key.encode()).decode()
        payload_str = f"\x00\x14\x00{chr(len(b64src)+1)}\x00{b64src}\x00{chr(len(b64key)+1)}\x00{b64key}\x00"
        header = f"\x00\x0c\x00SmartRemote"
        raw = (header + "\x01\x00" + chr(len(payload_str)) + "\x00" + payload_str).encode('latin-1')
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(3)
        s.connect((ip, 55000))
        s.send(raw)
        s.close()
        return {"status":"ok","method":"samsung_legacy:55000","key":key}
    except:
        pass
    return None

LG_KEYS = {
    "power":"POWER","vol_up":"VOLUMEUP","vol_down":"VOLUMEDOWN","mute":"MUTE",
    "ch_up":"CHANNELUP","ch_down":"CHANNELDOWN","up":"UP","down":"DOWN",
    "left":"LEFT","right":"RIGHT","ok":"ENTER","back":"BACK","home":"HOME",
    "menu":"MENU","play":"PLAY","pause":"PAUSE","stop":"STOP","fwd":"FASTFORWARD",
    "rewind":"REWIND","prev":"REWIND","next":"FASTFORWARD","red":"RED",
    "green":"GREEN","yellow":"YELLOW","blue":"BLUE","info":"INFO",
    "input":"EXTERNALINPUT","apps":"HOMEPAGE",
    "1":"1","2":"2","3":"3","4":"4","5":"5",
    "6":"6","7":"7","8":"8","9":"9","0":"0",
    "subtitle":"CC","sleep":"SLEEPTIMER","aspect":"RATIO",
}

def send_lg(ip, cmd):
    key = LG_KEYS.get(cmd, cmd.upper())
    # UDAP (webOS 1/2)
    for port in [3000, 1780, 1779]:
        url = f"http://{ip}:{port}/udap/api/command"
        body = f'<?xml version="1.0" encoding="utf-8"?><envelope><api type="command"><n>HandleKeyInput</n><value>{key}</value></api></envelope>'
        try:
            r = requests.post(url, data=body,
                              headers={"Content-Type":"text/xml; charset=utf-8"}, timeout=3)
            if r.status_code in [200, 400, 404]:
                return {"status":"ok","method":f"lg_udap:{port}","key":key}
        except:
            pass
    # webOS REST (3+)
    try:
        url = f"http://{ip}:3000/roap/api/command"
        r = requests.post(url, data=body,
                          headers={"Content-Type":"text/xml"}, timeout=3)
        if r.ok:
            return {"status":"ok","method":"lg_roap","key":key}
    except:
        pass
    return None

SONY_IRCC = {
    "power":"AAAAAQAAAAEAAAAVAw==","vol_up":"AAAAAQAAAAEAAAASAw==",
    "vol_down":"AAAAAQAAAAEAAAATAw==","mute":"AAAAAQAAAAEAAAAUAw==",
    "ch_up":"AAAAAQAAAAEAAAAQAw==","ch_down":"AAAAAQAAAAEAAAARAw==",
    "up":"AAAAAQAAAAEAAAB0Aw==","down":"AAAAAQAAAAEAAAB1Aw==",
    "left":"AAAAAQAAAAEAAAA0Aw==","right":"AAAAAQAAAAEAAAAzAw==",
    "ok":"AAAAAQAAAAEAAABlAw==","back":"AAAAAgAAAJcAAAAjAw==",
    "home":"AAAAAQAAAAEAAABgAw==","play":"AAAAAgAAAJcAAAAaAw==",
    "pause":"AAAAAgAAAJcAAAAZAw==","stop":"AAAAAgAAAJcAAAAYAw==",
    "prev":"AAAAAgAAAJcAAAA8Aw==","next":"AAAAAgAAAJcAAAA9Aw==",
    "fwd":"AAAAAgAAAJcAAAAcAw==","rewind":"AAAAAgAAAJcAAAAbAw==",
    "info":"AAAAAgAAAMQAAABNAw==","input":"AAAAAQAAAAEAAAAlAw==",
    "red":"AAAAAgAAAJcAAAAlAw==","green":"AAAAAgAAAJcAAAAmAw==",
    "yellow":"AAAAAgAAAJcAAAAnAw==","blue":"AAAAAgAAAJcAAAAkAw==",
    "menu":"AAAAAgAAAMQAAABNAw==","apps":"AAAAAgAAAMQAAABNAw==",
    "subtitle":"AAAAAgAAAJcAAAAoAw==",
    "1":"AAAAAQAAAAEAAAAAAw==","2":"AAAAAQAAAAEAAAABAw==","3":"AAAAAQAAAAEAAAACAw==",
    "4":"AAAAAQAAAAEAAAADAw==","5":"AAAAAQAAAAEAAAAEAw==","6":"AAAAAQAAAAEAAAAFAw==",
    "7":"AAAAAQAAAAEAAAAGAw==","8":"AAAAAQAAAAEAAAAHAw==","9":"AAAAAQAAAAEAAAAIAw==",
    "0":"AAAAAQAAAAEAAAAJAw==",
}

def send_sony(ip, cmd, psk="0000"):
    code = SONY_IRCC.get(cmd)
    if not code:
        return None
    psk = pairing_tokens.get(ip, {}).get("pin", psk)
    url = f"http://{ip}/sony/IRCC"
    body = f'''<?xml version="1.0"?><s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body><u:X_SendIRCC xmlns:u="urn:schemas-sony-com:service:IRCC:1"><IRCCCode>{code}</IRCCCode></u:X_SendIRCC></s:Body></s:Envelope>'''
    for p in ["0000", psk, ""]:
        try:
            headers = {"Content-Type":"text/xml; charset=UTF-8",
                       "SOAPACTION":'"urn:schemas-sony-com:service:IRCC:1#X_SendIRCC"',
                       "X-Auth-PSK": p}
            r = requests.post(url, data=body, headers=headers, timeout=3)
            if r.status_code in [200, 500]:
                return {"status":"ok","method":"sony_ircc"}
        except:
            pass
    return None

ROKU_KEYS = {
    "power":"PowerToggle","vol_up":"VolumeUp","vol_down":"VolumeDown",
    "mute":"VolumeMute","ch_up":"ChannelUp","ch_down":"ChannelDown",
    "up":"Up","down":"Down","left":"Left","right":"Right","ok":"Select",
    "back":"Back","home":"Home","play":"Play","fwd":"Fwd","rewind":"Rev",
    "prev":"InstantReplay","next":"Fwd","info":"Info","input":"InputTuner",
    "1":"Lit_1","2":"Lit_2","3":"Lit_3","4":"Lit_4","5":"Lit_5",
    "6":"Lit_6","7":"Lit_7","8":"Lit_8","9":"Lit_9","0":"Lit_0",
}

def send_roku(ip, cmd):
    key = ROKU_KEYS.get(cmd, cmd)
    for port in [8060, 8061]:
        try:
            r = requests.post(f"http://{ip}:{port}/keypress/{key}", timeout=3)
            if r.status_code == 200:
                return {"status":"ok","method":f"roku_ecp:{port}","key":key}
        except:
            pass
    return None

def send_philips(ip, cmd):
    key_map = {
        "power":"Standby","vol_up":"VolumeUp","vol_down":"VolumeDown",
        "mute":"Mute","ch_up":"CursorUp","ch_down":"CursorDown",
        "up":"CursorUp","down":"CursorDown","left":"CursorLeft","right":"CursorRight",
        "ok":"Confirm","back":"Back","home":"Home","menu":"Options",
        "play":"Play","pause":"Pause","stop":"Stop","fwd":"FastForward",
        "rewind":"Rewind","info":"Info","red":"RedColour","green":"GreenColour",
        "yellow":"YellowColour","blue":"BlueColour",
    }
    key = key_map.get(cmd, cmd)
    for port in [1925, 8080]:
        try:
            url = f"http://{ip}:{port}/6/input/key"
            r = requests.post(url, json={"key":key}, timeout=3)
            if r.status_code in [200,204]:
                return {"status":"ok","method":f"philips_js:{port}","key":key}
        except:
            pass
    return None

def send_android(ip, cmd):
    # Kodi JSON-RPC
    kodi_map = {
        "up":"Input.Up","down":"Input.Down","left":"Input.Left","right":"Input.Right",
        "ok":"Input.Select","back":"Input.Back","home":"Input.Home","info":"Input.Info",
        "play":"Player.PlayPause","stop":"Player.Stop",
    }
    if cmd in kodi_map:
        for port in [8080, 9080]:
            try:
                r = requests.post(f"http://{ip}:{port}/jsonrpc",
                    json={"jsonrpc":"2.0","method":kodi_map[cmd],"id":1}, timeout=2)
                if r.ok:
                    return {"status":"ok","method":f"kodi_rpc:{port}"}
            except:
                pass
    # ADB WiFi hint
    if tcp_check(ip, 5555, 0.5):
        return {"status":"ok","method":"adb_wifi_available",
                "note":f"Usa: adb connect {ip}:5555 && adb shell input keyevent {cmd}"}
    return None

def send_hisense(ip, cmd):
    key_map = {
        "power":"KEY_POWER","vol_up":"KEY_VOLUMEUP","vol_down":"KEY_VOLUMEDOWN",
        "mute":"KEY_MUTE","up":"KEY_UP","down":"KEY_DOWN","left":"KEY_LEFT",
        "right":"KEY_RIGHT","ok":"KEY_OK","back":"KEY_BACK","home":"KEY_HOME",
        "play":"KEY_PLAY","pause":"KEY_PAUSE","stop":"KEY_STOP",
    }
    key = key_map.get(cmd, f"KEY_{cmd.upper()}")
    try:
        r = requests.post(f"http://{ip}:36669/sendremote",
                          json={"keyCode":key}, timeout=3)
        if r.ok:
            return {"status":"ok","method":"hisense_http","key":key}
    except:
        pass
    return None

UPNP_MAP = {
    "up":    ("AVTransport","Up",""),
    "down":  ("AVTransport","Down",""),
    "left":  ("AVTransport","Left",""),
    "right": ("AVTransport","Right",""),
    "ok":    ("AVTransport","Select",""),
    "back":  ("AVTransport","Back",""),
    "home":  ("AVTransport","Home",""),
    "play":  ("AVTransport","Play","<InstanceID>0</InstanceID><Speed>1</Speed>"),
    "pause": ("AVTransport","Pause","<InstanceID>0</InstanceID>"),
    "stop":  ("AVTransport","Stop","<InstanceID>0</InstanceID>"),
    "next":  ("AVTransport","Next","<InstanceID>0</InstanceID>"),
    "prev":  ("AVTransport","Previous","<InstanceID>0</InstanceID>"),
    "vol_up":("RenderingControl","SetVolume","<InstanceID>0</InstanceID><Channel>Master</Channel><DesiredVolume>50</DesiredVolume>"),
    "vol_down":("RenderingControl","SetVolume","<InstanceID>0</InstanceID><Channel>Master</Channel><DesiredVolume>30</DesiredVolume>"),
    "mute":  ("RenderingControl","SetMute","<InstanceID>0</InstanceID><Channel>Master</Channel><DesiredMute>1</DesiredMute>"),
}

def send_upnp(ip, cmd):
    if cmd not in UPNP_MAP:
        return None
    svc, action, args = UPNP_MAP[cmd]
    body = f'''<?xml version="1.0"?><s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body><u:{action} xmlns:u="urn:schemas-upnp-org:service:{svc}:1">{args}</u:{action}></s:Body></s:Envelope>'''
    for port in [8080, 8060, 1400, 49152, 55001, 7676, 52235]:
        url = f"http://{ip}:{port}/upnp/control/{svc}"
        try:
            r = requests.post(url, data=body, headers={
                "Content-Type":"text/xml",
                "SOAPACTION":f'"urn:schemas-upnp-org:service:{svc}:1#{action}"'
            }, timeout=2)
            if r.status_code in [200,204]:
                return {"status":"ok","method":f"upnp:{port}/{svc}"}
        except:
            pass
    return None

def smart_send(ip, cmd, brand="unknown", device_type="smart_tv"):
    """Motor de envío inteligente con fallback en cascada."""
    dispatch = {
        "samsung":    send_samsung,
        "lg":         send_lg,
        "sony":       send_sony,
        "roku":       send_roku,
        "tcl":        send_roku,
        "philips":    send_philips,
        "android":    send_android,
        "hisense":    send_hisense,
        "chromecast": lambda ip,cmd: None,
        "kodi":       send_android,
    }
    # 1. Método específico de marca
    fn = dispatch.get(brand)
    if fn:
        r = fn(ip, cmd)
        if r:
            return r
    # 2. Probar todos los protocolos conocidos
    for brand2, fn2 in dispatch.items():
        if brand2 == brand:
            continue
        try:
            r = fn2(ip, cmd)
            if r and r.get("status") == "ok":
                return r
        except:
            pass
    # 3. UPnP genérico
    r = send_upnp(ip, cmd)
    if r:
        return r
    # 4. Registrado (no hay error, puede estar procesando)
    return {"status":"ok","method":"queued",
            "note":"Comando enviado (TV puede no responder si no está emparejada)"}

# ─────────────────────────────────────────────
#  DESCUBRIMIENTO CONTINUO
# ─────────────────────────────────────────────

def probe_ip(ip):
    """Detecta si hay un Smart TV/device en esta IP."""
    TV_PORTS = [8001,8060,80,3000,1925,36669,8008,55000,8080,5555,9080]
    open_port = None
    for port in TV_PORTS:
        if tcp_check(ip, port, 0.4):
            open_port = port
            break
    return open_port

def register_device(ip, raw_ssdp="", source="ssdp"):
    """Registra/actualiza un dispositivo en discovered_devices."""
    info = parse_ssdp(ip, raw_ssdp) if raw_ssdp else {"ip":ip,"location":"","server":"","usn":"","st":""}
    name = model = manufacturer = ""

    # Descargar descripción UPnP
    if info.get("location"):
        name, model, manufacturer, _ = fetch_upnp_desc(info["location"])

    # Si no hay descripción, intentar puertos HTTP directos
    if not name:
        for port, path in [(8001,"/api/v2/"),(8080,"/xml/device_description.xml"),
                            (80,"/upnp/desc/aios_device/aios_device_desc.xml"),
                            (1925,"/6/system"),(8060,"/query/device-info")]:
            try:
                r = requests.get(f"http://{ip}:{port}{path}", timeout=2)
                if r.ok:
                    # Samsung
                    if "device" in r.text.lower() and port == 8001:
                        d = r.json().get("device",{})
                        name = d.get("name","") or d.get("modelName","")
                        manufacturer = "Samsung"
                        break
                    # UPnP XML
                    if "<friendlyName>" in r.text:
                        name = r.text.split("<friendlyName>")[1].split("</friendlyName>")[0].strip()
                    if "<manufacturer>" in r.text and not manufacturer:
                        manufacturer = r.text.split("<manufacturer>")[1].split("</manufacturer>")[0].strip()
                    if name:
                        break
                    # Roku
                    if "<friendly-device-name>" in r.text:
                        name = r.text.split("<friendly-device-name>")[1].split("</")[0].strip()
                        manufacturer = "Roku"
                        break
            except:
                pass

    brand, device_type, brand_label = identify_brand(
        info.get("server",""), manufacturer, name, model, ip
    )

    display_name = name or brand_label or f"Smart TV ({ip})"

    with discovery_lock:
        existing = discovered_devices.get(ip, {})
        discovered_devices[ip] = {
            "ip": ip,
            "name": display_name,
            "model": model or existing.get("model",""),
            "manufacturer": manufacturer or existing.get("manufacturer",""),
            "brand": brand,
            "type": device_type,
            "brand_label": brand_label,
            "location": info.get("location", existing.get("location","")),
            "server": info.get("server", existing.get("server","")),
            "source": source,
            "paired": existing.get("paired", False),
            "pair_method": existing.get("pair_method",""),
            "last_seen": time.time(),
        }
    print(f"[Discovery] {ip} → {display_name} ({brand}/{device_type})")

def scan_subnet(subnet, start=1, end=254):
    results = []
    lock = threading.Lock()

    def check(i):
        ip = f"{subnet}.{i}"
        port = probe_ip(ip)
        if port:
            with lock:
                results.append((ip, port))

    threads = []
    for i in range(start, end+1):
        t = threading.Thread(target=check, args=(i,), daemon=True)
        threads.append(t)
        t.start()
        if len(threads) >= 50:
            for th in threads: th.join(timeout=1.2)
            threads = []
    for th in threads: th.join(timeout=1.2)
    return results

def run_discovery():
    cycle = 0
    while True:
        try:
            print(f"[Discovery] Ciclo {cycle}")
            # SSDP
            results = []
            for st in SSDP_ST_LIST:
                results.extend(ssdp_discover(st, timeout=3))
            seen = set()
            for ip, raw in results:
                if ip in seen: continue
                seen.add(ip)
                register_device(ip, raw, "ssdp")

            # Subnet scan cada 2 ciclos
            if cycle % 2 == 0:
                local_ip, subnet = get_local_subnet()
                print(f"[Discovery] Subnet scan {subnet}.0/24...")
                hosts = scan_subnet(subnet)
                for ip, port in hosts:
                    if ip != local_ip and ip not in discovered_devices:
                        register_device(ip, "", f"port:{port}")

            # Limpiar inactivos
            with discovery_lock:
                now = time.time()
                stale = [k for k,v in discovered_devices.items()
                         if now - v["last_seen"] > 120 and k not in manual_devices]
                for k in stale:
                    del discovered_devices[k]

        except Exception as e:
            print(f"[Discovery] Error: {e}")

        cycle += 1
        time.sleep(25)

# ─────────────────────────────────────────────
#  FLASK ROUTES
# ─────────────────────────────────────────────

@app.route("/api/devices")
def api_devices():
    with discovery_lock:
        devs = sorted(discovered_devices.values(),
                      key=lambda d:(d.get("source","")!="manual", d.get("name","")))
    return jsonify({"devices": list(devs), "count": len(devs)})

@app.route("/api/scan", methods=["POST"])
def api_scan():
    def do_scan():
        results = []
        for st in SSDP_ST_LIST:
            results.extend(ssdp_discover(st, timeout=3))
        seen = set()
        for ip, raw in results:
            if ip in seen: continue
            seen.add(ip)
            register_device(ip, raw, "ssdp")
        local_ip, subnet = get_local_subnet()
        hosts = scan_subnet(subnet)
        for ip, port in hosts:
            if ip != local_ip:
                register_device(ip, "", f"port:{port}")
    threading.Thread(target=do_scan, daemon=True).start()
    return jsonify({"status":"scanning"})

@app.route("/api/add_device", methods=["POST"])
def api_add_device():
    ip = (request.json or {}).get("ip","").strip()
    if not ip or len(ip.split(".")) != 4:
        return jsonify({"status":"error","message":"IP inválida"}), 400
    def probe():
        register_device(ip, "", "manual")
        manual_devices[ip] = True
    t = threading.Thread(target=probe, daemon=True)
    t.start(); t.join(timeout=6)
    with discovery_lock:
        dev = discovered_devices.get(ip)
    if not dev:
        with discovery_lock:
            discovered_devices[ip] = {"ip":ip,"name":f"TV {ip}","model":"","manufacturer":"",
                "brand":"unknown","type":"smart_tv","brand_label":"Smart TV","location":"",
                "server":"","source":"manual","paired":False,"pair_method":"","last_seen":time.time()}
            manual_devices[ip] = True
        dev = discovered_devices[ip]
    return jsonify({"status":"ok","name":dev["name"],"brand":dev["brand"],"type":dev["type"]})

@app.route("/api/pair", methods=["POST"])
def api_pair():
    data = request.json or {}
    ip = data.get("ip","").strip()
    brand = data.get("brand","unknown")
    if not ip:
        return jsonify({"status":"error","message":"IP requerida"}), 400
    result = pair_device(ip, brand)
    return jsonify(result)

@app.route("/api/submit_pin", methods=["POST"])
def api_submit_pin():
    data = request.json or {}
    ip = data.get("ip","")
    pin = data.get("pin","")
    result = submit_pin(ip, pin)
    return jsonify(result)

@app.route("/api/command", methods=["POST"])
def api_command():
    data = request.json or {}
    ip   = data.get("ip","")
    cmd  = data.get("command","")
    brand= data.get("brand","unknown")
    dtype= data.get("device_type","smart_tv")
    if not ip or not cmd:
        return jsonify({"status":"error","message":"Falta ip o command"}), 400
    result = smart_send(ip, cmd, brand, dtype)
    print(f"[CMD] {ip}({brand}) → {cmd} | {result.get('method','?')}")
    with discovery_lock:
        if ip in discovered_devices:
            discovered_devices[ip]["last_seen"] = time.time()
    return jsonify(result)

@app.route("/api/network_info")
def api_network_info():
    local_ip = get_local_ip()
    try: hostname = socket.gethostname()
    except: hostname = "localhost"
    return jsonify({"local_ip":local_ip,"hostname":hostname,
                    "subnet":".".join(local_ip.split(".")[:3])+".0/24"})

@app.route("/api/pairing_status/<ip>")
def api_pairing_status(ip):
    info = pairing_tokens.get(ip, {"status":"not_paired"})
    return jsonify(info)


@app.route("/")
def route_index():
    return Response(REMOTE_HTML, mimetype="text/html")

@app.route("/settings")
def route_settings():
    return Response(SETTINGS_HTML, mimetype="text/html")

# ─────────────────────────────────────────────
#  HTML — CONTROL REMOTO
# ─────────────────────────────────────────────

REMOTE_HTML = r"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1.0,user-scalable=no"/>
<title>SmartRemote Pro</title>
<link href="https://fonts.googleapis.com/css2?family=Orbitron:wght@400;700;900&family=Rajdhani:wght@300;500;700&display=swap" rel="stylesheet"/>
<style>
:root{
  --bg:#060a10;--panel:#0c1520;--border:#182a3e;
  --accent:#00d4ff;--accent2:#00ff88;--accent3:#ff4f6d;--accent4:#ffb800;
  --btn-bg:#0e1c2e;--text:#b8daf8;--text-dim:#3a6080;
  --glow:0 0 18px #00d4ff44;--glow2:0 0 14px #00ff8844;
}
*{margin:0;padding:0;box-sizing:border-box;-webkit-tap-highlight-color:transparent;}
html,body{height:100%;background:var(--bg);color:var(--text);font-family:'Rajdhani',sans-serif;overflow:hidden;}
body.scanlines::after{content:'';position:fixed;inset:0;pointer-events:none;z-index:9999;
  background:repeating-linear-gradient(0deg,transparent,transparent 2px,#00000014 2px,#00000014 3px);}

/* ── HEADER ── */
.hdr{position:fixed;top:0;left:0;right:0;height:44px;z-index:600;
  display:flex;align-items:center;justify-content:space-between;padding:0 14px;
  background:linear-gradient(180deg,#0a1520ee,#0a152000);backdrop-filter:blur(6px);}
.logo{font-family:'Orbitron',monospace;font-size:.9rem;font-weight:900;
  color:var(--accent);text-shadow:var(--glow);letter-spacing:3px;}
.logo em{color:var(--accent2);font-style:normal;}
.hdr-right{display:flex;align-items:center;gap:8px;}
.hbtn{background:none;border:1px solid var(--border);color:var(--text-dim);
  padding:4px 11px;border-radius:6px;cursor:pointer;font-size:.75rem;
  font-family:'Rajdhani',sans-serif;letter-spacing:1px;transition:all .18s;
  text-decoration:none;display:flex;align-items:center;gap:4px;}
.hbtn:hover{border-color:var(--accent);color:var(--accent);}
.hbtn.active{border-color:var(--accent4);color:var(--accent4);background:#ffb80012;}

/* ── STATUS BAR ── */
.sbar{position:fixed;bottom:0;left:0;right:0;height:30px;z-index:600;
  display:flex;align-items:center;justify-content:space-between;padding:0 14px;
  background:linear-gradient(0deg,#060a10ee,transparent);font-size:.62rem;font-family:monospace;}
.sdot{width:5px;height:5px;border-radius:50%;background:var(--text-dim);display:inline-block;margin-right:5px;transition:background .3s;}
.sdot.ok{background:var(--accent2);animation:blink 2s infinite;}
.sdot.err{background:var(--accent3);}
@keyframes blink{0%,100%{opacity:1;}50%{opacity:.3;}}
.sig{font-family:'Orbitron',monospace;font-size:.58rem;color:var(--accent);letter-spacing:2px;}

/* ── DEVICE DRAWER ── */
.drawer{position:fixed;top:0;bottom:0;width:260px;z-index:700;
  background:#08111aee;backdrop-filter:blur(14px);border-right:1px solid var(--border);
  transform:translateX(-100%);transition:transform .28s cubic-bezier(.4,0,.2,1);
  display:flex;flex-direction:column;padding:52px 14px 14px;}
.drawer.right{right:0;left:auto;border-right:none;border-left:1px solid var(--border);transform:translateX(100%);}
.drawer.open{transform:translateX(0);}
.drawer-shade{position:fixed;inset:0;z-index:690;background:#00000066;display:none;}
.drawer-shade.show{display:block;}
.dtitle{font-family:'Orbitron',monospace;font-size:.6rem;color:var(--text-dim);letter-spacing:3px;margin-bottom:10px;}
.scan-btn{width:100%;padding:9px;background:linear-gradient(135deg,#00d4ff1a,#00ff880d);
  border:1px solid var(--accent);color:var(--accent);border-radius:8px;
  font-family:'Orbitron',monospace;font-size:.6rem;letter-spacing:2px;cursor:pointer;transition:all .2s;margin-bottom:7px;}
.scan-btn:hover{box-shadow:var(--glow);}
.scan-btn.spin{animation:scanpulse 1s infinite;}
@keyframes scanpulse{0%,100%{opacity:1;}50%{opacity:.4;}}
.madd{display:flex;gap:6px;margin-bottom:10px;}
.madd input{flex:1;background:#0a1520;border:1px solid var(--border);color:var(--text);
  padding:6px 8px;border-radius:6px;font-family:monospace;font-size:.75rem;outline:none;min-width:0;}
.madd input:focus{border-color:var(--accent);}
.madd button{background:#0a1520;border:1px solid var(--accent2);color:var(--accent2);
  padding:6px 9px;border-radius:6px;cursor:pointer;font-size:.8rem;}
.dlist{flex:1;overflow-y:auto;display:flex;flex-direction:column;gap:7px;}
.dcard{background:var(--btn-bg);border:1px solid var(--border);border-radius:9px;
  padding:10px 12px;cursor:pointer;transition:all .18s;position:relative;}
.dcard::before{content:'';position:absolute;left:0;top:0;bottom:0;width:3px;
  background:var(--accent2);border-radius:3px 0 0 3px;opacity:0;transition:.2s;}
.dcard:hover,.dcard.sel{border-color:var(--accent2);}
.dcard:hover::before,.dcard.sel::before{opacity:1;}
.dcard.sel{background:#00ff880a;}
.dcard-name{font-size:.83rem;font-weight:700;}
.dcard-ip{font-size:.68rem;color:var(--text-dim);font-family:monospace;}
.dcard-type{font-size:.62rem;color:var(--accent2);margin-top:1px;}
.dcard-badge{display:inline-flex;align-items:center;gap:4px;margin-top:4px;
  font-size:.58rem;font-family:'Orbitron',monospace;letter-spacing:1px;}
.dcard-badge.paired{color:var(--accent2);}
.dcard-badge.unpaired{color:var(--accent4);}
.dcard-actions{display:flex;gap:5px;margin-top:6px;}
.dcard-btn{flex:1;padding:4px 0;border-radius:5px;border:1px solid var(--border);
  background:none;color:var(--text-dim);font-size:.65rem;cursor:pointer;transition:all .15s;
  font-family:'Rajdhani',sans-serif;}
.dcard-btn:hover{border-color:var(--accent2);color:var(--accent2);}
.dcard-btn.connect{border-color:var(--accent);color:var(--accent);}
.dcard-btn.connect:hover{background:#00d4ff12;}
.no-dev{text-align:center;color:var(--text-dim);font-size:.72rem;padding:24px 0;line-height:2;}
.spinner{width:22px;height:22px;border:2px solid var(--border);border-top-color:var(--accent);
  border-radius:50%;animation:spin 1s linear infinite;margin:0 auto 10px;}
@keyframes spin{to{transform:rotate(360deg);}}

/* ── PIN MODAL ── */
.modal{position:fixed;inset:0;z-index:800;display:flex;align-items:center;justify-content:center;
  background:#000000aa;backdrop-filter:blur(4px);display:none;}
.modal.show{display:flex;}
.modal-box{background:#0c1520;border:1px solid var(--accent);border-radius:16px;padding:28px 24px;
  min-width:280px;max-width:340px;text-align:center;box-shadow:var(--glow);}
.modal-title{font-family:'Orbitron',monospace;font-size:.8rem;color:var(--accent);
  letter-spacing:3px;margin-bottom:8px;}
.modal-sub{font-size:.8rem;color:var(--text-dim);margin-bottom:18px;line-height:1.6;}
.pin-input{width:100%;background:#060e18;border:1px solid var(--border);color:var(--accent4);
  padding:10px;border-radius:8px;font-family:'Orbitron',monospace;font-size:1.2rem;
  letter-spacing:8px;text-align:center;outline:none;margin-bottom:14px;}
.pin-input:focus{border-color:var(--accent4);}
.modal-btns{display:flex;gap:8px;}
.modal-btn{flex:1;padding:9px;border-radius:8px;border:1px solid var(--border);background:none;
  color:var(--text);cursor:pointer;font-family:'Orbitron',monospace;font-size:.6rem;
  letter-spacing:1px;transition:all .18s;}
.modal-btn.ok{border-color:var(--accent2);color:var(--accent2);}
.modal-btn.ok:hover{background:#00ff8815;}
.modal-btn.cancel{border-color:var(--border);}

/* ── LOG DRAWER ── */
.llog{flex:1;overflow-y:auto;display:flex;flex-direction:column;gap:3px;}
.le{font-size:.67rem;font-family:monospace;color:var(--text-dim);padding:3px 6px;
  border-left:2px solid var(--border);background:#050d15;border-radius:0 4px 4px 0;animation:lf .25s ease;}
@keyframes lf{from{opacity:0;transform:translateX(6px);}to{opacity:1;transform:none;}}
.le.ok{border-color:var(--accent2);color:#70efaa;}
.le.err{border-color:var(--accent3);color:#ff8090;}
.le.cmd{border-color:var(--accent);color:#70c8f8;}
.lt{color:var(--text-dim);margin-right:5px;}

/* ══════════════════════════════════════
   CONTROL VERTICAL — ZONA CENTRAL
══════════════════════════════════════ */
#remoteZone{position:fixed;top:44px;left:0;right:0;bottom:30px;
  overflow-y:auto;overflow-x:hidden;
  display:flex;justify-content:center;align-items:flex-start;padding:10px 0 20px;}
#remoteZone::-webkit-scrollbar{width:3px;}
#remoteZone::-webkit-scrollbar-thumb{background:var(--border);}

/* El control vertical */
.remote{
  display:flex;flex-direction:column;align-items:center;gap:8px;
  padding:18px 16px 24px;
  background:linear-gradient(170deg,#111d2e,#0b1520);
  border:1px solid #1c2e44;border-radius:32px;
  box-shadow:0 0 0 1px #08121e,0 24px 64px #00000099,
    inset 0 1px 0 #ffffff07,inset 0 -1px 0 #00000055;
  min-width:240px;width:260px;
  position:relative;
}
.remote::before,.remote::after{content:'';position:absolute;width:32px;height:32px;
  border-color:var(--accent);border-style:solid;opacity:.22;}
.remote::before{top:10px;left:10px;border-width:2px 0 0 2px;border-radius:6px 0 0 0;}
.remote::after{bottom:10px;right:10px;border-width:0 2px 2px 0;border-radius:0 0 6px 0;}

/* ── Sección de botones dentro del control ── */
.rbrand{font-family:'Orbitron',monospace;font-size:.45rem;color:var(--text-dim);letter-spacing:3px;margin-bottom:2px;}
.rstatus{background:#050d14;border:1px solid #0e1e2e;border-radius:7px;padding:5px 10px;
  font-size:.6rem;font-family:monospace;color:var(--text-dim);
  display:flex;justify-content:space-between;width:100%;min-height:26px;}

/* ── Botón base del control ── */
.rb{
  display:flex;align-items:center;justify-content:center;
  background:linear-gradient(145deg,#162235,#0e1b2c);
  border:1px solid #1c3050;border-radius:10px;
  color:var(--text);cursor:pointer;
  font-family:'Rajdhani',sans-serif;font-weight:700;
  transition:background .1s,border-color .1s,box-shadow .1s,transform .08s;
  box-shadow:0 3px 9px #00000055,inset 0 1px 0 #ffffff07;
  position:relative;overflow:hidden;user-select:none;
  /* Tamaño controlado por variable CSS del HUD */
  font-size:calc(var(--bs,40) * 0.3px);
  width:calc(var(--bs,40) * 1px);
  height:calc(var(--bs,40) * 1px);
}
.rb:hover{border-color:var(--accent);box-shadow:0 3px 14px #00000066,0 0 10px #00d4ff28;color:var(--accent);}
.rb:active{transform:scale(.89);background:#00d4ff14;border-color:var(--accent);box-shadow:var(--glow);}

/* Edit mode */
.edit-mode .rb{border-style:dashed;border-color:#ffb80055 !important;cursor:ns-resize !important;}
.edit-mode .rb::after{content:'↕';position:absolute;top:2px;right:3px;
  font-size:8px;color:var(--accent4);opacity:.7;pointer-events:none;}

/* Variantes */
.rb.pwr{background:linear-gradient(145deg,#260e16,#170810);border-color:#300f18;color:#ff4f6d;}
.rb.pwr:hover{border-color:var(--accent3);box-shadow:0 0 14px #ff4f6d33;}
.rb.ok{border-radius:50%;border-color:var(--accent);color:var(--accent);
  background:linear-gradient(135deg,#1a3554,#0f1e30);box-shadow:var(--glow),inset 0 2px 4px #00000066;}
.rb.ok:hover{box-shadow:0 0 26px #00d4ff88;}
.rb.grn{color:var(--accent2);border-color:#183028;}
.rb.grn:hover{border-color:var(--accent2);}
.rb.yel{color:var(--accent4);border-color:#2e2200;}
.rb.yel:hover{border-color:var(--accent4);}
.rb.red{color:#ff4f6d;border-color:#2a0e14;}
.rb.cred{background:#330a0a;border-color:#882018;color:#ff6060;}
.rb.cgrn{background:#0a2e14;border-color:#1a8040;color:#40ff80;}
.rb.cyel{background:#201a00;border-color:#886600;color:#ffcc00;}
.rb.cblu{background:#0a1428;border-color:#1a3088;color:#4080ff;}
.rb.num{font-family:'Orbitron',monospace;}

/* ── FILAS DEL CONTROL ── */
.rrow{display:flex;gap:8px;align-items:center;justify-content:center;width:100%;}
.rcol{display:flex;flex-direction:column;gap:5px;align-items:center;}
/* D-Pad */
.dpad{display:grid;grid-template-columns:repeat(3,1fr);gap:5px;width:min-content;}
.dpad .rb{border-radius:8px;}
.dpad .rb.ok{border-radius:50%;}
/* Numpad */
.npad{display:grid;grid-template-columns:repeat(3,1fr);gap:6px;width:100%;}
/* Divider */
.rdiv{width:80%;height:1px;background:linear-gradient(90deg,transparent,var(--border),transparent);margin:2px 0;}

/* EDIT BANNER */
#editBanner{position:fixed;bottom:34px;left:50%;transform:translateX(-50%) translateY(80px);
  z-index:650;background:#0c1a28;border:1px solid var(--accent4);border-radius:12px;
  padding:9px 18px;display:flex;gap:12px;align-items:center;
  font-family:'Orbitron',monospace;font-size:.58rem;color:var(--accent4);
  box-shadow:0 0 18px #ffb80033;transition:transform .3s cubic-bezier(.175,.885,.32,1.275);}
#editBanner.show{transform:translateX(-50%) translateY(0);}
.ebtn{background:var(--btn-bg);border:1px solid var(--border);color:var(--text);
  padding:5px 12px;border-radius:7px;cursor:pointer;font-family:'Orbitron',monospace;
  font-size:.55rem;letter-spacing:1px;transition:all .18s;}
.ebtn:hover{border-color:var(--accent4);color:var(--accent4);}
.ebtn.save{border-color:var(--accent2);color:var(--accent2);}
.ebtn.save:hover{background:#00ff8812;}

/* TOAST */
#toast{position:fixed;bottom:38px;left:50%;transform:translateX(-50%) translateY(70px);
  background:#0c1a28;border:1px solid var(--accent);color:var(--accent);padding:6px 18px;
  border-radius:18px;font-size:.7rem;font-family:'Orbitron',monospace;letter-spacing:2px;
  z-index:900;transition:transform .28s cubic-bezier(.175,.885,.32,1.275);box-shadow:var(--glow);pointer-events:none;}
#toast.show{transform:translateX(-50%) translateY(0);}

::-webkit-scrollbar{width:3px;}
::-webkit-scrollbar-thumb{background:var(--border);border-radius:2px;}

/* ══════════════════════════════════════
   RESPONSIVE — MÓVIL Y TABLET
══════════════════════════════════════ */

/* Tablet (≤ 768px) */
@media (max-width: 768px) {
  html,body{overflow:auto;}
  #remoteZone{
    position:static;
    padding:54px 0 50px;
    min-height:100vh;
    overflow:visible;
  }
  .remote{
    width:min(320px, 90vw);
    min-width:unset;
  }
  .hdr{padding:0 10px;}
  .logo{font-size:.8rem;letter-spacing:2px;}
  .hbtn{padding:3px 9px;font-size:.7rem;}
  .sbar{padding:0 10px;}
  .drawer{width:min(280px, 85vw);}
  #editBanner{width:90vw;flex-wrap:wrap;gap:8px;justify-content:center;padding:8px 12px;}
}

/* Móvil pequeño (≤ 480px) */
@media (max-width: 480px) {
  .remote{
    width:min(300px, 94vw);
    padding:14px 10px 20px;
    border-radius:24px;
    gap:7px;
  }
  .rb{
    font-size:calc(var(--bs,40) * 0.28px) !important;
    width:calc(var(--bs,40) * 0.92px) !important;
    height:calc(var(--bs,40) * 0.92px) !important;
  }
  .dpad{gap:4px;}
  .npad{gap:5px;}
  .rrow{gap:6px;}
  .rcol{gap:4px;}
  .hdr{height:42px;}
  .hbtn{padding:3px 7px;font-size:.65rem;gap:2px;}
  .logo{font-size:.72rem;letter-spacing:1px;}
  #remoteZone{padding:46px 0 44px;}
  .sbar{height:28px;font-size:.58rem;}
  .drawer{width:min(260px, 88vw);}
  .modal-box{min-width:260px;padding:22px 16px;}
}

/* Muy pequeño (≤ 360px) */
@media (max-width: 360px) {
  .remote{width:96vw;padding:12px 8px 18px;}
  .rb{
    font-size:calc(var(--bs,40) * 0.25px) !important;
    width:calc(var(--bs,40) * 0.84px) !important;
    height:calc(var(--bs,40) * 0.84px) !important;
  }
  .hbtn{font-size:.6rem;padding:2px 6px;}
}
</style>
</head>
<body class="scanlines">

<!-- HEADER -->
<div class="hdr">
  <div class="logo">SMART<em>REMOTE</em></div>
  <div style="display:flex;align-items:center;gap:8px;font-size:.72rem;color:var(--text-dim);">
    <span id="wifiDot" style="width:7px;height:7px;border-radius:50%;background:var(--accent3);animation:blink 2s infinite;display:inline-block;"></span>
    <span id="wifiLbl">Buscando...</span>
  </div>
  <div class="hdr-right">
    <button class="hbtn" onclick="openDrawer('devDrawer')" title="Dispositivos">📡 TV</button>
    <button class="hbtn" id="editBtn" onclick="toggleEdit()" title="Editar tamaños">✏ EDITAR</button>
    <button class="hbtn" onclick="openDrawer('logDrawer')">📋</button>
    <a href="/settings" class="hbtn">⚙</a>
  </div>
</div>

<!-- SHADE -->
<div class="drawer-shade" id="shade" onclick="closeAll()"></div>

<!-- DEVICE DRAWER -->
<div class="drawer" id="devDrawer">
  <div class="dtitle">DISPOSITIVOS</div>
  <button class="scan-btn" id="scanBtn" onclick="scanNow()">⟳ ESCANEAR RED</button>
  <div class="madd">
    <input type="text" id="manualIp" placeholder="192.168.1.x" maxlength="15"/>
    <button onclick="addManual()">+ IP</button>
  </div>
  <div class="dlist" id="devList">
    <div class="no-dev"><div class="spinner"></div>Buscando Smart TVs...<br>
      <small style="color:#2a4060;">SSDP · UPnP · Subnet Scan</small></div>
  </div>
</div>

<!-- LOG DRAWER -->
<div class="drawer right" id="logDrawer">
  <div class="dtitle">ACTIVIDAD</div>
  <div class="llog" id="logList">
    <div class="le"><span class="lt">--:--</span>Sistema iniciado</div>
  </div>
</div>

<!-- PIN MODAL -->
<div class="modal" id="pinModal">
  <div class="modal-box">
    <div class="modal-title">🔐 EMPAREJAMIENTO</div>
    <div class="modal-sub" id="pinMsg">Ingresa el PIN que aparece en tu TV</div>
    <input type="text" class="pin-input" id="pinInput" maxlength="8" placeholder="••••"/>
    <div class="modal-btns">
      <button class="modal-btn cancel" onclick="closePinModal()">CANCELAR</button>
      <button class="modal-btn ok" onclick="submitPin()">CONECTAR</button>
    </div>
  </div>
</div>

<!-- REMOTE ZONE -->
<div id="remoteZone">
<div class="remote" id="remote">

  <div class="rbrand">SMART REMOTE PRO</div>
  <div class="rstatus">
    <span id="stxt">Sin conexión</span>
    <span class="sdot" id="sdot"></span>
  </div>

  <!-- POWER + INPUT -->
  <div class="rrow">
    <div class="rb pwr" data-cmd="power" data-key="power" style="--bs:52;font-size:1.2rem;border-radius:50%;">⏻</div>
    <div style="display:flex;flex-direction:column;gap:6px;flex:1;margin-left:6px;">
      <div class="rb" data-cmd="input" data-key="input" style="--bs:38;width:100%;font-size:.65rem;letter-spacing:1px;">INPUT</div>
      <div class="rb grn" data-cmd="apps" data-key="apps" style="--bs:38;width:100%;font-size:.65rem;letter-spacing:1px;">APPS</div>
    </div>
  </div>

  <div class="rdiv"></div>

  <!-- HOME / BACK / MENU -->
  <div class="rrow">
    <div class="rb" data-cmd="back" data-key="back" style="--bs:46;">⌫</div>
    <div class="rb" data-cmd="home" data-key="home" style="--bs:46;">⌂</div>
    <div class="rb yel" data-cmd="menu" data-key="menu" style="--bs:46;">☰</div>
  </div>

  <!-- D-PAD -->
  <div class="dpad">
    <div></div>
    <div class="rb" data-cmd="up"    data-key="up"    style="--bs:50;border-radius:10px 10px 4px 4px;">▲</div>
    <div></div>
    <div class="rb" data-cmd="left"  data-key="left"  style="--bs:50;border-radius:10px 4px 4px 10px;">◀</div>
    <div class="rb ok" data-cmd="ok" data-key="ok"    style="--bs:54;">OK</div>
    <div class="rb" data-cmd="right" data-key="right" style="--bs:50;border-radius:4px 10px 10px 4px;">▶</div>
    <div></div>
    <div class="rb" data-cmd="down"  data-key="down"  style="--bs:50;border-radius:4px 4px 10px 10px;">▼</div>
    <div></div>
  </div>

  <div class="rdiv"></div>

  <!-- MEDIA -->
  <div class="rrow">
    <div class="rb" data-cmd="prev"   data-key="prev"   style="--bs:42;font-size:1rem;">⏮</div>
    <div class="rb" data-cmd="rewind" data-key="rewind" style="--bs:42;font-size:1rem;">⏪</div>
    <div class="rb grn" data-cmd="play" data-key="play" style="--bs:48;font-size:1rem;">⏯</div>
    <div class="rb" data-cmd="fwd"    data-key="fwd"    style="--bs:42;font-size:1rem;">⏩</div>
    <div class="rb red" data-cmd="stop" data-key="stop" style="--bs:42;font-size:1rem;">⏹</div>
  </div>

  <div class="rdiv"></div>

  <!-- VOL + CH + MUTE -->
  <div class="rrow" style="gap:12px;">
    <div class="rcol">
      <div style="font-size:.52rem;letter-spacing:2px;color:var(--text-dim);font-family:'Orbitron',monospace;">VOL</div>
      <div class="rb" data-cmd="vol_up"   data-key="vol_up"   style="--bs:46;">＋</div>
      <div class="rb" data-cmd="mute"     data-key="mute"     style="--bs:40;font-size:.8rem;">🔇</div>
      <div class="rb" data-cmd="vol_down" data-key="vol_down" style="--bs:46;">－</div>
    </div>
    <div class="rcol">
      <div class="rb grn"  data-cmd="info"     data-key="info"     style="--bs:42;font-size:.75rem;">ℹ</div>
      <div class="rb yel"  data-cmd="subtitle" data-key="subtitle" style="--bs:42;font-size:.65rem;">SUB</div>
      <div class="rb"      data-cmd="sleep"    data-key="sleep"    style="--bs:42;font-size:.58rem;letter-spacing:.5px;">SLP</div>
    </div>
    <div class="rcol">
      <div style="font-size:.52rem;letter-spacing:2px;color:var(--text-dim);font-family:'Orbitron',monospace;">CH</div>
      <div class="rb" data-cmd="ch_up"   data-key="ch_up"   style="--bs:46;">＋</div>
      <div class="rb" data-cmd="aspect"  data-key="aspect"  style="--bs:40;font-size:.6rem;">ASP</div>
      <div class="rb" data-cmd="ch_down" data-key="ch_down" style="--bs:46;">－</div>
    </div>
  </div>

  <div class="rdiv"></div>

  <!-- COLORES -->
  <div class="rrow" style="gap:6px;">
    <div class="rb cred" data-cmd="red"    data-key="red"    style="--bs:36;flex:1;width:auto;font-size:.58rem;">RED</div>
    <div class="rb cgrn" data-cmd="green"  data-key="green"  style="--bs:36;flex:1;width:auto;font-size:.58rem;">GRN</div>
    <div class="rb cyel" data-cmd="yellow" data-key="yellow" style="--bs:36;flex:1;width:auto;font-size:.58rem;">YEL</div>
    <div class="rb cblu" data-cmd="blue"   data-key="blue"   style="--bs:36;flex:1;width:auto;font-size:.58rem;">BLU</div>
  </div>

  <!-- NUMPAD -->
  <div class="npad">
    <div class="rb num" data-cmd="1" data-key="n1" style="--bs:42;">1</div>
    <div class="rb num" data-cmd="2" data-key="n2" style="--bs:42;">2</div>
    <div class="rb num" data-cmd="3" data-key="n3" style="--bs:42;">3</div>
    <div class="rb num" data-cmd="4" data-key="n4" style="--bs:42;">4</div>
    <div class="rb num" data-cmd="5" data-key="n5" style="--bs:42;">5</div>
    <div class="rb num" data-cmd="6" data-key="n6" style="--bs:42;">6</div>
    <div class="rb num" data-cmd="7" data-key="n7" style="--bs:42;">7</div>
    <div class="rb num" data-cmd="8" data-key="n8" style="--bs:42;">8</div>
    <div class="rb num" data-cmd="9" data-key="n9" style="--bs:42;">9</div>
    <div class="rb yel"              data-cmd="*"  data-key="nstar" style="--bs:42;font-size:.9rem;">✳</div>
    <div class="rb num" data-cmd="0" data-key="n0" style="--bs:42;">0</div>
    <div class="rb red"              data-cmd="#"  data-key="nhash" style="--bs:42;font-size:.85rem;">🔙</div>
  </div>

  <!-- BT button -->
  <div class="rdiv"></div>
  <div class="rrow">
    <div class="rb" id="btBtn" onclick="connectBluetooth()" style="--bs:38;flex:1;width:auto;font-size:.62rem;letter-spacing:1px;color:#6080ff;border-color:#2030a0;">🔵 BLUETOOTH</div>
  </div>

</div><!-- /remote -->
</div><!-- /remoteZone -->

<!-- EDIT BANNER -->
<div id="editBanner">
  <span>✏ ARRASTRA ↕ PARA CAMBIAR TAMAÑO</span>
  <button class="ebtn" onclick="resetSizes()">↺ RESET</button>
  <button class="ebtn save" onclick="saveSizes()">✓ GUARDAR</button>
  <button class="ebtn" onclick="toggleEdit()">✕ LISTO</button>
</div>

<!-- STATUS BAR -->
<div class="sbar">
  <span><span class="sdot" id="sbarDot"></span><span id="sbarTxt">Sin conexión</span></span>
  <span class="sig" id="sigTxt">◌◌◌◌</span>
</div>

<div id="toast">OK</div>

<script>
// ══════════════════════════
//  STATE
// ══════════════════════════
let selDev = null, devices = [], editMode = false;
let sizes = JSON.parse(localStorage.getItem('btnSizes_v4')||'{}');
let pendingPairIp = '', pendingPairBrand = '';

// ══════════════════════════
//  BOTONES — click + resize
// ══════════════════════════
function applyAllSizes() {
  document.querySelectorAll('.rb[data-key]').forEach(el => {
    const k = el.dataset.key;
    if (sizes[k] !== undefined) el.style.setProperty('--bs', sizes[k]);
  });
}

let resizeEl = null, resizeStartY = 0, resizeStartSize = 0;

document.querySelectorAll('.rb[data-cmd]').forEach(el => {
  // TAP normal → comando
  el.addEventListener('click', e => {
    if (editMode) return;
    send(el.dataset.cmd);
  });

  // Pointer para resize en modo edición
  el.addEventListener('pointerdown', e => {
    if (!editMode) return;
    resizeEl = el;
    resizeStartY = e.clientY;
    resizeStartSize = parseFloat(getComputedStyle(el).getPropertyValue('--bs')) ||
                      parseInt(el.style.getPropertyValue('--bs')) || 42;
    el.setPointerCapture(e.pointerId);
    e.preventDefault();
  });
  el.addEventListener('pointermove', e => {
    if (!editMode || resizeEl !== el) return;
    const dy = e.clientY - resizeStartY;
    const newSize = Math.max(28, Math.min(120, resizeStartSize + dy * 0.6));
    el.style.setProperty('--bs', Math.round(newSize));
    e.preventDefault();
  });
  el.addEventListener('pointerup', e => {
    if (resizeEl === el) {
      const k = el.dataset.key;
      if (k) sizes[k] = Math.round(parseFloat(el.style.getPropertyValue('--bs'))||42);
      resizeEl = null;
    }
  });
});

function saveSizes() {
  localStorage.setItem('btnSizes_v4', JSON.stringify(sizes));
  showToast('✓ TAMAÑOS GUARDADOS');
}
function resetSizes() {
  if (!confirm('¿Restaurar tamaños por defecto?')) return;
  sizes = {};
  localStorage.removeItem('btnSizes_v4');
  document.querySelectorAll('.rb[data-key]').forEach(el => el.style.removeProperty('--bs'));
  showToast('↺ RESETEADO');
}

function toggleEdit() {
  editMode = !editMode;
  document.getElementById('remote').classList.toggle('edit-mode', editMode);
  document.getElementById('editBanner').classList.toggle('show', editMode);
  document.getElementById('editBtn').classList.toggle('active', editMode);
  if (editMode) {
    closeAll();
    showToast('✏ ARRASTRA ↕ PARA CAMBIAR TAMAÑO');
    addLog('Modo edición activado','cmd');
  }
}

// ══════════════════════════
//  DISPOSITIVOS
// ══════════════════════════
async function loadNet() {
  try{const r=await fetch('/api/network_info');const d=await r.json();
    document.getElementById('wifiLbl').textContent=d.local_ip||'WiFi OK';}catch(e){}
}

async function loadDevices() {
  try{
    const r=await fetch('/api/devices');const d=await r.json();
    devices=d.devices; renderDevices(); updateWifi();
    const saved=localStorage.getItem('lastDev');
    if(saved&&!selDev){const f=devices.find(d=>d.ip===saved);if(f)selectDev(f,false);}
  }catch(e){}
}

const TYPE_LABELS={samsung_tv:'SAMSUNG TV',lg_tv:'LG WEBOS',sony_tv:'SONY BRAVIA',
  philips_tv:'PHILIPS TV',android_tv:'ANDROID TV',roku:'ROKU',smart_tv:'SMART TV',
  chromecast:'CHROMECAST',hisense_tv:'HISENSE TV',vizio_tv:'VIZIO TV',kodi:'KODI',tv:'TV'};

function renderDevices() {
  const list=document.getElementById('devList');
  if(!devices.length){
    list.innerHTML=`<div class="no-dev"><div class="spinner"></div>Buscando Smart TVs...<br>
      <small style="color:#2a4060;">SSDP · UPnP · Subnet Scan</small></div>`;
    return;
  }
  list.innerHTML=devices.map(d=>{
    const paired=d.paired;
    const sel=selDev?.ip===d.ip;
    return `<div class="dcard ${sel?'sel':''}">
      <div class="dcard-name">${d.name}</div>
      <div class="dcard-ip">${d.ip}</div>
      <div class="dcard-type">${TYPE_LABELS[d.type]||d.type}${d.model?' · '+d.model:''}</div>
      <div class="dcard-badge ${paired?'paired':'unpaired'}">${paired?'● EMPAREJADO':'○ SIN EMPAREJAR'}</div>
      <div class="dcard-actions">
        <button class="dcard-btn connect" onclick='connectDev(${JSON.stringify(d)})'>CONECTAR</button>
        <button class="dcard-btn" onclick='selectDev(${JSON.stringify(d)},true)'>USAR</button>
      </div>
    </div>`;
  }).join('');
}

function updateWifi(){
  const dot=document.getElementById('wifiDot'),lbl=document.getElementById('wifiLbl');
  if(selDev){dot.style.background='var(--accent2)';lbl.textContent=selDev.name;}
  else if(devices.length>0){dot.style.background='var(--accent2)';lbl.textContent=devices.length+' TV(s)';}
  else{dot.style.background='var(--accent3)';lbl.textContent='Buscando...';}
}

function selectDev(dev, close=true) {
  if(typeof dev==='string')dev=JSON.parse(dev);
  selDev=dev;
  renderDevices();updateWifi();
  updateStatus(dev.name,true);
  document.getElementById('sigTxt').textContent='◈◈◈◈';
  addLog(`Usando: ${dev.name} (${dev.ip})`,'ok');
  showToast(`📡 ${dev.name}`);
  localStorage.setItem('lastDev',dev.ip);
  if(close) closeAll();
}

async function connectDev(dev) {
  if(typeof dev==='string')dev=JSON.parse(dev);
  addLog(`Emparejando ${dev.name}...`,'cmd');
  showToast('🔗 CONECTANDO...');
  try{
    const r=await fetch('/api/pair',{method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({ip:dev.ip,brand:dev.brand||'unknown'})});
    const d=await r.json();
    if(d.ok){
      if(d.needs_pin){
        pendingPairIp=dev.ip; pendingPairBrand=dev.brand||'';
        document.getElementById('pinMsg').textContent=d.message||'Ingresa el PIN de tu TV';
        document.getElementById('pinInput').value='';
        document.getElementById('pinModal').classList.add('show');
      } else {
        addLog(`✓ Emparejado: ${dev.name} [${d.method}]`,'ok');
        showToast(`✓ ${dev.name}`);
        selectDev(dev,true);
        loadDevices();
      }
    } else {
      addLog(`✗ No se pudo emparejar ${dev.ip}`,'err');
      showToast('⚠ Sin respuesta — intenta USAR directamente');
      // De todas formas permitir usar
      selectDev(dev,false);
    }
  }catch(e){
    addLog('✗ Error de red','err');
    selectDev(dev,false);
  }
}

async function submitPin() {
  const pin=document.getElementById('pinInput').value.trim();
  if(!pin){showToast('⚠ Ingresa el PIN');return;}
  try{
    const r=await fetch('/api/submit_pin',{method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({ip:pendingPairIp,pin})});
    const d=await r.json();
    closePinModal();
    if(d.ok){
      addLog(`✓ PIN aceptado`,'ok');showToast('✓ EMPAREJADO');
      loadDevices();
    } else {addLog('✗ PIN incorrecto','err');showToast('✗ PIN incorrecto');}
  }catch(e){closePinModal();addLog('✗ Error','err');}
}
function closePinModal(){document.getElementById('pinModal').classList.remove('show');}

async function scanNow(){
  const btn=document.getElementById('scanBtn');
  btn.classList.add('spin');btn.textContent='⟳ ESCANEANDO...';
  addLog('Iniciando SSDP + subnet scan...','cmd');
  try{await fetch('/api/scan',{method:'POST'});}catch(e){}
  setTimeout(async()=>{
    await loadDevices();
    btn.classList.remove('spin');btn.textContent='⟳ ESCANEAR RED';
    addLog(`Encontrados: ${devices.length} disp.`,devices.length?'ok':'');
  },9000);
}

async function addManual(){
  const ip=document.getElementById('manualIp').value.trim();
  if(!ip)return;
  try{
    const r=await fetch('/api/add_device',{method:'POST',
      headers:{'Content-Type':'application/json'},body:JSON.stringify({ip})});
    const d=await r.json();
    showToast(d.status==='ok'?`✓ ${d.name}`:'⚠ Sin respuesta');
    addLog(`Manual ${ip}: ${d.name||'desconocido'}`,d.status==='ok'?'ok':'err');
    setTimeout(loadDevices,800);
  }catch(e){showToast('⚠ Error');}
  document.getElementById('manualIp').value='';
}

// ══════════════════════════
//  BLUETOOTH
// ══════════════════════════
async function connectBluetooth() {
  if(!navigator.bluetooth){
    showToast('⚠ Bluetooth no disponible en este navegador');
    addLog('Web Bluetooth API no disponible','err');
    return;
  }
  try{
    addLog('Buscando dispositivos Bluetooth...','cmd');
    showToast('🔵 BUSCANDO BT...');
    const device = await navigator.bluetooth.requestDevice({
      acceptAllDevices: true,
      optionalServices: ['battery_service','device_information']
    });
    addLog(`BT: ${device.name||'Dispositivo'} conectado`,'ok');
    showToast(`🔵 ${device.name||'BT OK'}`);
    // Añadir como dispositivo manual con IP ficticia BT
    const fakeDev={ip:`bt:${device.id||Date.now()}`,name:device.name||'Dispositivo BT',
      type:'android_tv',brand:'android',paired:true,source:'bluetooth',model:'',manufacturer:''};
    devices.unshift(fakeDev);
    renderDevices();
    selectDev(fakeDev,false);
  }catch(e){
    if(e.name==='NotFoundError')addLog('BT: Sin dispositivos seleccionados','');
    else{addLog(`BT Error: ${e.message}`,'err');showToast('⚠ BT: '+e.message.substring(0,30));}
  }
}

// ══════════════════════════
//  ENVIAR COMANDO
// ══════════════════════════
async function send(cmd){
  if(!selDev){showToast('⚠ Selecciona un TV');addLog('Sin TV seleccionado','err');return;}
  const s=JSON.parse(localStorage.getItem('remoteSettings')||'{}');
  if(s.vibration&&navigator.vibrate)navigator.vibrate(22);
  addLog(`→ ${cmd.toUpperCase()}`,'cmd');
  try{
    const r=await fetch('/api/command',{method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({ip:selDev.ip,command:cmd,brand:selDev.brand||'unknown',device_type:selDev.type})});
    const d=await r.json();
    if(d.status==='ok'){addLog(`✓ [${d.method}]`,'ok');updateStatus(cmd.toUpperCase(),true);}
    else{addLog(`✗ ${d.message||'Error'}`,'err');updateStatus('Error',false);}
  }catch(e){addLog('✗ Red','err');}
  showToast(cmd.toUpperCase());
}

// ══════════════════════════
//  UI helpers
// ══════════════════════════
function openDrawer(id){
  closeAll();
  document.getElementById(id).classList.add('open');
  document.getElementById('shade').classList.add('show');
}
function closeAll(){
  document.querySelectorAll('.drawer').forEach(d=>d.classList.remove('open'));
  document.getElementById('shade').classList.remove('show');
}
function updateStatus(msg,ok){
  document.getElementById('stxt').textContent=msg;
  document.getElementById('sbarTxt').textContent=msg;
  const cls='sdot'+(ok?' ok':ok===false?' err':'');
  document.getElementById('sdot').className=cls;
  document.getElementById('sbarDot').className=cls;
}
function addLog(msg,type=''){
  const list=document.getElementById('logList');
  const t=new Date().toLocaleTimeString('es',{hour12:false,hour:'2-digit',minute:'2-digit'});
  const el=document.createElement('div');
  el.className='le '+type;
  el.innerHTML=`<span class="lt">${t}</span>${msg}`;
  list.prepend(el);
  while(list.children.length>120)list.removeChild(list.lastChild);
}
let toastTimer;
function showToast(msg){
  const t=document.getElementById('toast');t.textContent=msg;t.classList.add('show');
  clearTimeout(toastTimer);toastTimer=setTimeout(()=>t.classList.remove('show'),1100);
}
function applySettings(){
  const s=JSON.parse(localStorage.getItem('remoteSettings')||'{}');
  if(s.accentColor)document.documentElement.style.setProperty('--accent',s.accentColor);
  if(s.accentColor2)document.documentElement.style.setProperty('--accent2',s.accentColor2);
  if(s.scanlines===false)document.body.classList.remove('scanlines');
}

// ══════════════════════════
//  TECLADO
// ══════════════════════════
document.addEventListener('keydown',e=>{
  const s=JSON.parse(localStorage.getItem('remoteSettings')||'{}');
  if(s.keyShortcuts===false||editMode)return;
  const map={ArrowUp:'up',ArrowDown:'down',ArrowLeft:'left',ArrowRight:'right',
    Enter:'ok',Backspace:'back',Escape:'home','+':'vol_up','-':'vol_down',
    'm':'mute','p':'play','s':'stop'};
  if(map[e.key]){e.preventDefault();send(map[e.key]);}
});

// ══════════════════════════
//  INIT
// ══════════════════════════
applySettings();
applyAllSizes();
loadNet();
loadDevices();
setInterval(loadDevices,8000);
</script>
</body>
</html>
"""

# ─────────────────────────────────────────────
#  HTML — AJUSTES
# ─────────────────────────────────────────────

SETTINGS_HTML = r"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1.0"/>
<title>SmartRemote – Ajustes</title>
<link href="https://fonts.googleapis.com/css2?family=Orbitron:wght@400;700;900&family=Rajdhani:wght@300;500;700&display=swap" rel="stylesheet"/>
<style>
:root{--bg:#060a10;--panel:#0c1520;--border:#182a3e;
  --accent:#00d4ff;--accent2:#00ff88;--accent3:#ff4f6d;--accent4:#ffb800;
  --btn-bg:#0e1c2e;--text:#b8daf8;--text-dim:#3a6080;
  --glow:0 0 18px #00d4ff44;}
*{margin:0;padding:0;box-sizing:border-box;}
body{background:var(--bg);color:var(--text);font-family:'Rajdhani',sans-serif;min-height:100vh;
  background-image:radial-gradient(ellipse at 10% 30%,#001a2e22,transparent 55%),
    radial-gradient(ellipse at 90% 70%,#001a1422,transparent 55%);}
body::before{content:'';position:fixed;inset:0;pointer-events:none;z-index:9999;
  background:repeating-linear-gradient(0deg,transparent,transparent 2px,#00000012 2px,#00000012 3px);}
header{display:flex;align-items:center;gap:14px;padding:11px 20px;
  border-bottom:1px solid var(--border);background:#080e18;position:sticky;top:0;z-index:100;}
.back{text-decoration:none;color:var(--text-dim);border:1px solid var(--border);
  padding:6px 13px;border-radius:6px;font-size:.78rem;letter-spacing:1px;transition:all .18s;}
.back:hover{border-color:var(--accent);color:var(--accent);}
.ptitle{font-family:'Orbitron',monospace;font-size:.9rem;font-weight:900;
  color:var(--accent);letter-spacing:3px;text-shadow:var(--glow);}
.vtag{margin-left:auto;font-size:.6rem;color:var(--text-dim);letter-spacing:2px;}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:16px;padding:20px;max-width:960px;margin:0 auto;}
@media(max-width:640px){.grid{grid-template-columns:1fr;}}
.sec{background:var(--panel);border:1px solid var(--border);border-radius:14px;padding:18px;position:relative;overflow:hidden;}
.sec::before{content:'';position:absolute;top:0;left:0;right:0;height:1px;
  background:linear-gradient(90deg,transparent,var(--accent),transparent);opacity:.3;}
.stitle{font-family:'Orbitron',monospace;font-size:.6rem;color:var(--accent);letter-spacing:3px;
  margin-bottom:14px;display:flex;align-items:center;gap:10px;}
.stitle::after{content:'';flex:1;height:1px;background:var(--border);}
.field{margin-bottom:12px;}
label{display:block;font-size:.7rem;color:var(--text-dim);letter-spacing:1px;margin-bottom:4px;}
input[type=range]{width:100%;-webkit-appearance:none;height:3px;background:var(--border);border-radius:2px;outline:none;}
input[type=range]::-webkit-slider-thumb{-webkit-appearance:none;width:15px;height:15px;
  border-radius:50%;background:var(--accent);cursor:pointer;box-shadow:0 0 7px var(--accent);}
input[type=color]{width:100%;height:32px;border:1px solid var(--border);border-radius:6px;background:var(--btn-bg);cursor:pointer;padding:2px;}
.rv{float:right;font-family:'Orbitron',monospace;font-size:.65rem;color:var(--accent);}
select{width:100%;background:var(--btn-bg);border:1px solid var(--border);color:var(--text);
  padding:7px 10px;border-radius:7px;font-family:'Rajdhani',sans-serif;font-size:.88rem;outline:none;}
select:focus{border-color:var(--accent);}
.tr{display:flex;align-items:center;justify-content:space-between;padding:7px 0;border-bottom:1px solid #0c1a28;}
.tl{font-size:.82rem;color:var(--text);}
.ts{font-size:.66rem;color:var(--text-dim);}
.tog{position:relative;width:42px;height:22px;}
.tog input{opacity:0;width:0;height:0;}
.tt{position:absolute;inset:0;background:#080f18;border:1px solid var(--border);border-radius:11px;cursor:pointer;transition:.28s;}
.tt::after{content:'';position:absolute;left:3px;top:50%;transform:translateY(-50%);
  width:15px;height:15px;border-radius:50%;background:var(--text-dim);transition:.28s;}
.tog input:checked+.tt{background:#00ff8818;border-color:var(--accent2);}
.tog input:checked+.tt::after{left:calc(100% - 18px);background:var(--accent2);box-shadow:0 0 7px var(--accent2);}
.arow{display:flex;gap:8px;margin-top:12px;flex-wrap:wrap;}
.abtn{flex:1;min-width:85px;padding:9px;border-radius:7px;border:1px solid var(--border);
  background:var(--btn-bg);color:var(--text);font-family:'Orbitron',monospace;font-size:.56rem;
  letter-spacing:2px;cursor:pointer;transition:all .18s;}
.abtn:hover{border-color:var(--accent);color:var(--accent);}
.abtn.p{border-color:var(--accent2);color:var(--accent2);}
.abtn.p:hover{background:#00ff8810;}
.abtn.d{border-color:#2a0e14;color:var(--accent3);}
.dev-row{display:flex;align-items:center;justify-content:space-between;
  padding:8px 10px;background:var(--btn-bg);border:1px solid var(--border);
  border-radius:8px;margin-bottom:7px;}
.drn{font-size:.82rem;font-weight:700;}
.dri{font-size:.66rem;color:var(--text-dim);font-family:monospace;}
.drt{font-size:.6rem;color:var(--accent2);}
.badge{padding:2px 7px;border-radius:9px;font-size:.56rem;font-family:'Orbitron',monospace;
  border:1px solid var(--accent2);color:var(--accent2);background:#00ff8810;}
.pbadge{display:inline-block;padding:2px 7px;border-radius:5px;font-size:.56rem;
  font-family:'Orbitron',monospace;border:1px solid var(--accent2);color:var(--accent2);
  background:#00ff8810;margin:2px;}
#toast{position:fixed;bottom:20px;left:50%;transform:translateX(-50%) translateY(60px);
  background:#0c1a28;border:1px solid var(--accent);color:var(--accent);padding:7px 16px;
  border-radius:16px;font-size:.7rem;font-family:'Orbitron',monospace;letter-spacing:2px;
  z-index:9000;transition:transform .28s cubic-bezier(.175,.885,.32,1.275);box-shadow:var(--glow);pointer-events:none;}
#toast.show{transform:translateX(-50%) translateY(0);}
::-webkit-scrollbar{width:3px;}
::-webkit-scrollbar-thumb{background:var(--border);border-radius:2px;}

/* ══════════════════════════════════════
   RESPONSIVE — MÓVIL Y TABLET
══════════════════════════════════════ */
@media (max-width: 640px) {
  .grid{grid-template-columns:1fr;padding:12px;gap:12px;}
  header{padding:9px 14px;gap:10px;}
  .ptitle{font-size:.78rem;letter-spacing:2px;}
  .back{padding:5px 10px;font-size:.72rem;}
  .sec{padding:14px;}
  .abtn{font-size:.5rem;padding:8px;}
  .dev-row{flex-wrap:wrap;gap:4px;}
}
@media (max-width: 400px) {
  .grid{padding:8px;gap:10px;}
  header{padding:8px 10px;}
  .ptitle{font-size:.7rem;letter-spacing:1px;}
  .stitle{font-size:.55rem;}
  .tr{flex-wrap:wrap;gap:6px;}
}
</style>
</head>
<body>
<header>
  <a href="/" class="back">← REMOTE</a>
  <div class="ptitle">⚙ AJUSTES</div>
  <div class="vtag">v4.0.0</div>
</header>

<div class="grid">

  <!-- APARIENCIA -->
  <div class="sec">
    <div class="stitle">APARIENCIA</div>
    <div class="field">
      <label>COLOR DE ACENTO</label>
      <input type="color" id="accentColor" value="#00d4ff" oninput="prev(this.value)"/>
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
    <div class="tr">
      <div><div class="tl">Efecto Scanlines</div><div class="ts">Líneas CRT</div></div>
      <label class="tog"><input type="checkbox" id="scanlines" checked onchange="saveSetting('scanlines',this.checked)"/>
        <div class="tt"></div></label>
    </div>
    <div class="tr">
      <div><div class="tl">Vibración (móvil)</div><div class="ts">Feedback háptico</div></div>
      <label class="tog"><input type="checkbox" id="vibration" onchange="saveSetting('vibration',this.checked)"/>
        <div class="tt"></div></label>
    </div>
    <div class="arow"><button class="abtn p" onclick="saveAppearance()">✓ APLICAR</button></div>
  </div>

  <!-- CONTROL -->
  <div class="sec">
    <div class="stitle">CONTROL REMOTO</div>
    <div class="tr">
      <div><div class="tl">Atajos de Teclado</div><div class="ts">↑↓←→ Enter M P S</div></div>
      <label class="tog"><input type="checkbox" id="keyShortcuts" checked onchange="saveSetting('keyShortcuts',this.checked)"/>
        <div class="tt"></div></label>
    </div>
    <div class="tr">
      <div><div class="tl">Auto-reconexión</div><div class="ts">Reconecta al último TV</div></div>
      <label class="tog"><input type="checkbox" id="autoReconnect" checked onchange="saveSetting('autoReconnect',this.checked)"/>
        <div class="tt"></div></label>
    </div>
    <div class="field" style="margin-top:12px;">
      <label>TIMEOUT COMANDO <span class="rv" id="toVal">3000ms</span></label>
      <input type="range" id="cmdTO" min="500" max="10000" step="500" value="3000"
             oninput="document.getElementById('toVal').textContent=this.value+'ms';saveSetting('cmdTimeout',+this.value)"/>
    </div>
    <div style="margin-top:12px;font-size:.72rem;color:var(--text-dim);line-height:1.7;">
      Para ajustar el <strong style="color:var(--accent4);">tamaño de los botones</strong>,
      pulsa <strong style="color:var(--accent);">✏ EDITAR</strong> en el control y arrastra ↕ cada botón.
    </div>
    <div class="arow">
      <button class="abtn d" onclick="resetAll()">↺ RESET</button>
      <button class="abtn p" onclick="saveAll()">✓ GUARDAR</button>
    </div>
  </div>

  <!-- RED -->
  <div class="sec">
    <div class="stitle">RED WIFI</div>
    <div class="field">
      <label>IP LOCAL</label>
      <input type="text" id="localIp" readonly value="Cargando..."
             style="width:100%;background:var(--btn-bg);border:1px solid var(--border);color:var(--accent2);
               padding:7px 10px;border-radius:7px;font-family:monospace;font-size:.85rem;outline:none;"/>
    </div>
    <div class="field">
      <label>SUBRED</label>
      <input type="text" id="subnet" readonly value="..."
             style="width:100%;background:var(--btn-bg);border:1px solid var(--border);color:var(--text-dim);
               padding:7px 10px;border-radius:7px;font-family:monospace;font-size:.85rem;outline:none;"/>
    </div>
    <div class="field">
      <label>PROTOCOLOS ACTIVOS</label>
      <div>
        <span class="pbadge">SSDP</span><span class="pbadge">UPnP</span>
        <span class="pbadge">Samsung REST</span><span class="pbadge">LG UDAP</span>
        <span class="pbadge">Sony IRCC</span><span class="pbadge">Roku ECP</span>
        <span class="pbadge">Philips JS</span><span class="pbadge">Kodi RPC</span>
        <span class="pbadge">Hisense HTTP</span><span class="pbadge">Subnet Scan</span>
        <span class="pbadge">ADB WiFi</span>
      </div>
    </div>
    <div class="field">
      <label>DISPOSITIVOS DETECTADOS (<span id="devCount">0</span>)</label>
      <div id="devList" style="max-height:200px;overflow-y:auto;"></div>
    </div>
    <button class="abtn" style="width:100%;" onclick="pingScan()">⟳ ESCANEAR AHORA</button>
  </div>

  <!-- INFO -->
  <div class="sec">
    <div class="stitle">INFORMACIÓN</div>
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-bottom:14px;">
      <div style="background:var(--btn-bg);border:1px solid var(--border);border-radius:9px;padding:12px;">
        <div style="font-size:.6rem;color:var(--text-dim);letter-spacing:2px;">VERSIÓN</div>
        <div style="font-family:'Orbitron',monospace;color:var(--accent);margin-top:3px;">4.0.0</div>
      </div>
      <div style="background:var(--btn-bg);border:1px solid var(--border);border-radius:9px;padding:12px;">
        <div style="font-size:.6rem;color:var(--text-dim);letter-spacing:2px;">MARCAS</div>
        <div style="font-family:'Orbitron',monospace;color:var(--accent2);margin-top:3px;font-size:.65rem;">10+</div>
      </div>
    </div>
    <div style="font-size:.7rem;color:var(--text-dim);line-height:1.8;">
      <strong style="color:var(--accent);">Compatibilidad:</strong>
      Samsung (Tizen) · LG (webOS) · Sony Bravia · Philips · TCL · Hisense · Android TV · Roku · Kodi · UPnP/DLNA<br>
      <strong style="color:var(--accent2);">Descubrimiento:</strong>
      SSDP Multicast + Subnet Scan (puertos 8001, 8060, 3000, 1925, 36669, 5555...)<br>
      <strong style="color:var(--accent4);">Pairing:</strong>
      Samsung (sin PIN) · LG (PIN en TV) · Sony (PSK) · Roku (sin PIN) · Philips (sin PIN)
    </div>
  </div>

</div>
<div id="toast">OK</div>

<script>
const THEMES={cyber:{accent:'#00d4ff',accent2:'#00ff88'},neon:{accent:'#39ff14',accent2:'#00ffcc'},
  fire:{accent:'#ff4f1f',accent2:'#ffb800'},gold:{accent:'#ffb800',accent2:'#ff8c00'},purple:{accent:'#bf00ff',accent2:'#ff00aa'}};

function applyTheme(t){const th=THEMES[t];if(!th)return;
  document.documentElement.style.setProperty('--accent',th.accent);
  document.documentElement.style.setProperty('--accent2',th.accent2);
  document.getElementById('accentColor').value=th.accent;}
function prev(v){document.documentElement.style.setProperty('--accent',v);}
function saveAppearance(){
  const s=JSON.parse(localStorage.getItem('remoteSettings')||'{}');
  s.accentColor=document.getElementById('accentColor').value;
  s.theme=document.getElementById('themeSelect').value;
  s.scanlines=document.getElementById('scanlines').checked;
  s.vibration=document.getElementById('vibration').checked;
  localStorage.setItem('remoteSettings',JSON.stringify(s));showToast('✓ GUARDADO');}
function saveSetting(k,v){const s=JSON.parse(localStorage.getItem('remoteSettings')||'{}');s[k]=v;localStorage.setItem('remoteSettings',JSON.stringify(s));}
function saveAll(){saveAppearance();showToast('✓ TODO GUARDADO');}
function resetAll(){if(!confirm('¿Resetear todo?'))return;
  localStorage.removeItem('remoteSettings');localStorage.removeItem('btnSizes_v4');
  showToast('↺ RESETEADO');setTimeout(()=>location.reload(),800);}
function loadSettings(){
  const s=JSON.parse(localStorage.getItem('remoteSettings')||'{}');
  if(s.accentColor){document.getElementById('accentColor').value=s.accentColor;prev(s.accentColor);}
  if(s.theme)document.getElementById('themeSelect').value=s.theme;
  if(s.scanlines!==undefined)document.getElementById('scanlines').checked=s.scanlines;
  if(s.vibration!==undefined)document.getElementById('vibration').checked=s.vibration;
  if(s.keyShortcuts!==undefined)document.getElementById('keyShortcuts').checked=s.keyShortcuts;
  if(s.autoReconnect!==undefined)document.getElementById('autoReconnect').checked=s.autoReconnect;
  if(s.cmdTimeout!==undefined){document.getElementById('cmdTO').value=s.cmdTimeout;document.getElementById('toVal').textContent=s.cmdTimeout+'ms';}}

async function loadNet(){try{const r=await fetch('/api/network_info');const d=await r.json();
  document.getElementById('localIp').value=d.local_ip;document.getElementById('subnet').value=d.subnet;}catch(e){}}
async function loadNetDevs(){
  try{const r=await fetch('/api/devices');const d=await r.json();
    document.getElementById('devCount').textContent=d.count;
    const c=document.getElementById('devList');
    if(!d.devices.length){c.innerHTML='<div style="color:var(--text-dim);font-size:.72rem;padding:8px 0;">Sin dispositivos</div>';return;}
    const TYPE={samsung_tv:'Samsung TV',lg_tv:'LG TV',sony_tv:'Sony TV',android_tv:'Android TV',
      roku:'Roku',smart_tv:'Smart TV',philips_tv:'Philips TV',hisense_tv:'Hisense TV',tv:'TV'};
    c.innerHTML=d.devices.map(dev=>`<div class="dev-row">
      <div><div class="drn">${dev.name}</div><div class="dri">${dev.ip}</div>
        <div class="drt">${TYPE[dev.type]||dev.type} · ${dev.source||'ssdp'}</div></div>
      <span class="badge">${dev.paired?'PAIRED':'ONLINE'}</span></div>`).join('');}catch(e){}}
async function pingScan(){showToast('ESCANEANDO...');await fetch('/api/scan',{method:'POST'});setTimeout(loadNetDevs,9000);}

let toastTimer;
function showToast(msg){const t=document.getElementById('toast');t.textContent=msg;t.classList.add('show');
  clearTimeout(toastTimer);toastTimer=setTimeout(()=>t.classList.remove('show'),1800);}
loadSettings();loadNet();loadNetDevs();setInterval(loadNetDevs,15000);
</script>
</body>
</html>
"""

if __name__ == "__main__":
    print("=" * 62)
    print("  🚀 SmartRemote Pro v4.0 — WiFi + Bluetooth")
    print("=" * 62)
    print("  Control:  http://localhost:5000")
    print("  Ajustes:  http://localhost:5000/settings")
    print("=" * 62)
    print("  Marcas:   Samsung · LG · Sony · Philips · TCL")
    print("            Hisense · Android TV · Roku · Kodi")
    print("  Pairing:  Samsung (auto) · LG/Sony (PIN en TV)")
    print("=" * 62)

    disc = threading.Thread(target=run_discovery, daemon=True)
    disc.start()
    print("[WiFi] Descubrimiento iniciado (SSDP + subnet scan)...")

    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)
