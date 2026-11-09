# Schnittstellen des Combo Generators

Stand: 8. November 2026 · Datenformat-Version `SCHEMA_VERSION = 2`

Der Combo Generator ist ein eigenständiges Modul ([src/deepyugioh/generator/](../src/deepyugioh/generator/)). Er hängt nur von den **Verträgen** in [src/deepyugioh/contracts/](../src/deepyugioh/contracts/) ab und nie von den Interna anderer Module. Andere Module wie Analyzer oder Deckbauer nutzen ihn genauso nur über diese Verträge.

```
 Card Knowledge System                Combo Generator                      Database 1
 (deepyugioh.cards)                   (deepyugioh.generator)               (deepyugioh.repository)
┌──────────────────────┐  CardPool   ┌───────────────────────────┐ ComboSink ┌──────────────────┐
│ CardStore            │────────────▶│ ComboGenerator            │──────────▶│ ComboRepository  │
│ CardGraph            │ Interaction │  search(StartSetup)       │ RunRecord │                  │
│                      │ Graph       │  run(tiers, max_jobs …)   │ ComboRec. │                  │
└──────────────────────┘────────────▶│  coverage()               │           └────────┬─────────┘
                                     └───────────────────────────┘                    │ ComboReader
              GeneratorConfig (Code / TOML) ──▶ │  │ ──▶ ProgressListener          ▼
                                                │                          Analyzer System (Phase 2)
                                      intern: candidates, search, engine
```

## 1. Eingänge

### `CardPool` ([contracts/cards.py](../src/deepyugioh/contracts/cards.py))

Kartendaten für die Engine. Implementiert von `cards.store.CardStore`.

| Methode | Zweck |
|---|---|
| `get(code) -> CardInfo \| None` | Kartendaten wie in cards.cdb |
| `resolve(ref) -> CardInfo` | Passcode oder exakter englischer Name, sonst `KeyError` |
| `name(code) -> str` | Anzeigename |

### `InteractionGraph` ([contracts/cards.py](../src/deepyugioh/contracts/cards.py))

Schritt 3, die Interaction Discovery. Wird nur für globale Läufe gebraucht. Implementiert von `cards.graph.CardGraph`.

| Methode | Zweck |
|---|---|
| `version()` | Ändert sich mit jedem Neuaufbau; die Kandidatenreihenfolge ist nur innerhalb einer Version stabil |
| `starters()` | Alle Karten, die eine Combo beginnen dürfen |
| `interactions()` | NAMED- und RESTRICTED-Interaktionen in fester Reihenfolge |
| `iter_generic(grade)` | CONSTRAINED- bzw. GENERIC-Interaktionen, lazy, weil es Millionen sind |
| `neighbors(code)` | Spezifische Interaktionen einer Karte |
| `related_extra(code)` | Verwandte Extra-Deck-Monster, die spezifischsten zuerst |
| `needs(code)` / `enablers` | Generische Helferrollen und ihr Vertreter (z. B. Foolish Burial) |

Ein anderer Kandidatengenerator kann `InteractionGraph` umgehen, indem er direkt eine `CandidateSource` übergibt (siehe 3).

## 2. Ausgänge

### `ComboSink` und `ComboReader` ([contracts/combos.py](../src/deepyugioh/contracts/combos.py))

Implementiert von `repository.combo_db.ComboRepository` (SQLite, `data/combos.sqlite`).

| Schnittstelle | Methode | Zusage |
|---|---|---|
| `ComboSink` | `store_run(run, combos) -> StoreResult` | Eine Transaktion pro Lauf. Nur `validated`-Combos werden gespeichert, bekannte nur verknüpft. |
| `ComboReader` | `get(id)`, `iter_combos(card=, max_cards=, positive=, limit=)`, `count()` | Sortiert nach Kartenzahl, dann Linienlänge |

### `ComboRecord`

Das einzige Combo-Format, das den Generator verlässt. Es sagt, **was möglich ist**, und nie, wie gut es ist.

| Feld | Inhalt |
|---|---|
| `signature` | Identität: benötigte Karten + Endzustand relativ zum Start. Unabhängig vom Startaufbau und von unberührten Gegnerkarten. |
| `hand`, `deck`, `extra` | Benötigte Karten nach Herkunft. `cards` und `card_count` werden daraus berechnet. |
| `benefit` | Rohwerte: `lp_gain`, `opp_lp_loss`, `hand_delta`, `field_delta`, `opp_field_loss`, `opp_hand_loss`, `card_advantage`, `wins` |
| `positive` | Kriterien mit Wert > 0 |
| `end_board` | Sichtbarer Zustand beider Spieler danach |
| `line` | `LineStep(decision, label, response)`. `response` ist die Engine-Antwort in Hex und macht die Linie wiederholbar. |
| `setup` | Der `StartSetup`, in dem die Linie abgespielt wird |
| `validated` | Unabhängig nachgespielt, mit identischem Ergebnis |

### `RunRecord`

Ein Suchlauf über einen Startaufbau. Er enthält Provenienz und Abdeckung:
- Setup, Kandidat und Stufe,
- die Grenzen der Suche,
- `complete` und `incomplete` (Grund → Anzahl, inklusive gekappter Nachbarschaft),
- `pruned`,
- die Commits von Engine, Skripten und Kartendatenbank sowie die Graph-Version.

## 3. Steuerung

### `StartSetup` ([contracts/setups.py](../src/deepyugioh/contracts/setups.py))

Der konkrete Spielzustand, von dem eine Suche ausgeht:
- `hand`, `deck`, `extra` (sortiert gespeichert, die Reihenfolge ist egal),
- `opponent: OpponentBoard`,
- `seed`, `starting_lp`, `master_rule` (nur 5),
- `branch_zones`.

`id` ist ein stabiler Hash.

### `ComboGenerator` ([generator/service.py](../src/deepyugioh/generator/service.py))

```python
from deepyugioh.generator import ComboGenerator, GeneratorConfig

gen = ComboGenerator(pool, sink, GeneratorConfig.load(path), graph=graph)
gen.search(setup)                        # ein Startaufbau -> SearchReport(run, combos, run_id, new_combos)
gen.run(tiers=[1, 2], max_jobs=10_000,   # global, fortsetzbar, parallel (config.workers)
        time_budget=3600, progress=listener,
        should_stop=event.is_set)        # optional: z. B. Stopp-Knopf der Test-GUI
gen.coverage()                           # {stufe: total, cursor, complete, incomplete, error, combos_new}
```

- Für parallele Läufe (`workers > 1`) braucht `ComboGenerator` eine picklebare `pool_factory`, z. B. die Klasse `CardStore`. Jeder Worker-Prozess baut sich damit seinen eigenen `CardPool`, und der Generator kennt keine konkrete Implementierung.
- `run()` setzt dort fort, wo der letzte Lauf aufgehört hat. Der Zustand liegt in `JobStore` (`data/generator.sqlite`). Pro Stufe gibt es einen Cursor, und bereits erledigte Kandidaten werden übersprungen.
- Ein Fehler in einem Kartenskript bricht den globalen Lauf nicht ab. Der Job wird mit `status = error` und Fehlermeldung gespeichert.
- `should_stop()` wird vor jeder neuen Suche abgefragt. Liefert es `True`, startet keine neue Suche mehr; laufende werden abgeschlossen und gespeichert, sodass ein späterer Lauf lückenlos fortsetzt.

### `GeneratorConfig` ([generator/config.py](../src/deepyugioh/generator/config.py))

Wird im Code gebaut oder aus TOML geladen. Alle Schlüssel mit ihren Voreinstellungen stehen in [deepyugioh.example.toml](../deepyugioh.example.toml). Unbekannte Schlüssel führen zu einem Fehler.

`check()` prüft die Werte auf Widersprüche (z. B. `min_cards > max_cards`, unbekannte Gegnerfelder) und wirft `ValueError`. Die dauerhafte lokale Konfiguration liest und schreibt [settings.py](../src/deepyugioh/settings.py) (`generator_config()`, `save_generator_config(cfg)`); sie liegt in `deepyugioh.toml` und enthält zusätzlich den Schalter `test_mode`.

### `CandidateSource` ([generator/candidates.py](../src/deepyugioh/generator/candidates.py))

Der austauschbare Startaufbau-Generator. Er muss jede Stufe in fester Reihenfolge liefern, sonst funktioniert das Fortsetzen nicht.

| Methode | Zweck |
|---|---|
| `version()` | Ändert sich, wenn sich die Reihenfolge ändert |
| `candidates(tier) -> Iterator[Candidate]` | `Candidate(setup, tier, origin, truncated)` |
| `tier_size(tier) -> int \| None` | Für die Abdeckungsanzeige |

### `ProgressListener`

`on_job(JobReport)` wird nach jedem Suchlauf aufgerufen. `JobReport` enthält Kandidat, Stufe, Position, Status, gefundene und neue Combos, Laufzeit und Fehler.

## 4. Regeln für Änderungen

- Was in `deepyugioh.generator.__all__` und `deepyugioh.contracts` steht, ist Schnittstelle. Alles andere (`generator.search`, `generator.execution`, `engine`) ist intern.
- Ändert sich das gespeicherte Format von `ComboRecord` oder `RunRecord`, wird `SCHEMA_VERSION` erhöht. Database 1 verweigert dann das Öffnen einer Datenbank mit anderer Version (`SchemaMismatch`), statt sie stillschweigend falsch zu lesen.
- Die Konformität der Implementierungen zu den Protokollen prüft [tests/test_generator.py](../tests/test_generator.py).
