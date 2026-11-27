#!/usr/bin/env python3
"""
calibrefind — ricerca fuzzy combinabile su una libreria Calibre, con TUI
(rich + textual).

La logica di ricerca (normalizzazione, cache, scansione fuzzy, dedup delle
copie, tag) vive in `cfcore.py` ed è condivisa con la GUI Flet
(`calibrefind_flet.py`): le due interfacce mostrano esattamente gli stessi
risultati con gli stessi punteggi.

Uso:
    ./calibrefind.py                       # TUI completa
    ./calibrefind.py -a "Gibson"           # query autore precompilata
    ./calibrefind.py -t "neuromancer"      # query titolo precompilata
    ./calibrefind.py -a "gibson" -t "mancer"   # criteri combinati
    ./calibrefind.py -d "cyberspazio"      # ricerca dentro le descrizioni
    ./calibrefind.py -g Fantascienza -g Cyberpunk   # libri con TUTTI i tag
    ./calibrefind.py -l /percorso/altro/metadata.db
    ./calibrefind.py -n 100                # 100 risultati per pagina

I quattro criteri (titolo, autore, descrizione, tag) sono indipendenti e si
combinano liberamente: i libri mostrati soddisfano TUTTI i criteri impostati.
Anche dentro un campo le parole della query si combinano in AND: un libro
entra solo se la stringa del campo contiene tutte le parole (sottostringa;
quelle di 1-2 lettere come parola intera). Con una sola parola la ricerca
resta fuzzy, tollerante ai refusi. I tag sono un filtro esatto per
sottoinsieme (un libro deve avere l'intera selezione). I risultati non hanno
un limite fisso: la lista si allunga a pagine mentre la si scorre.

Tasti principali (vedi footer):
    ctrl+a / ctrl+t / ctrl+d / ctrl+e
                     vai al campo autore / titolo / descrizione / tag
    invio  vai ai risultati
    d                mostra/nasconde il dettaglio    o      apre la cartella del libro
    s                focus sullo slider della soglia (frecce = ±1, home/end = 0/100)
    plus / minus     alza/abbassa la soglia score    ctrl+r ricarica la libreria
    q                esci

Lo slider in alto si trascina col mouse (clic o drag) per regolare in tempo
reale la tolleranza: alzandola restano solo i match più stretti, abbassandola
rientrano quelli vaghi. Con più campi attivi un libro entra solo se OGNI campo
raggiunge la soglia; il punteggio mostrato è la media dei campi di testo.

Note di prestazione (su Raspberry Pi, libreria da ~25k libri):
  - cache compatta (~20 MB, pickle+zlib) a fianco del metadata.db: si carica
    in ~1s. Il primo avvio indicizza e comprime tutto (~15s).
  - le descrizioni leggibili non sono in cache: vengono lette dal db (una
    riga) quando si apre il dettaglio di un libro.
  - la scansione gira su processi separati (fork) che condividono le stringhe
    in sola lettura: su 4 core una ricerca sulla descrizione di tutta la
    libreria costa ~0.3s invece di oltre 1s. Senza fork funziona uguale,
    solo più lenta. Con più campi attivi si fa una scansione per campo.
  - i punteggi dell'intera libreria per una query vengono calcolati una
    volta e tenuti in cache: muovere la soglia riusa i punteggi e filtra
    subito, senza riscansare nulla (sotto soglia 20 c'è una nuova scansione).
  - le copie della stessa opera vengono fuse una volta sola, in background;
    la lista disegna solo le pagine che servono, non tutti i risultati.
  - con un filtro per tag le copie NON vengono fuse: i tag sono un filtro
    esatto e il numero di risultati coincide col conteggio mostrato nella
    finestra di scelta dei tag.
"""

import argparse
import os
import sqlite3
import subprocess
import sys
import time

import cfcore
from cfcore import (LOOKAHEAD, PAGE_SIZE, POOL_MIN_BOOKS, SCAN_FLOOR,
                    build_tag_index, html_to_text, load_books, rating_stars,
                    score_hex, search)

# se textual non è nel python di sistema, rilancia con il venv del progetto
try:
    import textual  # noqa: F401
except ImportError:
    _here = os.path.dirname(os.path.abspath(__file__))
    _venv_py = os.path.join(_here, ".venv", "bin", "python")
    if os.path.exists(_venv_py):
        os.execv(_venv_py, [_venv_py, os.path.abspath(__file__), *sys.argv[1:]])
    sys.exit("Manca 'textual':  uv venv .venv && uv pip install --python .venv/bin/python textual")

from rich.markup import escape
from rich.panel import Panel
from rich.table import Table as RTable
from rich.text import Text as RText
from rich.console import Group
from textual import on, work
from textual import events
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, ScrollableContainer, Vertical
from textual.css.query import NoMatches
from textual.message import Message
from textual.screen import ModalScreen
from textual.worker import get_current_worker
from textual.widgets import (Footer, Header, Input, Label, ListItem, ListView,
                             SelectionList, Static)

# ------------------------------------------------------------ rendering -----

def score_bar(s: float, width: int = 20) -> str:
    filled = round(s / 100 * width)
    return "▰" * filled + "▱" * (width - filled)

def card_markup(i: int, r: dict) -> str:
    """markup della scheda-resultato dentro la lista."""
    col = score_hex(r["score"])
    lines = [f"[bold #b4befe]{i + 1:>2})[/] [dim]#{r['id']:<6}[/]"
             f"[dim #585b70]{'─' * 14}[/] [bold {col}]{r['score']:5.1f}%[/]",
             f"[bold #f9e2af]{escape(r['title'])}[/]",
             f"[#f5c2e7]{escape(r['author'])}[/]"]
    if r["series"]:
        lines.append(f"[dim]serie[/] [#89dceb]{escape(r['series'])}[/]")
    meta = []
    if r["formats"]:
        meta.append(f"[#a6e3a1]{escape(', '.join(r['formats']))}[/]")
    if r["rating"]:
        meta.append(f"[#f9e2af]{rating_stars(r['rating'])}[/]")
    if r["pubdate"]:
        meta.append(f"[dim]{r['pubdate']}[/]")
    if meta:
        lines.append("  ".join(meta))
    if r["tags"]:
        lines.append(f"[dim #6c7086]{escape(r['tags'])}[/]")
    return "\n".join(lines)

def detail_renderable(r: dict, lib_name: str):
    """pannello di dettaglio (renderable rich) per il libro selezionato."""
    col = score_hex(r["score"])
    t = RTable.grid(padding=(0, 1))
    t.add_column(justify="right", style="dim", no_wrap=True)
    t.add_column()
    t.add_row("titolo", f"[bold #f9e2af]{escape(r['title'])}[/]")
    t.add_row("autore", f"[#f5c2e7]{escape(r['author'])}[/]")
    if r["series"]:
        t.add_row("serie", f"[#89dceb]{escape(r['series'])}[/]")
    if r["formats"]:
        t.add_row("formati", f"[#a6e3a1]{escape(', '.join(r['formats']))}[/]")
    if r["pubdate"]:
        t.add_row("pubblicazione", f"[#89b4fa]{r['pubdate']}[/]")
    if r["rating"]:
        t.add_row("rating", f"[#f9e2af]{rating_stars(r['rating'])}[/] ({r['rating']}/10)")
    if r["tags"]:
        t.add_row("tags", f"[dim]{escape(r['tags'])}[/]")
    if r["path"]:
        t.add_row("cartella", f"[dim]{escape(r['path'])}/[/]")

    ncopies = r.get("copies", 1)
    head = Group(
        RText(f"  {score_bar(r['score'])}  ", style=col),
        RText(f"{r['score']:5.1f}%", style=f"bold {col}"),
        RText(f"   su {ncopies} copie fuse" if ncopies > 1 else "", style="dim"),
        RText(),
        t,
    )
    parts = [head]
    if r["desc"]:
        parts += [RText("\n─ descrizione ", style="dim"),
                  RText("─" * 60, style="dim #45475a"),
                  RText(r["desc"])]
    else:
        parts += [RText("\n(nessuna descrizione)", style="dim italic")]
    parts += [RText(f"\nlibreria: {os.path.basename(lib_name)}", style="dim")]
    return Panel(Group(*parts), title=f"#{r['id']}", border_style=col,
                 title_align="left", padding=(0, 1))

# ----------------------------------------------------------------- app ------

class ResultItem(ListItem, can_focus=False):
    def __init__(self, r: dict, idx: int):
        super().__init__(Static(card_markup(idx, r), markup=True, classes="card"))
        self.result = r

class ResultsList(ListView):
    """ListView che chiede la pagina successiva quando si arriva in fondo.

    La richiesta parte sia dalla tastiera (la selezione si avvicina alla fine)
    sia dal mouse/scrollbar (la vista arriva in fondo)."""

    class NeedMore(Message):
        """Servono altri risultati: la pagina successiva non è ancora disegnata."""

    def _maybe_need_more(self) -> None:
        if self.max_scroll_y <= 0 or self.scroll_y >= self.max_scroll_y - 1:
            self.post_message(self.NeedMore())

    def watch_scroll_y(self, old_value: float, new_value: float) -> None:
        super().watch_scroll_y(old_value, new_value)
        if new_value >= old_value:
            self._maybe_need_more()

class TagBar(Static, can_focus=True):
    """riga dei tag scelti: clic o invio aprono la scelta multipla."""

    BINDINGS = [Binding("enter", "open_picker", "scegli tag", show=False)]

    def action_open_picker(self) -> None:
        self.app.action_pick_tags()

    def on_click(self, event) -> None:
        self.app.action_pick_tags()

class TagPicker(ModalScreen[list]):
    """Scelta multipla dei tag: i libri mostrati devono averli TUTTI."""

    BINDINGS = [Binding("escape", "apply", "applica")]

    CSS = """
    TagPicker { align: center middle; }
    #tag-dialog { width: 72; max-width: 95%; height: 80%; padding: 1 2;
                  border: thick #89b4fa; background: $surface; }
    #tag-title { color: #f9e2af; height: auto; }
    #tag-filter { height: 1; border: none; padding: 0 1; background: $boost;
                  margin: 1 0 0 0; }
    #tag-list { height: 1fr; margin: 1 0; border: round #45475a; }
    #tag-help { color: $text-muted; height: 1; }
    """

    def __init__(self, options, selected):
        super().__init__()
        self._options = list(options)     # [(etichetta, nome), …]
        self._selected = set(selected)

    def compose(self) -> ComposeResult:
        with Vertical(id="tag-dialog"):
            yield Label("🏷  Tag — i libri mostrati devono avere TUTTI "
                        "quelli scelti", id="tag-title")
            yield Input(placeholder="filtra i tag…", id="tag-filter")
            yield SelectionList(id="tag-list")
            yield Label("spazio/invio = attiva o disattiva · esc = applica "
                        "e chiude", id="tag-help")

    def on_mount(self) -> None:
        self._populate("")
        self.query_one("#tag-filter", Input).focus()

    def _populate(self, flt: str) -> None:
        try:
            lst = self.query_one("#tag-list", SelectionList)
        except NoMatches:
            return
        f = flt.strip().casefold()
        lst.clear_options()
        items = [(RText(label), name, name in self._selected)
                 for label, name in self._options
                 if not f or f in name.casefold()]
        if items:
            lst.add_options(items)
            lst.highlighted = 0

    @on(Input.Changed, "#tag-filter")
    def filter_changed(self, event: Input.Changed) -> None:
        self._populate(event.value)

    @on(Input.Submitted, "#tag-filter")
    def filter_submitted(self, event: Input.Submitted) -> None:
        self.action_apply()

    @on(SelectionList.SelectionToggled, "#tag-list")
    def selection_toggled(self, event: SelectionList.SelectionToggled) -> None:
        name = event.selection.value
        if name in self._selected:
            self._selected.discard(name)
        else:
            self._selected.add(name)

    def action_apply(self) -> None:
        self.dismiss(sorted(self._selected, key=str.casefold))

# ---------------------------------------------------------------- slider ----

class ScoreSlider(Static, can_focus=True):
    """slider orizzontale 0-100 per la soglia minima di score.

    Trascinabile col mouse (clic o drag), con tastiera: left/right ±1,
    home/end 0/100. Ad ogni cambiamento emette Changed(value)."""

    class Changed(Message):
        def __init__(self, value: int) -> None:
            super().__init__()
            self.value = value

    def __init__(self, value: float = 30.0, **kwargs) -> None:
        super().__init__(**kwargs)
        self.value = int(round(value))
        self._dragging = False

    # --- rendering -----------------------------------------------------
    # linea spessa il doppio: la traccia e' disegnata su due righe;
    # pallino grosso il doppio: 3 celle di larghezza x 2 di altezza,
    # costruito con quadranti di blocco (2 celle alle estremita' della traccia)
    def render(self):
        w = max(10, self.size.width)
        pos = round(self.value / 100 * (w - 1))
        filled, empty, knob = "#a6e3a1", "#585b70", "bold #f9e2af"
        cols = [c for c in (pos - 1, pos, pos + 1) if 0 <= c < w]
        top, bottom = RText(), RText()
        for i in range(w):
            if i in cols:
                g_top = "▛" if i == cols[0] else ("▜" if i == cols[-1] else "▀")
                g_bot = "▙" if i == cols[0] else ("▟" if i == cols[-1] else "▄")
                top.append(g_top, style=knob)
                bottom.append(g_bot, style=knob)
            else:
                style = filled if i < pos else empty
                glyph = "▰" if i < pos else "▱"
                top.append(glyph, style=style)
                bottom.append(glyph, style=style)
        top.append("\n")
        top.append(bottom)
        return top

    def on_resize(self) -> None:
        self.refresh()

    # --- valore ---------------------------------------------------------
    def set_value(self, value: float, notify: bool = True) -> None:
        v = max(0, min(100, int(round(value))))
        if v != self.value:
            self.value = v
            self.refresh()
            if notify:
                self.post_message(self.Changed(v))

    def _set_from_x(self, x: int) -> None:
        track = max(10, self.size.width)
        frac = x / (track - 1) if track > 1 else 0.0
        self.set_value(frac * 100)

    # --- mouse ----------------------------------------------------------
    def on_mouse_down(self, event: events.MouseDown) -> None:
        self._dragging = True
        self.capture_mouse(event)
        self._set_from_x(event.x)

    def on_mouse_move(self, event: events.MouseMove) -> None:
        if self._dragging:
            self._set_from_x(event.x)

    def on_mouse_up(self, event: events.MouseUp) -> None:
        self._dragging = False
        self.release_mouse()

    # --- tastiera ---------------------------------------------------------
    def on_key(self, event: events.Key) -> None:
        if event.key == "left":
            self.set_value(self.value - 1)
        elif event.key == "right":
            self.set_value(self.value + 1)
        elif event.key == "home":
            self.set_value(0)
        elif event.key == "end":
            self.set_value(100)
        else:
            return
        event.stop()

class CalibreFind(App):
    TITLE = "📚 Calibre fuzzy search"
    CSS = """
    #top-spacer { height: 1; }
    #thresholdbar { height: 2; padding: 0 1; margin-bottom: 1; }
    #threshold-label { color: $text-muted; margin-right: 1;
                       height: 2; content-align: left middle; }
    #threshold-value { color: #f9e2af; margin-left: 1;
                       height: 2; content-align: left middle; }
    #score-slider, #score-slider:focus {
        width: 1fr; border: none; padding: 0; background: transparent;
    }
    #fields { height: auto; padding: 0 1; }
    #fields Horizontal { height: 1; }
    #fields .field-label { width: 12; height: 1; color: $text-muted;
                           content-align: left middle; }
    #fields .field-label.second { width: auto; margin: 0 1 0 2; }
    #fields Input { width: 1fr; height: 1; border: none; padding: 0 1;
                    background: $boost; }
    #fields Input:focus { background: $surface-lighten-2; }
    #tag-bar { height: 1; width: 1fr; padding: 0 1; background: $boost;
               color: #89dceb; content-align: left middle; }
    #tag-bar:focus { background: $surface-lighten-2; }
    #main { height: 1fr; }
    #results { width: 1fr; border: round #45475a; padding: 0 1; background: transparent; }
    #results > ListItem { padding: 0 0 1 0; background: transparent; height: auto; }
    #results > ListItem.-highlight { background: #313244; }
    .card { width: 1fr; }
    #detail { width: 52; padding: 0; border: none; background: transparent;
              overflow-y: auto; overflow-x: auto; }
    #detail.hidden { display: none; }
    #detail-content { width: auto; padding: 0; background: transparent; }
    #status { height: 1; padding: 0 2; color: $text-muted; width: 1fr;
              text-wrap: nowrap; text-overflow: ellipsis;
              background: transparent; }
    #loading { padding: 1 2; }
    """
    BINDINGS = [
        Binding("ctrl+a", "focus_author", "autore", priority=True),
        Binding("ctrl+t", "focus_title", "titolo", priority=True),
        Binding("ctrl+d", "focus_desc", "descrizione", priority=True),
        Binding("ctrl+e", "focus_tag", "tag", priority=True),
        Binding("d", "toggle_detail", "dettaglio"),
        Binding("s", "focus_slider", "soglia"),
        Binding("o", "open_folder", "apri cartella"),
        Binding("plus", "raise_threshold", "soglia+", show=False),
        Binding("minus", "lower_threshold", "soglia-", show=False),
        Binding("ctrl+r", "reload", "ricarica libreria"),
        Binding("escape", "focus_search", "ricerca", show=False),
        Binding("q", "quit", "esci"),
    ]

    def __init__(self, db_path: str, page_size: int = PAGE_SIZE,
                 min_score: float = 30.0):
        super().__init__()
        self.db_path = db_path
        self.books = []
        self._cols = None         # colonne di scansione (condivise coi fork)
        self._pool = None         # pool di processi per la scansione parallela
        self.page_size = max(1, int(page_size))
        self.min_score = min_score
        self._tag_index = {}      # tag -> indici dei libri che lo hanno
        self._tag_options = []    # [(etichetta con conteggio, nome tag)]
        self._selected_tags = []  # filtro per sottoinsieme (AND)
        self._gen = 0             # generazione ricerca: scarta i risultati stanti
        self._debounce = None
        self._force_reload = False
        self._clustered = None    # schede di OGNI libro a gate > 0
                                  # (deduplicate solo se non c'è filtro tag)
        self._hits_key = None     # (titolo, autore, desc, tag) dei dati in cache
        self._hits_floor = 0.0    # floor con cui _clustered è stato calcolato
        self._n_hits = 0          # libri prima della fusione delle copie
        self._results = []        # schede visibili con la soglia attuale
        self._rendered = 0        # quante schede di _results sono già disegnate
        self._last_elapsed = 0.0
        self._dbcon = None        # connessione ro per le descrizioni on-demand

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Static("", id="top-spacer")        # spazio sopra lo slider
        with Horizontal(id="thresholdbar"):
            yield Label("soglia", id="threshold-label")
            yield ScoreSlider(self.min_score, id="score-slider")
            yield Label(f"≥ {self.min_score:g}", id="threshold-value")
        with Vertical(id="fields"):
            with Horizontal(id="row-title"):
                yield Label("titolo", classes="field-label")
                yield Input(placeholder="fuzzy sul titolo  (ctrl+t)",
                            id="q-title")
                yield Label("autore", classes="field-label second")
                yield Input(placeholder="fuzzy sull'autore  (ctrl+a)",
                            id="q-author")
            with Horizontal(id="row-desc"):
                yield Label("descrizione", classes="field-label")
                yield Input(placeholder="fuzzy nella descrizione  (ctrl+d)",
                            id="q-desc")
            with Horizontal(id="row-tags"):
                yield Label("tag", classes="field-label")
                yield TagBar("nessun tag — ctrl+e per sceglierne uno o più",
                             id="tag-bar")
        yield Label("", id="status")
        with Horizontal(id="main"):
            yield ResultsList(id="results")
            with ScrollableContainer(id="detail"):
                yield Static("", id="detail-content", markup=False)
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#q-title", Input).value = self._preset_title
        self.query_one("#q-author", Input).value = self._preset_author
        self.query_one("#q-desc", Input).value = self._preset_desc
        self._selected_tags = list(self._preset_tags)
        self._update_tag_bar()
        self.query_one("#status", Label).update(
            "⏳ Carico la libreria… (la prima volta: indicizzo e comprimo "
            "tutti i libri, ~15s)")
        self._load_library()

    # ------------------------------------------------------- caricamento ---
    @work(thread=True, group="load", exclusive=True)
    def _load_library(self) -> None:
        books, cols = load_books(self.db_path, force=self._force_reload)
        self._force_reload = False
        try:
            w = get_current_worker()
            if w is not None and w.is_cancelled:
                return
        except Exception:
            pass
        self.call_from_thread(self._library_ready, books, cols)

    def _library_ready(self, books, cols) -> None:
        self.books = books
        self._cols = cols
        cfcore._MP_COLS = cols
        self._tag_index, self._tag_options = build_tag_index(books)
        # i tag scelti che non esistono più dopo una ricarica decadono
        self._selected_tags = [t for t in self._selected_tags
                               if t in self._tag_index]
        self._update_tag_bar()
        try:
            if self._dbcon is not None:
                self._dbcon.close()
        except Exception:
            pass
        try:
            self._dbcon = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
        except Exception:
            self._dbcon = None
        self._close_pool()
        # processi forkati qui, col main thread fermo: ereditano le colonne
        self._pool = (cfcore.make_pool()
                      if len(books) >= POOL_MIN_BOOKS else None)
        self._clustered = None
        self._hits_key = None
        self._hits_floor = 0.0
        self._n_hits = 0
        self.notify(f"{len(books)} libri caricati da {os.path.basename(self.db_path)}",
                    title="Libreria", severity="information", timeout=4)
        self.run_search()

    def _close_pool(self) -> None:
        pool, self._pool = self._pool, None
        if pool is not None:
            try:
                pool.terminate()
            except Exception:
                pass

    def _fetch_desc(self, book_id: int) -> str:
        """descrizione leggibile di un libro, letta dal db al momento."""
        if self._dbcon is None:
            return ""
        try:
            row = self._dbcon.execute(
                "SELECT text FROM comments WHERE book=?", (book_id,)).fetchone()
            return html_to_text(row[0]) if row and row[0] else ""
        except Exception:
            return ""

    # ----------------------------------------------------------- ricerca ---
    QUERY_FIELDS = ("q-title", "q-author", "q-desc")

    @on(Input.Changed)
    def field_changed(self, event: Input.Changed) -> None:
        if event.input.id not in self.QUERY_FIELDS:
            return                         # es. il filtro della finestra tag
        if self._debounce:
            self._debounce.stop()
        self._debounce = self.set_timer(0.30, self.run_search)

    @on(Input.Submitted)
    def field_submitted(self, event: Input.Submitted) -> None:
        if event.input.id not in self.QUERY_FIELDS:
            return
        self.run_search()
        self.query_one("#results", ResultsList).focus()

    @on(ScoreSlider.Changed)
    def slider_changed(self, event: ScoreSlider.Changed) -> None:
        self.min_score = float(event.value)
        self.query_one("#threshold-value", Label).update(f"≥ {self.min_score:g}")
        if self._debounce:                 # durante il drag non ricerco a raffica
            self._debounce.stop()
        self._debounce = self.set_timer(0.05, self._refresh_results)

    def _current_key(self):
        """(titolo, autore, descrizione, tag scelti): la chiave dei risultati."""
        try:
            t = self.query_one("#q-title", Input).value.strip()
            a = self.query_one("#q-author", Input).value.strip()
            d = self.query_one("#q-desc", Input).value.strip()
        except NoMatches:              # app in chiusura: widget già smontati
            return None
        return (t, a, d, tuple(self._selected_tags))

    @staticmethod
    def _has_criteria(key) -> bool:
        return bool(key) and (any(key[:3]) or bool(key[3]))

    def _cache_ok(self, key) -> bool:
        """i punteggi in cache valgono per questi criteri e questa soglia?"""
        return (key is not None and self._clustered is not None
                and key == self._hits_key
                and self.min_score >= self._hits_floor)

    def run_search(self) -> None:
        if not self.books:
            return
        key = self._current_key()
        if key is None:
            return
        if self._cache_ok(key):
            # stessi criteri, soglia coperta dal floor: i punteggi sono già
            # calcolati, basta rifiltrare (istantaneo)
            self._apply_filtered()
            return
        if not self._has_criteria(key):
            self._clustered = []
            self._n_hits = 0
            self._hits_key = key
            self._hits_floor = self.min_score
            self._apply_filtered()
            return
        self._gen += 1
        self._search_worker(key, self._gen)

    def _refresh_results(self) -> None:
        """dopo un cambio di soglia: riusa i punteggi in cache se coprono la
        soglia, altrimenti riscansiona."""
        key = self._current_key()
        if self._cache_ok(key):
            self._apply_filtered()
        else:
            self.run_search()

    @work(thread=True, group="search", exclusive=True)
    def _search_worker(self, key, gen: int) -> None:
        t0 = time.perf_counter()
        try:
            worker = get_current_worker()
        except Exception:
            worker = None
        cancelled = (lambda: worker is not None and worker.is_cancelled)

        floor = min(SCAN_FLOOR, self.min_score)
        got = search(self.books, self._cols, self._tag_index, key, floor,
                     cancelled=cancelled, pool=self._pool)
        if got is None or cancelled():
            return
        clustered, n_hits, _ = got
        elapsed = (time.perf_counter() - t0) * 1000
        if cancelled():
            return
        self.call_from_thread(self._apply_hits, clustered, n_hits,
                              key, gen, elapsed, floor)

    def _apply_hits(self, clustered, n_hits, key, gen, elapsed, floor) -> None:
        if gen != self._gen:            # risultati stanti: è arrivata una query nuova
            return
        self._clustered = clustered
        self._n_hits = n_hits
        self._hits_key = key
        self._hits_floor = floor
        self._last_elapsed = elapsed
        self._apply_filtered()

    def _apply_filtered(self) -> None:
        try:
            lv = self.query_one("#results", ResultsList)
            status = self.query_one("#status", Label)
            detail = self.query_one("#detail-content", Static)
        except NoMatches:               # app in chiusura
            return
        lv.clear()
        self._results = []
        self._rendered = 0
        key = self._hits_key
        if not self._has_criteria(key):
            detail.update("")
            status.update("✍ scrivi in almeno un campo (titolo, autore, "
                          "descrizione) o scegli dei tag — si combinano")
            return
        ms = self.min_score
        self._results = [r for r in (self._clustered or ())
                         if r["_gate"] >= ms]
        if not self._results:
            detail.update("")
            status.update(self._status_text())
            lv.append(ListItem(Label("❌ Nessun risultato: togli un criterio "
                                     "o abbassa la soglia.")))
            return
        lv.extend([ResultItem(self._results[i], i)
                   for i in range(min(self.page_size, len(self._results)))])
        self._rendered = min(self.page_size, len(self._results))
        lv.index = 0
        self._update_status()

    def _append_page(self) -> None:
        """disegna la pagina successiva di risultati, se c'è."""
        try:
            lv = self.query_one("#results", ResultsList)
        except NoMatches:
            return
        total = len(self._results)
        if self._rendered >= total:
            return
        start = self._rendered
        end = min(total, start + self.page_size)
        self._rendered = end
        lv.extend([ResultItem(self._results[i], i) for i in range(start, end)])
        self._update_status()

    def _ensure_more(self, index) -> None:
        """carica in anticipo quando la selezione si avvicina alla fine."""
        if index is not None and index >= self._rendered - LOOKAHEAD:
            self._append_page()

    @on(ResultsList.NeedMore)
    def results_need_more(self, event: ResultsList.NeedMore) -> None:
        self._append_page()

    def _status_text(self) -> str:
        key = self._hits_key
        if not key:
            return ""
        parts = []
        for label, val in (("titolo", key[0]), ("autore", key[1]),
                           ("desc", key[2])):
            if val:
                v = val if len(val) <= 24 else val[:23] + "…"
                parts.append(f"{label}:{v}")
        if key[3]:
            parts.append("tag:" + "+".join(key[3]))
        crit = " · ".join(parts)
        total = len(self._results)
        if not total:
            return (f"⏱ {self._last_elapsed:.0f} ms · 0 risultati · "
                    f"≥ {self.min_score:g} · {crit}")
        noun = "risultato" if total == 1 else "risultati"
        shown = (f" · {self._rendered}/{total} mostrati"
                 if self._rendered < total else "")
        libri = (f" · da {self._n_hits} libri"
                 if self._n_hits > total else "")
        return (f"⏱ {self._last_elapsed:.0f} ms · {total} {noun}{libri}"
                f"{shown} · ≥ {self.min_score:g} · {crit}")

    def _update_status(self) -> None:
        try:
            self.query_one("#status", Label).update(self._status_text())
        except NoMatches:
            pass

    # ----------------------------------------------------------- dettaglio ---
    @on(ListView.Highlighted, "#results")
    def result_highlighted(self, event: ListView.Highlighted) -> None:
        item = event.item
        if isinstance(item, ResultItem):
            r = item.result
            if not r.get("_desc_done"):       # descrizione letta dal db al primo uso
                r["_desc_done"] = True
                r["desc"] = self._fetch_desc(r["id"])
            self.query_one("#detail-content", Static).update(
                detail_renderable(r, self.db_path))
            self.query_one("#detail", ScrollableContainer).scroll_home(animate=False)
        self._ensure_more(event.list_view.index)

    # ------------------------------------------------------------ azioni -----
    def action_focus_author(self) -> None:
        self.query_one("#q-author", Input).focus()

    def action_focus_title(self) -> None:
        self.query_one("#q-title", Input).focus()

    def action_focus_desc(self) -> None:
        self.query_one("#q-desc", Input).focus()

    def action_focus_tag(self) -> None:
        self.action_pick_tags()

    def action_pick_tags(self) -> None:
        if not self._tag_options:
            self.notify("Nessun tag nella libreria", severity="warning")
            return
        self.push_screen(TagPicker(self._tag_options, self._selected_tags),
                         self._tags_picked)

    def _tags_picked(self, result) -> None:
        if result is None:                 # annullata: niente è cambiato
            return
        self._selected_tags = list(result)
        self._update_tag_bar()
        self.run_search()

    def _update_tag_bar(self) -> None:
        try:
            bar = self.query_one("#tag-bar", TagBar)
        except NoMatches:
            return
        if self._selected_tags:
            bar.update(" + ".join(escape(t) for t in self._selected_tags))
        else:
            bar.update("[dim]nessun tag — ctrl+e per sceglierne uno o più[/]")

    def action_focus_slider(self) -> None:
        self.query_one("#score-slider", ScoreSlider).focus()

    def action_focus_search(self) -> None:
        for wid in ("#q-title", "#q-author", "#q-desc"):
            inp = self.query_one(wid, Input)
            if inp.value.strip():
                inp.focus()
                return
        self.query_one("#q-title", Input).focus()

    def action_toggle_detail(self) -> None:
        self.query_one("#detail", ScrollableContainer).toggle_class("hidden")

    def action_open_folder(self) -> None:
        lv = self.query_one("#results", ResultsList)
        item = lv.highlighted_child
        if isinstance(item, ResultItem) and item.result["path"]:
            base = os.path.dirname(self.db_path)
            subprocess.Popen(["xdg-open", os.path.join(base, item.result["path"])],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.notify(item.result["path"], title="Cartella aperta", timeout=3)

    def action_raise_threshold(self) -> None:
        self.min_score = min(95.0, self.min_score + 5)
        self._sync_threshold_widgets()
        self._refresh_results()

    def action_lower_threshold(self) -> None:
        self.min_score = max(0.0, self.min_score - 5)
        self._sync_threshold_widgets()
        self._refresh_results()

    def _sync_threshold_widgets(self) -> None:
        slider = self.query_one("#score-slider", ScoreSlider)
        slider.value = int(round(self.min_score))
        slider.refresh()
        self.query_one("#threshold-value", Label).update(f"≥ {self.min_score:g}")

    def action_reload(self) -> None:
        self.books = []
        self.notify("Ricarico la libreria…", timeout=3)
        self._force_reload = True
        self._load_library()

    def action_quit(self) -> None:
        # niente attese sui processi/worker alla chiusura
        self._close_pool()
        self.exit()

    # preset salvati dopo la costruzione (argparse passa i valori)
    _preset_author = ""
    _preset_title = ""
    _preset_desc = ""
    _preset_tags = ()

# ---------------------------------------------------------------- main ------

def main():
    ap = argparse.ArgumentParser(
        description="Ricerca fuzzy combinabile titolo/autore/descrizione/tag "
                    "su libreria Calibre (TUI)")
    ap.add_argument("-a", "--author", default="", help="query autore (fuzzy)")
    ap.add_argument("-t", "--title",  default="", help="query titolo (fuzzy)")
    ap.add_argument("-d", "--desc",   default="",
                    help="query dentro le descrizioni (fuzzy)")
    ap.add_argument("-g", "--tag", action="append", default=[], metavar="TAG",
                    help="tag richiesto; ripetibile: i libri devono averli tutti")
    ap.add_argument("-l", "--library", default=os.environ.get("CALIBRE_LIBRARY",
                    os.path.expanduser("~/CalibreLibrary/metadata.db")),
                    help="path di metadata.db")
    ap.add_argument("-n", "--page-size", "--limit", dest="page_size", type=int,
                    default=PAGE_SIZE,
                    help=f"risultati disegnati per pagina (default {PAGE_SIZE}; "
                         "il totale non è limitato)")
    ap.add_argument("-m", "--min-score", type=float, default=30.0,
                    help="score minimo 0-100 (default 30)")
    args = ap.parse_args()

    if not os.path.exists(args.library):
        sys.exit(f"✗ metadata.db non trovato: {args.library}")

    CalibreFind._preset_author = args.author
    CalibreFind._preset_title = args.title
    CalibreFind._preset_desc = args.desc
    CalibreFind._preset_tags = tuple(dict.fromkeys(args.tag))
    app = CalibreFind(args.library, page_size=args.page_size,
                      min_score=args.min_score)
    app.run()


if __name__ == "__main__":
    main()
