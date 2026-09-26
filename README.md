# PhoneBot — wyszukiwarka opłacalnych ofert iPhone'ów

Aplikacja desktopowa (Windows) do wyszukiwania ofert używanych iPhone'ów na
Allegro Lokalnie, Vinted i Sprzedajemy.pl, wyceny ich opłacalności i podpowiadania, czy i za ile kupić.

> **Status: wszystkie 5 etapów gotowe.** Trzy portale, wycena, werdykty i negocjacje, filtry,
> ustawienia, automatyczne odświeżanie, powiadomienia Windows i Telegram, opcjonalna analiza AI
> oraz gotowy plik `PhoneBot.exe`.

![Okno główne](docs/screenshots/okno.png)
![Szczegóły oferty](docs/screenshots/szczegoly.png)

## Szybki start: gotowy plik PhoneBot.exe

1. Wejdź na GitHubie w zakładkę **Actions** → workflow **build-windows** → ostatni udany przebieg
   (zielony) → sekcja **Artifacts** → pobierz **PhoneBot-windows** (plik ZIP z `PhoneBot.exe`).
   Jeśli oznaczysz wersję tagiem `v…` (np. `v1.0.0`), plik trafi też do zakładki **Releases**.
2. Rozpakuj i uruchom `PhoneBot.exe`; nie wymaga instalowania Pythona.
   Windows SmartScreen może ostrzec o nieznanym wydawcy (plik nie jest podpisany cyfrowo).
   Kliknij wtedy „Więcej informacji” → „Uruchom mimo to”.
3. Przy pierwszym uruchomieniu:
   - sprawdź miejscowość w panelu filtrów (domyślnie Kacwin),
   - przejrzyj **⚙ Ustawienia** (zysk, prowizje) i **🔧 Tabelę części**,
   - kliknij **⟳ Odśwież oferty** (F5).

Pierwsze pobranie nie wysyła powiadomień, bo wszystkie oferty są wtedy „nowe”.
Powiadomienia przychodzą od kolejnych odświeżeń.

### Budowanie PhoneBot.exe samodzielnie

```powershell
pip install -r requirements.txt pyinstaller
pyinstaller --noconfirm phonebot.spec     # wynik: dist\PhoneBot.exe
dist\PhoneBot.exe --self-test             # sprawdzenie, czy plik ma wszystkie moduły
```

## Uruchomienie na Windows (tryb deweloperski)

1. Zainstaluj **Python 3.12** z [python.org](https://www.python.org/downloads/windows/)
   i zaznacz „Add python.exe to PATH”.
2. Otwórz PowerShell w folderze projektu i wykonaj:

   ```powershell
   py -3.12 -m venv .venv
   .\.venv\Scripts\Activate.ps1
   pip install -r requirements-dev.txt
   ```

   Jeśli PowerShell blokuje aktywację, wykonaj raz:
   `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`.
3. Uruchom aplikację: `python -m phonebot`.
   Kliknij **⟳ Odśwież oferty** (F5), aby pobrać ogłoszenia z portali.
4. Testy: `python -m pytest`.
5. Demo wyceny w konsoli: `python -m phonebot.demo`.

### Automatyczne odświeżanie i praca w tle

- Oferty są pobierane automatycznie co **15 minut**; zmienisz to w Ustawieniach, a wartość 0 wyłącza
  automat. Pasek stanu pokazuje godzinę następnego odświeżenia.
- Zamknięcie okna chowa aplikację do **zasobnika systemowego** (obok zegara) i odświeżanie działa dalej.
  Kliknij ikonę, aby wrócić. Całkowite zamknięcie: prawy przycisk na ikonie → „Zakończ”.
  To zachowanie wyłączysz w Ustawieniach.
- O nowych **zielonych** ofertach, a opcjonalnie także o obniżce ceny do zielonej, informuje
  **powiadomienie Windows**. Każda oferta jest zgłaszana tylko raz.

### Powiadomienia Telegram

1. W Telegramie otwórz **@BotFather**, wyślij `/newbot` i nadaj botowi nazwę. Dostaniesz **token**
   (np. `123456789:AAH…`).
2. Otwórz rozmowę ze swoim nowym botem i wyślij mu `/start`.
3. W PhoneBot: **⚙ Ustawienia → Powiadomienia i AI**. Wklej token i kliknij **Pobierz chat ID**.
4. Kliknij **Wyślij test**. Na Telegramie powinna przyjść wiadomość.
5. Zaznacz „Wysyłaj powiadomienia na Telegram” i zapisz.

![Ustawienia powiadomień i AI](docs/screenshots/ustawienia_powiadomienia.png)

Każda wiadomość zawiera model, cenę, szacowany zysk, max cenę, sugestię negocjacji, lokalizację,
czerwone flagi i link do ogłoszenia. Na jedno odświeżenie wysyłanych jest maksymalnie 5 osobnych
wiadomości (do ustawienia), a resztę dostajesz w jednym podsumowaniu.

### Analiza opisów przez AI (opcjonalna)

Reguły tekstowe rozpoznają typowe sformułowania, ale nie każde. Po włączeniu analizy
opisy **nowych** ofert trafiają do modelu Claude (API Anthropic). Model zwraca usterki i czerwone flagi
w ściśle określonym formacie. Wynik jest **dokładany** do wyniku reguł, nigdy go nie usuwa;
w oknie szczegółów usterki znalezione przez AI mają dopisek „(AI)”, a model dodaje krótką uwagę.

1. Załóż konto na <https://console.anthropic.com>, doładuj środki i utwórz **klucz API** (`sk-ant-…`).
2. **⚙ Ustawienia → Powiadomienia i AI**: wklej klucz, zaznacz „Analizuj opisy nowych ofert”.
3. Model domyślny to `claude-opus-5` (najdokładniejszy). Tańsze opcje to `claude-sonnet-5`
   i `claude-haiku-4-5`, z nieco mniejszą dokładnością. Koszt ogranicza limit ofert na odświeżenie
   (domyślnie 20). Każda oferta jest analizowana tylko raz; ponownie dopiero po zmianie opisu.
   Aktualne ceny API: <https://www.anthropic.com/pricing>.

Błąd AI (np. zły klucz, brak środków) nie przerywa pobierania. Pasek stanu pokaże „AI: BŁĄD”,
a szczegóły są w podpowiedzi i w logu.

Token Telegrama i klucz API są przechowywane w lokalnej bazie aplikacji bez szyfrowania.

### Status źródeł i diagnostyka

Pod paskiem narzędzi widać status każdego portalu z ostatniego pobrania:

| Status | Znaczenie | Co zrobić |
|---|---|---|
| ✔ działa (N) | portal zwrócił N ofert | nic |
| ⚠ brak ofert | portal odpowiada, ale nic nie zwrócił | możliwa zmiana formatu — uruchom Diagnostykę |
| ⛔ zablokowane | portal blokuje automatyczne pobieranie (403, captcha) | automat robi pauzę 3 h (ręczne „Odśwież” ją pomija); jeśli trwa — wyłącz źródło |
| ✖ zmiana formatu | portal zmienił API lub stronę | adapter do aktualizacji — prześlij raport Diagnostyki |
| ✖ brak połączenia | brak internetu / zapora | sprawdź połączenie |

Najedź myszą na status, żeby zobaczyć szczegóły błędu. Przycisk **🩺 Diagnostyka**
sprawdza każdy portal osobno i pokazuje etap problemu (połączenie, blokada, parsowanie,
filtrowanie). Raport zapisuje się w `%LOCALAPPDATA%\PhoneBot\diagnostyka.txt`.
Ten sam raport wygenerujesz poleceniem `PhoneBot.exe --diagnose`.

Workflow **live-sources** w GitHub Actions codziennie sprawdza portale na żywo i oznacza
przebieg na czerwono, gdy któryś adapter przestanie zwracać oferty.

### Twoja miejscowość

Domyślnie ustawiony jest Kacwin. Zmienisz go w panelu filtrów („Lokalizacja → Zmień…”)
albo w ustawieniach. Miejscowość możesz wybrać na trzy sposoby:

- wpisać jej nazwę i kliknąć „Szukaj” (OpenStreetMap znajdzie dowolną miejscowość w Polsce),
- wybrać z wbudowanej listy (Podhale i większe miasta; działa bez internetu),
- wpisać współrzędne ręcznie.

Od wybranej miejscowości liczone są odległości do ofert, koszt dojazdu przy odbiorze osobistym
i filtr promienia.

![Wybór lokalizacji](docs/screenshots/lokalizacja.png)

### Filtr ogłoszeń i „Odrzucone”

Zanim oferta trafi do tabeli, przechodzi przez kilka etapów:

1. **Kategoria portalu** — np. „Akcesoria GSM” odpada, „Telefony” przechodzi.
2. **„Kupię / zamienię / szukam”** na początku tytułu → odrzucone („Sprzedam lub zamienię” przechodzi).
3. **Akcesoria i części z kontekstem** — „Etui do iPhone 13” odpada, ale „iPhone 13 128GB + etui gratis”
   czy „iPhone 12 z pudełkiem i ładowarką” przechodzą. „Sam wyświetlacz iPhone 11” odpada,
   a „iPhone 11 zbity wyświetlacz” i „iPhone XR na części” to cały telefon.
4. **Model** — ogłoszenie bez rozpoznanego modelu iPhone'a odpada.
5. **Test ceny** — cena poniżej 15% mediany rynkowej: jeśli opis wskazuje na akcesorium/atrapę,
   oferta odpada; w przeciwnym razie zostaje z czerwoną flagą „Cena nierealnie niska — sprawdź”
   i nigdy nie dostaje zielonego „KUPUJ”.

Odrzucone ogłoszenia z powodem znajdziesz pod przyciskiem **🚫 Odrzucone** na pasku narzędzi.
Przycisk **„✔ To jest telefon”** przywraca ofertę do tabeli i zapamiętuje ją, więc filtr nie odrzuci
jej ponownie. Listy słów (akcesoria, części, „kupię”, słowa dodatków itd.) oraz próg ceny edytujesz
w **Ustawienia → Filtr ogłoszeń**; zakładka podpowiada też słowa, które najczęściej dawały
fałszywe odrzucenia.

### Okno główne

- Tabela ma kolumny: zdjęcie, model, pamięć, stan, cena, wartość rynkowa,
  szacowany zysk, max cena zakupu, werdykt, portal, lokalizacja (z odległością
  od Kacwina), data dodania i link.
- Każdą kolumnę można sortować kliknięciem nagłówka. Domyślnie tabela jest
  posortowana po szacowanym zysku, malejąco.
- Kolor wiersza zależy od oceny 0–100:
  - zielony (≥ 65 pkt) — oferta warta uwagi,
  - żółty (≥ 40 pkt) — przeciętna,
  - czerwony — nieatrakcyjna.

  Kolumna „Werdykt” pokazuje KUPUJ / NEGOCJUJ / ODPUŚĆ razem z oceną.
- Oznaczenia przy modelu:
  - **⚑N** — liczba czerwonych flag; najedź myszą, aby zobaczyć listę. Przy poważnej fladze
    (iCloud, IMEI, MDM, podróbka) nazwa modelu jest czerwona;
  - **★** — oferta obserwowana.
- **Podwójne kliknięcie** wiersza albo Enter otwiera okno szczegółów. Znajdziesz w nim:
  - zdjęcia oferty,
  - werdykt z uzasadnieniem,
  - rekomendację negocjacji (cena otwierająca i maksymalna),
  - pełne wyliczenie: wartość rynkową i jej źródło, każdą pozycję kosztów, zysk, wymagany zysk i max cenę,
  - czerwone flagi z karą punktową,
  - dane rozpoznane z ogłoszenia, historię ceny i opis ogłoszenia.

  Z tego okna możesz też otworzyć ogłoszenie w przeglądarce, obserwować je albo ukryć.
- Kliknięcie „Otwórz ↗” od razu otwiera ogłoszenie w przeglądarce.
- **Prawy przycisk myszy** na wierszu otwiera menu: szczegóły, otwórz, obserwuj, ukryj.
  Ukryte oferty znikają z listy. Przycisk „Pokaż ukryte” pozwala je przywrócić.
- Przełącznik trybu (Naprawa → sprzedaż / Szybki resell) od razu przelicza wyceny.
- Pobieranie działa w osobnym wątku, więc okno nie zawiesza się w trakcie.
  Błąd portalu widać w pasku stanu; szczegóły są w podpowiedzi po najechaniu myszą
  i w logu `%LOCALAPPDATA%\PhoneBot\logs\phonebot.log`.
- **Panel filtrów** po lewej działa natychmiast, bez ponownego pobierania ofert,
  i zapamiętuje ustawienia. Możesz filtrować po:
  - lokalizacji i promieniu (oferty z wysyłką mogą zostać mimo odległości),
  - „tylko z wysyłką”,
  - tekście,
  - cenie od–do,
  - minimalnym zysku,
  - ocenie (zielone / żółte / czerwone),
  - stanie,
  - portalu,
  - modelach.
- **⚙ Ustawienia**: wszystkie progi i koszty, edytowalne bez zmian w kodzie:
  - portale i tempo pobierania,
  - minimalny zysk dla każdego trybu,
  - kanały sprzedaży i prowizje,
  - koszty zakupu i naprawy,
  - opłaty kupującego (np. ochrona kupujących na Vinted),
  - parametry wyceny rynkowej i ręczne wartości rynkowe,
  - progi werdyktu i kolorów,
  - kary za flagi.
- **🔧 Tabela części**: ceny części dla każdego modelu (wiersz „*” to cena domyślna),
  z filtrem po modelu. Możesz dodawać i usuwać pozycje.
- Pobierane są frazy „iphone”, w trybie naprawy dodatkowo „iphone uszkodzony / zbity / na części”,
  oraz frazy dopisane w ustawieniach. Z każdej frazy pobierane są maksymalnie 3 strony wyników. Aplikacja pomija:
  - akcesoria i części (etui, szkła, wyświetlacze…),
  - ogłoszenia „kupię / skup / zamienię”,
  - oferty z nierozpoznanym modelem.
- Ceny wszystkich zebranych ofert zasilają bazę do liczenia wartości rynkowej.
  Im dłużej aplikacja działa, tym dokładniejsze są wyceny.

Gotowy plik `PhoneBot.exe` (bez instalowania Pythona) pojawi się w etapie 5.

Dane aplikacji (baza SQLite, logi, miniatury) trafiają do `%LOCALAPPDATA%\PhoneBot`.
Inne miejsce ustawisz zmienną środowiskową `PHONEBOT_HOME`.

## Jak działa wycena

Każde ogłoszenie przechodzi przez następujące kroki:

1. **Normalizacja** (`core/normalizer.py`): z tytułu, opisu i parametrów portalu
   wyciągane są model, pojemność, stan, kondycja baterii, gotowość do negocjacji
   i lista usterek. Rozpoznawanie uwzględnia zaprzeczenia: „ekran nie zbity” czy
   „bez blokad iCloud” nie są traktowane jak usterki ani flagi.
2. **Czerwone flagi** (`core/red_flags.py`): blokada iCloud, zablokowany IMEI,
   MDM, podróbka, simlock, brak zasięgu, zamienniki, „na części” bez opisu,
   brak zdjęć, podejrzanie niska cena i nieznany koszt naprawy. Flagi obniżają
   ocenę (kary można edytować), ale domyślnie nie zmieniają werdyktu.
3. **Wartość rynkowa V** (`core/market.py`): mediana cen porównywalnych ofert
   (ten sam model, pojemność i klasa stanu) z ostatnich 30 dni, po odrzuceniu
   wartości odstających (IQR). Wynik jest mnożony przez korektę 0,90, bo ceny
   wywoławcze są wyższe od transakcyjnych. Gdy danych brakuje, stosowana jest
   kolejno: mediana z mniejszej próby, mediana innych pojemności (przeliczona)
   albo wartość wpisana ręcznie w ustawieniach.
4. **Koszty i zysk** (`core/valuation.py`):
   - naprawa R = części z edytowalnej tabeli + wysyłka części + własna robocizna
     (usterka o nieznanym koszcie, np. „nie włącza się”, liczy się jako ryzyko 200 zł),
   - dostarczenie Sb = wysyłka albo dojazd (odległość × 2 × stawka za km),
   - sprzedaż Cs = prowizja wybranego kanału + opłata stała + wysyłka + pakowanie,
   - **zysk = V − cena − R − Sb − Cs**.
5. **Maksymalna cena zakupu** to najwyższa cena, przy której zysk jest nie
   mniejszy niż wymagany: kwota (np. 150 zł) i/lub procent od zainwestowanej
   kwoty (np. 20%). Tryb łączenia tych progów ustawisz w opcjach.
6. **Werdykt i negocjacje** (`core/negotiation.py`):
   - **KUPUJ**: cena ≤ max. Dostajesz sugestię delikatnej propozycji niższej ceny.
   - **NEGOCJUJ**: cena do 15% powyżej max (do 25%, gdy w ogłoszeniu jest
     „do negocjacji”). Dostajesz cenę otwierającą i maksymalną.
   - **ODPUŚĆ**: w pozostałych przypadkach, a także przy „cenie ostatecznej” powyżej max.
7. **Ocena 0–100 i kolor**: ocena uwzględnia atrakcyjność ceny, pewność
   wyceny rynkowej i kary za flagi. Kolor zielony oznacza ≥ 65 pkt, żółty ≥ 40 pkt,
   czerwony poniżej 40 pkt.

W trybie **„Szybki resell”** telefony z usterkami wymagającymi naprawy dostają
ODPUŚĆ. Słaba bateria i drobne wgniecenia nie dyskwalifikują oferty.

### Wartości domyślne do weryfikacji

Ceny części (`core/parts.py`) i prowizje portali (`core/settings.py`) to
**wartości orientacyjne**. Po uruchomieniu GUI można je edytować w aplikacji.
Sprawdź zwłaszcza:

- ceny części u swojego dostawcy,
- aktualny cennik OLX, jeśli tam sprzedajesz (opłaty za ogłoszenia w kategorii Telefony),
- prowizję Allegro dla smartfonów (domyślnie 8% + 1 zł).

## Struktura projektu

```
phonebot/
  core/          logika domenowa (bez GUI i sieci) — w pełni testowana
    models.py        modele: oferta, stan, usterki, flagi, wynik wyceny
    text.py          normalizacja tekstu i dopasowanie fraz z zaprzeczeniami
    catalog.py       katalog modeli iPhone i pojemności
    normalizer.py    model / pojemność / stan / usterki / bateria / negocjacje
    red_flags.py     czerwone flagi
    market.py        wartość rynkowa (mediana, IQR, fallbacki)
    parts.py         domyślna tabela cen części
    valuation.py     koszty, zysk, max cena zakupu
    negotiation.py   werdykt, negocjacje, ocena i kolor
    settings.py      wszystkie ustawienia (JSON w bazie)
    geo.py           odległości
  storage/       SQLite: schemat z migracjami, repozytoria
  core/listing_filter.py  wieloetapowy filtr: akcesoria, części, „kupię”, test ceny
  net/http.py    klient HTTP: limit zapytań na host, ponawianie (tenacity), cache odpowiedzi
  services/      evaluator.py (baza + wycena), scanner.py (równoległe pobieranie z izolacją błędów),
                 post_scan.py (AI + powiadomienia po skanie), ai_analysis.py (Claude),
                 notifications.py (Telegram)
  core/view_filter.py  filtry widoku;  core/places.py  wbudowana lista miejscowości
  net/geocode.py wyszukiwanie miejscowości (OpenStreetMap Nominatim)
  sources/       adaptery portali: base.py (interfejs), allegro_lokalnie.py, vinted.py, sprzedajemy.py,
                 extract.py (odporne wyciąganie ofert z JSON osadzonego w stronach)
  ui/            GUI PySide6: main_window.py, table_model.py, offer_details.py (+ details_html.py),
                 images.py (miniatury), workers.py (wątek), theme.py (kolory), filters_panel.py,
                 settings_dialog.py, parts_editor.py, location_dialog.py
tests/           testy jednostkowe (+ fixtures z przykładowymi odpowiedziami portali)
tools/           screenshot.py — zrzut okna na danych testowych
phonebot.spec    konfiguracja PyInstaller (PhoneBot.exe); run_phonebot.py — punkt wejścia
assets/          ikona aplikacji
```

Aby dodać kolejny portal, utwórz plik w `sources/` z klasą dziedziczącą po
`SourceAdapter` (metoda `search`) i oznacz ją dekoratorem `@register`.
Awaria jednego adaptera jest izolowana i nie zatrzymuje pozostałych.

## Plan etapów

1. ✅ Architektura, baza danych i logika wyceny z testami.
2. ✅ Pierwszy adapter i podstawowa tabela ofert w GUI (adapter OLX później usunięty, patrz niżej).
3. ✅ Kolorowanie, werdykty i rekomendacje negocjacji w GUI (okno szczegółów).
4. ✅ Allegro Lokalnie i Vinted, filtry, tryby, okno ustawień, wybór miejscowości i edytor tabeli części.
5. ✅ Automatyczne odświeżanie, zasobnik systemowy, powiadomienia Windows i Telegram, opcjonalna
   analiza opisów przez AI (Claude), wydajność (5000 ofert: wczytanie ok. 0,7 s, filtrowanie
   poniżej 10 ms) oraz gotowy plik `PhoneBot.exe` budowany automatycznie przez GitHub Actions.

## Uwaga o źródłach danych

Allegro Lokalnie nie ma API dla ogłoszeń, więc adapter czyta dane osadzone w stronie wyników
(JSON / JSON-LD). Wyszukuje w nich obiekty wyglądające jak oferta, zamiast polegać na sztywnej
ścieżce, dlatego drobne zmiany serwisu go nie psują. Jeśli serwis całkowicie zmieni wygląd,
aplikacja zgłosi błąd źródła („możliwa zmiana formatu serwisu”).
Allegro Lokalnie podaje tylko nazwę miasta. Odległość jest liczona, gdy to miasto jest
na wbudowanej liście miejscowości.

**OLX i Facebook Marketplace nie są obsługiwane.** OLX blokuje automatyczne pobieranie
(zapora CloudFront odpowiada „403 Request blocked” na API i stronę wyników), a jego oficjalne
Partner API służy tylko do zarządzania własnymi ogłoszeniami. Facebook Marketplace nie ma
publicznego API, a regulamin Meta zabrania automatycznego zbierania danych. Adapter OLX
został usunięty z projektu. Stare oferty z OLX znikają z listy przy aktualizacji bazy,
ale ich ceny nadal zasilają wycenę rynkową do końca okna czasowego (30 dni).
OLX pozostaje dostępny jako **kanał sprzedaży** w ustawieniach zysku, bo dotyczy Twojej
ręcznej sprzedaży, a nie pobierania ofert.

Vinted działa w trybie „best effort”. We wrześniu 2026 Vinted przeniósł katalog na
`api.vinted.pl/svc-catalogue/items` (stary adres zwraca 404) i adapter korzysta już z nowego. Adapter pobiera anonimowy token sesji
(ciasteczko `access_token_web`) i wysyła go do endpointu katalogu. Vinted często blokuje automaty; wtedy pasek stanu
pokaże błąd, a pozostałe portale działają normalnie. Katalog Vinted nie zawiera opisów ofert,
więc usterki rozpoznawane są tylko z tytułu. Do kosztu zakupu doliczana jest opłata za ochronę
kupujących: dokładna, jeśli podaje ją Vinted, w przeciwnym razie z ustawień (domyślnie 5% + 2,90 zł,
wartość orientacyjna).

Sprzedajemy.pl nie ma API. Strona wyników zawiera listę ofert w standardowym formacie JSON-LD,
którą czyta ten sam uniwersalny ekstraktor co Allegro Lokalnie. Miasto jest odczytywane z adresu
ogłoszenia. Pobierana jest pierwsza strona wyników na frazę, a ceny filtruje sama aplikacja.

Adaptery Allegro Lokalnie i Vinted są przetestowane na przykładowych danych w `tests/fixtures/`.
Pierwsze uruchomienie na prawdziwych portalach może wymagać dopasowania.

Żaden z trzech portali nie udostępnia publicznego API do wyszukiwania cudzych ogłoszeń.
Adaptery będą korzystać z danych, które strony same ładują w przeglądarce. Każdy adapter:

- ma limit zapytań (domyślnie jedno zapytanie na 4 s na portal),
- korzysta z cache,
- przy błędach ponawia próby z rosnącym opóźnieniem.

Automatyczne pobieranie może naruszać regulaminy portali. Używaj aplikacji
na własną odpowiedzialność, wyłącznie do użytku osobistego i z umiarkowaną
częstotliwością odświeżania.
