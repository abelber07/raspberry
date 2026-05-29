#!/usr/bin/env python3
"""
DS18B20 Temperature Logger per Raspberry Pi
- No necesita pip3 install (usa solo librerías estándar de Python)
- En el primer arranque pregunta el token y detecta el sensor automáticamente
"""

import os
import glob
import time
import json
import base64
import logging
import getpass
from urllib.request import urlopen, Request
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from datetime import datetime, timezone

# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("/home/pi/temperature_logger.log"),
    ],
)
log = logging.getLogger(__name__)

# ── Rutas ─────────────────────────────────────────────────────────────────────
CONFIG_PATH    = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
GITHUB_API     = "https://api.github.com"
DEFAULT_CONFIG = {
    "github_token":      "",
    "github_repo":       "abelber07/raspberry",
    "github_file_path":  "temperatures.json",
    "local_data_path":   "/home/pi/temperature_data.json",
    "sensor_path":       "",
    "interval_seconds":  30,
    "max_records":       500,
}

# ── Setup interactivo ─────────────────────────────────────────────────────────
def ask(prompt, default="", secret=False):
    """Pregunta al usuario con un valor por defecto opcional."""
    hint = f" [{default}]" if default else ""
    full_prompt = f"{prompt}{hint}: "
    if secret:
        val = getpass.getpass(full_prompt)
    else:
        val = input(full_prompt).strip()
    return val if val else default


def interactive_setup() -> dict:
    """Primera configuración: pide token y detecta sensor."""
    print("\n" + "="*55)
    print("  CONFIGURACIÓN INICIAL - Temperature Logger DS18B20")
    print("="*55)

    cfg = dict(DEFAULT_CONFIG)

    # Token GitHub
    print("\n🔑 TOKEN DE GITHUB")
    print("  Consíguelo en: https://github.com/settings/tokens")
    print("  Necesita el permiso: repo")
    while not cfg["github_token"]:
        cfg["github_token"] = ask("  Pega tu token aquí", secret=True)
        if not cfg["github_token"]:
            print("  ⚠️  El token no puede estar vacío.")

    # Sensor DS18B20
    print("\n🌡️  BUSCANDO SENSOR DS18B20...")
    os.system("modprobe w1-gpio 2>/dev/null")
    os.system("modprobe w1-therm 2>/dev/null")
    time.sleep(1)

    sensors = glob.glob("/sys/bus/w1/devices/28-*/w1_slave")
    if sensors:
        print(f"  ✅ Sensor detectado automáticamente: {sensors[0]}")
        if len(sensors) > 1:
            print("  Se han encontrado varios sensores:")
            for i, s in enumerate(sensors):
                print(f"    [{i}] {s}")
            idx = ask("  ¿Cuál quieres usar? (número)", default="0")
            try:
                cfg["sensor_path"] = sensors[int(idx)]
            except (ValueError, IndexError):
                cfg["sensor_path"] = sensors[0]
        else:
            cfg["sensor_path"] = sensors[0]
    else:
        print("  ⚠️  No se ha detectado ningún sensor.")
        print("  Comprueba el cableado (GPIO4, resistencia 4.7kΩ) y vuelve a intentarlo.")
        print("  Si quieres continuar de todas formas, introduce la ruta manualmente.")
        cfg["sensor_path"] = ask(
            "  Ruta del sensor (deja vacío para salir)",
            default=""
        )
        if not cfg["sensor_path"]:
            print("  Saliendo. Comprueba el sensor y vuelve a ejecutar el script.")
            raise SystemExit(1)

    print(f"\n  Intervalo de lectura: cada {cfg['interval_seconds']} segundos")
    intervalo = ask("  ¿Cambiar intervalo? (segundos, Enter para mantener)", default="")
    if intervalo.isdigit():
        cfg["interval_seconds"] = int(intervalo)

    return cfg


def load_or_create_config() -> dict:
    """Carga config existente o lanza el setup interactivo."""
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, "r") as f:
            cfg = json.load(f)
        # Completa campos que falten
        for k, v in DEFAULT_CONFIG.items():
            cfg.setdefault(k, v)
        # Si falta el token o el sensor, vuelve a preguntar
        if not cfg.get("github_token") or not cfg.get("sensor_path"):
            print("⚠️  Configuración incompleta. Iniciando configuración...")
            cfg = interactive_setup()
            save_config(cfg)
    else:
        cfg = interactive_setup()
        save_config(cfg)
    return cfg


def save_config(cfg: dict):
    with open(CONFIG_PATH, "w") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)
    log.info("Configuración guardada en %s", CONFIG_PATH)


# ── Sensor ────────────────────────────────────────────────────────────────────
def read_temperature(device_file: str) -> float:
    with open(device_file, "r") as f:
        lines = f.readlines()

    retries = 0
    while lines[0].strip()[-3:] != "YES" and retries < 5:
        time.sleep(0.2)
        with open(device_file, "r") as f:
            lines = f.readlines()
        retries += 1

    if lines[0].strip()[-3:] != "YES":
        raise RuntimeError("El sensor no retorna datos válidos (CRC error).")

    equals_pos = lines[1].find("t=")
    if equals_pos == -1:
        raise RuntimeError("Formato de datos inesperado del sensor.")

    temp_c = float(lines[1][equals_pos + 2:]) / 1000.0
    return round(temp_c, 2)


# ── Datos locales ─────────────────────────────────────────────────────────────
def load_local_data(path: str) -> list:
    if os.path.exists(path):
        try:
            with open(path, "r") as f:
                data = json.load(f)
            if isinstance(data, list):
                return data
        except json.JSONDecodeError:
            log.warning("JSON local corrupto, se reinicia.")
    return []


def save_local_data(path: str, records: list):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w") as f:
        json.dump(records, f, indent=2, ensure_ascii=False)


# ── GitHub (urllib, sin pip) ──────────────────────────────────────────────────
def _headers(token: str) -> dict:
    return {
        "Authorization": f"token {token}",
        "Accept":        "application/vnd.github.v3+json",
        "Content-Type":  "application/json",
    }


def github_get_sha(repo: str, path: str, token: str):
    url = f"{GITHUB_API}/repos/{repo}/contents/{path}"
    req = Request(url, headers=_headers(token))
    try:
        with urlopen(req, timeout=15) as r:
            return json.loads(r.read()).get("sha")
    except HTTPError as e:
        if e.code == 404:
            return None
        raise


def push_to_github(repo: str, path: str, token: str, records: list):
    content  = json.dumps(records, indent=2, ensure_ascii=False)
    encoded  = base64.b64encode(content.encode("utf-8")).decode("utf-8")
    sha      = github_get_sha(repo, path, token)
    url      = f"{GITHUB_API}/repos/{repo}/contents/{path}"
    now_str  = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    payload = {
        "message": f"[auto] Temperatura {now_str}",
        "content": encoded,
    }
    if sha:
        payload["sha"] = sha

    data = json.dumps(payload).encode("utf-8")
    req  = Request(url, data=data, headers=_headers(token), method="PUT")
    with urlopen(req, timeout=20) as r:
        r.read()
    log.info("✅ Subido a GitHub (%d registros)", len(records))


# ── Bucle principal ───────────────────────────────────────────────────────────
def main():
    log.info("=== Temperature Logger DS18B20 ===")

    cfg = load_or_create_config()

    token      = cfg["github_token"]
    repo       = cfg["github_repo"]
    gh_path    = cfg["github_file_path"]
    local_path = cfg["local_data_path"]
    sensor     = cfg["sensor_path"]
    interval   = cfg["interval_seconds"]
    max_rec    = cfg["max_records"]

    print(f"\n🚀 Iniciando lectura cada {interval}s  |  Sensor: {sensor}")
    print("   Pulsa Ctrl+C para detener.\n")

    while True:
        try:
            temp      = read_temperature(sensor)
            timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            record    = {"timestamp": timestamp, "temperature_c": temp}
            log.info("🌡️  Temperatura: %.2f °C", temp)

            records = load_local_data(local_path)
            records.append(record)
            if len(records) > max_rec:
                records = records[-max_rec:]

            save_local_data(local_path, records)
            push_to_github(repo, gh_path, token, records)

        except KeyboardInterrupt:
            print("\n👋 Detenido por el usuario.")
            break
        except Exception as exc:
            log.error("❌ Error: %s", exc)

        time.sleep(interval)


if __name__ == "__main__":
    main()
