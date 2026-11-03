#!/usr/bin/env python3
"""
Monitor de stock de Pokémon TCG -> alerta push al teléfono (ntfy).

No compra nada: solo avisa cuando un producto pasa a "disponible"
para que tú entres y compres manualmente. Al tocar la notificación
se abre directamente la página del producto.

Uso:
  python tracker.py                       # revisa todo y alerta si hay cambios
  python tracker.py --prueba-notificacion # manda una notificación de prueba
  python tracker.py --diagnostico URL     # muestra qué ve el script en esa página
"""

import argparse
import json
import os
import random
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
import yaml

BASE = Path(__file__).resolve().parent
ARCHIVO_PRODUCTOS = BASE / "productos.yaml"
ARCHIVO_ESTADO = BASE / "estado.json"

NTFY_SERVIDOR = os.environ.get("NTFY_SERVIDOR", "https://ntfy.sh").rstrip("/")
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "").strip()
BESTBUY_API_KEY = os.environ.get("BESTBUY_API_KEY", "").strip()

# Tras cuántas revisiones seguidas bloqueadas te avisamos (1 sola vez).
AVISO_BLOQUEO_TRAS = 12

DISPONIBLE, AGOTADO, BLOQUEADO, ERROR, DESCONOCIDO = (
    "disponible", "agotado", "bloqueado", "error", "desconocido",
)

NAVEGADOR = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

# Señales por defecto en el HTML de cada tienda. Se pueden sobrescribir
# por producto en productos.yaml (textos_disponible / textos_agotado).
SENALES = {
    "walmart": {
        "disponible": ['"availabilityStatus":"IN_STOCK"'],
        "agotado": ['"availabilityStatus":"OUT_OF_STOCK"', "Out of stock"],
    },
    "amazon": {
        "disponible": ['id="add-to-cart-button"', 'id="buy-now-button"'],
        "agotado": ["Currently unavailable", "Temporarily out of stock"],
    },
    "target": {
        "disponible": ['"availability_status":"IN_STOCK"', "Add to cart"],
        "agotado": ['"availability_status":"OUT_OF_STOCK"', "Out of stock", "Sold out"],
    },
    "generico": {"disponible": ["Add to cart", "Add to Cart"], "agotado": ["Out of stock", "Sold out"]},
}

SENALES_BLOQUEO = [
    "captcha", "are you a robot", "robot or human", "press & hold",
    "access denied", "request unsuccessful", "px-captcha", "validatecaptcha",
]


# ----------------------------------------------------------------- revisores

def revisar_bestbuy(producto):
    """Best Buy tiene API oficial gratuita: lo más confiable."""
    sku = str(producto.get("sku", "")).strip()
    if not sku:
        return ERROR, "falta 'sku' en productos.yaml", None
    if not BESTBUY_API_KEY:
        # Sin llave de API, intentamos con la página normal.
        return revisar_pagina(producto, tienda="generico")
    url = (
        f"https://api.bestbuy.com/v1/products/{sku}.json"
        f"?show=name,onlineAvailability,salePrice,url&apiKey={BESTBUY_API_KEY}"
    )
    r = requests.get(url, timeout=20)
    if r.status_code in (403, 429):
        return BLOQUEADO, f"API respondió {r.status_code}", None
    if r.status_code != 200:
        return ERROR, f"API respondió {r.status_code}", None
    datos = r.json()
    precio = datos.get("salePrice")
    estado = DISPONIBLE if datos.get("onlineAvailability") else AGOTADO
    return estado, "API Best Buy", precio


def revisar_pagina(producto, tienda=None):
    """Descarga la página del producto y busca señales de stock en el HTML."""
    tienda = tienda or producto.get("tienda", "generico")
    base = SENALES.get(tienda, SENALES["generico"])
    textos_si = producto.get("textos_disponible") or base["disponible"]
    textos_no = producto.get("textos_agotado") or base["agotado"]

    r = requests.get(producto["url"], headers=NAVEGADOR, timeout=25)
    html = r.text
    bajo = html.lower()

    if r.status_code in (403, 429, 503) or any(s in bajo for s in SENALES_BLOQUEO):
        return BLOQUEADO, f"la tienda bloqueó la consulta (HTTP {r.status_code})", None
    if r.status_code != 200:
        return ERROR, f"HTTP {r.status_code}", None

    precio = extraer_precio(html)
    # "Agotado" gana si aparecen ambas señales (evita falsas alarmas).
    if any(t in html for t in textos_no):
        return AGOTADO, "señal de agotado encontrada", precio
    if any(t in html for t in textos_si):
        return DISPONIBLE, "señal de disponible encontrada", precio
    return DESCONOCIDO, "no se encontró ninguna señal (revisa con --diagnostico)", precio


def extraer_precio(html):
    """Intento simple de leer el precio; si falla devuelve None."""
    for patron in (
        r'"currentPrice"\s*:\s*\{[^}]*?"price"\s*:\s*([\d.]+)',
        r'"current_retail"\s*:\s*([\d.]+)',
        r'"priceAmount"\s*:\s*([\d.]+)',
        r'itemprop="price"\s+content="([\d.]+)"',
    ):
        m = re.search(patron, html)
        if m:
            try:
                return float(m.group(1))
            except ValueError:
                pass
    return None


REVISORES = {"bestbuy": revisar_bestbuy}


def revisar(producto):
    revisor = REVISORES.get(producto.get("tienda"), revisar_pagina)
    try:
        return revisor(producto)
    except requests.RequestException as e:
        return ERROR, f"error de red: {type(e).__name__}", None


# ------------------------------------------------------------- notificación

def notificar(titulo, mensaje, url=None, prioridad=5, etiquetas=("rotating_light",)):
    if not NTFY_TOPIC:
        print(f"  [sin NTFY_TOPIC, no se envía] {titulo}: {mensaje}")
        return False
    cuerpo = {
        "topic": NTFY_TOPIC,
        "title": titulo,
        "message": mensaje,
        "priority": prioridad,
        "tags": list(etiquetas),
    }
    if url:
        cuerpo["click"] = url  # tocar la notificación abre el producto
        cuerpo["actions"] = [{"action": "view", "label": "Abrir y comprar", "url": url, "clear": True}]
    try:
        r = requests.post(NTFY_SERVIDOR, json=cuerpo, timeout=15)
        r.raise_for_status()
        return True
    except requests.RequestException as e:
        print(f"  ! No se pudo enviar la notificación: {e}")
        return False


# ------------------------------------------------------------------- estado

def cargar_estado():
    try:
        return json.loads(ARCHIVO_ESTADO.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def guardar_estado(estado):
    ARCHIVO_ESTADO.write_text(json.dumps(estado, indent=2, ensure_ascii=False), encoding="utf-8")


def clave(producto):
    return producto.get("id") or f'{producto.get("tienda")}:{producto.get("sku") or producto.get("url")}'


# --------------------------------------------------------------- principal

def ejecutar():
    config = yaml.safe_load(ARCHIVO_PRODUCTOS.read_text(encoding="utf-8")) or {}
    productos = [p for p in config.get("productos", []) if p.get("activo", True)]
    estado = cargar_estado()
    ahora = datetime.now(timezone.utc).isoformat(timespec="seconds")

    print(f"Revisando {len(productos)} productos ({ahora})")
    for i, p in enumerate(productos):
        if i:
            time.sleep(random.uniform(2, 5))  # pausa para no saturar a las tiendas

        nuevo, detalle, precio = revisar(p)
        k = clave(p)
        previo = estado.get(k, {})
        anterior = previo.get("estado")
        bloqueos = previo.get("bloqueos_seguidos", 0) + 1 if nuevo == BLOQUEADO else 0

        tope = p.get("precio_maximo")
        caro = tope is not None and precio is not None and precio > float(tope)
        precio_txt = f"${precio:,.2f}" if precio is not None else "precio n/d"
        print(f"  [{p.get('tienda')}] {p['nombre']}: {nuevo} ({detalle}; {precio_txt})")

        # Alerta solo cuando cambia a disponible (no en cada revisión).
        if nuevo == DISPONIBLE and anterior != DISPONIBLE:
            if caro:
                print(f"    disponible pero arriba de tu precio máximo (${float(tope):,.2f}); no se alerta")
            else:
                notificar(
                    f"¡DISPONIBLE en {p.get('tienda', '').title()}!",
                    f"{p['nombre']}\n{precio_txt}",
                    url=p.get("url"),
                )

        # Aviso único si una tienda lleva mucho tiempo bloqueando al script.
        if bloqueos == AVISO_BLOQUEO_TRAS:
            notificar(
                "Monitor: tienda bloqueando consultas",
                f"{p.get('tienda', '').title()} lleva {bloqueos} revisiones bloqueando "
                f"'{p['nombre']}'. Ese producto no se está vigilando bien.",
                prioridad=3, etiquetas=("warning",),
            )

        # Un bloqueo o error temporal no borra el último estado real conocido.
        # Disponible pero caro se guarda aparte, para avisar si luego baja de precio.
        if nuevo == DISPONIBLE and caro:
            estado_guardado = "caro"
        elif nuevo in (DISPONIBLE, AGOTADO):
            estado_guardado = nuevo
        else:
            estado_guardado = anterior or nuevo
        estado[k] = {
            "nombre": p["nombre"],
            "estado": estado_guardado,
            "ultimo_resultado": nuevo,
            "detalle": detalle,
            "precio": precio,
            "bloqueos_seguidos": bloqueos,
            "revisado": ahora,
        }

    guardar_estado(estado)


def diagnostico(url):
    r = requests.get(url, headers=NAVEGADOR, timeout=25)
    html = r.text
    print(f"HTTP {r.status_code}, {len(html):,} caracteres")
    bajo = html.lower()
    print("Señales de bloqueo:", [s for s in SENALES_BLOQUEO if s in bajo] or "ninguna")
    for tienda, s in SENALES.items():
        for tipo in ("disponible", "agotado"):
            hallados = [t for t in s[tipo] if t in html]
            if hallados:
                print(f"  {tienda}/{tipo}: {hallados}")
    print("Precio detectado:", extraer_precio(html))


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Monitor de stock Pokémon TCG")
    ap.add_argument("--prueba-notificacion", action="store_true")
    ap.add_argument("--diagnostico", metavar="URL")
    args = ap.parse_args()

    if args.prueba_notificacion:
        ok = notificar("Prueba del monitor Pokémon", "Si ves esto, las alertas funcionan.",
                       url="https://www.pokemon.com/us/pokemon-tcg", prioridad=4, etiquetas=("white_check_mark",))
        sys.exit(0 if ok else 1)
    elif args.diagnostico:
        diagnostico(args.diagnostico)
    else:
        ejecutar()
