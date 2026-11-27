#!/usr/bin/env python3
"""calibrefind_flet — la ricerca fuzzy di calibrefind con interfaccia Flet.

Riprende 1:1 la logica di `calibrefind.py` / `cfcore.py`: quattro criteri
indipendenti e combinabili (titolo, autore, descrizione, tag) che si
intersecano in AND, ricerca fuzzy tollerante ai refusi, soglia minima di
score regolabile, fusione delle copie della stessa opera (tranne quando c'è
un filtro per tag) e lista di risultati paginata che si allunga scorrendo.

Uso:
    ./cfindf                                       # launcher rapido (come cfind)
    ./calibrefind_flet.py                          # finestra desktop Flet
    ./calibrefind_flet.py --web                    # server web locale
    ./calibrefind_flet.py -a "Gibson"              # query autore precompilata
    ./calibrefind_flet.py -t "neuromancer"
    ./calibrefind_flet.py -a g -t mancer           # criteri combinati
    ./calibrefind_flet.py -d "cyberspazio"
    ./calibrefind_flet.py -g Fantascienza -g Cyberpunk
    ./calibrefind_flet.py -l /percorso/metadata.db
    ./calibrefind_flet.py -n 100 -m 45

Dipendenze (già installate nel .venv del progetto): `flet`, `flet-web`
(modalità web) e `flet-desktop` (finestra nativa), oltre a `rapidfuzz`.
Senza un display grafico il programma passa da solo alla modalità web.

La cache, il pool di processi forkati per la scansione e la fusione delle
copie sono gli stessi di `cfcore`: all'avvio il primo accesso indicizza la
libreria e scrive la cache accanto al metadata.db (~15s su 25k libri), poi
si carica in ~1s.

Note Flet:
  - l'evento di caricamento/selezione gira sul thread dell'event loop; la
    scansione pesante gira in `asyncio.to_thread`, mai sul thread della UI.
  - il pool di fork viene creato dentro un worker thread, come nella TUI:
    i processi ereditano le colonne condivise in sola lettura.
  - in `--web` ogni scheda del browser è una sessione separata e carica la
    propria copia della libreria: va bene per uso locale, non per più utenti.
"""

import argparse
import asyncio
import atexit
import os
import sqlite3
import subprocess
import sys
import threading

import flet as ft

import cfcore
from cfcore import (LOOKAHEAD, PAGE_SIZE, POOL_MIN_BOOKS, SCAN_FLOOR,
                    build_tag_index, html_to_text, load_books, rating_stars,
                    score_hex, search)

# ---------------------------------------------------------------- palette ---
# gli stessi colori Catppuccin-ish della TUI
BG         = "#1e1e2e"
PANEL      = "#181825"
SURFACE    = "#313244"
SURFACE_HI = "#45475a"
TEXT       = "#cdd6f4"
MUTED      = "#6c7086"
DIM        = "#585b70"
BLUE       = "#89b4fa"
YELLOW     = "#f9e2af"
PINK       = "#f5c2e7"
GREEN      = "#a6e3a1"
TEAL       = "#89dceb"
RED        = "#f38ba8"

TITLE = "📚 Calibre fuzzy search"

_INPUT_BORDER = {
    ft.ControlState.DEFAULT: ft.OutlineInputBorder(
        border_radius=8, side=ft.BorderSide(1, SURFACE_HI)),
    ft.ControlState.FOCUSED: ft.OutlineInputBorder(
        border_radius=8, side=ft.BorderSide(1.5, BLUE)),
}


class CalibreFindFlet:
    """Controller + vista Flet: una istanza per pagina/sessione."""

    def __init__(self, page: ft.Page, db_path: str,
                 page_size: int = PAGE_SIZE, min_score: float = 30.0,
                 preset_title: str = "", preset_author: str = "",
                 preset_desc: str = "", preset_tags=()):
        self.page = page
        self.db_path = db_path
        self.page_size = max(1, int(page_size))
        self.min_score = float(min_score)

        self.books = []
        self._cols = None
        self._pool = None
        self._dbcon = None
        self._tag_index = {}
        self._tag_options = []
        self._selected_tags = list(dict.fromkeys(preset_tags))

        self._gen = 0               # generazione ricerca: scarta risultati stanti
        self._clustered = None      # schede di ogni libro a gate > 0
        self._hits_key = None       # criteri con cui _clustered è stato calcolato
        self._hits_floor = 0.0      # floor della cache
        self._n_hits = 0            # libri prima della fusione delle copie
        self._results = []          # schede visibili con la soglia attuale
        self._rendered = 0          # quante schede sono già disegnate
        self._selected = None       # indice della scheda selezionata
        self._card_containers = []
        self._last_elapsed = 0.0
        self._search_lock = threading.Lock()
        self._loading = False
        self._closed = False
        self._text_focused = False

        self._pending_tags = set()
        self._tag_dialog = None
        self._tag_dialog_filter = None
        self._tag_dialog_list = None

        self._build_ui(preset_title, preset_author, preset_desc)
        self.page.on_close = self._on_close
        atexit.register(self._shutdown)

    # --------------------------------------------------------------- UI ----
    def _build_ui(self, preset_title, preset_author, preset_desc) -> None:
        page = self.page

        def text_field(value: str, label: str, hint: str, **kw):
            return ft.TextField(
                value=value, label=label, hint_text=hint,
                dense=True, filled=True, fill_color=SURFACE,
                border=_INPUT_BORDER,
                content_padding=ft.Padding.symmetric(horizontal=12, vertical=10),
                text_size=13, color=TEXT,
                label_style=ft.TextStyle(size=12, color=MUTED),
                hint_style=ft.TextStyle(size=12, color=DIM),
                on_change=self._on_field_changed,
                on_submit=self._on_field_submitted,
                on_focus=lambda e: setattr(self, "_text_focused", True),
                on_blur=lambda e: setattr(self, "_text_focused", False),
                **kw)

        self.title_tf = text_field(preset_title, "titolo",
                                   "fuzzy sul titolo  (Ctrl+T)")
        self.author_tf = text_field(preset_author, "autore",
                                    "fuzzy sull'autore  (Ctrl+A)")
        self.desc_tf = text_field(preset_desc, "descrizione",
                                  "fuzzy nella descrizione  (Ctrl+D)")

        self.tag_text = ft.Text("", size=13, color=MUTED, expand=True)
        self.tag_bar = ft.Container(
            content=ft.Row(
                [ft.Icon(ft.Icons.LABEL, color=TEAL, size=16), self.tag_text],
                spacing=8, vertical_alignment=ft.CrossAxisAlignment.CENTER),
            padding=ft.Padding.symmetric(horizontal=12, vertical=10),
            bgcolor=SURFACE, border=ft.Border.all(1, SURFACE_HI),
            border_radius=8, ink=True,
            on_click=lambda e: self.open_tag_picker(),
            tooltip="Scegli uno o più tag (Ctrl+E)")

        self.slider = ft.Slider(
            min=0, max=100, divisions=100, value=self.min_score,
            active_color=GREEN, inactive_color=SURFACE_HI, thumb_color=YELLOW,
            on_change=self._on_slider_changed, expand=True,
            tooltip="Soglia minima di score")
        self.threshold_value = ft.Text(f"≥ {self.min_score:g}", size=13,
                                       color=YELLOW, weight=ft.FontWeight.BOLD)

        self.status = ft.Text("", size=12, color=MUTED, max_lines=1,
                              overflow=ft.TextOverflow.ELLIPSIS,
                              no_wrap=True, expand=True)

        self.results_list = ft.ListView(
            expand=True, spacing=6,
            padding=ft.Padding.symmetric(horizontal=4, vertical=6),
            on_scroll=self._on_results_scroll)
        self._more_text = ft.Text("", size=12, color=BLUE)
        self._more_button = ft.Container(
            content=ft.Row(
                [self._more_text, ft.Icon(ft.Icons.EXPAND_MORE, size=16,
                                          color=BLUE)],
                spacing=4, vertical_alignment=ft.CrossAxisAlignment.CENTER),
            padding=ft.Padding.symmetric(horizontal=10, vertical=4),
            bgcolor=SURFACE, border=ft.Border.all(1, SURFACE_HI),
            border_radius=6, ink=True, visible=False,
            on_click=self._on_more,
            tooltip="Disegna la pagina successiva di risultati")
        self.detail_column = ft.Column(
            controls=[], scroll=ft.ScrollMode.AUTO, expand=True, spacing=8)
        self.detail_box = ft.Container(
            content=self.detail_column, width=390, bgcolor=PANEL,
            border=ft.Border.all(1, SURFACE_HI), border_radius=12,
            padding=ft.Padding.symmetric(horizontal=14, vertical=12))
        self._show_detail(None)

        self.detail_button = ft.IconButton(
            icon=ft.Icons.VISIBILITY, icon_color=BLUE,
            tooltip="Mostra/nascondi il dettaglio",
            on_click=self._toggle_detail)
        self.reload_button = ft.IconButton(
            icon=ft.Icons.REFRESH, icon_color=BLUE,
            tooltip="Ricarica la libreria (Ctrl+R)",
            on_click=lambda e: self._reload())

        header = ft.Row(
            [ft.Icon(ft.Icons.MENU_BOOK, color=YELLOW, size=26),
             ft.Text("Calibre fuzzy search", size=20,
                     weight=ft.FontWeight.BOLD, color=YELLOW),
             ft.Text(os.path.basename(self.db_path), size=12, color=MUTED,
                     expand=True),
             self.reload_button, self.detail_button],
            spacing=10, vertical_alignment=ft.CrossAxisAlignment.CENTER)

        threshold = ft.Row(
            [ft.Text("soglia", size=12, color=MUTED),
             self.slider, self.threshold_value],
            spacing=12, vertical_alignment=ft.CrossAxisAlignment.CENTER)

        fields = ft.Column(
            [ft.Row([self.title_tf, self.author_tf], spacing=10),
             self.desc_tf,
             ft.Row([ft.Text("tag", size=12, color=MUTED), self.tag_bar],
                    spacing=10, vertical_alignment=ft.CrossAxisAlignment.CENTER)],
            spacing=8)

        main_row = ft.Row(
            [ft.Container(self.results_list, expand=True, bgcolor=PANEL,
                          border=ft.Border.all(1, SURFACE_HI),
                          border_radius=12,
                          padding=ft.Padding.symmetric(horizontal=8, vertical=6)),
             self.detail_box],
            expand=True, spacing=10,
            vertical_alignment=ft.CrossAxisAlignment.STRETCH)

        footer = ft.Text(
            "Ctrl+T titolo · Ctrl+A autore · Ctrl+D descrizione · Ctrl+E tag · "
            "Ctrl+R ricarica · scorri in fondo per altri risultati",
            size=11, color=DIM, no_wrap=True,
            overflow=ft.TextOverflow.ELLIPSIS)

        page.add(header, threshold, fields,
                 ft.Row([self.status, self._more_button], spacing=8,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER),
                 main_row, footer)

    # ------------------------------------------------------- caricamento ----
    async def start(self) -> None:
        if self._loading:
            return
        self._set_status("⏳ Carico la libreria… (la prima volta indicizzo e "
                         "comprimo tutti i libri, ~15s)")
        await self._load(force=False)

    async def _load(self, force: bool) -> None:
        self._loading = True
        try:
            try:
                books, cols = await asyncio.to_thread(
                    load_books, self.db_path, force)
            except Exception as exc:
                self._set_status(f"✗ Errore nel caricamento della libreria: {exc}")
                return
            if self._closed:
                return
            self.books = books
            self._cols = cols
            cfcore._MP_COLS = cols           # ereditato dai fork
            self._tag_index, self._tag_options = build_tag_index(books)
            # i tag scelti che non esistono più dopo una ricarica decadono
            self._selected_tags = [t for t in self._selected_tags
                                   if t in self._tag_index]
            self._pending_tags &= set(self._tag_index)
            self._update_tag_bar()

            self._close_db()
            try:
                self._dbcon = await asyncio.to_thread(self._connect_db)
            except Exception:
                self._dbcon = None

            self._close_pool()
            self._pool = await asyncio.to_thread(
                self._make_pool, len(books))

            self._clustered = None
            self._hits_key = None
            self._hits_floor = 0.0
            self._n_hits = 0

            self._snack(f"{len(books)} libri caricati da "
                        f"{os.path.basename(self.db_path)}")
            await self._run_search(reuse_cache=False)
        finally:
            self._loading = False

    def _connect_db(self):
        return sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True,
                               check_same_thread=False)

    def _make_pool(self, n_books: int):
        if n_books < POOL_MIN_BOOKS:
            return None
        return cfcore.make_pool()

    def _reload(self) -> None:
        if self._loading:
            return
        self._gen += 1                    # annulla la ricerca in corso
        self._set_status("⏳ Ricarico la libreria…")
        self.page.run_task(self._reload_async)

    async def _reload_async(self) -> None:
        if self._loading:
            return
        await self._load(force=True)

    def _close_pool(self) -> None:
        pool, self._pool = self._pool, None
        if pool is not None:
            try:
                pool.terminate()
            except Exception:
                pass

    def _close_db(self) -> None:
        con, self._dbcon = self._dbcon, None
        if con is not None:
            try:
                con.close()
            except Exception:
                pass

    def _fetch_desc(self, book_id: int) -> str:
        """descrizione leggibile di un libro, letta dal db al momento."""
        con = self._dbcon
        if con is None:
            return ""
        try:
            row = con.execute(
                "SELECT text FROM comments WHERE book=?", (book_id,)).fetchone()
            return html_to_text(row[0]) if row and row[0] else ""
        except Exception:
            return ""

    # ----------------------------------------------------------- ricerca ----
    def _criteria_key(self):
        return (self.title_tf.value.strip(), self.author_tf.value.strip(),
                self.desc_tf.value.strip(), tuple(self._selected_tags))

    @staticmethod
    def _has_criteria(key) -> bool:
        return bool(key) and (any(key[:3]) or bool(key[3]))

    def _cache_ok(self, key) -> bool:
        return (key is not None and self._clustered is not None
                and key == self._hits_key
                and self.min_score >= self._hits_floor)

    def _on_field_changed(self, _e) -> None:
        self._schedule_search(0.30)

    def _on_field_submitted(self, _e) -> None:
        self._schedule_search(0.0)

    def _on_slider_changed(self, e) -> None:
        self.min_score = float(e.control.value)
        self.threshold_value.value = f"≥ {self.min_score:g}"
        self.page.update()
        self._schedule_search(0.05)

    def _schedule_search(self, delay: float) -> None:
        if not self.books or self._closed:
            return
        self._gen += 1
        gen = self._gen
        self.page.run_task(self._debounced_search, gen, delay)

    async def _debounced_search(self, gen: int, delay: float) -> None:
        try:
            if delay > 0:
                await asyncio.sleep(delay)
            if gen != self._gen or self._closed:
                return
            await self._run_search(gen, reuse_cache=True)
        except Exception as exc:          # mai far cadere l'event loop
            if not self._closed:
                self._set_status(f"✗ Errore nella ricerca: {exc}")

    async def _run_search(self, gen=None, reuse_cache: bool = True) -> None:
        if not self.books or self._closed:
            return
        if gen is None:
            self._gen += 1
            gen = self._gen
        key = self._criteria_key()
        if not self._has_criteria(key):
            self._clustered = []
            self._n_hits = 0
            self._hits_key = key
            self._hits_floor = self.min_score
            self._apply_filtered()
            return
        if reuse_cache and self._cache_ok(key):
            self._apply_filtered()
            return

        floor = min(SCAN_FLOOR, self.min_score)
        self._set_status("⏳ ricerca…")
        try:
            got = await asyncio.to_thread(self._search_sync, key, floor, gen)
        except Exception as exc:
            if not self._closed:
                self._set_status(f"✗ Errore nella ricerca: {exc}")
            return
        if got is None or gen != self._gen or self._closed:
            return
        clustered, n_hits, elapsed = got
        self._clustered = clustered
        self._n_hits = n_hits
        self._hits_key = key
        self._hits_floor = floor
        self._last_elapsed = elapsed
        self._apply_filtered()

    def _search_sync(self, key, floor: float, gen: int):
        def cancelled() -> bool:
            return self._closed or gen != self._gen
        with self._search_lock:
            if cancelled():
                return None
            return search(self.books, self._cols, self._tag_index, key,
                          floor, cancelled=cancelled, pool=self._pool)

    # --------------------------------------------------- lista risultati ----
    def _apply_filtered(self) -> None:
        if self._closed:
            return
        self.results_list.controls = []
        self._card_containers = []
        self._results = []
        self._rendered = 0
        self._selected = None
        self._more_button.visible = False
        self._show_detail(None)

        if not self._has_criteria(self._hits_key):
            self._set_status("✍ scrivi in almeno un campo (titolo, autore, "
                             "descrizione) o scegli dei tag — si combinano")
            self.page.update()
            return

        ms = self.min_score
        self._results = [r for r in (self._clustered or ())
                         if r["_gate"] >= ms]
        if not self._results:
            self.results_list.controls.append(self._message_card(
                "❌ Nessun risultato: togli un criterio o abbassa la soglia."))
            self._update_status()
            self.page.update()
            return

        self._append_page()
        self._select(0)
        self._update_status()
        self.page.update()

    def _sync_more(self) -> None:
        """Aggiorna il pulsante "Mostra altri" accanto allo stato.

        Serve anche quando la pagina non riempie la vista (finestra molto
        alta o -n piccolo): lì lo scroll non scatterebbe mai."""
        remaining = len(self._results) - self._rendered
        self._more_button.visible = remaining > 0
        if remaining > 0:
            self._more_text.value = (f"Mostra altri {min(self.page_size, remaining)}"
                                     f" · {remaining} rimanenti")

    def _append_page(self) -> bool:
        total = len(self._results)
        if self._rendered >= total:
            return False
        start = self._rendered
        end = min(total, start + self.page_size)
        for i in range(start, end):
            card = self._card(self._results[i], i)
            self._card_containers.append(card)
            self.results_list.controls.append(card)
        self._rendered = end
        self._sync_more()
        return True

    def _on_more(self, _e=None) -> None:
        if self._closed:
            return
        if self._append_page():
            self._update_status()
            self.page.update()

    def _on_results_scroll(self, e) -> None:
        if self._closed or not self._results:
            return
        max_y = getattr(e, "max_scroll_extent", 0) or 0
        pixels = getattr(e, "pixels", 0) or 0
        if max_y > 0 and pixels >= max_y - 80:
            if self._append_page():
                self._update_status()
                self.page.update()

    def _message_card(self, message: str) -> ft.Container:
        return ft.Container(
            content=ft.Text(message, size=13, color=MUTED, italic=True),
            padding=ft.Padding.symmetric(horizontal=14, vertical=24))

    def _card(self, r: dict, idx: int) -> ft.Container:
        col = score_hex(r["score"])
        lines = [
            ft.Row(
                [ft.Text(f"{idx + 1:>2})", size=12, color=BLUE,
                         weight=ft.FontWeight.BOLD),
                 ft.Text(f"#{r['id']}", size=11, color=DIM),
                 ft.Container(expand=True),
                 ft.Text(f"{r['score']:5.1f}%", size=13, color=col,
                         weight=ft.FontWeight.BOLD)],
                spacing=10, vertical_alignment=ft.CrossAxisAlignment.CENTER),
            ft.Text(r["title"], size=15, weight=ft.FontWeight.BOLD,
                    color=YELLOW, max_lines=1,
                    overflow=ft.TextOverflow.ELLIPSIS, tooltip=r["title"]),
            ft.Text(r["author"], size=13, color=PINK, max_lines=1,
                    overflow=ft.TextOverflow.ELLIPSIS),
        ]
        if r["series"]:
            lines.append(ft.Row(
                [ft.Text("serie", size=11, color=MUTED),
                 ft.Text(r["series"], size=12, color=TEAL, expand=True,
                         max_lines=1, overflow=ft.TextOverflow.ELLIPSIS)],
                spacing=8))
        meta = []
        if r["formats"]:
            meta.append(ft.Text(", ".join(r["formats"]), size=11, color=GREEN))
        if r["rating"]:
            meta.append(ft.Text(rating_stars(r["rating"]), size=11, color=YELLOW))
        if r["pubdate"]:
            meta.append(ft.Text(r["pubdate"], size=11, color=DIM))
        if meta:
            lines.append(ft.Row(meta, spacing=14))
        if r["tags"]:
            lines.append(ft.Text(r["tags"], size=11, color=MUTED, max_lines=1,
                                 overflow=ft.TextOverflow.ELLIPSIS))
        lines.append(ft.ProgressBar(value=max(0.0, min(1.0, r["score"] / 100.0)),
                                    color=col, bgcolor=SURFACE_HI,
                                    bar_height=4, border_radius=2))
        return ft.Container(
            content=ft.Column(lines, spacing=3, tight=True),
            padding=ft.Padding.symmetric(horizontal=12, vertical=9),
            bgcolor=None, border=ft.Border.all(1, SURFACE_HI),
            border_radius=10, ink=True,
            on_click=lambda e, i=idx: self._click_card(i))

    def _click_card(self, idx: int) -> None:
        if self._closed:
            return
        self._select(idx)
        self.page.update()

    def _select(self, idx: int) -> None:
        if idx is None or not (0 <= idx < len(self._results)):
            return
        if idx >= self._rendered - LOOKAHEAD:
            self._append_page()           # carica in anticipo la pagina dopo
        prev = self._selected
        if prev is not None and 0 <= prev < len(self._card_containers):
            self._card_containers[prev].bgcolor = None
            self._card_containers[prev].border = ft.Border.all(1, SURFACE_HI)
        if idx < len(self._card_containers):
            card = self._card_containers[idx]
            card.bgcolor = SURFACE
            card.border = ft.Border.all(1, BLUE)
        self._selected = idx
        self._show_detail(self._results[idx])

    # --------------------------------------------------------- dettaglio ----
    def _show_detail(self, r) -> None:
        if r is None:
            self.detail_column.controls = [
                ft.Container(
                    content=ft.Column(
                        [ft.Icon(ft.Icons.INFO_OUTLINE, color=DIM, size=34),
                         ft.Text("Seleziona un libro per vedere i dettagli.",
                                 size=13, color=MUTED, italic=True,
                                 text_align=ft.TextAlign.CENTER)],
                        spacing=10,
                        horizontal_alignment=ft.CrossAxisAlignment.CENTER),
                    padding=ft.Padding.symmetric(vertical=40))]
            return

        if not r.get("_desc_done"):
            r["_desc_done"] = True
            r["desc"] = self._fetch_desc(r["id"])

        col = score_hex(r["score"])
        ncopies = r.get("copies", 1)
        controls = [
            ft.Row(
                [ft.Text(f"{r['score']:5.1f}%", size=17, color=col,
                         weight=ft.FontWeight.BOLD),
                 ft.ProgressBar(value=max(0.0, min(1.0, r["score"] / 100.0)),
                                color=col, bgcolor=SURFACE_HI, bar_height=6,
                                border_radius=3, expand=True)],
                spacing=12, vertical_alignment=ft.CrossAxisAlignment.CENTER),
        ]
        if ncopies > 1:
            controls.append(ft.Text(f"su {ncopies} copie fuse", size=11,
                                    color=MUTED, italic=True))
        controls += [
            ft.Divider(height=1, color=SURFACE_HI),
            self._kv("titolo", r["title"], YELLOW, bold=True),
            self._kv("autore", r["author"], PINK),
        ]
        if r["series"]:
            controls.append(self._kv("serie", r["series"], TEAL))
        if r["formats"]:
            controls.append(self._kv("formati", ", ".join(r["formats"]), GREEN))
        if r["pubdate"]:
            controls.append(self._kv("pubblicazione", r["pubdate"], BLUE))
        if r["rating"]:
            controls.append(self._kv(
                "rating", f"{rating_stars(r['rating'])}  ({r['rating']}/10)",
                YELLOW))
        if r["tags"]:
            controls.append(self._kv("tags", r["tags"], MUTED))
        if r["path"]:
            controls.append(self._kv("cartella", r["path"] + "/", DIM))
        controls.append(ft.Divider(height=1, color=SURFACE_HI))
        controls.append(ft.Text("descrizione", size=12, color=MUTED))
        if r["desc"]:
            controls.append(ft.Text(r["desc"], size=13, color=TEXT,
                                    selectable=True))
        else:
            controls.append(ft.Text("(nessuna descrizione)", size=12,
                                    color=MUTED, italic=True))
        controls += [
            ft.Divider(height=1, color=SURFACE_HI),
            ft.Row(
                [ft.OutlinedButton(
                    content="Apri cartella", icon=ft.Icons.FOLDER_OPEN,
                    on_click=lambda e: self.open_folder(),
                    disabled=not bool(r["path"])),
                 ft.Text(f"libreria: {os.path.basename(self.db_path)}",
                         size=11, color=DIM, expand=True,
                         text_align=ft.TextAlign.RIGHT)],
                spacing=10, vertical_alignment=ft.CrossAxisAlignment.CENTER),
        ]
        self.detail_column.controls = controls
        if not self._closed:
            self.page.run_task(self._scroll_detail_home)

    async def _scroll_detail_home(self) -> None:
        try:
            await self.detail_column.scroll_to(offset=0)
        except Exception:
            pass

    def _kv(self, label: str, value: str, color: str = TEXT,
            bold: bool = False) -> ft.Row:
        return ft.Row(
            [ft.Text(label, size=11, color=MUTED, width=102),
             ft.Text(value, size=13, color=color, selectable=True, expand=True,
                     weight=ft.FontWeight.BOLD if bold else ft.FontWeight.NORMAL)],
            spacing=8, vertical_alignment=ft.CrossAxisAlignment.START)

    def _toggle_detail(self, _e=None) -> None:
        self.detail_box.visible = not self.detail_box.visible
        self.detail_button.icon = (ft.Icons.VISIBILITY
                                   if self.detail_box.visible
                                   else ft.Icons.VISIBILITY_OFF)
        self.page.update()

    def open_folder(self) -> None:
        idx = self._selected
        if idx is None or not (0 <= idx < len(self._results)):
            return
        r = self._results[idx]
        if not r["path"]:
            self._snack("Questo libro non ha una cartella.")
            return
        base = os.path.dirname(self.db_path)
        target = os.path.join(base, r["path"])
        try:
            subprocess.Popen(["xdg-open", target],
                             stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
            self._snack(f"Cartella aperta: {r['path']}")
        except Exception as exc:
            self._snack(f"✗ Impossibile aprire la cartella: {exc}")

    # -------------------------------------------------------------- tag ----
    def _update_tag_bar(self) -> None:
        if self._selected_tags:
            self.tag_text.value = " + ".join(self._selected_tags)
            self.tag_text.color = TEAL
        else:
            self.tag_text.value = ("nessun tag — clicca per sceglierne uno o "
                                   "più (Ctrl+E)")
            self.tag_text.color = MUTED

    def open_tag_picker(self) -> None:
        if not self._tag_options:
            self._snack("Nessun tag nella libreria")
            return
        self._pending_tags = set(self._selected_tags)
        self._tag_dialog_filter = ft.TextField(
            hint_text="filtra i tag…", dense=True, filled=True,
            fill_color=SURFACE, border=_INPUT_BORDER, autofocus=True,
            content_padding=ft.Padding.symmetric(horizontal=12, vertical=8),
            text_size=13, color=TEXT,
            hint_style=ft.TextStyle(size=12, color=DIM),
            on_change=lambda e: self._populate_tag_dialog(e.control.value),
            on_submit=lambda e: self._tag_apply())
        self._tag_dialog_list = ft.ListView(expand=True, spacing=0,
                                            padding=4)
        self._populate_tag_dialog("")

        width = min(620, max(320, int((self.page.width or 900) - 120)))
        height = min(560, max(280, int((self.page.height or 800) - 200)))
        body = ft.Column(
            [ft.Row(
                [ft.Icon(ft.Icons.LABEL_OUTLINE, color=TEAL, size=18),
                 ft.Text("Tag — i libri mostrati devono avere TUTTI quelli "
                         "scelti", size=13, color=YELLOW, expand=True)],
                spacing=8, vertical_alignment=ft.CrossAxisAlignment.CENTER),
             self._tag_dialog_filter,
             ft.Container(self._tag_dialog_list, expand=True,
                          border=ft.Border.all(1, SURFACE_HI),
                          border_radius=8, bgcolor=PANEL),
             ft.Text("clicca per attivare/disattivare · Applica per chiudere",
                     size=11, color=MUTED)],
            spacing=8, expand=True)
        self._tag_dialog = ft.AlertDialog(
            modal=True, bgcolor=BG,
            content=ft.Container(content=body, width=width, height=height),
            actions=[
                ft.TextButton(content="Annulla",
                              on_click=lambda e: self._tag_cancel()),
                ft.FilledButton(content="Applica",
                                on_click=lambda e: self._tag_apply()),
            ],
            actions_alignment=ft.MainAxisAlignment.END)
        self.page.show_dialog(self._tag_dialog)

    def _populate_tag_dialog(self, flt: str) -> None:
        if self._tag_dialog_list is None:
            return
        f = (flt or "").strip().casefold()
        rows = []
        for label, name in self._tag_options:
            if f and f not in name.casefold():
                continue
            rows.append(ft.Checkbox(
                label=label, value=name in self._pending_tags,
                active_color=BLUE, label_style=ft.TextStyle(size=12, color=TEXT),
                visual_density=ft.VisualDensity.COMPACT,
                on_change=lambda e, n=name: self._set_tag(n, e.control.value)))
        if not rows:
            rows.append(ft.Container(
                content=ft.Text("Nessun tag corrisponde al filtro.",
                                size=12, color=MUTED, italic=True),
                padding=12))
        self._tag_dialog_list.controls = rows
        if not self._closed:
            self.page.update()

    def _set_tag(self, name: str, checked: bool) -> None:
        if checked:
            self._pending_tags.add(name)
        else:
            self._pending_tags.discard(name)

    def _tag_cancel(self) -> None:
        self._tag_dialog = None
        try:
            self.page.pop_dialog()
        except Exception:
            pass

    def _tag_apply(self) -> None:
        self._selected_tags = sorted(self._pending_tags, key=str.casefold)
        self._tag_dialog = None
        try:
            self.page.pop_dialog()
        except Exception:
            pass
        self._update_tag_bar()
        self.page.update()
        self._schedule_search(0.0)

    # ----------------------------------------------------------- stato ------
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
        self.status.value = self._status_text()

    def _set_status(self, message: str) -> None:
        if self._closed:
            return
        self.status.value = message
        try:
            self.page.update()
        except Exception:
            pass

    def _snack(self, message: str) -> None:
        if self._closed:
            return
        try:
            self.page.show_dialog(ft.SnackBar(
                content=ft.Text(message, color=TEXT), bgcolor=SURFACE,
                duration=ft.Duration(milliseconds=3000)))
        except Exception:
            pass

    def _adjust_threshold(self, delta: float) -> None:
        self.min_score = max(0.0, min(100.0, self.min_score + delta))
        self.slider.value = round(self.min_score)
        self.threshold_value.value = f"≥ {self.min_score:g}"
        self.page.update()
        self._schedule_search(0.05)

    # --------------------------------------------------------- tastiera -----
    def on_keyboard(self, e: ft.KeyboardEvent) -> None:
        if self._closed:
            return
        key = (e.key or "").lower()
        if self._tag_dialog is not None:
            if key == "escape":
                self._tag_cancel()
            return
        if not e.ctrl:
            if self._text_focused:
                return
            if key == "o":
                self.open_folder()
            elif key == "d":
                self._toggle_detail()
            elif key in ("+", "="):
                self._adjust_threshold(5)
            elif key in ("-", "_"):
                self._adjust_threshold(-5)
            return
        if key == "r":
            self._reload()
        elif key == "e":
            self.open_tag_picker()
        elif self._text_focused:
            # mentre si scrive lascio fare al campo (Ctrl+A = seleziona tutto)
            return
        elif key == "t":
            self.page.run_task(self.title_tf.focus)
        elif key == "a":
            self.page.run_task(self.author_tf.focus)
        elif key == "d":
            self.page.run_task(self.desc_tf.focus)

    # ---------------------------------------------------------- chiusura ----
    def _on_close(self, _e=None) -> None:
        self._shutdown()

    def _shutdown(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._gen += 1
        self._close_pool()
        self._close_db()
        try:
            atexit.unregister(self._shutdown)
        except Exception:
            pass


# ---------------------------------------------------------------- main ------

def print_no_display_hint(port: int) -> None:
    print(f"Nessun display grafico rilevato: avvio in modalità web su "
          f"http://127.0.0.1:{port}\n"
          "Apri l'indirizzo nel browser (anche da un altro dispositivo se "
          "usi --host 0.0.0.0).", file=sys.stderr)


def build_main(args):
    def main(page: ft.Page):
        page.title = TITLE
        page.theme_mode = ft.ThemeMode.DARK
        page.bgcolor = BG
        page.padding = ft.Padding.symmetric(horizontal=16, vertical=12)
        page.spacing = 10
        scheme = ft.ColorScheme(
            primary=BLUE, on_primary="#11111b",
            secondary=PINK, on_secondary="#11111b",
            tertiary=YELLOW, on_tertiary="#11111b",
            surface=BG, on_surface=TEXT,
            on_surface_variant=MUTED,
            surface_container=SURFACE,
            surface_container_high=SURFACE_HI,
            outline=SURFACE_HI, outline_variant=SURFACE,
            error=RED, on_error="#11111b")
        page.dark_theme = ft.Theme(color_scheme=scheme)
        page.theme = ft.Theme(color_scheme=scheme)
        try:
            page.window.width = 1280
            page.window.height = 840
            page.window.min_width = 760
            page.window.min_height = 520
        except Exception:
            pass

        app = CalibreFindFlet(
            page, args.library,
            page_size=args.page_size, min_score=args.min_score,
            preset_title=args.title, preset_author=args.author,
            preset_desc=args.desc, preset_tags=args.tag)
        page.on_keyboard_event = app.on_keyboard
        page.run_task(app.start)

    return main


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Ricerca fuzzy combinabile titolo/autore/descrizione/tag "
                    "su libreria Calibre (GUI Flet)")
    ap.add_argument("-a", "--author", default="", help="query autore (fuzzy)")
    ap.add_argument("-t", "--title", default="", help="query titolo (fuzzy)")
    ap.add_argument("-d", "--desc", default="",
                    help="query dentro le descrizioni (fuzzy)")
    ap.add_argument("-g", "--tag", action="append", default=[], metavar="TAG",
                    help="tag richiesto; ripetibile: i libri devono averli tutti")
    ap.add_argument("-l", "--library",
                    default=os.environ.get(
                        "CALIBRE_LIBRARY",
                        os.path.expanduser("~/CalibreLibrary/metadata.db")),
                    help="path di metadata.db")
    ap.add_argument("-n", "--page-size", "--limit", dest="page_size", type=int,
                    default=PAGE_SIZE,
                    help=f"risultati disegnati per pagina (default {PAGE_SIZE}; "
                         "il totale non è limitato)")
    ap.add_argument("-m", "--min-score", type=float, default=30.0,
                    help="score minimo 0-100 (default 30)")
    ap.add_argument("--web", action="store_true",
                    help="avvia come server web locale invece che finestra "
                         "desktop")
    ap.add_argument("--host", default=None,
                    help="indirizzo del server web (es. 0.0.0.0 per la rete)")
    ap.add_argument("--port", type=int, default=0,
                    help="porta del server web (default: automatica)")
    args = ap.parse_args()

    if not os.path.exists(args.library):
        sys.exit(f"✗ metadata.db non trovato: {args.library}")

    # senza display la finestra desktop non può partire: si usa il browser
    if (not args.web and sys.platform.startswith("linux")
            and not os.environ.get("DISPLAY")
            and not os.environ.get("WAYLAND_DISPLAY")):
        args.web = True
        print_no_display_hint(args.port or 8550)

    view = ft.AppView.WEB_BROWSER if args.web else ft.AppView.FLET_APP
    if args.web and not args.host:
        args.host = "127.0.0.1"
    if args.web and args.port == 0:
        args.port = 8550

    ft.run(build_main(args), view=view, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
