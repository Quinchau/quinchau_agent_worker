import os
import sys
import unicodedata
import pymysql
from openai import OpenAI
from qdrant_client import QdrantClient
from rapidfuzz import fuzz

# ─────────────────────────────────────────
# CONFIG (debe coincidir con reindex_multi_term.py)
# ─────────────────────────────────────────
COLLECTION      = 'quinchau_productos'
TOP_K_VECTOR    = 30   # traemos más candidatos porque después filtramos por modelo
TOP_K_SHOW      = 10
MAX_ALIAS_WORDS = 3     # ventana deslizante máxima (ajustar si hay alias más largos)
FUZZY_THRESHOLD = 85    # umbral de aceptación fuzzy (0-100)
FUZZY_MIN_LEN   = 5     # no aplicar fuzzy a alias/queries muy cortos (evita falsos positivos tipo "par")


def normalizar_texto(texto: str) -> str:
    if not texto:
        return ""
    texto = texto.lower()
    texto = unicodedata.normalize('NFKD', texto).encode('ASCII', 'ignore').decode('utf-8')
    return texto


def colapsar(texto: str) -> str:
    """Quita espacios: 'spor 125' -> 'spor125'"""
    return texto.replace(" ", "")


# ─────────────────────────────────────────
# CARGA DE ALIAS DESDE MYSQL
# ─────────────────────────────────────────
def cargar_alias(conn):
    cursor = conn.cursor()
    cursor.execute("""
        SELECT a.alias, a.id_termino, t.termino
        FROM terminos_alias a
        JOIN terminos_semanticos t ON t.id = a.id_termino
    """)
    rows = cursor.fetchall()

    alias_map = []  # lista de dicts: {alias_norm, alias_colapsado, id_termino, termino}
    for r in rows:
        alias_norm = normalizar_texto(r['alias'])
        alias_map.append({
            'alias_norm': alias_norm,
            'alias_colapsado': colapsar(alias_norm),
            'n_palabras': len(alias_norm.split()),
            'id_termino': r['id_termino'],
            'termino': r['termino'],
        })

    # ordenar por cantidad de palabras desc -> priorizamos matches más específicos
    alias_map.sort(key=lambda x: -x['n_palabras'])
    return alias_map


# ─────────────────────────────────────────
# GENERAR VENTANAS DESLIZANTES DE LA QUERY
# ─────────────────────────────────────────
def generar_ventanas(query_norm: str, max_palabras: int):
    palabras = query_norm.split()
    ventanas = []
    n = len(palabras)
    for size in range(1, max_palabras + 1):
        for i in range(0, n - size + 1):
            ventana = " ".join(palabras[i:i + size])
            ventanas.append(ventana)
    # más largas primero (más específicas)
    ventanas.sort(key=lambda v: -len(v.split()))
    return ventanas


# ─────────────────────────────────────────
# RESOLVER ALIAS -> TERMINO CANONICO
# ─────────────────────────────────────────
def resolver_termino(query_norm: str, alias_map):
    ventanas = generar_ventanas(query_norm, MAX_ALIAS_WORDS)

    # ── CAPA 1: match exacto (word-boundary, vía comparación de tokens) ──
    for ventana in ventanas:
        for a in alias_map:
            if ventana == a['alias_norm'] or colapsar(ventana) == a['alias_colapsado']:
                return {
                    'metodo': 'exacto',
                    'ventana': ventana,
                    'alias': a['alias_norm'],
                    'id_termino': a['id_termino'],
                    'termino': a['termino'],
                    'score': 100,
                }

    # ── CAPA 2: fuzzy (typos, espacios movidos, orden distinto) ──
    mejor = None
    for ventana in ventanas:
        if len(ventana) < FUZZY_MIN_LEN:
            continue
        for a in alias_map:
            if len(a['alias_norm']) < FUZZY_MIN_LEN:
                continue

            r1 = fuzz.ratio(ventana, a['alias_norm'])
            r2 = fuzz.ratio(colapsar(ventana), a['alias_colapsado'])
            r3 = fuzz.token_sort_ratio(ventana, a['alias_norm'])
            score = max(r1, r2, r3)

            if score >= FUZZY_THRESHOLD and (mejor is None or score > mejor['score']):
                mejor = {
                    'metodo': 'fuzzy',
                    'ventana': ventana,
                    'alias': a['alias_norm'],
                    'id_termino': a['id_termino'],
                    'termino': a['termino'],
                    'score': score,
                }

    return mejor


# ─────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────
def main():
    query_raw = " ".join(sys.argv[1:]) if len(sys.argv) > 1 else "Mandos Spor125"
    query_norm = normalizar_texto(query_raw)

    print("=" * 60)
    print(f"Query original   : {query_raw}")
    print(f"Query normalizada: {query_norm}")
    print("=" * 60)

    conn = pymysql.connect(
        host='db', port=3306,
        user='tum12607_webmas2', password='6060',
        database='tum12607_maracay',
        cursorclass=pymysql.cursors.DictCursor
    )
    alias_map = cargar_alias(conn)
    conn.close()
    print(f"📋 Alias cargados: {len(alias_map)}")

    match = resolver_termino(query_norm, alias_map)
    if match:
        print(f"\n✅ Alias resuelto ({match['metodo']}, score={match['score']}):")
        print(f"   ventana query : '{match['ventana']}'")
        print(f"   alias matched : '{match['alias']}'")
        print(f"   id_termino    : {match['id_termino']}")
        print(f"   termino       : '{match['termino']}'")
    else:
        print("\n⚠️  No se encontró alias/término. Búsqueda sin filtro de modelo.")

    # ── Búsqueda vectorial ──
    client = OpenAI(
        base_url="https://openrouter.ai/api/v1",
        api_key=os.getenv("OPENROUTER_API_KEY")
    )
    qdrant = QdrantClient(host='qdrant', port=6333)

    emb = client.embeddings.create(
        model="openai/text-embedding-3-small",
        input=[query_norm]
    ).data[0].embedding

    response = qdrant.query_points(
        collection_name=COLLECTION,
        query=emb,
        limit=TOP_K_VECTOR,
        with_payload=True
    )
    results = response.points

    # ── Filtrar por término canónico si hubo match ──
    if match:
        termino_norm = normalizar_texto(match['termino'])
        filtrados = [
            r for r in results
            if termino_norm in normalizar_texto((r.payload or {}).get('modelos', ''))
        ]
        if filtrados:
            print(f"\n🎯 Filtrado por modelo '{match['termino']}': {len(filtrados)} producto(s) de {len(results)} candidatos")
            results = filtrados
        else:
            print(f"\n⚠️  El filtro por '{match['termino']}' no dejó resultados. Fallback a vector search sin filtrar.")

    print(f"\nTop {min(TOP_K_SHOW, len(results))} resultados finales:\n")
    for i, r in enumerate(results[:TOP_K_SHOW], 1):
        payload = r.payload or {}
        print(f"{i:2d}. score={r.score:.6f}  stockid={payload.get('stockid_original')}")
        print(f"     original : {payload.get('description_original')}")
        print(f"     modelos  : {(payload.get('modelos') or '')[:120]}")
        print()


if __name__ == "__main__":
    main()