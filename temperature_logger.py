#!/usr/bin/env python3
"""
DS18B20 Temperature Logger per Raspberry Pi
Llegeix la temperatura cada 30 segons i la puja a GitHub.

Requisits:
    pip3 install requests

Configuració al fitxer config.json (a la mateixa carpeta):
{
    "github_token": "EL_TEU_TOKEN_GITHUB",
    "github_repo": "abelber07/raspberry",
    "github_file_path": "temperatures.json",
    "local_config_path": "/home/pi/temperature_config.json",
    "interval_seconds": 30,
    "max_records": 500
}
"""

import os
import glob
import time
import json
import base64
import logging
import requests
from datetime import datetime, timezone

# ── Configuració de logging ──────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("/home/pi/temperature_logger.log"),
    ],
)
log = logging.getLogger(__name__)

# ── Constants ────────────────────────────────────────────────────────────────
CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config.json")
DEFAULT_CONFIG = {
    "github_token": "",
    "github_repo": "abelber07/raspberry",
    "github_file_path": "temperatures.json",
    "local_config_path": "/home/pi/temperature_config.json",
    "interval_seconds": 30,
    "max_records": 500,
}

# ── Inicialitza el mòdul 1-Wire ──────────────────────────────────────────────
def init_sensor():
    """Carrega els mòduls del kernel necessaris per al DS18B20."""
    os.system("modprobe w1-gpio")
    os.system("modprobe w1-therm")
    time.sleep(1)


def find_sensor():
    """Localitza el fitxer de dades del sensor DS18B20."""
    base_dir = "/sys/bus/w1/devices/"
    device_folders = glob.glob(os.path.join(base_dir, "28-*"))
    if not device_folders:
        raise FileNotFoundError(
            "No s'ha trobat cap sensor DS18B20. "
            "Comprova el cablejat i que el pin GPIO4 (o el configurat) estigui actiu."
        )
    return os.path.join(device_folders[0], "w1_slave")


def read_temperature(device_file: str) -> float:
    """Llegeix la temperatura del sensor i la retorna en °C."""
    with open(device_file, "r") as f:
        lines = f.readlines()

    # El sensor pot retornar CRC invalid; reintentem fins 5 cops
    retries = 0
    while lines[0].strip()[-3:] != "YES" and retries < 5:
        time.sleep(0.2)
        with open(device_file, "r") as f:
            lines = f.readlines()
        retries += 1

    if lines[0].strip()[-3:] != "YES":
        raise RuntimeError("El sensor no retorna dades vàlides (CRC error).")

    equals_pos = lines[1].find("t=")
    if equals_pos == -1:
        raise RuntimeError("Format de dades inesperat del sensor.")

    temp_string = lines[1][equals_pos + 2:]
    temp_c = float(temp_string) / 1000.0
    return round(temp_c, 2)


# ── Gestió de configuració local ────────────────────────────────────────────
def load_config() -> dict:
    if not os.path.exists(CONFIG_PATH):
        log.warning("config.json no trobat. Creant fitxer per defecte...")
        with open(CONFIG_PATH, "w") as f:
            json.dump(DEFAULT_CONFIG, f, indent=2)
        log.warning("Edita %s amb el teu token de GitHub.", CONFIG_PATH)
    with open(CONFIG_PATH, "r") as f:
        cfg = json.load(f)
    # Omple els camps que faltin
    for k, v in DEFAULT_CONFIG.items():
        cfg.setdefault(k, v)
    return cfg


# ── Gestió del JSON local de la Raspberry ───────────────────────────────────
def load_local_data(path: str) -> list:
    """Carrega les dades locals existents o retorna llista buida."""
    if os.path.exists(path):
        try:
            with open(path, "r") as f:
                data = json.load(f)
            if isinstance(data, list):
                return data
        except json.JSONDecodeError:
            log.warning("El fitxer local JSON estava corrupte. Es reinicia.")
    return []


def save_local_data(path: str, records: list):
    """Guarda les dades al fitxer local."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w") as f:
        json.dump(records, f, indent=2, ensure_ascii=False)


# ── Interacció amb GitHub ────────────────────────────────────────────────────
GITHUB_API = "https://api.github.com"


def _headers(token: str) -> dict:
    return {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github.v3+json",
    }


def get_github_file_sha(repo: str, path: str, token: str) -> str | None:
    """Obté el SHA del fitxer a GitHub (necessari per actualitzar-lo)."""
    url = f"{GITHUB_API}/repos/{repo}/contents/{path}"
    r = requests.get(url, headers=_headers(token), timeout=15)
    if r.status_code == 200:
        return r.json().get("sha")
    elif r.status_code == 404:
        return None  # El fitxer no existeix encara
    else:
        r.raise_for_status()


def push_to_github(repo: str, path: str, token: str, records: list):
    """Crea o actualitza el fitxer JSON a GitHub."""
    content = json.dumps(records, indent=2, ensure_ascii=False)
    encoded = base64.b64encode(content.encode("utf-8")).decode("utf-8")

    sha = get_github_file_sha(repo, path, token)
    url = f"{GITHUB_API}/repos/{repo}/contents/{path}"

    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    payload = {
        "message": f"[auto] Actualització temperatura {now_str}",
        "content": encoded,
    }
    if sha:
        payload["sha"] = sha

    r = requests.put(url, headers=_headers(token), json=payload, timeout=20)
    r.raise_for_status()
    log.info("Dades pujades a GitHub correctament (%d registres).", len(records))


# ── Bucle principal ──────────────────────────────────────────────────────────
def main():
    log.info("=== Iniciant Temperature Logger ===")
    cfg = load_config()

    if not cfg["github_token"]:
        log.error(
            "Falta el token de GitHub a config.json. "
            "Genera un token a https://github.com/settings/tokens "
            "(necessita permís 'repo') i afegeix-lo al fitxer."
        )
        return

    init_sensor()
    try:
        device_file = find_sensor()
        log.info("Sensor trobat: %s", device_file)
    except FileNotFoundError as e:
        log.error(str(e))
        return

    interval = cfg["interval_seconds"]
    max_records = cfg["max_records"]
    local_path = cfg["local_config_path"]
    repo = cfg["github_repo"]
    gh_path = cfg["github_file_path"]
    token = cfg["github_token"]

    log.info(
        "Configuració: interval=%ds, màx. registres=%d, repo=%s",
        interval,
        max_records,
        repo,
    )

    while True:
        try:
            temp = read_temperature(device_file)
            timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            record = {"timestamp": timestamp, "temperature_c": temp}
            log.info("Temperatura: %.2f °C", temp)

            # Carrega dades locals, afegeix el nou registre i trunca si cal
            records = load_local_data(local_path)
            records.append(record)
            if len(records) > max_records:
                records = records[-max_records:]

            # Guarda localment
            save_local_data(local_path, records)

            # Puja a GitHub
            push_to_github(repo, gh_path, token, records)

        except Exception as exc:
            log.error("Error durant la lectura/pujada: %s", exc)

        time.sleep(interval)


if __name__ == "__main__":
    main()
