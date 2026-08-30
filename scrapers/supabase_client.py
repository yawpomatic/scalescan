"""
Pequeña utilidad para conectar con Supabase desde los scrapers.

Lee las credenciales de variables de entorno (nunca las escribas
directamente en el código):
  SUPABASE_URL  -> la "Project URL" que viste en Supabase (Settings > API)
  SUPABASE_KEY  -> la "anon public key" (o una "service role key" si
                   prefieres saltarte las políticas de seguridad de fila,
                   más simple para empezar, ver nota más abajo)

En GitHub Actions estas variables se configuran como "Secrets" del
repositorio (Settings > Secrets and variables > Actions), NUNCA se
escriben en el código.
"""

import os

from supabase import create_client, Client


def get_client() -> Client:
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_KEY")

    if not url or not key:
        raise RuntimeError(
            "Faltan las variables de entorno SUPABASE_URL y/o SUPABASE_KEY. "
            "En GitHub Actions, comprueba que están definidas como Secrets "
            "del repositorio y referenciadas en el workflow (scrape.yml)."
        )

    return create_client(url, key)
