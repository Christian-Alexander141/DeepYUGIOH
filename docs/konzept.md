# DeepYuGiOh – Gesamtprojektübersicht

Projektvision, Systemarchitektur, Forschungsansatz und Entwicklungsplan · Stand: 8. November 2026 · Konzeptversion 1.0

DeepYuGiOh ist ein langfristiges KI-Forschungs- und Entwicklungsprojekt mit dem Ziel, ein autonomes System zu entwickeln, das eigenständig neue Yu-Gi-Oh!-Kartenkombinationen entdeckt, diese analysiert und daraus später leistungsfähige, möglichst kostengünstige und wettbewerbsfähige Decks konstruiert.

Die zentrale Idee ist, nicht einfach einen bestehenden Spielagenten nachzubauen oder ein LLM auf Kartentexte zu trainieren. Stattdessen soll ein System entstehen, das einen erheblichen Teil der strategischen Forschungsarbeit selbst übernimmt.

DeepYuGiOh soll herausfinden, welche Kombinationen möglich sind, wie zuverlässig sie funktionieren, wie stark sie sind und wie man sie optimal miteinander zu einem Deck verbindet.

Das Projekt ist bewusst in mehrere unabhängige Einheiten aufgeteilt. Zunächst konzentrieren wir uns ausschließlich auf den Combo Generator. Der Deckaufbau wird ein späteres, eigenständiges Entwicklungsprojekt.

## 1. Die übergeordnete Vision

Das langfristige Ziel von DeepYuGiOh ist ein autonomes System, das folgende Aufgaben bewältigen kann:

1. Den gesamten verfügbaren Kartenpool analysieren.
2. Unbekannte Karteninteraktionen entdecken.
3. Mehrstufige Kombinationen aus diesen Interaktionen entwickeln.
4. Die tatsächliche Ausführbarkeit der Kombinationen überprüfen.
5. Ihre Zuverlässigkeit und Brick-Wahrscheinlichkeit berechnen.
6. Ihre Widerstandsfähigkeit gegen gegnerische Unterbrechungen untersuchen.
7. Ihre strategische Stärke bewerten.
8. Aus den Erkenntnissen eigenständig Decks konstruieren.
9. Decks gegen unterschiedliche Gegner simulieren.
10. Aus den Ergebnissen neue Strategien und bessere Decks entwickeln.

Ein besonders wichtiges langfristiges Ziel ist die Entwicklung von Meta-fähigen Decks aus günstigen Karten.

DeepYuGiOh soll nicht zwingend die Karten verwenden, die menschliche Deckbauer bereits als besonders stark identifiziert haben. Es soll auch nach bisher wenig beachteten Synergien und alternativen Strategien suchen. Damit könnte das System einen Wettbewerbsvorteil erzielen, den ein klassischer Deckbauer möglicherweise nicht entdeckt hätte.

**DeepYuGiOh – Autonomous Yu-Gi-Oh! Strategy Research System**

| Entdecken | Analysieren | Konstruieren | Optimieren |
|---|---|---|---|
| Neue Kartenkombinationen finden. | Konsistenz und Stärke bewerten. | Optimale Decks entwickeln. | Durch Simulationen besser werden. |

## 2. Die grundlegende Systemarchitektur

Eine wichtige Entscheidung aus unserer bisherigen Planung ist die klare Trennung zwischen dem Entdecken von Kombinationen und ihrer anschließenden Bewertung. Die Architektur besteht deshalb aus mehreren eigenständigen Systemen.

```
Card Knowledge System        Gesamter erfasster Kartenpool, Kartentexte, Effektinformationen, Kartenskripte
        │
Combo Discovery Engine       Globale Suche nach Karteninteraktionen und ausführbaren Kombinationsfolgen
        │
Database 1 – Combo Repository (nur Kombinationen)
        │  lesender Zugriff
Analyzer System
  ├─ Brick Analyzer          Brick-Wahrscheinlichkeit und Konsistenz
  ├─ Execution Analyzer      Erreichbarkeit und Ausführbarkeit
  ├─ Robustness Analyzer     Gegnerische Unterbrechungen
  └─ Strategy Analyzer       Strategischer Nutzen und Stärke
        │
Database 2 – Analyzer Repository (nur Bewertungen)
        │
Quality Filter               Stellt dem Deckbauer nur Kombinationen bereit, die seinen Anforderungen entsprechen
        │
Deck Construction Engine     (spätere Phase) Konstruiert aus bewerteten Kombinationen vollständige Decks
```

Der wichtigste Grundsatz:

> Die Combo-Datenbank speichert, was möglich ist. Die Analyzer-Datenbank speichert, wie gut es ist. Der Deckbauer entscheidet, was davon zusammengehört.

Damit bleiben Entdeckungen und Bewertungen dauerhaft getrennt.

## 3. Phase 1: Der Combo Generator

*Aktueller Entwicklungsschwerpunkt.* Der Combo Generator soll den gesamten erfassten Kartenpool untersuchen, ohne dass ihm bestimmte Starterkarten oder Archetypen vorgegeben werden.

### 3.1 Global Discovery

Wir haben uns ausdrücklich für eine globale Suche entschieden:

- Keine vorgegebenen Starterkarten.
- Keine Beschränkung auf bestimmte Archetypen.
- Keine Beschränkung auf bekannte Meta-Decks.
- Keine Beschränkung auf aktuell besonders beliebte Karten.
- Keine Vorauswahl nach vermuteter Spielstärke.

Jede erfasste Karte darf Ausgangspunkt oder Bestandteil einer Kombination werden. Die Engine soll auch ungewöhnliche Interaktionen entdecken, beispielsweise zwischen Karten völlig unterschiedlicher Archetypen.

### 3.2 Ausgabestrategie: ALL

Der Combo Generator darf nicht entscheiden, dass eine Kombination zu schwach oder zu inkonsistent sei, um gespeichert zu werden. Auch folgende Kombinationen sollen erhalten bleiben:

- Kombinationen mit niedriger Zugänglichkeit.
- Kombinationen mit ungewöhnlichen Kartenanforderungen.
- Kombinationen, die mehrere bestimmte Karten benötigen.
- Kombinationen mit nur einer funktionierenden Ausführungslinie.
- Kombinationen, die leicht unterbrochen werden können.
- Kombinationen mit wenig strategischem Nutzen.

Eine heute wenig interessante Kombination kann durch eine später entdeckte Karte oder eine andere Interaktion relevant werden.

Einschränkung: Die globale Suche muss wegen der kombinatorischen Explosion über dokumentierte Suchgrenzen verfügen. Alle validierten Funde innerhalb eines Suchbereichs werden gespeichert; unvollständige Suchläufe werden ausdrücklich als solche gekennzeichnet.

### 3.3 Wie der Combo Generator arbeitet

1. **Karten importieren** – Karteninformationen, Karteneffekte und ausführbare Kartenskripte in eine lokale Datenbasis laden.
2. **Effekte analysieren** – Die Effect Knowledge Engine strukturiert die Karteneffekte (z. B. „beschwört ein Monster aus dem Deck“, „löst aus, wenn ein Monster auf den Friedhof gelangt“, „ermöglicht eine Spezialbeschwörung“, „verändert das Level eines Monsters“).
3. **Interaktionen finden** – Die Interaction Discovery Engine sucht nach Beziehungen zwischen diesen Effekten. Beispiel: Karte A sendet ein Monster vom Deck auf den Friedhof → Karte B löst aus, wenn sie auf den Friedhof gelangt → Karte C ermöglicht eine weitere Spezialbeschwörung → Kandidat für eine neue Kombinationslinie.
4. **Kombinationsfolgen entwickeln** – Die Combo Search Engine untersucht, welche weiteren Aktionen möglich sind, unter Berücksichtigung von Ressourcen, Kartenzonen, Beschwörungsbedingungen, Effektnutzungen und Beschränkungen.
5. **Simulation** – Die Kombination wird durch eine echte Spiel-Engine ausgeführt. Das LLM darf Hypothesen erzeugen, aber nicht selbst verbindlich behaupten, dass eine Combo regeltechnisch funktioniert.
6. **Speicherung** – Jede erfolgreich validierte Kombination wird in das Combo Repository aufgenommen.

Die verbindlichen Definitionen für Phase 1 (was eine Combo ist, Suchgrenzen, Pruning) stehen in [combo-generator-spec.md](combo-generator-spec.md).
