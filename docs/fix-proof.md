# Fix-Proof: Regressionstests müssen ohne den Fix fehlschlagen

Für jeden neuen oder geänderten Rust-Test eines Pull Requests prüft der CI-Job `fix-proof` zwei
Dinge: **ohne** die Produktionsänderung des PRs muss der Test **fehlschlagen**, **mit** ihr muss er
**bestehen**. Ein `fix:`-PR, bei dem kein Test diese Probe besteht, ist rot.

English summary: [below](#english-summary).

## Warum

Ein Regressionstest schützt nur, wenn er den behobenen Fehler tatsächlich erkennt. Ein Test, der
schon vor dem Fix grün war, prüft etwas anderes. Im Diff und im Review sieht man das nicht: Der Test
ist neu, er ist grün, und er wirkt plausibel. Feststellen lässt es sich nur, indem man ihn gegen den
Code vor dem Fix laufen lässt.

Fix-Proof macht genau das bei jedem PR. Damit wird „Regressionstest hinzugefügt“ zu einem
nachprüfbaren Lauf, unabhängig davon, ob ein Mensch oder ein KI-Agent den Test geschrieben hat. Ein
bewiesener Test bleibt Teil der Suite und läuft in jedem weiteren CI-Lauf mit.

## Ablauf

1. **Plan** (`tools/fix_proof.py plan`, ohne Build): Aus dem Diff werden alle Testfunktionen gelesen,
   die neu sind oder deren Text sich geändert hat: `#[test]` und `#[tokio::test]` in
   `#[cfg(test)]`-Modulen, in Testdateien (`tests.rs`, `*_tests.rs`, `tests/`, Module mit
   `#[cfg(test)] mod x;`) und in Integrationstests unter `<crate>/tests/*.rs`. Erfasst werden die
   Pakete des Root-Workspaces. Ein PR gilt als Fix, wenn sein Titel mit `fix:` oder `fix(<scope>):`
   beginnt.
2. **Head:** Genau diese Tests laufen auf dem PR-Stand (`cargo test --exact …`).
3. **Base:** Ein `git worktree` des Base-Commits erhält nur den Testcode des PRs. Testdateien werden
   übernommen; in Dateien mit Produktionscode und Tests ersetzt ein kleiner Lexer nur die
   `#[cfg(test)]`-Einträge. Der Produktionscode bleibt auf dem Stand vor dem Fix.
   Kompiliert die Base dann nicht, weil ein Test eine API braucht, die erst der Fix einführt, werden
   genau die vom Compiler genannten Dateien auf den Base-Stand zurückgesetzt und neu gebaut (bis zu
   vier Runden). Die Tests dieser Dateien zählen als schwacher Beweis, alle anderen behalten ein
   echtes Ergebnis.
4. **Urteil** pro Test und für den PR (Job-Summary, Artefakt `fix-proof`).

| Status | Bedeutung | Beweis |
|---|---|---|
| rot → grün | schlägt ohne den Fix fehl, besteht mit ihm | stark |
| rot (ohne Fix nicht baubar) → grün | braucht eine API, die erst der Fix einführt | schwach |
| rot (Timeout) → grün | der Lauf ohne den Fix hängt | schwach |
| grün → grün | besteht auch ohne den Fix | keiner |
| auf dem PR rot | schlägt mit dem Fix fehl (fällt auch im Job `rust` auf) | – |
| nicht gelaufen | z. B. `#[ignore]` oder nur unter macOS übersetzt | – |

| Urteil | Bedingung | `fix:`-PR |
|---|---|---|
| Fix bewiesen | mindestens ein Test rot → grün | grün |
| schwach belegt | nur „nicht baubar“ oder „Timeout“ | grün |
| nicht bewiesen | Tests vorhanden, keiner ohne den Fix rot | rot |
| ohne Test | kein neuer oder geänderter Test | rot |
| nicht gelaufen | Tests geplant, aber kein Ergebnis | rot |
| Ausnahme | `Fix-Proof: skip – <Grund>` im PR-Body | grün |

Bei PRs, die keine Fixes sind, läuft dieselbe Probe nur zur Information.

**Ausnahmen.** Manche Fixes lassen sich nicht sinnvoll automatisch testen, etwa ein Log-Text, ein
CI- oder Build-Fix oder Verhalten, das nur gegen ein echtes Bankportal auftritt. Dann steht im
PR-Body (oder in einer Commit-Nachricht) `Fix-Proof: skip – <Grund>`. Ohne Begründung gilt die
Ausnahme nicht; die Job-Summary zeigt sie an.

**Eigene Build-Einheit für die Base.** Cargo identifiziert ein Workspace-Mitglied über seinen Pfad
relativ zum Workspace. Zwei Worktrees desselben Repos sind für Cargo daher derselbe Baustein, und in
einem gemeinsamen `target/` würde der Base-Lauf das Testprogramm des PRs wiederverwenden. Die Base
bekommt deshalb über eine wirkungslose Profil-Einstellung (`codegen-units`) eine eigene Einheit;
Abhängigkeiten bleiben geteilt und gecacht.

## Lokal ausführen

```sh
# Base = merge-base mit upstream/main, Head = Arbeitsverzeichnis (inkl. nicht committeter Änderungen)
python3 -I tools/fix_proof.py check --base upstream/main

# Schritte einzeln, wie in der CI (Head = ausgecheckter Commit)
python3 -I tools/fix_proof.py plan --base HEAD^1 --head HEAD --title "fix: …" --out plan.json
python3 -I tools/fix_proof.py run --plan plan.json --out rust.json --keep    # --keep: Worktree behalten
python3 -I tools/fix_proof.py verdict --plan plan.json --results rust.json --enforce
```

`check` endet mit Exit-Code 1, wenn ein `fix:`-PR nicht bewiesen ist. Tests des Werkzeugs:
`python3 -m unittest tools/test_fix_proof.py`; sie enthalten einen echten Lauf rot → grün an einem
temporären Cargo-Workspace.

## Erprobung an gemergten Fixes

| PR | Tests | Ergebnis |
|---|---|---|
| #923 Amex: erster Abruf übernimmt die aktive Karte | 1 | bewiesen: ohne den Fix schlägt `assert_eq!` fehl |
| #938 Spuerkeess über Enable Banking | 1 | schwach: der Test ruft `needs_lean_request` auf, das erst der Fix einführt |
| #835 Sicherheits-Audit | 25 | 18 bewiesen (alle durch fehlschlagende Assertions), 6 schwach, 1 grün → grün |

Der grüne Test in #835 (`vault::backup::tests::checkpoint_succeeds_on_an_idle_vault`) sichert
vorhandenes Verhalten ab. Das ist als Test sinnvoll, beweist aber nichts über den Fix. Ohne das
dateiweise Zurücksetzen wären in #835 nur 2 der 25 Tests stark belegt gewesen.

## Grenzen

- Ein Test, der eine erst mit dem Fix eingeführte API aufruft, ist nur ein schwacher Beweis; das
  betrifft alle geänderten Tests derselben Datei. Stärker ist ein Test über eine bestehende
  Schnittstelle (aufrufende Funktion, Integrationstest).
- Geprüft werden Tests, deren eigener Text sich ändert. Eine geänderte Test-Hilfsfunktion allein
  wählt keinen Test aus.
- Nur unter macOS übersetzte Tests laufen im Linux-Job nicht. Swift-Tests sind nicht erfasst.
- `#[cfg(test)]`-Elemente innerhalb von `impl`-Blöcken und Hilfsmodule von Integrationstests werden
  nicht einzeln übertragen; kompiliert die Base deswegen nicht, gilt der Test als schwach belegt.
- Fix-Proof zeigt, dass ein Test den Fehler erkennt, nicht, dass der Fix vollständig ist.
- Der Check blockiert Merges erst, wenn er in den Branch-Regeln als erforderlich eingetragen ist.
- Ändert ein PR `tools/fix_proof.py` oder `.github/workflows/ci.yml`, prüft er sich mit dem eigenen
  Code; die Job-Summary weist darauf hin.

## English summary

The `fix-proof` CI job runs every new or changed Rust test twice: on the base commit without the
PR's production changes, where it must fail, and on the PR, where it must pass. The base is a
`git worktree` that receives only the PR's test code (whole test files, and `#[cfg(test)]` items
spliced into mixed files). If a test needs an API that only the fix adds, the files rustc names are
reset to the base and the build is retried, so the remaining tests still get a real result. A
`fix:` PR fails unless at least one test is red without the fix and green with it, or the PR body
states `Fix-Proof: skip – <reason>`. Run it locally with
`python3 -I tools/fix_proof.py check --base upstream/main`.
