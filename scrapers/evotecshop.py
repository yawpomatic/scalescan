"""
Scraper de Evotec Shop (evotecshop.com) para el Buscador de Slot.

Qué hace:
  1. Recorre las categorías de "Coches" de la tienda (1/32 y 1/24).
  2. Por cada producto encontrado, extrae: fabricante de la miniatura,
     referencia, nombre, precio, precio original (si hay oferta), si está
     en stock, la URL del producto y la imagen.
  3. Guarda cada referencia nueva en la tabla `products` de Supabase
     (con los campos "bonitos" vacíos, para que el usuario los rellene
     a mano más adelante).
  4. Guarda siempre una fila en `listings` con el precio y estado actual.

Diseñado para bajo impacto en el servidor de la tienda:
  - Pausa de varios segundos entre peticiones (ver REQUEST_DELAY_SECONDS).
  - Un único hilo, sin peticiones en paralelo.
  - User-Agent identificado con datos de contacto.
  - Pensado para ejecutarse 1-2 veces al día vía GitHub Actions, no en
    tiempo real.

NOTA IMPORTANTE para quien mantenga este script:
  Los selectores CSS de abajo (product_selectors) están basados en una
  inspección del contenido visible de la web, no del HTML exacto letra
  por letra. La primera vez que se ejecute conviene revisar los logs
  (print) para comprobar que se están extrayendo bien los datos, y
  ajustar los selectores si hace falta. El extractor por texto/regex es
  el plan B si los selectores CSS fallan.
"""

import os
import re
import sys
import time
from datetime import datetime, timezone

import requests
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# CONFIGURACIÓN
# ---------------------------------------------------------------------------

BASE_URL = "https://evotecshop.com"

# Categorías de "Coches" a recorrer (1/32 y 1/24). Añade o quita según
# necesites. Puedes sacar más categorías navegando la web y copiando la URL.
CATEGORY_URLS = [
    "https://evotecshop.com/es/29-rally",
    "https://evotecshop.com/es/27-turismos",
    "https://evotecshop.com/es/26-f-1",
    "https://evotecshop.com/es/323-formula-1-nsr-86-89-y-22",
    "https://evotecshop.com/es/289-grupo-5",
    "https://evotecshop.com/es/25-lmp",
    "https://evotecshop.com/es/24-gt",
    "https://evotecshop.com/es/31-road-car",
    "https://evotecshop.com/es/28-clasicos",
    "https://evotecshop.com/es/30-raid",
    "https://evotecshop.com/es/92-coleccionismo",
    "https://evotecshop.com/es/52-coches-grz-1-24",
    "https://evotecshop.com/es/284-coches-sc-gt-1-24",
    "https://evotecshop.com/es/320-coches-brm-tts",
]

STORE_NAME = "Evotec Shop"

# Pausa entre peticiones (segundos). No bajar de 3.
REQUEST_DELAY_SECONDS = 4

# Cabeceras completas, como las que envía un navegador normal. Algunas
# tiendas con protección anti-bot (Cloudflare, etc.) devuelven 403 si
# faltan cabeceras como Accept o Accept-Language, aunque el User-Agent
# sea legítimo y transparente.
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 "
        "BuscadorSlotBot/1.0 (+https://github.com/TU_USUARIO/buscador-slot; "
        "contacto: tu-email@example.com)"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}

REQUEST_TIMEOUT_SECONDS = 20

# ---------------------------------------------------------------------------
# EXTRACCIÓN DE UNA PÁGINA DE CATEGORÍA
# ---------------------------------------------------------------------------

# Patrón para sacar fabricante + referencia de un bloque de texto, cubre
# variantes vistas en la web: "Fabricante X Referencia Y" y
# "X (Ref.:Y)" y "Referencia: Y Fabricante: X"
REF_PATTERNS = [
    re.compile(r"Fabricante:?\s*(?P<brand>[^\n]+?)\s*Referencia:?\s*(?P<ref>[A-Za-z0-9\-\._/]+)", re.IGNORECASE),
    re.compile(r"Referencia:?\s*(?P<ref>[A-Za-z0-9\-\._/]+)\s*Fabricante:?\s*(?P<brand>[^\n]+)", re.IGNORECASE),
    re.compile(r"(?P<brand>[A-Za-z0-9\.\s]+?)\s*\(Ref\.:\s*(?P<ref>[A-Za-z0-9\-\._/]+)\)", re.IGNORECASE),
]

PRICE_PATTERN = re.compile(r"(\d{1,3}(?:[.,]\d{2}))\s*€")


def extract_reference_and_brand(text: str):
    """Intenta sacar (fabricante, referencia) de un bloque de texto."""
    for pattern in REF_PATTERNS:
        match = pattern.search(text)
        if match:
            brand = match.group("brand").strip(" .-")
            ref = match.group("ref").strip()
            return brand, ref
    return None, None


def extract_prices(text: str):
    """Devuelve (precio_actual, precio_original_o_None) a partir del texto."""
    prices = PRICE_PATTERN.findall(text)
    if not prices:
        return None, None
    # Convertir "31,86" -> 31.86
    prices = [float(p.replace(".", "").replace(",", ".")) for p in prices]
    if len(prices) == 1:
        return prices[0], None
    # Si hay dos precios, el más alto es el original y el más bajo el actual
    return min(prices), max(prices)


def is_out_of_stock(text: str) -> bool:
    lowered = text.lower()
    return "en reposicion" in lowered or "en reposición" in lowered or "agotado" in lowered


# Sesión reutilizada en todo el scraper: mantiene cookies entre peticiones,
# igual que haría un navegador normal (algunas protecciones anti-bot lo
# comprueban).
SESSION = requests.Session()
SESSION.headers.update(HEADERS)


def fetch_category_page(url: str, page: int = 1, _retried: bool = False):
    """Descarga una página de categoría (con paginación de PrestaShop)."""
    params = {"page": page} if page > 1 else {}
    response = SESSION.get(url, params=params, timeout=REQUEST_TIMEOUT_SECONDS)

    if response.status_code == 403 and not _retried:
        # Reintento único tras una pausa más larga: a veces el primer
        # bloqueo es temporal (rate-limit) y no un bloqueo permanente.
        print("  [aviso] 403 recibido, esperando 15s y reintentando una vez...")
        time.sleep(15)
        return fetch_category_page(url, page, _retried=True)

    response.raise_for_status()
    return response.text


def parse_products_from_html(html: str, category_url: str):
    """
    Extrae los productos de una página de categoría.

    Devuelve una lista de diccionarios con:
      reference, slot_brand, name, price, original_price, in_stock,
      product_url, image_url
    """
    soup = BeautifulSoup(html, "html.parser")
    products = []

    # PrestaShop suele marcar cada producto con la clase "product-miniature"
    # o el atributo data-id-product. Probamos varias variantes.
    candidates = soup.select("[data-id-product]") or soup.select(".product-miniature") or soup.select("article.product")

    if not candidates:
        print(f"  [aviso] No se encontraron bloques de producto reconocibles en {category_url}")
        return products

    for block in candidates:
        block_text = block.get_text(separator=" ", strip=True)

        # Nombre del producto: normalmente el enlace con clase product-title
        title_tag = block.select_one(".product-title a") or block.select_one("h3 a") or block.select_one("a")
        name = title_tag.get_text(strip=True) if title_tag else None
        product_url = title_tag["href"] if title_tag and title_tag.has_attr("href") else None

        # Imagen
        img_tag = block.select_one("img")
        image_url = None
        if img_tag:
            image_url = img_tag.get("src") or img_tag.get("data-src")

        brand, reference = extract_reference_and_brand(block_text)
        price, original_price = extract_prices(block_text)
        in_stock = not is_out_of_stock(block_text)

        if not name or not reference:
            # Si no hay referencia, lo registramos aparte para revisión manual
            # (no lo descartamos del todo: lo marcamos como sin referencia)
            reference = reference or f"SIN-REF-{abs(hash(name or product_url)) % 100000}"

        products.append(
            {
                "reference": reference,
                "slot_brand": brand,
                "name": name,
                "price": price,
                "original_price": original_price,
                "in_stock": in_stock,
                "product_url": product_url,
                "image_url": image_url,
                "has_reference": bool(brand and reference and not reference.startswith("SIN-REF-")),
            }
        )

    return products


def scrape_category(category_url: str):
    """Recorre todas las páginas de una categoría hasta que no haya más productos."""
    all_products = []
    page = 1
    while True:
        print(f"Descargando {category_url} (página {page})...")
        html = fetch_category_page(category_url, page)
        products = parse_products_from_html(html, category_url)

        if not products:
            break

        all_products.extend(products)
        page += 1
        time.sleep(REQUEST_DELAY_SECONDS)

        # Límite de seguridad para no quedarnos en un bucle infinito si la
        # paginación no funciona como se espera.
        if page > 50:
            print("  [aviso] Límite de 50 páginas alcanzado, se corta aquí.")
            break

    return all_products


def scrape_all():
    all_products = []
    for category_url in CATEGORY_URLS:
        try:
            products = scrape_category(category_url)
            print(f"  -> {len(products)} productos en {category_url}")
            all_products.extend(products)
        except requests.RequestException as exc:
            print(f"  [error] Fallo al descargar {category_url}: {exc}")
        time.sleep(REQUEST_DELAY_SECONDS)
    return all_products


# ---------------------------------------------------------------------------
# GUARDADO EN SUPABASE
# ---------------------------------------------------------------------------

def save_to_supabase(products):
    from supabase_client import get_client

    supabase = get_client()
    now = datetime.now(timezone.utc).isoformat()

    for product in products:
        reference = product["reference"]

        # 1) Asegurar que existe en `products` (tabla maestra). Si no
        #    existe, se crea con los campos "bonitos" vacíos.
        existing = (
            supabase.table("products")
            .select("reference")
            .eq("reference", reference)
            .execute()
        )
        if not existing.data:
            supabase.table("products").insert(
                {
                    "reference": reference,
                    "slot_brand": product["slot_brand"],
                    "has_reference": product["has_reference"],
                }
            ).execute()

        # 2) Guardar siempre una fila nueva en `listings` con el precio
        #    y estado de hoy.
        supabase.table("listings").insert(
            {
                "reference": reference,
                "store": STORE_NAME,
                "price": product["price"],
                "original_price": product["original_price"],
                "is_sale": bool(product["original_price"]),
                "in_stock": product["in_stock"],
                "product_url": product["product_url"],
                "image_url": product["image_url"],
                "last_checked_at": now,
            }
        ).execute()


# ---------------------------------------------------------------------------
# PUNTO DE ENTRADA
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print("Iniciando scraper de Evotec Shop...")
    products = scrape_all()
    print(f"\nTotal de productos encontrados: {len(products)}")

    if not products:
        print("No se ha encontrado ningún producto. Revisa los selectores antes de guardar nada.")
        sys.exit(1)

    print("Guardando en Supabase...")
    save_to_supabase(products)
    print("Listo.")
