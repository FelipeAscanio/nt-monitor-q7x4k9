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
from zoneinfo import ZoneInfo

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
    "User-Agent": "Mozilla/5.0 (compatible; monitor-stock-personal/1.0)",
    "Accept": "text/html,application/xhtml+xml",
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
    "bestbuy": {
        "disponible": ['"buttonState":"ADD_TO_CART"', "Add to Cart"],
        "agotado": ['"buttonState":"SOLD_OUT', "Sold Out", "Coming Soon"],
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
        return revisar_pagina(producto, tienda="bestbuy")
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

DIAS = {"lun": 0, "mar": 1, "mie": 2, "jue": 3, "vie": 4, "sab": 5, "dom": 6}


def en_ventana(v, ahora_local):
    """¿La hora local cae dentro de la ventana {dias, desde, hasta}?"""
    dias = [DIAS[d.lower()[:3].replace("é", "e").replace("á", "a")] for d in v.get("dias", DIAS)]
    h = ahora_local.strftime("%H:%M")
    desde, hasta = v["desde"], v["hasta"]
    if desde <= hasta:
        return ahora_local.weekday() in dias and desde <= h < hasta
    # Ventana que cruza la medianoche (ej. 23:00-01:00): cuenta el día en que empieza.
    ayer = (ahora_local.weekday() - 1) % 7
    return (ahora_local.weekday() in dias and h >= desde) or (ayer in dias and h < hasta)


def ejecutar():
    config = yaml.safe_load(ARCHIVO_PRODUCTOS.read_text(encoding="utf-8")) or {}
    productos = [p for p in config.get("productos", []) if p.get("activo", True)]
    ventanas = config.get("ventanas") or {}
    zona = ZoneInfo(config.get("zona_horaria", "America/Chicago"))
    duracion = float(config.get("duracion_ciclo_segundos", 270))
    estado = cargar_estado()
    inicio = time.monotonic()

    def activa(tienda):
        v = ventanas.get(tienda)
        return v is not None and en_ventana(v, datetime.now(zona))

    # Fuera de su ventana, una tienda se revisa 1 vez por ejecución (cada ~5 min),
    # salvo que su ventana diga fuera_de_ventana: nada.
    normales = [p for p in productos if not activa(p.get("tienda"))
                and (ventanas.get(p.get("tienda")) or {}).get("fuera_de_ventana", "normal") != "nada"]
    intensivos = [p for p in productos if activa(p.get("tienda"))]

    print(f"Hora local: {datetime.now(zona):%a %H:%M} | normales: {len(normales)} | en ventana: {len(intensivos)}")
    revisar_lista(normales, estado)
    guardar_estado(estado)

    # Dentro de una ventana: revisar en ciclo cada ~20-30 s hasta completar el ciclo.
    ronda = 1
    while intensivos and time.monotonic() - inicio < duracion:
        cada = min(float((ventanas.get(p.get("tienda")) or {}).get("cada_segundos", 25)) for p in intensivos)
        hubo_bloqueo = revisar_lista(intensivos, estado, ronda=ronda)
        guardar_estado(estado)
        espera = 90 if hubo_bloqueo else random.uniform(cada * 0.8, cada * 1.2)  # frena si nos bloquean
        if time.monotonic() - inicio + espera >= duracion:
            break
        time.sleep(espera)
        ronda += 1
        intensivos = [p for p in intensivos if activa(p.get("tienda"))]


def revisar_lista(productos, estado, ronda=None):
    hubo_bloqueo = False
    if ronda:
        print(f"-- ronda {ronda} en ventana")
    for i, p in enumerate(productos):
        if i:
            time.sleep(random.uniform(1.5, 3.5))  # pausa para no saturar a las tiendas
        hubo_bloqueo |= procesar(p, estado) == BLOQUEADO
    return hubo_bloqueo


def procesar(p, estado):
    ahora = datetime.now(timezone.utc).isoformat(timespec="seconds")
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
    return nuevo


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
