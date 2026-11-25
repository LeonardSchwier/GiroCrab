# Qualitäts-Gates: wie GiroCrab Änderungen belegt

GiroCrab verwaltet Bankzugänge, Salden und Belege. Ein großer Teil des Codes entsteht mit
KI-Agenten ([KI-Transparenz](ai-transparency.md)). Daraus folgt eine einfache Regel:

> **Was zählt, prüft eine Maschine. Agenten und Menschen urteilen dazwischen, und jede
> Erfolgsmeldung wird gegen einen Lauf geprüft.**

Diese Seite listet alle Gates an einer Stelle: was sie prüfen, wer oder was sie durchsetzt und
wo sie stehen. English summary: [below](#english-summary).

## Zwei Arten von Kontrolle

| Urteil (Mensch oder Agent) | Durchsetzung (Maschine, lässt sich nicht überreden) |
|---|---|
| Was ist der Fehler, was die richtige Änderung? | **Fix-Proof:** der Test ist ohne den Fix rot, mit ihm grün |
| Welche Tests fehlen noch? | **Mutation Testing** zeigt Code, dessen Fehler kein Test bemerkt |
| Ist der Diff sicher? (Review, `.sensitive()`, Logs) | **Secret-Scan**, Lints (Vault-Zugriff, Schriften, Bug-Report-Dialoge, Marken), `cargo audit` |
| Ist die Änderung schnell genug? | `category_perf` mit Messwerten, Lint gegen Vault-Zugriff aus Views |
| Was wurde geprüft, was nicht? | **Evidence-Block** und **Review-Angabe** auf der PR-Card (Selbstauskunft, sichtbar) |

Selbstauskünfte blockieren nichts, eine fehlende Angabe ist aber auf der Card mit ⚠️ markiert. Harte
Telemetrie (Tests, Fix-Proof) setzt das ✅ in Stufe 4 „Verification & Audit“.

## Die Gates

### 🧪 Fix-Proof (Pflicht für `fix:`-PRs, maschinell)

Jeder neue oder geänderte Test läuft auf der Base **ohne** den Fix (muss rot sein) und auf dem
PR-Head **mit** dem Fix (muss grün sein). Ein `fix:`-PR ohne einen solchen Test ist rot, außer
mit begründeter Ausnahme (`Fix-Proof: skip – <Grund>`). Ergebnis auf der PR-Card.
→ **[docs/fix-proof.md](fix-proof.md)**

Warum das der wichtigste Baustein ist: Ein Regressionstest schützt das Projekt, solange es
existiert, aber nur, wenn er den Fehler wirklich fängt. Das sieht man einem Test nicht an; man
muss ihn gegen den alten Code laufen lassen. Fix-Proof tut das bei jedem PR.

### 🧬 Mutation Testing (Hinweis, maschinell)

cargo-mutants baut kleine absichtliche Fehler in die geänderten Rust-Funktionen und prüft, ob die
Tests sie bemerken. Höchstens 12 Mutanten pro PR, überlebende Mutanten stehen mit Datei und Zeile
in der Job-Summary. Blockiert nie. → [docs/fix-proof.md › Phase 3](fix-proof.md#phase-3-mutation-testing)

### 🔑 Secret-Scan (maschinell)

[gitleaks](https://github.com/gitleaks/gitleaks) prüft jeden PR (seine Commits), jeden Push auf
`main` und wöchentlich die ganze Historie (Job `secrets` in `ci.yml`), lokal auch der Pre-push-Hook,
wenn gitleaks installiert ist. Zusätzlich zu den Standardregeln:

* **HAR-Dateien** (Browser-Netzwerkmitschnitte) sind nie erlaubt: Sie enthalten Session-Cookies,
  Tokens und Kontodaten. Connector-Arbeit beginnt oft mit einem solchen Mitschnitt; er bleibt auf
  dem eigenen Rechner, ins Repo kommen nur anonymisierte Ausschnitte als Testdaten.
* **Authorization-/Cookie-Header** mit echt aussehendem Wert.

Ausnahmen stehen eng gefasst und begründet in `.gitleaks.toml` (Testschlüssel unter `Tests/`,
`fixtures/`). Ein echtes Geheimnis wird nie freigeschaltet, sondern gewechselt und aus der
Historie entfernt.

### 📋 Evidence: Behauptung → Befehl → Ergebnis (Selbstauskunft, sichtbar)

Jeder PR enthält einen Abschnitt `### Evidence` (Vorlage: `.github/pull_request_template.md`):
pro Behauptung der genaue Befehl und das beobachtete Ergebnis. Eine Prüfung, die nicht laufen
konnte (z. B. iOS-Build ohne Mac, Qt-Jobs ohne Qt), steht dort als **„nicht gelaufen“**, nie als
grün. Formulierungen wie „sollte funktionieren“ ersetzen keinen Beleg. Die Card zählt die Belege
und die nicht gelaufenen Prüfungen.

### 👀 Review: wer hat den Diff geprüft? (Selbstauskunft, sichtbar)

Jeder KI-PR nennt in der Reflexion oder als Trailer `Review:`, wer den Diff geprüft hat: eine
Person, ein unabhängiger Agentenlauf (eigene Session ohne den Verlauf des Autors) oder
`self-review – <Grund>`. Das erzwingt keinen zweiten Prüfer, macht aber sichtbar, wann es keinen
gab. Bei Änderungen an der Security-Oberfläche (Vault, Krypto, Connectoren, CI) bleibt ein
menschliches Review Pflicht.

### 🔁 Process-Learning: Fehler fließen in die Regeln zurück

Läuft ein Agent oder ein Mensch in eine Falle (veraltete Bindings, Vault-Zugriff aus einer View,
fehlender serde-`cfg_attr`, …), endet das nicht mit dem Fix. Die Erkenntnis wird zu einer Regel in
`CLAUDE.md`, einem Punkt in `CONTRIBUTING.md` oder, am besten, zu einem Lint in `scripts/`. Der PR
nennt das in der Reflexion unter `Process-Learning:`. Viele der heutigen Lints sind so entstanden;
Fix-Proof selbst hat dabei einen eigenen Fehler gefunden (Cargo teilte Base und Head ein
Testprogramm, siehe [fix-proof.md](fix-proof.md#warum-ein-eigener-cargo-build-für-die-base)).

### 🧯 Robustheit: Checkliste für jede erreichbare Änderung

Fehlerpfade, Lebenszyklus (Tresor gesperrt oder gewechselt), Grenzwerte, Nebenläufigkeit und
Eingaben von außen: die Checkliste steht in [CONTRIBUTING.md](../CONTRIBUTING.md#-robustheit--robustness).

### 📖 Glossar

Gemeinsame Begriffe für Code, UI-Texte und Issues: [docs/GLOSSARY.md](GLOSSARY.md).

### Bestehende Gates (maschinell)

| Gate | Wo |
|---|---|
| `cargo fmt --check`, `cargo clippy -D warnings`, `cargo nextest` (Workspace), Swift-Build und -Tests | `ci.yml` (`rust`, `swift`) |
| Lints: Vault-Zugriff, `AppModel`-Interna, Schriften, iOS-Kompatibilität, Marken, Bug-Report-Dialoge, `UserDefaults` in Tests, serde-Feature, FFI-Exporte, String-Kataloge | `scripts/lint-*.sh`, `scripts/xcstrings.py` |
| bekannte Schwachstellen in Abhängigkeiten | `cargo audit` (Job `audit`, auch wöchentlich) |
| Lizenzhinweise für alle direkten Abhängigkeiten | `scripts/check-third-party-notices.sh` |
| keine FinTS-Produkt-ID im Repo | `scripts/fints-registrierung.sh scan` |
| veröffentlichte Updates und Sicherheitssperre echt signiert | `release-integrity.yml` |
| Linux-, Windows-, Flatpak-Builds | `ci.yml` (nach geänderten Pfaden) |

## Herkunft der Ideen

Evidence statt Beteuerung, sichtbare Reviews, die Rückkopplung von Fehlern in die Regeln, das
Glossar und die Robustheits-Checkliste übernehmen Ideen aus dem Prozessdokument
[Agentic PDLC](https://github.com/LeonardSchwier/leotheca/blob/main/documentation/AGENTIC_PDLC.md)
des Projekts Leotheca. Dort ist „ein Regressionstest muss vor dem Fix rot sein“ eine geschriebene
Regel; GiroCrab prüft sie mit Fix-Proof maschinell.

---

## English summary

GiroCrab's rule: **what matters is checked by a machine; people and agents judge in between, and
every success claim is checked against a run.** Machine-enforced: **Fix-Proof** (every new or
changed test must be red on the base without the fix and green with it; `fix:` PRs fail without
such a test, [details](fix-proof.md)), **mutation testing** of the changed Rust functions (report
only), **secret scanning** with gitleaks (PR commits, pushes to main, weekly full history; HAR
captures and auth headers are extra rules), plus the existing fmt/clippy/tests, lints,
`cargo audit` and licence checks. Visible self-reports on the PR card: an **Evidence** block
(claim → command → observed result; checks that could not run are reported as not run, never as
green), **Review** (who checked the diff, or `self-review – <reason>`) and **Process-Learning**
(a trap found becomes a rule or a lint). A robustness checklist and a shared glossary complete it.
