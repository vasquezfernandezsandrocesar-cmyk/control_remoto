# 📱 SmartRemote Pro — Control Remoto Universal WiFi

Control remoto futurista para Smart TV y Android Box via WiFi, con diseño cyberpunk y descubrimiento automático de dispositivos.

## ✅ Características

- **Descubrimiento automático** de Smart TVs por SSDP/UPnP en red WiFi
- **Control completo**: Power, navegación, volumen, canales, media, números, colores
- **Página de ajustes** separada con:
  - HUD arrastrable para posicionar el control
  - Slider de escala/tamaño
  - Temas de color (Cyber, Neon, Fire, Gold, Purple)
  - Toggles de opciones
- **Log de actividad** en tiempo real
- **Atajos de teclado**: Flechas, Enter, M (mute), P (play), S (stop)
- **API REST** para enviar comandos

## 🚀 Instalación y uso

```bash
# 1. Instalar dependencias
pip install -r requirements.txt

# 2. Ejecutar el servidor
python app.py

# 3. Abrir en el navegador
http://localhost:5000

# Ajustes
http://localhost:5000/settings
```

## 📡 Protocolos soportados

| Protocolo | Uso |
|-----------|-----|
| SSDP      | Descubrimiento de dispositivos |
| UPnP/SOAP | Envío de comandos AV |
| HTTP      | API REST del servidor |

## 🎮 Atajos de teclado

| Tecla | Acción |
|-------|--------|
| ↑↓←→ | Navegación D-Pad |
| Enter | OK |
| Backspace | Atrás |
| Escape | Home |
| M | Mute |
| P | Play/Pause |
| S | Stop |
| +/- | Volumen |

## 🔧 API REST

```
GET  /api/devices        → Lista dispositivos detectados
POST /api/scan           → Escaneo inmediato
POST /api/command        → Enviar comando
GET  /api/network_info   → IP local y subnet
```

### Ejemplo enviar comando:
```json
POST /api/command
{
  "ip": "192.168.1.100",
  "command": "vol_up"
}
```

## 📺 Compatibilidad

- Samsung Smart TV (Tizen)
- LG Smart TV (webOS)  
- Sony Bravia
- Android TV / Box
- Roku
- Cualquier dispositivo UPnP/DLNA
