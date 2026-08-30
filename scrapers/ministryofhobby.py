"""
Scraper de Ministry of Hobby (ministryofhobby.com) para el Buscador de Slot.

Qué hace:
  1. Recorre únicamente las categorías de "Coches Slot" (nunca repuestos,
     taller, electrónica, decoración ni diecast).
  2. Por cada producto extrae: fabricante de la miniatura, referencia,
     nombre, precio, precio original (si hay oferta), si está en stock,
     la URL del producto y la imagen.
  3. Guarda cada referencia nueva en `products` (tabla maestra) y siempre
     una fila en `listings` con el precio/estado del día.

Formato de referencia en esta tienda (más fiable que el de otras, no hace
falta regex complejo):
    Fabricante: NombreMarca
    Ref: REFERENCIA

Bajo impacto en el servidor:
  - Pausa entre peticiones (ver REQUEST_DELAY_SECONDS).
  - Un único hilo, sin peticiones en paralelo.
  - Pensado para 1 vez al día vía GitHub Actions.
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

BASE_URL = "https://www.ministryofhobby.com"

# SOLO categorías de "Coches Slot" (menú del mismo nombre en la web).
# No incluir repuestos, taller, electrónica, decoración ni diecast.
CATEGORY_URLS = [
    "https://www.ministryofhobby.com/es/44-rally",
    "https://www.ministryofhobby.com/es/139-rally-clasicos",
    "https://www.ministryofhobby.com/es/140-endurance-classic",
    "https://www.ministryofhobby.com/es/141-le-mans-80-s-90-s",
    "https://www.ministryofhobby.com/es/50-le-mans-modernos",
    "https://www.ministryofhobby.com/es/51-gt-modernos",
    "https://www.ministryofhobby.com/es/125-f1",
    "https://www.ministryofhobby.com/es/183-turismos-deportivos-y-cine",
    "https://www.ministryofhobby.com/es/49-raid",
    "https://www.ministryofhobby.com/es/146-resinas-y-coleccionistas",
    "https://www.ministryofhobby.com/es/52-coches-1-24",
    "https://www.ministryofhobby.com/es/53-coches-1-43",
]

STORE_NAME = "Ministry of Hobby"

REQUEST_DELAY_SECONDS = 4
REQUEST_TIMEOUT_SECONDS = 20

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

SESSION = requests.Session()
SESSION.headers.update(HEADERS)

# ---------------------------------------------------------------------------
# EXTRACCIÓN
# ---------------------------------------------------------------------------

# "Fabricante: Scaleauto" y "Ref: SC-0014c" (a veces en líneas separadas)
BRAND_PATTERN = re.compile(r"Fabricante:?\s*([^\n]+)", re.IGNORECASE)
REF_PATTERN = re.compile(r"Ref:?\s*([A-Za-z0-9\-\._/]+)", re.IGNORECASE)
PRICE_PATTERN = re.compile(r"(\d{1,3}(?:[.,]\d{2}))\s*€")


def extract_reference_and_brand(text: str):
    brand_match = BRAND_PATTERN.search(text)
    ref_match = REF_PATTERN.search(text)
    brand = brand_match.group(1).strip(" .-") if brand_match else None
    ref = ref_match.group(1).strip() if ref_match else None
    return brand, ref


def extract_prices(text: str):
    prices = PRICE_PATTERN.findall(text)
    if not prices:
        return None, None
    prices = [float(p.replace(".", "").replace(",", ".")) for p in prices]
    if len(prices) == 1:
        return prices[0], None
    return min(prices), max(prices)


def is_out_of_stock(text: str) -> bool:
    lowered = text.lower()
    return "fuera de stock" in lowered or "agotado" in lowered or "bajo pedido" in lowered


def fetch_category_page(url: str, page: int = 1, _retried: bool = False):
    params = {"page": page} if page > 1 else {}
    response = SESSION.get(url, params=params, timeout=REQUEST_TIMEOUT_SECONDS)

    if response.status_code == 403 and not _retried:
        print("  [aviso] 403 recibido, esperando 15s y reintentando una vez...")
        time.sleep(15)
        return fetch_category_page(url, page, _retried=True)

    response.raise_for_status()
    return response.text


def parse_products_from_html(html: str, category_url: str):
    soup = BeautifulSoup(html, "html.parser")
    products = []

    candidates = soup.select("[data-id-product]") or soup.select(".product-miniature") or soup.select("article.product")

    if not candidates:
        print(f"  [aviso] No se encontraron bloques de producto reconocibles en {category_url}")
        return products

    for block in candidates:
        block_text = block.get_text(separator=" ", strip=True)

        title_tag = block.select_one(".product-title a") or block.select_one("h3 a") or block.select_one("a")
        name = title_tag.get_text(strip=True) if title_tag else None
        product_url = title_tag["href"] if title_tag and title_tag.has_attr("href") else None

        img_tag = block.select_one("img")
        image_url = None
        if img_tag:
            image_url = img_tag.get("src") or img_tag.get("data-src")

        brand, reference = extract_reference_and_brand(block_text)
        price, original_price = extract_prices(block_text)
        in_stock = not is_out_of_stock(block_text)

        if not name:
            continue  # bloque sin nombre reconocible, lo saltamos

        if not reference:
            reference = f"SIN-REF-{abs(hash(name)) % 100000}"

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
                "has_reference": bool(brand and not reference.startswith("SIN-REF-")),
            }
        )

    return products


def scrape_category(category_url: str):
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
    print("Iniciando scraper de Ministry of Hobby...")
    products = scrape_all()
    print(f"\nTotal de productos encontrados: {len(products)}")

    if not products:
        print("No se ha encontrado ningún producto. Revisa los selectores antes de guardar nada.")
        sys.exit(1)

    print("Guardando en Supabase...")
    save_to_supabase(products)
    print("Listo.")
