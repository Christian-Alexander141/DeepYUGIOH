# Stufe 2: Analyzer System, Database 2 und Qualitätsfilter

Stand: 9. November 2026 · Status: Schritte 2.1 bis 2.3 umgesetzt (siehe Abschnitte 13 und 14), der Rest ist Konzept

Stufe 2 bewertet die Combos, die Phase 1 in Database 1 gefunden hat: wie stark sie sind, in welcher Situation, und wie gut sie zu anderen Combos und Decks passen. Sie macht das für den Deckbauer sichtbar und filterbar. Grundsatz aus dem [Gesamtkonzept](konzept.md):

> Die Combo-Datenbank speichert, was möglich ist. Die Analyzer-Datenbank speichert, wie gut es ist. Der Deckbauer entscheidet, was davon zusammengehört.

Database 1 wird von Stufe 2 nur gelesen (`ComboReader`) und nie verändert.

## 1. Ziele

1. **Allgemeine Spielstärke** jeder Combo als ein Wert, der Combos vergleichbar macht.
2. **Situationswerte**: Spielbeginn (als Erster), Durchbrechen (als Zweiter gegen ein Feld) und späteres Spiel (mit Ressourcen im Friedhof, wenigen Handkarten). Weitere Situationen müssen ergänzt werden können.
3. **Deckspezifität**: wie sehr eine Combo auf ein bestimmtes Deck angewiesen ist (Archetyp, Typ, Attribut, Sperren). Je generischer, desto allgemeiner einsetzbar. Deckspezifisch ist nicht schlecht, es muss aber erkennbar sein, *wofür* eine Combo taugt.
4. **Kompatibilität**: welche Combos sich ein Deck teilen können und welche sich ausschließen.
5. **Gefilterte Combo-Mengen** für den Deckbauer.
6. **Genauigkeit vor Geschwindigkeit**: Ziel ist eine übermenschliche Spielstärke. Wo die Engine eine Frage beantworten kann, entscheidet die Engine und keine Näherung. Robustheit wird per Engine geprüft.

## 2. Entscheidungen

| Frage | Entscheidung |
|---|---|
| Späteres Spiel | Phase 1 wird um Startaufbauten für spätere Spielsituationen erweitert (Friedhof, verbannte Karten, Feld, kleinere Hand). |
| Robustheit | Direkt per Engine: Der Gegner bekommt Handtraps und unterbricht, wir suchen die beste Antwort. Keine Näherung als Endergebnis. |
| Datenbanken | **Eine** Database 2 (`data/analysis.sqlite`) mit allen Bewertungen *und* den gespeicherten Ergebnissen der Filterprofile. Ein Filterprofil lässt sich bei Bedarf als eigene Datei exportieren. So wird nichts doppelt gespeichert und kein gefilterter Stand veraltet unbemerkt. |
| Gewichte | Feste Standardgewichte in der Konfiguration. Sie werden später am Simulator kalibriert und sind in der GUI änderbar, auch zu Testzwecken. |

## 3. Bewertungsprofil statt einer einzigen Zahl

Jede Combo bekommt ein **Bewertungsprofil** aus mehreren **Achsen**. Pro Achse werden gespeichert:

- **Rohwerte**: messbare Fakten, z. B. „2 Störungen auf dem Endfeld“ oder „3 Handkarten nötig“.
- **Wert 0–100**: normiert als Perzentil über alle bewerteten Combos derselben Kartenmenge. 80 heißt „besser als 80 % der Combos“.
- **Begründung**: kurzer Text, warum der Wert so ist, für den Deckbauer und zum Prüfen.
- **Version** des Analyzers, damit alte und neue Bewertungen nie vermischt werden.

Neue Anforderungen kommen als neue Achse hinzu, ohne bestehende zu ändern.

| Achse | Misst | Grundlage |
|---|---|---|
| **Ergebnis** | Endfeld: Monster (ATK, Stufen), Störungen für den gegnerischen Zug (Effekte, die im Zug des Gegners nutzbar sind, Negates, Zerstörung, Verbannung), Schutz, Kartenvorteil, Verluste des Gegners, LP-Schaden, Sieg | Endfeld in Database 1 und die Effect Signatures der Endfeldkarten |
| **Einsatz** | benötigte Karten, davon Handkarten, LP-Kosten, Normalbeschwörung verbraucht, Once-per-Turn-Effekte verbraucht, Länge der Linie | Database 1 |
| **Konsistenz** | Wahrscheinlichkeit, die nötigen Handkarten zu ziehen (Deck mit 40 Karten, 3 Kopien, 5 Karten als Erster, 6 als Zweiter). Mehrere Starter für dasselbe Ergebnis (*Combo-Familie*) erhöhen die Chance. | Hypergeometrische Verteilung über die Familien |
| **Robustheit** | Was von der Combo übrig bleibt, wenn der Gegner mit Handtraps unterbricht: das garantierte Endfeld je Handtrap und über alle Handtraps | **Engine**, siehe Abschnitt 6 |
| **Deckspezifität** | Anforderungsprofil, Sperren, Generizität (Abschnitt 4) | Kartendaten, Skripte (Effect Signatures) |
| **Situationen** | Spielbeginn, Durchbrechen, späteres Spiel (Abschnitt 5) | Kombination der Achsen und eigene Startaufbauten |

**Allgemeine Spielstärke** = gewichtete Kombination aus Ergebnis, Einsatz, Konsistenz und Robustheit. Die Standardgewichte stehen in `[analysis.weights]`, sind in der GUI änderbar und werden später am Simulator kalibriert (Phase 4). Gespeichert werden immer die Achsenwerte. Der Gesamtwert wird aus ihnen und den Gewichten berechnet, mit denen er erzeugt wurde. Andere Gewichte verlangen also keine neue Analyse.

## 4. Deckspezifität

Drei Teile, die gemeinsam zeigen, was eine Combo dem Deck abverlangt:

1. **Anforderungsprofil**: Merkmale, die das Deck mitbringen muss, abgeleitet aus den benötigten Karten und den Filtern ihrer Effekte, etwa „Typ Drache“, „Archetyp Blue-Eyes“, „Attribut LIGHT“, „Level 4“. Unterschieden wird zwischen *Karten der Combo* (müssen im Deck sein) und *Bedingungen ihrer Effekte* (das Deck braucht passende weitere Karten).
2. **Sperren**: Viele Effekte schränken den Rest des Zuges ein, z. B. „du kannst nur noch Drachen spezialbeschwören“ oder „nur einmal pro Zug“. Sperren sind der Hauptgrund, warum zwei Combos nicht zusammenpassen. Sie werden aus den Skripten ausgelesen, als Erweiterung der Effect Signatures um Effekte wie `EFFECT_CANNOT_SPECIAL_SUMMON` mit Zielfilter.
3. **Generizität 0–100**:
   - *Pro Karte*: wie stark sie an einen Deckkontext gebunden ist. Allgemeine Karten ohne Archetyp- und Typbezug sind hoch, Archetypkarten mit Sperre niedrig.
   - *Pro Combo*: aus ihren Karten, ihren Sperren und dem Anteil anderer Combos in Database 1, mit denen sie ohne Konflikt in ein Deck passt.

Für den Deckbauer gibt es zusätzlich die **Passung zu einem Deckprofil**. Ein Deckprofil ist eine Menge von Merkmalen, z. B. „Drachen, LIGHT, Blue-Eyes“. Eine Drachen-Combo hat eine niedrige Generizität, aber im Profil „Drachendeck“ eine hohe Passung. So wird sichtbar, wofür eine Combo taugt, ohne sie abzuwerten.

**Kompatibilität zwischen Combos** wird als Kanten gespeichert:

- gemeinsame Karten (spart Plätze im Deck, oft Synergie),
- gemeinsamer Archetyp oder gemeinsame Merkmale,
- Sperrkonflikte (Combo A sperrt, was Combo B braucht).

Gespeichert werden nur Kanten mit einer dieser Beziehungen, nicht alle Paare. Das wird die wichtigste Eingabe für den Deckbauer.

## 5. Situationen

| Situation | Bedeutung | Bewertet über |
|---|---|---|
| **Spielbeginn** | als Erster, leeres gegnerisches Feld, volle Hand | Ergebnis (vor allem Störungen), Konsistenz mit 5 Handkarten, Robustheit |
| **Durchbrechen** | als Zweiter gegen ein aufgebautes Feld | Verluste des Gegners und Schaden gegen die Gegnerfelder (Phase 1 sucht schon gegen `standard`; dazu kommen stärkere Gegnerfelder mit Störungen), Konsistenz mit 6 Handkarten |
| **Späteres Spiel** | Ressourcen im Friedhof oder verbannt, wenige Handkarten, Karten auf dem Feld | neue Startaufbauten in Phase 1 (Abschnitt 7), Kartenvorteil, Unabhängigkeit von der Hand |

Situationen sind Einträge in der Konfiguration, die aus Achsen und Gewichten bestehen. Weitere lassen sich ohne Codeänderung ergänzen, solange ihre Achsen existieren.

## 6. Robustheit per Engine

Heute beantwortet die Suche jede Entscheidung des Gegners mit „passen“ (`ComboSearch._opponent`). Für die Robustheit wird daraus eine **adversariale Suche**:

1. Der Gegner bekommt eine Handtrap aus einem Katalog auf die Hand. Der Katalog enthält zunächst die gängigen generischen: Ash Blossom & Joyous Spring, Effect Veiler, Infinite Impermanence, Nibiru, the Primal Being, Droll & Lock Bird, Ghost Belle & Haunted Mansion, Ghost Ogre & Snow Rabbit, D.D. Crow, PSY-Framegear Gamma, Maxx "C".
2. An jeder Stelle, an der der Gegner die Handtrap einsetzen *kann*, verzweigt die Suche: einsetzen oder nicht.
3. Nach jeder Unterbrechung sucht die Engine wie bisher die **beste Fortsetzung** mit unseren Karten aus dem Startaufbau der Combo.
4. Ergebnis je Handtrap: das **garantierte Endfeld**, also das Minimum über die Wahlen des Gegners vom Maximum über unsere Antworten. Dazu kommt der kritische Schritt, an dem die Unterbrechung am meisten kostet.
5. Die Achse Robustheit vergleicht das garantierte Endfeld mit dem ungestörten Ergebnis. Für die allgemeine Spielstärke werden die Handtraps nach ihrer Verbreitung gewichtet (Standardgewichte, später kalibriert).

Das ist rechenintensiv: eine Suche pro Combo und Handtrap. Der Robustness Analyzer läuft deshalb wie der Generator **fortsetzbar und parallel**, mit eigenem Fortschritt. Er bearbeitet die Combos nach ihrem vorläufigen Stärkewert (ohne Robustheit) in absteigender Reihenfolge, die stärksten zuerst. Es wird keine Combo ausgelassen, nur später geprüft. Unvollständige Robustheitssuchen werden wie in Phase 1 als unvollständig markiert, nie als bestanden.

## 7. Erweiterung von Phase 1: spätere Spielsituationen

- `StartSetup` bekommt zusätzliche Zonen für Spieler 0: Friedhof, verbannt, Feld. Leere Zonen werden nicht serialisiert. Die IDs aller bisherigen Startaufbauten bleiben dadurch gleich, ebenso Fortschritt und Database 1.
- Die Engine-Anbindung legt Karten direkt in diese Zonen.
- Eine neue Kandidatenquelle erzeugt Spielmitte-Aufbauten: kleinere Hand (2–3 Karten), Ressourcen im Friedhof, z. B. aus den Friedhofseffekten der Effect Signatures. Optional kommen eigene Karten auf das Feld.
- Combos daraus kommen wie alle anderen in Database 1. Ihr Startaufbau zeigt, dass sie eine spätere Situation voraussetzen. Die Situation „späteres Spiel“ bewertet sie.
- Die Combo-Definition wird um Karten erweitert, die zu Beginn schon im Friedhof oder auf dem Feld liegen. Sie zählen als benötigte Karten mit eigener Herkunft.

## 8. Database 2 (`data/analysis.sqlite`)

| Tabelle | Inhalt |
|---|---|
| `analyses` | ein Lauf eines Analyzers: Name, Version, Einstellungen, Zeit, Quelle (Pfad und Schema von Database 1) |
| `combo_axes` | pro Combo (`signature`) und Achse: Rohwerte (JSON), Wert 0–100, Begründung, Analyzer-Version, Lauf |
| `card_profiles` | pro Karte: Merkmale, Sperren, Generizität |
| `combo_profiles` | pro Combo: Anforderungsprofil, Sperren, Familie |
| `compatibility` | Kanten zwischen Combos: Art (gemeinsame Karten, Archetyp, Sperrkonflikt), Stärke |
| `robustness_jobs` | Fortschritt des Robustness Analyzers: Combo, Handtrap, Status, garantiertes Endfeld |
| `filter_profiles` | gespeicherte Filterprofile (Kriterien, Gewichte, Deckprofil) |
| `filter_results` | Mitgliedschaft und Rang einer Combo in einem Profil, mit der Version der Bewertung |
| `meta` | Schema-Version |

Combos werden über ihre `signature` angesprochen, nicht über die interne ID von Database 1. So bleibt die Zuordnung auch bei einer neu aufgebauten Database 1 stabil. Neue Combos in Database 1 werden inkrementell bewertet.

## 9. Qualitätsfilter

- Ein **Filterprofil** besteht aus Mindestwerten pro Achse oder Situation, einem Deckprofil für die Passung, erlaubten und verbotenen Sperren, Kartenzahl und Gewichten. Beispiele: „allgemein stark“, „Drachendeck“, „als Zweiter“.
- **Unterlegene Combos** lassen sich ausblenden: gleiche oder mehr benötigte Karten bei einem in jeder Achse schlechteren Ergebnis (Pareto-Dominanz innerhalb einer Familie). Phase 1 speichert bewusst alles. Erst hier wird entschieden, was der Deckbauer sieht.
- Das Ergebnis wird in `filter_results` gespeichert und ist per Abfrage, Export (eigene Datei pro Profil) und in der GUI verfügbar.

## 10. Schnittstellen

- Eingang: `ComboReader` (Database 1), `CardPool`, `EffectKnowledge` (Effect Signatures), die Engine für die Robustheit.
- Analyzer-Vertrag: `Analyzer.analyze(combo, context) -> AxisResult(raw, explanation)`, plus `name` und `version`. Die Normierung (Perzentile) übernimmt das Analyzer System für alle Achsen gleich.
- Ausgang: `AnalysisReader`, für den Deckbauer und die GUI: Bewertungsprofil einer Combo, Abfragen nach Achsen, Filterprofile, Kompatibilitätskanten.

## 11. Umsetzung in Teilschritten

| Schritt | Inhalt |
|---|---|
| 2.1 ✓ | Grundgerüst: Database 2, Analyzer-Vertrag, inkrementeller und versionierter Lauf über Database 1, CLI `deepyugioh analyze` |
| 2.2 ✓ | Rechnerische Achsen: Ergebnis (Endfeld mit Störungen aus den Effect Signatures), Einsatz, Konsistenz mit Familien |
| 2.3 ✓ | Deckspezifität: Sperren in den Effect Signatures, Anforderungsprofil, Generizität pro Karte und Combo, Passung zu Deckprofilen |
| 2.4 | Normierung, allgemeine Spielstärke und Situationswerte mit Standardgewichten (`[analysis.weights]`) |
| 2.5 | Kompatibilitätskanten zwischen Combos |
| 2.6 | Qualitätsfilter: Profile, gespeicherte Ergebnisse, Dominanzfilter, Export |
| 2.7 | Test-GUI für Stufe 2 nach dem Vorbild der GUI von Phase 1: Bewertungsprofile, Gewichte änderbar, Filterprofile, Deckprofil-Passung |
| 2.8 | Robustheit per Engine: Gegner mit Handtraps in der Suche, Handtrap-Katalog, fortsetzbarer paralleler Lauf |
| 2.9 | Phase-1-Erweiterung: Startaufbauten für spätere Spielsituationen, Situation „späteres Spiel“ |

Jeder Schritt bekommt Tests und Dokumentation. Database 1 und das Verhalten von Phase 1 bleiben unverändert, außer durch die ausdrücklich geplante Erweiterung in 2.9.

## 12. Offene Punkte

- **Kalibrierung**: Die Standardgewichte sind begründete Startwerte, kein Beweis für Spielstärke. Belastbar werden sie erst durch Simulationen ganzer Spiele (Phase 4). Bis dahin zeigt jede Bewertung ihre Rohwerte und Begründung, damit sie nachvollziehbar bleibt.
- **Handtrap-Katalog und Verbreitung**: Der Startkatalog ist generisch. Archetypspezifische Unterbrechungen und eine an Daten gemessene Verbreitung folgen später.
- **Gegnerfelder für „Durchbrechen“**: Die heutigen Gegnerfelder handeln nie. Für echtes Durchbrechen braucht es Gegnerfelder mit Störungen. Das hängt mit der adversarialen Suche aus Schritt 2.8 zusammen.

## 13. Umsetzungsstand (Schritte 2.1 und 2.2)

### Benutzung

```bash
uv run deepyugioh analyze                    # bewertet neue oder veraltete Combos, normiert alle Achsen
uv run deepyugioh analyze --full             # alles neu berechnen
uv run deepyugioh analysis stats             # Achsen, Familien, letzter Lauf
uv run deepyugioh analysis show --combo 19542
uv run deepyugioh analysis top --axis outcome --limit 10
```

Der erste Lauf über die 41.518 Combos von Database 1 dauert etwa 26 Sekunden. Danach werden nur neue Combos und Achsen mit geänderter Analyzer-Version neu berechnet. Die Konsistenz wird immer vollständig neu berechnet, weil sie von den anderen Combos ihrer Familie abhängt (etwa 19 Sekunden).

### Code

| Datei | Inhalt |
|---|---|
| [contracts/analysis.py](../src/deepyugioh/contracts/analysis.py) | Vertrag: `Analyzer` (`name`, `version`, `global_`, `prepare`, `analyze`), `AxisResult`, `AxisScore`, `AnalysisReader` |
| [analysis/axes.py](../src/deepyugioh/analysis/axes.py) | Achsen Ergebnis, Einsatz, Konsistenz, Combo-Familien, Deckmodell |
| [analysis/probability.py](../src/deepyugioh/analysis/probability.py) | exakte Starthand-Wahrscheinlichkeiten (multivariat hypergeometrisch, ohne Stichproben) |
| [analysis/store.py](../src/deepyugioh/analysis/store.py) | Database 2 (`data/analysis.sqlite`): `analyses`, `combo_axes`, `combo_profiles`, `meta` |
| [analysis/system.py](../src/deepyugioh/analysis/system.py) | Analyzer System: inkrementell, versioniert, Database 1 wird gestreamt, Normierung auf Perzentile |
| [tests/test_analysis.py](../tests/test_analysis.py) | Wahrscheinlichkeiten, Achsen an echten Karten, inkrementelle und versionierte Läufe, Deckmodell |

### Achsen

| Achse | Rohwerte | Wert (höher = besser) |
|---|---|---|
| **Ergebnis** (`outcome`, Version 1) | Monster, Feldkarten, ATK gesamt/max., Störungen für den gegnerischen Zug (davon Negates) mit Karte und Zone, unklare Störungen, Kartenvorteil, Hand danach, Verluste des Gegners, Schaden, Sieg | 100·Sieg + 4·Negates + 3·übrige Störungen + 1·unklare + 2·gegnerische Feldverluste + 2·gegnerische Handverluste + Schaden/1000 + Kartenvorteil + 0,5·Monster + 0,3·ATK/1000 |
| **Einsatz** (`cost`, Version 1) | Karten (Hand, Deck, Extra Deck), Aktionen, gezahlte LP, Normalbeschwörung verbraucht | −(2·Hand + 1·Deck + 0,5·Extra + 0,05·Aktionen + LP/1000 + 0,5·Normalbeschwörung) |
| **Konsistenz** (`consistency`, Version 3) | Wahrscheinlichkeit der eigenen Starthand und der Familie, als Erster (5 Karten) und als Zweiter (6 Karten), Größe der Familie | Familie als Erster |

**Störungen** sind Effekte, die im Zug des Gegners nutzbar sind und den Gegner treffen. Dazu zählen Quick-Effekte in der passenden Zone, gesetzte Fallen und Schnellzauber sowie Auslöser auf Aktionen des Gegners. Ein Effekt mit „hart einmal pro Zug“ zählt pro Karte einmal. Negates erkennt die Analyse an den Kategorien NEGATE, DISABLE und DISABLE_SUMMON, Entfernung an Bewegungen von Gegnerkarten. Ob die Bedingungen eines Effekts im Spiel tatsächlich erfüllt sind, prüft erst die Robustheit per Engine (Schritt 2.8). Bis dahin ist die Störungszahl eine Ableitung aus den Effect Signatures. In Database 1 haben 15 % der Combos mindestens eine erkannte Störung.

**Combo-Familie**: Combos mit demselben Ergebnis (eigenes Feld, gewonnene Handkarten, Verluste des Gegners) und unterschiedlichen Startkarten. Ihre Konsistenz ist die Wahrscheinlichkeit, *irgendeine* ihrer Starthände zu ziehen. Database 1 hat 16.684 Familien, die größte erreicht dasselbe Ergebnis über 20 verschiedene Starthände. Bei mehr als 14 relevanten Handkarten wird die Familie gekürzt, der Wert ist dann eine untere Grenze und als solche markiert.

**Deckmodell** (`[analysis.deck]` in `deepyugioh.toml`): `deck_size` 40, `copies` 3, `hand_first` 5, `hand_second` 6, `family_max_cards` 14.

### Befund aus Phase 1

30 Combos haben keine Handkarte als Voraussetzung, obwohl ihr Startaufbau eine hat. Die Karte wird benutzt, verlässt die Hand aber nicht (z. B. vorgezeigt). Die Konsistenz nimmt dann vorsichtig die Starthand des Aufbaus und markiert das (`hand_from_setup`). Korrigieren lässt sich das in der Bedarfsermittlung von Phase 1 (`requirements` in `generator/search/combo.py`). Das ändert Combo-Signaturen und gehört deshalb zur Phase-1-Erweiterung (Schritt 2.9).

## 14. Umsetzungsstand (Schritt 2.3: Deckspezifität)

### Benutzung

```bash
uv run deepyugioh analyze                                         # berechnet auch die Achse genericness
uv run deepyugioh analysis card --card "Sage with Eyes of Blue"   # Kartenprofil mit Anforderungen und Sperren
uv run deepyugioh analysis fit --race Dragon --archetype Blue-Eyes --limit 10
uv run deepyugioh analysis top --axis genericness
```

### Sperren in den Effect Signatures

Die Extraktion (Version 2) liest Einschränkungen des Spielers als `Lock` (`contracts/effects.py`): Art (`SPECIAL_SUMMON`, `NORMAL_SUMMON`, `ACTIVATE`), Bereich (`all`, `extra` = nur Beschwörungen aus dem Extra Deck, `main`, ein Beschwörungstyp wie `PENDULUM`, `field` = solange die Karte auf dem Feld liegt), das Erlaubte oder Verbotene als `Condition` und ob die Sperre den ganzen Zug betrifft (Aktivitätszähler, „wenn du in diesem Zug noch nicht …“). Erkannt wird eine Sperre, wenn ein Effekt sie per `Duel.RegisterEffect(e, tp)` beim Spieler registriert, an einer Karte mit Wirkung auf die eigene Seite (`SetTargetRange(1, …)`, `SetAbsoluteRange(tp, 1, …)`, z. B. an einer Spielmarke) oder als Dauereffekt. Beschwörungsbedingungen einer Karte für sich selbst sind keine Sperren. Ergebnis: 1.207 Sperren auf 1.082 der teilnahmeberechtigten Karten, davon 131 nicht lesbar (als solche markiert).

### Kartenprofil (`card_profiles` in Database 2)

| Wert | Bedeutung |
|---|---|
| Anforderungsanteil | Anteil des Kartenpools, mit dem die Effekte der Karte arbeiten können. Pro Effekt der kleinste Anteil seiner Bewegungen (z. B. „Krieger Level ≤ 4 aus dem Deck“: 5,1 %); Effekte ohne Kartenbedarf (sich selbst beschwören, ziehen, den Gegner treffen) zählen mit 100 %. Für die Karte zählt das geometrische Mittel über alle wirksamen Effekte. |
| Sperranteil | Anteil, den die Sperren der Karte noch erlauben. Sperren nur für das Extra Deck zählen zur Hälfte, weil Beschwörungen aus dem Main Deck frei bleiben. |
| Generizität | log-skaliert aus Anforderungs- mal Sperranteil: 100 = alles, 75 = 10 %, 50 = 1 %, 25 = 0,1 %, 0 = höchstens 0,01 % |

Beispiele: Monster Reborn, Pot of Greed, Ash Blossom 100 · Foolish Burial 94 · Swordsoul of Mo Ye 95 (Sperre: aus dem Extra Deck nur Synchro, 25 % davon erlaubt) · Reinforcement of the Army 68 · Elfnote Lucina 72 (ein Effekt sucht „Elfnote“) · Sage with Eyes of Blue 35 · Mitsurugi-Karten 17.

Die Anteile werden über Bitmengen gezählt (ein Bit pro Karte, eine Maske pro Eigenschaft). Ein Filter ist damit eine Folge bitweiser UND-Operationen, ohne zusätzliche Abhängigkeiten.

### Achse `genericness` (Version 3)

- **Generizität der Combo**: geometrisches Mittel der Anforderungsanteile ihrer Karten, sodass eine sehr spezifische Karte die Combo herunterzieht. Dazu kommen die Sperren, die die Combo auslöst.
- **Ausgelöste Sperren**: Eine Sperre gilt als *sicher*, wenn die Karte in der Linie benutzt wurde und alle ihre aktivierbaren Effekte sperren. Sonst gilt sie als *möglich* und zählt mit halbem Gewicht (Quadratwurzel des Anteils). Welcher Effekt einer Karte genau benutzt wurde, lässt sich aus der Linie nicht sicher ablesen.
- **Anforderungsprofil** (`tags`): Archetypen, Typen und Attribute der Karten mit Anzahl, dazu die am wenigsten generischen Karten.
- Verteilung über Database 1: Werte von 0 bis 100, Schwerpunkt 30–70, 2.434 Combos unter 10 (stark an einen Archetyp gebunden). 3.324 Combos lösen eine sichere Sperre aus.

### Passung zu einem Deckprofil

Ein Deckprofil besteht aus Archetypen, Typen und Attributen (`DeckProfile`). Die Passung (0–100) einer Combo ist der Mittelwert über ihre Karten:

- Karte gehört zum Profil (Archetyp, Typ oder Attribut): 1,
- sonst ihre Generizität / 100, dazu ein Faktor von 0,5 bei einem fremden Archetyp und 0,6 bei einem Monster anderen Typs oder Attributs,
- mal einen Faktor für jede sichere Sperre: 1, wenn das Profil selbst erlaubt ist (z. B. „nur Drachen“ in einem Drachendeck), sonst der Anteil, den die Sperre erlaubt.

So hat eine Dragonmaid-Combo eine Generizität von 38 und im Profil „Drache, Blue-Eyes“ eine Passung von 100. Eine generische Combo aus Zaubern passt überall mit 100. Die Faktoren 0,5 und 0,6 sind Startwerte und werden wie die Gewichte später am Simulator kalibriert. Die Passung wird bei Bedarf berechnet (`analysis fit`). Gespeicherte Filterprofile folgen in Schritt 2.6.
