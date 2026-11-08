# Combo Generator – Spezifikation (Phase 1)

Stand: 8. November 2026 · gehört zu [konzept.md](konzept.md) · Schnittstellen: [schnittstellen.md](schnittstellen.md)

Dieses Dokument hält die verbindlichen Definitionen, den Suchraum und die Suchgrenzen des Combo Generators fest. Werte in `code` sind Voreinstellungen aus [deepyugioh.example.toml](../deepyugioh.example.toml).

## 1. Entscheidungen

| Thema | Entscheidung |
|---|---|
| Spiel-Engine | ygopro-core (edo9300), als Submodule eingebunden, wird geforkt |
| Kartenskripte / Kartendaten | ProjectIgnis CardScripts + BabelCDB, als Submodules auf festen Commits |
| Suchraum | global: jede Karte darf Teil einer Combo sein, kein Deckprofil |
| Ausgabe | ALL: jede gültige Combo wird gespeichert, keine Stärkebewertung im Generator |
| Autorität | Nur die Engine entscheidet, ob eine Combo funktioniert. Jede gespeicherte Combo wird per Replay validiert. |
| Architektur | Der Generator ist ein Modul mit festen Schnittstellen ([schnittstellen.md](schnittstellen.md)) |

## 2. Was ist eine Combo?

Eine Combo ist eine Folge von Aktionen von Spieler 0 innerhalb eines Zuges, die

1. **höchstens 5 bestimmte Karten benötigt** (`max_cards = 5`), längere Ketten gelten als zu anfällig,
2. **mindestens 2 bestimmte Karten benötigt** (`min_cards = 2`), sonst ist es der Effekt einer einzelnen Karte,
3. **mindestens einen positiven Nutzen erzeugt** und
4. **keine überflüssigen Karten enthält**: Wenn eine echte Teilmenge ihrer Karten mindestens denselben Nutzen erreicht, ist sie keine eigene Combo (`drop_redundant = true`).

**Benötigte Karten** sind:
- Handkarten, die benutzt wurden,
- Karten, die aus dem Deck genommen wurden (gesucht, geschickt, beschworen, aber nicht gezogen),
- Karten aus dem Extra Deck.

Tokens und Karten des Gegners zählen nicht.

**Nutzen** wird als Differenz zwischen Start- und Endzustand gemessen ([combo.py](../src/deepyugioh/generator/search/combo.py)). Positiv in mindestens einem Kriterium heißt Combo:

| Kriterium | Bedeutung |
|---|---|
| `lp_gain` | eigene LP gewonnen |
| `opp_lp_loss` | Schaden beim Gegner |
| `hand_delta` | Handkarten plus |
| `field_delta` | eigene Feldkarten plus (Monster- und Zauber-/Fallenzonen) |
| `opp_field_loss` | gegnerische Feldkarten entfernt (braucht ein Gegnerfeld, siehe 3) |
| `opp_hand_loss` | gegnerische Handkarten entfernt (braucht Gegnerhandkarten) |
| `wins` | Duell gewonnen |

`card_advantage` (Hand + Feld) wird mitgespeichert, ist aber kein eigenes Kriterium. Der Generator speichert nur Rohwerte. Die Bewertung gehört in Database 2.

Ein Endzustand ist jeder Zeitpunkt, an dem Spieler 0 in der Main Phase wieder frei handeln kann, sowie das Zugende nach der End Phase.

**Wann gilt eine Combo als dieselbe?** Die Identität (`signature`) bilden:
- die benötigten Karten,
- die eigene Seite des Endzustands (Feld, Hand, Friedhof, verbannte Karten, LP),
- die **Veränderung** der Gegnerseite (verlorene Karten, LP-Verlust).

Eine Combo, die den Gegner nicht berührt, ist deshalb gegen jedes Gegnerfeld dieselbe. Liegt am Ende kein Link-Monster auf dem Feld, gelten die fünf Hauptmonsterzonen als gleichwertig. Wird eine Combo erneut gefunden, wird sie nur mit dem neuen Lauf verknüpft. So bleibt erhalten, in welchen Startaufbauten sie funktioniert.

## 3. Startaufbauten

Die Engine kann nur konkrete Spielzustände ausführen. Ein **Startaufbau** (`StartSetup`) legt fest:
- welche Karten Spieler 0 in Hand, Deck und Extra Deck hat,
- was der Gegner auf Feld und Hand hat,
- welche Regeln gelten.

Die globale Suche wird deshalb in sehr viele kleine Suchen zerlegt. Jede beginnt bei einem Startaufbau, den der Startaufbau-Generator ([candidates.py](../src/deepyugioh/generator/candidates.py)) aus dem Interaktionsgraphen erzeugt.

**Feste Regeln für jeden Startaufbau:**
- Master Rule 5, 8000 LP.
- Spieler 0 beginnt Zug 1 in Main Phase 1. Die Hand wird direkt gesetzt, es gibt keine Ziehung.
- Fester Seed, Deck pseudo-gemischt. Jede Linie ist damit wiederholbar.
- Der Gegner handelt nie (`DUEL_SIMPLE_AI`).

**Gegnerfelder** (`opponent_boards`): Jeder Kandidat wird gegen jedes konfigurierte Feld gesucht.

| Feld | Inhalt | Wozu |
|---|---|---|
| `empty` | nichts | Karten mit „wenn der Gegner keine Monster kontrolliert“ |
| `standard` | 1 offenes Monster (Gene-Warped Warwolf), 1 gesetzte Karte (Pot of Greed), 2 Handkarten (Mystical Elf) | Removal, Handzerstörung und „wenn der Gegner ein Monster kontrolliert“ |

Gegnerkarten sind nur Normale Monster und Normale Zauber, damit sie in unserem Zug nie aktiviert werden können.

**Aufbau eines Kandidaten** aus 1–2 Kernkarten:

| Bereich | Inhalt |
|---|---|
| Hand | Kernkarten, generische Helfer für ihre Bedürfnisse (z. B. Foolish Burial für Karten mit Friedhofseffekt oder Friedhofsziel), bei Extra-Deck-Kernkarten die namentlich genannten Materialien |
| Deck | zweite Kopie jeder Kernkarte, dazu was die Kernkarten holen können (NAMED/RESTRICTED-Nachbarn, `max_deck_neighbors = 15`) |
| Extra Deck | verwandte Extra-Deck-Monster, namentliche zuerst (`max_extra = 15`) |

Wird gekappt, ist der Lauf als `neighbourhood_truncated` unvollständig markiert.

**Helferrollen** ([graph.py](../src/deepyugioh/cards/graph.py)): Pro Rolle gibt es einen Vertreter. Das ist der generische, ungefilterte Zauber mit dem kürzesten Skript, also den wenigsten Zusatzbedingungen.

| Rolle | Vertreter |
|---|---|
| `SEND_GY_FROM_DECK` | Foolish Burial |
| `SPSUMMON_FROM_GY` | Monster Reborn |
| `BANISH_FROM_GY` | Soul Release |

**Stufen.** Gemeinsam bilden sie den vollständigen Suchraum, die Stufe legt nur die Reihenfolge fest. Zahlen bei 2 Gegnerfeldern, Graph-Version `c9e376f78350a602`:

| Stufe | Kernkarten | Kandidaten |
|---|---|---|
| 1 | jede Startkarte allein (13.521) | 27.042 |
| 2 | NAMED/RESTRICTED-Paare: eine Karte nennt die andere oder ihren Archetyp | 133.164 |
| 3 | CONSTRAINED-Paare: generischer Effekt mit Filter, z. B. Junk Synchron → Level-≤2-Monster | 1.502.412 |
| 4 | GENERIC-Paare: „irgendein Monster“ | 9.810.700 |

**Durchsatz** bei ersten Messungen mit 4 Workern: etwa 1,4 Kandidaten/s. Das ergibt grob:
- Stufe 1: Stunden,
- Stufe 2: etwa 1 Tag,
- Stufe 3: Wochen,
- Stufe 4: Monate.

Läufe sind deshalb fortsetzbar (`deepyugioh generate` setzt am Cursor fort), und `deepyugioh plan` zeigt die Abdeckung pro Stufe.

## 4. Suchgrenzen und Pruning

Innerhalb dieser Grenzen ist die Suche erschöpfend. Jede überschrittene Grenze wird im Lauf protokolliert (`runs.incomplete`). Ein Lauf ist nur dann `complete`, wenn nichts davon eingetreten ist.

| Grenze | Wert | Bei Überschreitung |
|---|---|---|
| `max_depth` | 80 Entscheidungen pro Linie | Ast endet, Lauf unvollständig |
| `max_nodes` | 20.000 Entscheidungspunkte | Suche bricht ab, Lauf unvollständig |
| `time_limit` | 120 s pro Startaufbau | Suche bricht ab, Lauf unvollständig |
| `max_choices` | 128 Optionen pro Kartenauswahl | Optionen abgeschnitten, Lauf unvollständig |
| `max_cards` | 5 | Ast wird verworfen (Definition, nicht unvollständig) |

Diese Pruning-Regeln verkleinern den Suchraum:

| Regel | Annahme | Abschaltbar |
|---|---|---|
| Zonenwahl: Gibt es ein Link-Monster im Startaufbau (`branch_zones`), wird zwischen Hauptzone und jeder Extra-Monsterzone verzweigt. Liegt ein Link-Monster auf dem Feld, über alle freien Monsterzonen. Sonst gilt die erste freie Zone. | Ohne Link-Pfeile sind die Hauptzonen gleichwertig. Spalteneffekte werden ignoriert. | `branch_zones` pro Aufbau |
| Zauber-/Fallenzonen: erste freie Zone | Spalteneffekte sind selten | nein |
| Kampfposition: offene Angriffsposition bevorzugt | Position ist für Combos selten relevant | nein |
| Setzen (Monster/Zauber/Fallen) wird nicht ausprobiert | Setzen erzeugt selten Combo-Nutzen | `allow_mset`, `allow_sset` |
| Positionswechsel, Hand mischen, Battle Phase: nie | kein Combo-Nutzen in Zug 1 | nein |
| Gleiche Kopien in Hand/Deck/Friedhof gelten als eine Wahl | Kopien sind austauschbar | nein |
| Transpositionen: gleicher sichtbarer Zustand und gleiche Menge an Aktivierungen/Beschwörungen gilt als derselbe Knoten | versteckter Zustand entsteht fast immer durch Aktivierungen oder Beschwörungen | `transpositions` |
| Tributwahl: nur minimale Tributmengen | Über-Tributieren bringt nichts | nein |

Nicht unterstützt (der Ast wird als `unsupported:*` protokolliert): **Karte ansagen** (`MSG_ANNOUNCE_CARD`). Typ- und Attributansagen nehmen die erste verfügbare Option.

## 5. Ablauf eines Suchlaufs

1. **Suche** ([dfs.py](../src/deepyugioh/generator/search/dfs.py)): Tiefensuche über alle Antworten, die die Engine anbietet. An jedem Endzustand wird der Nutzen gemessen.
2. **Filter**: Funde mit zu wenigen Karten und Funde mit überflüssigen Karten werden entfernt.
3. **Validierung** ([validate.py](../src/deepyugioh/generator/search/validate.py)): Jede Linie wird in einem frischen Duell erneut abgespielt. Der Endzustand muss exakt übereinstimmen.
4. **Speicherung** über `ComboSink` in Database 1, zusammen mit Startaufbau, Lauf, Commits und Graph-Version.

## 6. Verzweigung und Core-Fork

Bisher wird per **Replay** verzweigt. Messung vom 8.11.2026 an einer Linie mit 24 Entscheidungen:

| Methode | Kosten pro Verzweigung |
|---|---|
| Replay | ca. 15 ms, davon ca. 12 ms Duell-Erstellung durch das Laden der Lua-Skripte |
| Prozess-Fork (`fork(2)`, Copy-on-Write) | ca. 1,9 ms |

Die eigentliche Schwierigkeit eines nativen Snapshots im Core-Fork ist der **Lua-Zustand**: Effekte, Filter und Karten-Tabellen sind Lua-Objekte mit Closures, und dafür gibt es keine fertige Kopierfunktion. Empfehlung:
- zuerst `fork(2)` als Snapshot,
- den Core-Fork für Dinge, die nur der Core liefern kann: stabile Karten-Instanz-IDs und Abfrage der Einmal-pro-Zug-Zähler und Beschwörungssperren.

## 7. Offene Punkte

- **Austauschbare Karten (Substitution):** Combos, die mit einem Helfer-Vertreter gefunden wurden, gegen alle Karten derselben Rolle erneut validieren. Das spart einen großen Teil der Stufen 3 und 4.
- **Neue Kartenskripte:** Ändert sich der Graph (neue Karten), ändert sich die Kandidatenreihenfolge. Erledigte Kandidaten werden anhand der Setup-ID trotzdem übersprungen, aber neu hinzugekommene liegen verstreut. Ein Delta-Modus „nur Kandidaten mit neuen Karten“ fehlt noch.
- **Spalteneffekte** und Zonenwahl für Zauber/Fallen.
- **Karte ansagen** unterstützen (Kandidatenliste aus dem Kartenpool).
- **Schnellere Verzweigung** (`fork(2)`, siehe 6).
- Kopien derselben Karte werden über den Code identifiziert. Exakt wird das erst mit Instanz-IDs aus dem Fork.
