#!/usr/bin/env python3
"""cfcore — motore di ricerca fuzzy per una libreria Calibre.

Logica condivisa fra `calibrefind.py` (TUI Textual) e `calibrefind_flet.py`
(GUI Flet): normalizzazione delle stringhe, cache compatta dei libri,
scansione fuzzy per campo (forkata su più processi), fusione delle copie
della stessa opera e indice dei tag.

Qui non c'è nulla che riguardi la UI: il modulo può essere importato e usato
headless.  Le note di prestazione e le scelte non ovvie sono documentate
accanto al codice che le implementa (vedi anche `load_books`, `scan_range`,
`dedup_hits`).
"""

import os
import pickle
import re
import sqlite3
import time
import unicodedata
import warnings
import zlib
import html as _html
from array import array
from difflib import SequenceMatcher

# rapidfuzz (C++) se disponibile: stesso algoritmo indel di difflib, ~100x piu veloce
try:
    from rapidfuzz import fuzz as _fuzz
    def _ratio(a: str, b: str) -> float:
        return _fuzz.ratio(a, b)
except ImportError:
    def _ratio(a: str, b: str) -> float:
        return SequenceMatcher(None, a, b).ratio() * 100

# ------------------------------------------------------------ normalizzare ---

# tabella di translitterazione accentata→base, precomputata una volta sola:
# rende normalize() ~20x piu veloce di un NFKD fatto carattere per carattere
_ACCENTS = None

def _accent_table() -> dict:
    global _ACCENTS
    if _ACCENTS is None:
        m = {}
        for cp in range(0x3000):                    # latino/greco/cirillico/…
            _accent_add(m, cp)
        for cp in range(0xFB00, 0x10000):           # legature e forme compatte
            _accent_add(m, cp)
        _ACCENTS = m
    return _ACCENTS

def _accent_add(m: dict, cp: int) -> None:
    ch = chr(cp)
    d = unicodedata.normalize("NFKD", ch)
    if len(d) > 1:
        base = "".join(c for c in d if not unicodedata.combining(c))
        if base != ch:
            m[cp] = base

def html_to_text(h: str) -> str:
    """toglie tag HTML e decodifica le entita: descrizione pulita."""
    h = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", h)
    h = re.sub(r"(?i)<br\s*/?>", "\n", h)
    h = re.sub(r"(?i)</(p|div|li|h[1-6])>", "\n", h)
    h = re.sub(r"<[^>]+>", "", h)
    h = _html.unescape(h)
    h = re.sub(r"[ \t]+", " ", h)
    h = "\n".join(line.strip() for line in h.splitlines() if line.strip())
    return h.strip()

_WORD_RE = re.compile(r"[a-z0-9]+")

def normalize(s: str) -> str:
    """minuscolo, niente accenti/diacritici, solo parole alfanumeriche."""
    if not s:
        return ""
    s = s.lower().replace("&", " and ").translate(_accent_table())
    return " ".join(_WORD_RE.findall(s))

def trigrams(s: str):
    s = s.replace(" ", "")
    if len(s) < 3:
        return {s} if s else set()
    return {s[i:i + 3] for i in range(len(s) - 2)}

def query_terms(q: str):
    """termini precomputati della query: (normalizzata, senza spazi,
    trigrammi di 3 lettere, token piu corti di 3 lettere)."""
    n = normalize(q)
    nsl = n.replace(" ", "")
    qtri = tuple(t for t in trigrams(nsl) if len(t) == 3)
    qshort = tuple(tok for tok in n.split() if len(tok) < 3)
    return n, nsl, qtri, qshort

# --------------------------------------------------------------------- db ---

QUERY = """
SELECT b.id, b.title, b.sort, b.pubdate, b.series_index, b.path,
       COALESCE(s.name,'') AS series,
       COALESCE(rt.rating,0) AS rating,
       (SELECT GROUP_CONCAT(a.name, ' & ') FROM books_authors_link l
        JOIN authors a ON a.id = l.author WHERE l.book = b.id) AS authors,
       (SELECT GROUP_CONCAT(DISTINCT f.format) FROM data f WHERE f.book = b.id) AS formats,
       (SELECT GROUP_CONCAT(t.name) FROM books_tags_link lt
        JOIN tags t ON t.id = lt.tag WHERE lt.book = b.id) AS tags,
       cm.text AS comments
FROM books b
LEFT JOIN comments cm ON cm.book = b.id
LEFT JOIN books_series_link sl ON sl.book = b.id
LEFT JOIN series s ON s.id = sl.series
LEFT JOIN books_ratings_link brl ON brl.book = b.id
LEFT JOIN ratings rt ON rt.id = brl.rating
"""

CACHE_VERSION = 6   # cambia per invalidare cache vecchie

SCAN_FLOOR = 20.0   # sotto questa soglia i punteggi in cache non bastano
POOL_MIN_BOOKS = 2000
PAGE_SIZE = 30      # risultati disegnati per pagina (la lista si allunga scorrendo)
LOOKAHEAD = 8       # quanti risultati prima della fine caricare la pagina dopo

def build_columns(books):
    """Colonne di scansione: per ogni campo (_t titolo, _a autore, _d
    descrizione) le stringhe normalizzate di tutti i libri concatenate in un
   'unica grande stringa — con e senza spazi — piu gli offset. I processi
    forkati le condividono in sola lettura senza duplicarle."""
    cols = {}
    for fld in ("_t", "_a", "_d"):
        big, bigs = [], []
        off, offs = array("I", [0]), array("I", [0])
        o1 = o2 = 0
        for b in books:
            n = b[fld]
            big.append(n)
            o1 += len(n)
            off.append(o1)
            nsl = n.replace(" ", "")
            bigs.append(nsl)
            o2 += len(nsl)
            offs.append(o2)
        cols[fld] = ("".join(big), "".join(bigs), off, offs)
    return cols

def load_books(db_path: str, force: bool = False):
    """Carica i libri dal db. Ritorna (books, cols).

    Cache pickle+zlib a fianco del db, invalidata dal mtime e dalla
    versione: ~20 MB, si carica in ~1s; la prima volta indicizza tutti i
    libri dal db (~15s). force=True ricostruisce e riscrive la cache.

    I libri in cache NON contengono il testo leggibile delle descrizioni
    (viene letto dal db al volo quando serve il dettaglio)."""
    cache = db_path + ".cache.pkl"
    try:
        mtime = os.path.getmtime(db_path)
    except OSError:
        mtime = None
    use_cache = mtime is not None and not force

    payload = None
    if use_cache and os.path.exists(cache):
        try:
            with open(cache, "rb") as f:
                blob = f.read()
            cached_mtime, cached_ver, payload = pickle.loads(zlib.decompress(blob))
            if cached_mtime != mtime or cached_ver != CACHE_VERSION:
                payload = None
        except Exception:
            payload = None

    if payload is None:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        try:
            rows = con.execute(QUERY).fetchall()
        finally:
            con.close()
        books = []
        for r in rows:
            title = r["title"] or ""
            author = r["authors"] or "???"
            books.append({
                "id":     r["id"],
                "title":  title,
                "author": author,
                "series": (r["series"] + f" [{r['series_index']:g}]") if r["series"] else "",
                "rating": r["rating"] or 0,
                "formats": (r["formats"] or "").split(",") if r["formats"] else [],
                "tags":   (r["tags"] or ""),
                "pubdate": (r["pubdate"] or "")[:10],
                "path":   r["path"] or "",
                "desc":   "",
                "_t": normalize(title),
                "_a": normalize(author),
                "_d": normalize(html_to_text(r["comments"])) if r["comments"] else "",
            })
        cols = build_columns(books)
        for b in books:               # _d vive solo nelle colonne
            b.pop("_d", None)
        payload = (books, cols)
        if mtime is not None:
            try:
                blob = zlib.compress(
                    pickle.dumps((mtime, CACHE_VERSION, payload), -1), 1)
                with open(cache, "wb") as f:
                    f.write(blob)
            except Exception:
                pass
        else:                         # niente cache possibile: ripulisci le vecchie
            try:
                os.unlink(cache)
            except OSError:
                pass
    return payload

# --------------------------------------------------------------- scansione --

def scan_range(q: str, mode: str, floor: float, lo: int, hi: int,
               cols: dict, cancelled=None):
    """Punteggio esatto (formula originale: trigrammi + similarita + substring)
    per i libri [lo, hi) delle colonne. Ritorna [(score, indice)] — o None se
    annullato.

    AND per parola: con più di un termine la query è un insieme di parole che
    devono essere TUTTE presenti nella stringa normalizzata del campo (le
    parole di 1-2 lettere come parola intera, le altre come sottostringa);
    i libri che ne mancano una vengono scartati prima del calcolo fuzzy. Con
    un solo termine resta la ricerca fuzzy, tollerante ai refusi.

    floor: le voci il cui punteggio PUO al massimo raggiungere un valore
    sotto questa soglia vengono scartate senza calcolare la similarita
    (il loro punteggio vero sarebbe comunque < floor ≤ soglia mostrata).
    La similarita (termine costoso) si calcola solo se serve davvero."""
    if not q:
        return []
    qn, qnsl, qtri, qshort = query_terms(q)
    terms = qn.split()
    and_mode = len(terms) > 1
    must = tuple(t for t in terms if len(t) >= 3)
    if and_mode:
        # trigrammi per parola, non a cavallo: verificate tutte le parole la
        # copertura e' piena e il punteggio riflette il match reale
        qtri = tuple(dict.fromkeys(
            tg for t in terms if len(t) >= 3 for tg in trigrams(t)))
    ntok = len(qtri) + len(qshort)
    if not ntok:
        return []
    bigN, bigNS, offN, offNS = cols[{"author": "_a", "title": "_t", "desc": "_d"}[mode]]
    wspat = tuple(f" {t} " for t in qshort)
    hits = []
    append = hits.append
    qlen = len(qn)
    check = 4096
    for i in range(lo, hi):
        if cancelled is not None and not check:
            if cancelled():
                return None
            check = 4096
        check -= 1
        o1 = offN[i]
        o2 = offN[i + 1]
        if o1 == o2:
            continue
        n = bigN[o1:o2]
        if qn == n:
            append((100.0, i))
            continue
        padded = None
        if and_mode:
            if any(t not in n for t in must):
                continue
            if wspat:
                padded = f" {n} "
                if any(w not in padded for w in wspat):
                    continue
        h = 0
        if qtri:
            ns = bigNS[offNS[i]:offNS[i + 1]]
            for t in qtri:
                if t in ns:
                    h += 1
        e = 0
        for w in wspat:
            if w in (padded if padded is not None else f" {n} "):
                e += 1
        tri = 100.0 * (h + e) / ntok
        nl = o2 - o1
        bound = 200.0 * min(qlen, nl) / (qlen + nl)
        base = 0.45 * tri + 0.35 * bound
        if base + 8.0 < floor:        # +8: contributo massimo della substring
            continue
        sub = 40.0 if qn in n else 0.0
        if tri == 0 and sub == 0:
            continue
        if base + 0.20 * sub < floor:
            continue
        sim = _ratio(qn, n)
        s = 0.45 * tri + 0.35 * sim + 0.20 * sub
        if s > 100.0:
            s = 100.0
        if s > 0.0:
            append((s, i))
    return hits

# ------------------------------------------------- pool di scansioni fork ---

_MP_COLS = None    # colonne condivise coi processi forkati (sola lettura)

def _mp_scan_job(q, mode, floor, lo, hi):
    return scan_range(q, mode, floor, lo, hi, _MP_COLS)

def pool_scan(pool, q: str, mode: str, floor: float, n_books: int,
              cancelled=None):
    """scansione sui processi forkati: un blocco di libri per worker.

    Ritorna la lista (score, indice), o None se annullata.

    starmap_async + attesa pollata: uno starmap bloccante resterebbe appeso
    per sempre se il pool viene chiuso sotto (es. quit durante la scansione),
    e il join dell'executor alla chiusura si pianta."""
    n = n_books
    nproc = getattr(pool, "_processes", 1) or 1
    nchunks = nproc * 3                # piu blocchi che processi: bilancia
    step = -(-n // nchunks)
    jobs = []
    for k in range(nchunks):
        lo = k * step
        hi = min(n, lo + step)
        if lo < hi:
            jobs.append((q, mode, floor, lo, hi))
    ar = pool.starmap_async(_mp_scan_job, jobs)
    t0 = time.monotonic()
    while not ar.ready():
        if cancelled is not None and cancelled():
            return None
        if time.monotonic() - t0 > 30:  # pool morto: ripiega su in-process
            raise TimeoutError("scansione pool senza risposta")
        ar.wait(0.05)
    hits = []
    for part in ar.get():
        hits.extend(part)
    return hits

def make_pool(max_processes: int = 6):
    """Pool di processi fork (None se non disponibile/conveniente).

    Va chiamato DOPO aver impostato `cfcore._MP_COLS`: i worker ereditano le
    colonne al fork e le condividono in sola lettura."""
    try:
        nproc = min(getattr(os, "process_cpu_count", os.cpu_count)() or 1,
                    max_processes)
        if nproc < 2:
            return None
        import multiprocessing
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            ctx = multiprocessing.get_context("fork")
            return ctx.Pool(nproc)
    except Exception:
        return None

# --------------------------------------------------------------- dedup ------

def fuzzy_title_eq(a: str, b: str) -> bool:
    """titoli normalizzati 'uguali' in modo fuzzy: identici, quasi identici
    (ratio alto) oppure uno contenuto nell'altro (es. titolo con prefisso
    serie/sottotitolo)."""
    if a == b:
        return True
    if not a or not b:
        return False
    r = _ratio(a, b)
    if r >= 85:
        return True
    return (a in b or b in a) and r >= 70

def fuzzy_author_eq(a: str, b: str) -> bool:
    """autori normalizzati 'uguali': match in ordine dei token del più corto
    nel più lungo, dove ogni token può essere uguale, un prefisso o una
    semplice iniziale. Cosi 'm polidoro' ≡ 'massimo polidoro'."""
    if a == b:
        return True
    ta, tb = a.split(), b.split()
    if not ta or not tb:
        return False
    short, long_ = (ta, tb) if len(ta) <= len(tb) else (tb, ta)
    it = iter(long_)
    for tok in short:
        for other in it:
            if other == tok or other.startswith(tok) or tok.startswith(other):
                break
        else:
            return False
    return True

_EXACT_DEDUP = 160
"""Fino a questo numero di schede il raggruppamento confronta tutto (stessa
semantica di sempre); oltre, usa un indice di prefissi per non pagare un
confronto fuzzy con ogni scheda già vista."""

def _dedup_key(title: str, author: str) -> str:
    """chiave approssimativa (inizio del titolo + cognome dell'autore) per
    trovare al volo le schede candidate al raggruppamento. Due copie della
    stessa opera condividono quasi sempre l'inizio del titolo e il cognome;
    la somiglianza vera viene poi confermata dal confronto fuzzy."""
    atoks = author.split()
    return title.replace(" ", "")[:4] + "\x00" + (atoks[-1][:4] if atoks else "")

def dedup_hits(hits, books):
    """Una sola scheda per opera duplicata fra i risultati, che arrivano già
    ordinati per punteggio decrescente come [(score, gate, indice)].
    Titolo e autore confrontati in modo fuzzy; i formati di tutte le copie
    vengono fusi (es. EPUB(2)) e 'copies' dice quante copie sono state fuse.
    'gate' è il punteggio del campo peggiore della copia: serve al filtro
    della soglia, che deve considerare la copia migliore del gruppo."""
    reps = []
    buckets = {}
    for score, gate, bi in hits:
        b = books[bi]
        t, a = b["_t"], b["_a"]
        key = _dedup_key(t, a)
        if len(reps) <= _EXACT_DEDUP:
            candidates = range(len(reps))
        else:
            candidates = buckets.get(key, ())
        rep = None
        for ci in candidates:
            cand = reps[ci]
            if fuzzy_title_eq(t, cand["_t"]) and fuzzy_author_eq(a, cand["_a"]):
                rep = cand
                break
        if rep is None:
            rep = dict(b, score=score, _gate=gate, _formats=list(b["formats"]))
            rep["copies"] = 1
            reps.append(rep)
            buckets.setdefault(key, []).append(len(reps) - 1)
        else:
            rep["copies"] += 1
            rep["_formats"].extend(b["formats"])
            if gate > rep["_gate"]:
                rep["_gate"] = gate

    for rep in reps:
        counts = {}
        for f in rep.pop("_formats"):
            fu = f.strip().upper()
            if fu:
                counts[fu] = counts.get(fu, 0) + 1
        rep["formats"] = [f"{fu}({n})" if n > 1 else fu
                          for fu, n in sorted(counts.items())]
    return reps

# ---------------------------------------------------------------- tag -------

def build_tag_index(books):
    """mappa tag -> indici dei libri che lo hanno, piu le opzioni della
    finestra di scelta (etichetta con conteggio, valore = nome del tag).

    In Calibre i tag si inseriscono separati da virgole, quindi la virgola
    non puo far parte di un nome: lo split della colonna 'tags' e' esatto."""
    index = {}
    for i, b in enumerate(books):
        seen = set()
        for name in (b.get("tags") or "").split(","):
            name = name.strip()
            if name and name not in seen:
                seen.add(name)
                index.setdefault(name, []).append(i)
    names = sorted(index, key=str.casefold)
    options = [(f"{name}  ({len(index[name])})", name) for name in names]
    return index, options

# ------------------------------------------------------------ punteggi ------

def score_hex(s: float) -> str:
    if s >= 90: return "#5fff87"
    if s >= 75: return "#87d787"
    if s >= 60: return "#d7d75f"
    if s >= 45: return "#ffaf5f"
    if s >= 30: return "#ff875f"
    return "#808080"

def rating_stars(r: int) -> str:
    if not r:
        return ""
    stars = round(r / 2)          # calibre: rating 0..10 (punti da 2)
    return "★" * stars + "☆" * (5 - stars)

# ------------------------------------------------------- ricerca combinata --

def scan_field(books, cols, q: str, mode: str, floor: float,
               cancelled=None, pool=None):
    """scansione di un campo su tutta la libreria (pool se disponibile)."""
    if (pool is not None and len(books) >= POOL_MIN_BOOKS
            and not getattr(pool, "_cf_broken", False)):
        try:
            got = pool_scan(pool, q, mode, floor, len(books), cancelled)
            if got is None:                # annullato
                return None
            return got
        except Exception:                  # pool rotto: si continua in-process
            try:
                pool._cf_broken = True
            except Exception:
                pass
    if cancelled is not None and cancelled():
        return None
    return scan_range(q, mode, floor, 0, len(books), cols, cancelled=cancelled)

def search(books, cols, tag_index, key, floor: float,
           cancelled=None, pool=None):
    """Risultati pronti da mostrare: (schede, numero di libri prima della
    fusione, elapsed_ms) — oppure None se annullata.

    key = (query titolo, query autore, query descrizione, tuple di tag).
    Ogni campo di testo si scansiona a parte; un libro entra solo se è
    presente in ogni scansione (e nei tag scelti). 'score' è la media dei
    campi attivi, '_gate' il peggiore: la soglia filtra sul gate, così
    alzarla può solo togliere libri, coerentemente con la cache.

    Ordina per punteggio (poi titolo), e fonde le copie della stessa opera
    tranne quando c'è un filtro per tag (in quel caso un risultato per libro,
    così il totale coincide col conteggio mostrato nel selettore dei tag)."""
    t0 = time.perf_counter()
    q_title, q_author, q_desc, tags = key
    fields = [(mode, q) for mode, q in
              (("title", q_title), ("author", q_author), ("desc", q_desc))
              if q]
    maps = []
    for mode, q in fields:
        got = scan_field(books, cols, q, mode, floor, cancelled, pool)
        if got is None:
            return None
        maps.append({i: s for s, i in got})

    allowed = None
    empty_tags = False
    if tags:
        for tag in tags:
            ids = set(tag_index.get(tag, ()))
            allowed = ids if allowed is None else (allowed & ids)
            if not allowed:            # intersezione vuota: inutile
                empty_tags = True
                break

    if empty_tags or (not fields and not tags):
        hits = []
    elif not fields:                   # solo tag: filtro esatto, ordine per titolo
        hits = [(100.0, 100.0, i) for i in allowed]
    else:
        hits = _intersect(books, maps, allowed)

    # ordine stabile: punteggio, poi titolo (a parità di punteggio)
    hits.sort(key=lambda h: (-h[0], books[h[2]]["_t"]))
    if cancelled is not None and cancelled():
        return None
    if tags:
        # filtro per tag: nessuna fusione delle copie, un risultato per
        # libro, così il totale coincide col conteggio del tag
        clustered = [dict(books[bi], score=score, _gate=gate)
                     for score, gate, bi in hits]
    else:
        clustered = dedup_hits(hits, books)
    elapsed = (time.perf_counter() - t0) * 1000
    return clustered, len(hits), elapsed

def _intersect(books, maps, allowed):
    """[(media, gate, indice)] dei libri presenti in OGNI scansione,
    eventualmente limitati all'insieme `allowed` (tag)."""
    base = min(maps, key=len)
    nf = len(maps)
    out = []
    for i in base:
        if allowed is not None and i not in allowed:
            continue
        vals = []
        for m in maps:
            s = m.get(i)
            if s is None:              # manca un campo: non soddisfa tutti
                vals = None
                break
            vals.append(s)
        if vals is None:
            continue
        out.append((sum(vals) / nf, min(vals), i))
    return out
