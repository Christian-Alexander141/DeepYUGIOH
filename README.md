# DeepYUGIOH

Autonomes Forschungssystem für Yu-Gi-Oh!-Strategien. Phase 1 ist der **Combo Generator**: Er erzeugt aus dem gesamten Kartenpool Startaufbauten, lässt eine echte Spiel-Engine alle Combos darin suchen und speichert jede validierte Combo im Combo Repository (Database 1).

- Vision und Architektur: [docs/konzept.md](docs/konzept.md)
- Definitionen, Startaufbauten, Suchgrenzen: [docs/combo-generator-spec.md](docs/combo-generator-spec.md)
- Schnittstellen des Generator-Moduls: [docs/schnittstellen.md](docs/schnittstellen.md)

## Einrichtung

Voraussetzungen: Linux, gcc/g++ (C++17), ninja, [uv](https://docs.astral.sh/uv/).

```bash
git submodule update --init --recursive   # Engine, Kartenskripte, Kartendatenbank
uv sync                                   # Python-Umgebung inkl. meson
scripts/build_core.sh                     # baut build/ocgcore/libocgcore.so
uv run deepyugioh import-cards            # Schritte 1-3: Karten, Effekte, Interaktionsgraph
uv run pytest                             # Tests gegen die echte Engine
```

## Benutzung

```bash
# globale Suche: Stufen, Abdeckung und Größe des Suchraums
uv run deepyugioh plan

# globale Suche starten bzw. fortsetzen (setzt am letzten Stand fort)
uv run deepyugioh generate --tiers 1 2 --workers 8 --time-budget 3600
uv run deepyugioh generate --config deepyugioh.example.toml

# einen einzelnen Startaufbau durchsuchen
uv run deepyugioh search \
    --hand "Junk Synchron" "Foolish Burial" \
    --deck Doppelwarrior Tuningware \
    --extra "Junk Warrior" "Stardust Dragon" \
    --opponent standard --lines

# Database 1 abfragen
uv run deepyugioh combos --card "Junk Synchron" --lines
uv run deepyugioh combos --positive opp_field_loss
```

Karten können per englischem Namen oder Passcode angegeben werden.

## Aufbau

| Pfad | Inhalt |
|---|---|
| [src/deepyugioh/contracts/](src/deepyugioh/contracts/) | Verträge zwischen den Modulen: Datentypen und Schnittstellen |
| [src/deepyugioh/cards/](src/deepyugioh/cards/) | Card Knowledge System: Import, Skriptanalyse, Interaktionsgraph (Schritte 1–3) |
| [src/deepyugioh/generator/](src/deepyugioh/generator/) | Combo Generator: Startaufbauten, Suche, Validierung, Jobs (Schritte 4–5) |
| [src/deepyugioh/engine/](src/deepyugioh/engine/) | ctypes-Anbindung an ygopro-core |
| [src/deepyugioh/repository/](src/deepyugioh/repository/) | Database 1 – Combo Repository (Schritt 6) |
| [third_party/](third_party/) | ygopro-core, CardScripts, BabelCDB (Submodules) |

Erzeugte Daten liegen in `data/` (nicht versioniert):
- `cards.sqlite`: Karten und Graph,
- `combos.sqlite`: Database 1,
- `generator.sqlite`: Fortschritt der globalen Suche.

## Lizenz

ygopro-core und die CardScripts stehen unter der AGPL-3.0. Weil dieses Projekt die Engine einbindet, gilt die AGPL auch für das Gesamtsystem, sobald es weitergegeben oder als Netzwerkdienst betrieben wird. Yu-Gi-Oh! ist eine Marke von Shueisha und Konami. Dieses Projekt ist damit nicht verbunden.
