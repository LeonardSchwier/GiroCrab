## Zusammenfassung

<!-- Was ändert sich und warum? Issue: Closes #… / Refs #… -->

## Evidence

<!-- Jede Behauptung mit dem genauen Befehl und dem beobachteten Ergebnis. Was nicht laufen konnte
     (kein Mac, kein Qt, kein Bankkonto), steht hier als „nicht gelaufen“, nie als bestanden.
     „Sollte funktionieren“ ist kein Beleg. Siehe docs/quality-gates.md. -->

| Behauptung | Befehl | Ergebnis |
|---|---|---|
| Fix bewiesen (rot ohne Fix, grün mit Fix) | `python3 -I tools/fix_proof.py check --base upstream/main` | |
| Core-Tests grün | `cargo test -q -p girocrab-core --lib` | |
| Clippy sauber | `cargo clippy -q --workspace --all-targets -- -D warnings` | |
| Swift-Tests grün | `cd GiroCrabApp && swift test -q` | |

<!-- fix:-PR, der sich nicht automatisch testen lässt? Dann (mit Grund):
Fix-Proof: skip – <Grund>
-->

## Checkliste

Die Punkte stehen in [CONTRIBUTING.md › PR Checklist](https://github.com/GiroCrab/GiroCrab/blob/main/CONTRIBUTING.md#pull-request-checkliste-security-performance--qualität--pr-checklist-security-performance--quality)
(Security, Performance, Robustheit, Fix-Proof). Nicht Zutreffendes: „n/a: <warum>“.

### 🤖 AI Process Reflection

<!-- Für PRs mit KI-Hilfe; sonst löschen. Die fünf ersten Labels bleiben wie geschrieben. -->
- **Human-Steering:** <!-- was der Mensch vorgegeben oder entschieden hat (Architektur, Security, Umfang) -->
- **AI-Agency:** <!-- was die KI selbst entschieden hat (Namen, Struktur, Randfälle) -->
- **Review-Focus:** <!-- wo ein Reviewer zuerst hinsehen sollte -->
- **Security-Check:** <!-- Geheimnisse/personenbezogene Daten: Logs, .sensitive(), neue Netz-/Prozess-/Dateizugriffe, Abhängigkeiten + THIRD_PARTY_NOTICES.md; „Nichts relevant: <warum>“ ist gültig -->
- **Perf-Check:** <!-- offMain ab ~16 ms, keine list_transactions für Zählungen/Summen, category_perf-Werte; „Kein Hot Path berührt: <warum>“ ist gültig -->
- **Review:** <!-- wer den Diff geprüft hat: Person, unabhängiger Agentenlauf oder „self-review – <Grund>“ -->
- **Process-Learning:** <!-- optional: welche Falle dieser PR gefunden hat und welche Regel/welcher Lint sie jetzt verhindert -->
