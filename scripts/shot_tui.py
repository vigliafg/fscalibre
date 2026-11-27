#!/usr/bin/env python3
"""Screenshot headless della TUI via Textual pilot (uso interno).

Salva docs/tui.svg (formato nativo di Textual) e lo converte in docs/tui.png
con Chromium headless. Per la GUI Flet: avvia `calibrefind_flet.py --web`,
apri la pagina in Chromium sotto Xvfb e cattura con scrot (vedi README)."""
import asyncio
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import calibrefind as cf  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
LIB = str(Path.home() / "CalibreLibrary/metadata_big.db")


async def main():
    cf.CalibreFind._preset_author = "gibson"
    cf.CalibreFind._preset_title = "mancer"
    app = cf.CalibreFind(LIB, page_size=30, min_score=25)
    async with app.run_test(size=(118, 30)) as pilot:
        for _ in range(300):          # attende il caricamento della libreria
            if getattr(app, "books", None):
                break
            await asyncio.sleep(0.2)
        for _ in range(150):          # attende che la ricerca dia risultati
            if getattr(app, "_n_hits", 0) > 0:
                break
            await asyncio.sleep(0.2)
        await pilot.pause(2.0)
        out = ROOT / "docs" / "tui.svg"
        out.parent.mkdir(exist_ok=True)
        app.save_screenshot(str(out))
        subprocess.run(["chromium", "--headless", "--no-sandbox", "--disable-gpu",
                        f"--screenshot={ROOT/'docs/tui.png'}",
                        "--window-size=1460,800", "--hide-scrollbars", str(out)],
                       check=False, capture_output=True)
        print("salvati", out, "e", ROOT / "docs" / "tui.png")

asyncio.run(main())
