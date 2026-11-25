# Glossar / Glossary

Gemeinsame Begriffe für Code, UI-Texte, Issues und PRs. Die **UI-Spalte** ist das Wort, das die
App zeigt (`Localizable.xcstrings`); **Code** nennt den Namen im Rust-Core oder in Swift, wenn er
abweicht. Neue Begriffe kommen alphabetisch dazu, wenn ein PR sie einführt; ein Begriff, der in der
App anders heißt als im Code, steht hier, damit beide Seiten zusammenfinden.

Common terms for code, UI texts, issues and PRs. The **UI** column is the word the app shows; **Code**
names the identifier in the Rust core or in Swift when it differs.

| UI (DE) | EN | Code | Bedeutung / Meaning |
|---|---|---|---|
| Abruf, Abrufen | sync, fetch | `sync_session`, `begin_sync` | Umsätze, Salden und Belege bei einer Bank holen. / Fetching bookings, balances and documents from a bank. |
| Abruf-Protokoll | sync log | `sync_log` | Protokoll der Abrufe. Wird nie in einen Bug-Report-Screenshot übernommen; Text daraus nur über `sanitize_report_text`. |
| Abrechnungszeitraum | statement period | – | Abschnitt, den ein Kreditkartenportal als Abrechnung zeigt. Nicht zu verwechseln mit dem Zeitraum der Ansicht. |
| Anfangssaldo | opening balance | `opening_balance` | Saldo vor der ersten bekannten Buchung, damit der Verlauf stimmt. |
| Bankzugang | bank login, connection | `connections` | Zugangsdaten zu einer Bank (ein Login kann mehrere Konten haben). Eine gespeicherte PIN liegt im Tresor. |
| Beleg | document | `documents` | PDF zu einer Buchung oder aus der Postbox (z. B. Kontoauszug, Rechnung). |
| Betrag | amount | `amount_cents` | Immer in Cent als Ganzzahl im Core, nie als Gleitkommazahl. |
| Buchung, Umsatz | booking, transaction | `bookings`, `list_transactions` | Eine Kontobewegung. `list_transactions` reichert jede Zeile an: nie für Zählungen oder Summen verwenden. |
| Budget | budget | `budgets` | Geplanter Betrag pro Kategorie und Zeitraum. |
| Connector | connector | `BankConnector` | Implementierung eines Bankzugangs (FinTS, PSD2 über Enable Banking, Portale). |
| Dauerauftrag | standing order | `standing_orders` | Wiederkehrende Überweisung bei der Bank. |
| Demo-Tresor | demo vault | `--demo-seed` | Tresor mit erfundenen Daten für Tests, Screenshots und Ausprobieren. |
| Depot | depot, securities account | `depot`, `assets` | Wertpapierkonto mit Beständen, Kursen und Depot-Aktivitäten. |
| Empfänger, Gegenpartei | counterparty | `counterparty` | Die andere Seite einer Buchung. Personenbezogen: `.sensitive()`. |
| FinTS | FinTS (HBCI) | `connectors/fints/` | Deutsches Bank-Protokoll. Braucht eine registrierte Produkt-ID, die nie im Repo steht. |
| Händler | merchant | `merchants.rs`, `merchants.tsv` | Erkanntes Unternehmen hinter einer Buchung (Name, optional Logo). |
| Händler-Logos | merchant logos | `logos` | Opt-in, standardmäßig aus: ohne Zustimmung keine Netzanfrage. |
| Kategorie | category | `categories` | Einordnung einer Buchung (z. B. Lebensmittel). Bei Bug-Reports nur auf Wunsch verdeckt (`.sensitive(.labels)`). |
| Konto | account | `accounts` | Girokonto, Kreditkarte, Depot, Darlehen, Sachwert … |
| Kontogruppe | account group | `user_groups` | Vom Nutzer gebildete Gruppe von Konten. |
| Lastschrift | direct debit | `mandates` (Mandate) | Einzug durch den Zahlungsempfänger. |
| lokale Schnittstelle | local API | `local_api` | Nur-Lese-Zugriff für externe Auswertungen über einen Unix-Socket (`docs/local-api.md`). |
| Postbox | postbox | `postbox` | Elektronisches Postfach einer Bank, aus dem Belege kommen. |
| Problem melden | report a problem | `BugReport` | Bug-Report mit geschwärztem Screenshot (`.sensitive()`, `.capturableSheet`). |
| Regel | rule | `rules` | Automatische Zuordnung (Kategorie, Tag, Notiz) nach Bedingungen. |
| Sachwert | tangible asset | `assets` | Wert außerhalb von Konten, z. B. Gold oder ein Auto. |
| Saldo | balance | `balances` | Kontostand zu einem Zeitpunkt. Beträge: `.sensitive(.amounts)`. |
| Sammelüberweisung | batch transfer | `batch_csv` (Import) | Mehrere Überweisungen in einem Auftrag. |
| Sicherheitssperre | security lock (banking policy) | `banking-policy.json` | Signierte Datei, mit der der Herausgeber Bankkontakt älterer, fehlerhafter Versionen sperren kann (`docs/banking-policy.md`). |
| Sicherung | backup | `vault/backup` | Verschlüsselte Kopie des Tresors. |
| Sparplan | savings plan | – | Regelmäßiger Wertpapierkauf im Depot. |
| Tag | tag | `tags.rs` | Freies Schlagwort an einer Buchung, zusätzlich zur Kategorie. |
| TAN | TAN, SCA | – | Bestätigung eines Auftrags oder Logins (starke Kundenauthentifizierung). Liegt nur im Speicher (`zeroize`). |
| Tresor | vault | `Vault`, `vault.rs` | Die verschlüsselte Datenbank mit allen Daten der Nutzerin oder des Nutzers. |
| Überweisung | transfer | `transfer_session` | Zahlungsauftrag an die Bank. |
| Verwendungszweck | purpose, remittance info | `purpose` | Freitext einer Buchung. Personenbezogen: `.sensitive()`. |
| Vormerkung, vorgemerkt | pending booking | `pending` | Noch nicht gebuchter Umsatz; wird beim Abruf mit der späteren Buchung abgeglichen. |
| Zeitraum | period | `AppModel+Selection` | Ausgewählter Datumsbereich der Ansicht. |

## Entwicklung / Development

| Begriff | Bedeutung |
|---|---|
| Base, Head | Base: der Stand, gegen den ein PR verglichen wird (Ziel-Branch). Head: der Stand des PRs. |
| Fix-Proof | Probe, dass ein Test ohne den Fix rot und mit ihm grün ist ([fix-proof.md](fix-proof.md)). |
| Mutant | Absichtlich eingebauter kleiner Fehler beim Mutation Testing; „überlebt“, wenn kein Test ihn bemerkt. |
| PR-Card | AI Provenance & Transparency Card auf jedem PR ([ai-transparency.md](ai-transparency.md)). |
| Evidence | Abschnitt im PR: Behauptung → Befehl → beobachtetes Ergebnis ([quality-gates.md](quality-gates.md)). |
| Schwärzung, Redaction | Abdecken persönlicher Daten im Bug-Report (`.sensitive()`), siehe `CLAUDE.md`. |
| Schema-Schritt | Neuer Eintrag am Ende von `STEPS` in `schema.rs`; bestehende Schritte werden nie geändert. |
| `offMain` | Core-Aufruf außerhalb des Main-Threads (alles über ~16 ms). |
