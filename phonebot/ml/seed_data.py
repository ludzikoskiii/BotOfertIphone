"""Zbiór startowy dla klasyfikatora tytułów — żeby model działał od pierwszego uruchomienia.

Klasy: ``phone`` (telefon na sprzedaż, także uszkodzony / na części), ``accessory`` (akcesorium),
``part`` (pojedyncza część), ``wanted`` (kupię / szukam / zamienię).

* ``handwritten()`` — przykłady pisane ręcznie, po polsku i w językach sąsiednich,
* ``generated()`` — przykłady z szablonów (deterministycznie: ten sam zbiór przy każdym treningu),
* ``BENCHMARK`` — prawdziwe tytuły z portali (próby z września 2026, zrzut ekranu, testy);
  NIE są używane do treningu — służą do uczciwego pomiaru skuteczności.
"""
from __future__ import annotations

import random

LABELS = ("phone", "accessory", "part", "wanted")
LABEL_NAMES = {"phone": "telefon", "accessory": "akcesorium", "part": "część", "wanted": "kupię / zamienię"}

_HANDWRITTEN: dict[str, list[str]] = {
    "phone": [
        "iPhone 13 128GB", "iPhone 13 128GB Midnight, bateria 89%", "Apple iPhone 12 Pro Max 256 GB grafitowy",
        "iPhone 11 64GB czarny stan bardzo dobry", "iPhone XR 64GB czerwony", "iPhone 12 mini 64GB biały",
        "iPhone 14 Pro 128GB gwiezdna czerń", "iPhone 15 128GB niebieski gwarancja", "iPhone SE 2020 64GB",
        "iPhone 8 64GB złoty", "iPhone X 256GB srebrny", "iPhone XS Max 64GB", "iPhone 11 Pro 256GB zielony",
        "iPhone 13 mini 128GB różowy", "iPhone 14 Plus 256GB fioletowy", "iPhone 15 Pro Max 256GB tytan naturalny",
        "iPhone 16 128GB ultramaryna", "iPhone 16 Pro 256GB pustynny tytan", "iPhone 12 64GB zbity ekran",
        "iPhone 11 zbity wyświetlacz, reszta sprawna", "iPhone 13 pęknięta tylna szyba", "iPhone XR na części",
        "iPhone 12 Pro nie włącza się", "iPhone 11 bootloop, na części lub do naprawy", "iPhone X blokada iCloud",
        "iPhone 13 128GB + etui gratis", "iPhone 12 z pudełkiem i ładowarką", "iPhone 14 Pro 128GB komplet",
        "iPhone 13 Pro 256GB, etui, szkło, pudełko", "Sprzedam iPhone 11 64GB", "Sprzedam lub zamienię iPhone 12",
        "Iphone 13 256gb", "IPHONE 12 MINI", "iphone 11 pro max 64", "Iphone 7 128gb", "iPhone 8 plus 256",
        "Apple iPhone 13 (128 GB) - Północ", "Telefon iPhone 12 128GB", "Smartfon Apple iPhone 14 128GB",
        "iPhone 13 128GB bateria 100%, jak nowy", "iPhone 15 Pro 128GB nowy zaplombowany",
        "iPhone 12 Pro 128GB face id nie działa", "iPhone 11 64GB bez face id", "iPhone 13 mini zablokowany",
        "iPhone 14 128GB e-sim", "iPhone 12 64GB simlock Orange", "iPhone 11 128GB, wymieniona bateria",
        "iPhone XS 64GB nieoryginalny ekran", "iPhone 13 Pro Max 1TB", "iPhone 16e 128GB biały",
        "iPhone Air 256GB", "iPhone 17 Pro 256GB pomarańczowy", "iPhone 12 128GB, 2 lata gwarancji",
        "2x iPhone 11 na części", "iPhone 11 dawca", "iPhone 12 do naprawy, płyta sprawna",
        # czeski / słowacki
        "Prodám iPhone 12 mini 64GB", "iPhone 13 128GB, velmi dobrý stav", "Predám iPhone 11 64GB čierny",
        "iPhone 12 Pro 256GB zánovní", "iPhone 11 prasklý displej, funkční", "iPhone XR 64GB modrá",
        # niemiecki
        "iPhone 13 128GB Zustand sehr gut", "iPhone 12 64GB gebraucht, Akku 88%", "iPhone 11 Pro defekt Display",
        "iPhone 14 Pro 256GB neuwertig mit OVP",
        # litewski
        "Parduodu iPhone 12 128GB", "iPhone 11 64GB, geros būklės", "iPhone 13 naujas",
        # angielski / francuski / hiszpański
        "iPhone 13 128GB unlocked great condition", "iPhone 12 Pro 128GB cracked back, works perfectly",
        "iPhone 14 Pro Max 256GB brand new sealed", "iPhone 11 64 Go très bon état", "iPhone 12 128GB como nuevo",
    ],
    "accessory": [
        "Etui do iPhone 13", "Etui iPhone 12 Pro silikonowe czarne", "Case iPhone 14 Pro Max przezroczysty",
        "Szkło hartowane iPhone 13", "Szkło 9H do iPhone 11", "Folia ochronna iPhone 12 mini",
        "Ładowarka 20W USB-C do iPhone", "Kabel lightning 1m oryginalny", "Pudełko po iPhone 13 Pro",
        "Atrapa iPhone 14 Pro", "Obudowa iPhone 12 niebieska", "Pokrowiec skórzany iPhone 13 mini",
        "Uchwyt samochodowy MagSafe", "Portfel MagSafe do iPhone", "Pasek do Apple Watch 44mm",
        "Etui z klapką iPhone 11", "Etui iPhone 7/8/SE", "Etui iPhone 12/13/14", "3 etui na iPhone 13",
        "Popsocket uchwyt na telefon", "Słuchawki EarPods lightning", "Powerbank MagSafe 5000 mAh",
        "Obal na iPhone 13 průhledný", "Kryt iPhone 12 mini", "Pouzdro iPhone 11 Pro kožené",
        "Tvrzené sklo iPhone 14", "Puzdro na iPhone 13 čierne", "Hülle für iPhone 14 Pro Max",
        "Schutzhülle iPhone 13 Silikon", "Panzerglas iPhone 15 Pro", "Dėklas iPhone 13", "Apsauginis stiklas iPhone 12",
        "iPhone 13 Pro Max case", "MagSafe case iPhone 15 Pro", "Screen protector iPhone 14", "Tempered glass iPhone 13",
        "iPhone 16 Pro Max silicone phone cases", "Wallet case iPhone 12", "Coque iPhone 14 transparente",
        "Funda iPhone 13 silicona", "Custodia iPhone 12", "13 14 15 terakota skal obal", "obal 13 14 15 pro",
        "iPhone 13 kryt", "iPhone 14 obal", "iPhone 12 etui", "Etui gratis",
    ],
    "part": [
        "Wyświetlacz iPhone 11 OLED", "Sam wyświetlacz iPhone 12 oryginalny", "Ekran LCD iPhone XR",
        "Płyta główna iPhone 12 sprawna", "Bateria do iPhone X", "Akumulator iPhone 11 nowy",
        "Taśma flex iPhone 8 ładowania", "Tylna szyba iPhone 13 niebieska", "Klapka baterii iPhone 12",
        "Korpus iPhone 11 Pro z ramką", "Aparat tylny iPhone 12 Pro", "Kamera przednia iPhone 13",
        "Głośnik iPhone 11", "Port ładowania iPhone X", "Taptic engine iPhone 12", "Moduł Face ID iPhone 13",
        "Tylko płyta iPhone 11 Pro 64GB", "Szufladka SIM iPhone 12", "Zestaw części iPhone 11",
        "Displej iPhone 11", "Displej na iPhone 12 Pro", "Baterie iPhone XS", "Akku iPhone 12 neu",
        "Display iPhone 13 original", "Screen replacement iPhone 12", "Battery iPhone 11 new",
        "iPhone 12 płyta główna", "iPhone 11 wyświetlacz zamiennik", "iPhone 13 tylna obudowa z ramką",
    ],
    "wanted": [
        "Kupię iPhone 13 uszkodzony", "Kupię każdego iPhone'a", "Szukam iPhone 12 mini", "Skup telefonów iPhone",
        "Zamienię iPhone 11 na Samsung", "Kupię iPhone zablokowany na części", "Poszukuję iPhone 13 Pro",
        "Skupuję iPhone'y, płacę gotówką", "Kupię iPhone 14 Pro za gotówkę", "Wymienię iPhone 12 na laptopa",
        "Koupím iPhone 13", "Hledám iPhone 12", "Kúpim iPhone 11", "Kaufe iPhone defekt", "Suche iPhone 13 Pro",
        "Wanted iPhone 12 broken", "Buying iPhones any condition", "Perku iPhone 13",
    ],
}

_MODELS = ["iPhone 8", "iPhone X", "iPhone XR", "iPhone XS", "iPhone 11", "iPhone 11 Pro", "iPhone 11 Pro Max",
           "iPhone 12 mini", "iPhone 12", "iPhone 12 Pro", "iPhone 12 Pro Max", "iPhone 13 mini", "iPhone 13",
           "iPhone 13 Pro", "iPhone 13 Pro Max", "iPhone 14", "iPhone 14 Plus", "iPhone 14 Pro", "iPhone 14 Pro Max",
           "iPhone 15", "iPhone 15 Pro", "iPhone 15 Pro Max", "iPhone 16", "iPhone 16 Pro", "iPhone SE 2022"]
_STORAGE = [64, 128, 128, 256, 256, 512]
_COLORS = ["czarny", "biały", "niebieski", "zielony", "fioletowy", "różowy", "czerwony", "Midnight", "Starlight",
           "grafitowy", "złoty", "srebrny", "tytan naturalny", "gwiezdna czerń", "Space Gray"]
_PHONE_T = [
    "{m} {s}GB", "Apple {m} {s} GB {c}", "{m} {s}GB stan idealny", "{m} {s}GB bateria {b}%", "Sprzedam {m} {s}GB",
    "{m} {s}GB zbity ekran", "{m} {s}GB pęknięty tył, sprawny", "{m} na części", "{m} {s}GB nie włącza się",
    "{m} {s}GB + etui gratis", "{m} {s}GB z pudełkiem i ładowarką", "{m} {c} {s}GB, bez blokad",
    "{m} {s}GB kondycja baterii {b}%", "{m} {s}GB uszkodzony", "{M} {s}", "{m} {s}GB, etui i szkło w zestawie",
    "Prodám {m} {s}GB", "{m} {s}GB Zustand gut", "Parduodu {m} {s}GB", "{m} {s}GB unlocked, good condition",
    # krótkie tytuły bez pojemności — częste na Sprzedajemy.pl i Vinted
    "{m}", "{M}", "{m} na gwarancji", "{m} okazja", "{m} tanio", "{m} stan bdb", "{m} jak nowy", "{m} sprawny",
    "{m} {c}", "Apple {m}", "{m} pilnie sprzedam", "Sprzedam {m}", "{m} stan idealny!", "{M} zadbany",
    "{m} używany", "{m} z gwarancją", "{m} bez rys", "{m} do negocjacji",
]
_ACC = ["etui", "case", "szkło hartowane", "szkło 9H", "folia ochronna", "pokrowiec", "etui z klapką",
        "etui silikonowe", "obudowa", "pudełko po", "atrapa", "ładowarka do", "kabel do", "uchwyt na",
        "obal", "kryt", "pouzdro", "puzdro", "tvrzené sklo", "Hülle", "Schutzhülle", "Panzerglas", "dėklas",
        "screen protector", "tempered glass", "cover", "coque", "funda", "custodia", "magsafe case"]
_ACC_T = ["{a} {m}", "{A} do {m}", "{A} na {m} {c}", "{m} {a}", "Nowe {a} {m}", "{A} {m} {c}", "{A} {g1}/{g2}/{g3}",
          "{g1} {g2} {g3} {c} {a}", "{A} iPhone {g1} {g2}", "2x {a} {m}"]
_PARTS = ["wyświetlacz", "ekran LCD", "ekran OLED", "płyta główna", "bateria", "akumulator", "taśma flex",
          "tylna szyba", "klapka", "korpus", "ramka", "aparat tylny", "kamera przednia", "głośnik",
          "port ładowania", "taptic engine", "moduł Face ID", "szufladka SIM", "displej", "Akku", "display"]
_PART_T = ["{P} {m}", "{P} do {m} oryginał", "Oryginalny {p} {m}", "Sam {p} {m}", "{P} {m} zamiennik",
           "Nowy {p} {m}", "{m} {p}", "Tylko {p} {m}"]
_WANTED_T = ["Kupię {m}", "Kupię {m} uszkodzony", "Szukam {m}", "Skup {m}", "Zamienię {m} na {m2}",
             "Kupię {m} na części", "Poszukuję {m} {s}GB", "Koupím {m}", "Kaufe {m}", "Wanted {m}"]


def handwritten() -> list[tuple[str, str]]:
    return [(title, label) for label, titles in _HANDWRITTEN.items() for title in titles]


def generated(seed: int = 2026) -> list[tuple[str, str]]:
    rng = random.Random(seed)
    out: list[tuple[str, str]] = []

    def pick(seq):
        return seq[rng.randrange(len(seq))]

    for _ in range(300):
        m = pick(_MODELS)
        title = pick(_PHONE_T).format(m=m, M=m.upper() if rng.random() < 0.3 else m.lower(), s=pick(_STORAGE),
                                      c=pick(_COLORS), b=rng.randrange(78, 101))
        out.append((title, "phone"))
    for _ in range(190):
        a = pick(_ACC)
        gens = rng.sample(["11", "12", "13", "14", "15", "16"], 3)
        title = pick(_ACC_T).format(a=a, A=a[:1].upper() + a[1:], m=pick(_MODELS), c=pick(_COLORS),
                                    g1=gens[0], g2=gens[1], g3=gens[2])
        out.append((title, "accessory"))
    for _ in range(110):
        p = pick(_PARTS)
        out.append((pick(_PART_T).format(p=p, P=p[:1].upper() + p[1:], m=pick(_MODELS)), "part"))
    for _ in range(60):
        out.append((pick(_WANTED_T).format(m=pick(_MODELS), m2=pick(_MODELS), s=pick(_STORAGE)), "wanted"))
    return out


def seed_examples() -> list[tuple[str, str]]:
    """Wszystkie przykłady startowe (bez duplikatów i bez tytułów z zestawu kontrolnego)."""
    seen: set[str] = {title.lower() for title, _ in BENCHMARK}  # kontrola musi być „nieznana” modelowi
    out = []
    for title, label in handwritten() + generated():
        key = title.lower()
        if key not in seen:
            seen.add(key)
            out.append((title, label))
    return out


# prawdziwe tytuły — tylko do pomiaru skuteczności (nie do treningu)
BENCHMARK: list[tuple[str, str]] = [
    # zrzut ekranu z Vinted
    ("13 14 15 terakota skal obal", "accessory"),
    # Vinted (próby we wrześniu 2026)
    ("Apple iPhone 16e 128GB Black AT&T Only Locked Smartphone Great Condition", "phone"),
    ("iPhone 16 charger case.", "accessory"), ("MagSafe case with stand, iPhone 17", "accessory"),
    ("Leopard Print Glitter Mobile Phone case for iPhone 17 Pro", "accessory"),
    ("IPhone 18 Pro Max phone case - Burgundy", "accessory"), ("Quad lock case for iPhone 16 motorcycle and cycle",
                                                                "accessory"),
    ("Rhinoshield iPhone 13 mini front and back screen protector", "accessory"), ("BURGA case iPhone pro 16",
                                                                                  "accessory"),
    ("iPhone 15 phone case/wallet", "accessory"), ("Brand new LED iPhone 14 case", "accessory"),
    ("Iphone 17", "phone"), ("iPhone 17 pro max Verizon", "phone"), ("iPhone 16 pro max 1 TB", "phone"),
    ("Apple iPhone 18 Pro Max - 256 GB - Black (Unlocked)", "phone"),
    # Allegro Lokalnie
    ("IPHONE APPLE 8  IOS 16.7.16", "phone"), ("Apple IPhone 16 pro 128gb Natural Titanium", "phone"),
    ("iPhone 15 256 GB jak nowy", "phone"), ("IPhone 16 Pro 256GB", "phone"),
    ("Iphone 18 pro 256 burgund nowy zaplombowany faktura apple store", "phone"),
    ("Wyświetlacz Do iPhone 15 Pro Max LCD Ekran Soft OLED DD IC +Uszczelka", "part"),
    ("Apple Iphone 16 128gb bialy na czesci", "phone"), ("Smartfon Apple IPhone 16 Pro 128GB WOLNY RYNEK! GWARANACJ!",
                                                        "phone"),
    ("Apple iPhone Air 256GB + Bateria MagSafe | Bateria 100% | Zestaw + Paragon", "phone"),
    ("iPhone 16 Pro Max 256 GB – Czarny Tytan – bateria 95%", "phone"), ("iPhone 17 PRO stan jak nowy", "phone"),
    # Sprzedajemy.pl
    ("IPHONE 12 MINI", "phone"), ("Iphone stan idealny!", "phone"), ("IPhone 8 64bg", "phone"),
    ("Iphone 14 na gwarancji", "phone"), ("Iphone Apple SE", "phone"), ("IPhone X 256GB", "phone"),
    ("IPhone 13pro 256", "phone"), ("etui iphone 13 pro", "accessory"), ("Etui iPhone 17", "accessory"),
    ("iPhone 17 Pro Max 256 G wersja e-sim większa bateria ! + nowe szkło o etui", "phone"),
    ("Etui na iPhone 17 Anime Grunge Waifu Samurai Emo Japanisch Anime Girl", "accessory"),
    ("Iphone 16 pro 256gb 99% kondycji kolor Czarny komplet plus Etui", "phone"),
    ("Zadbany Apple iPhone 13 256 GB (Północ) – Świetny stan + etui", "phone"),
    # przypadki graniczne z testów filtra
    ("iPhone 12 z pudełkiem i ładowarką", "phone"), ("Etui gratis! iPhone 13 128GB", "phone"),
    ("iPhone 11 nie włącza się, płyta?", "phone"), ("iPhone 11 kompletny, płyta sprawna", "phone"),
    ("Pudełko po iPhone 13 Pro", "accessory"), ("Sam wyświetlacz iPhone 11 oryginalny", "part"),
    ("Płyta główna iPhone 12 sprawna", "part"), ("Kupię iPhone 13 uszkodzony", "wanted"),
    ("Zamienię iPhone 11 na Samsung", "wanted"), ("Szkło hartowane iPhone 12 Pro", "accessory"),
    ("Bateria do iPhone X", "part"), ("Tylna szyba iPhone 13 niebieska", "part"),
    ("Hülle iPhone 13 Pro Max schwarz", "accessory"), ("Dėklas iPhone 14", "accessory"),
    ("Prodám iPhone 11 64GB", "phone"), ("Kryt na iPhone 12 mini", "accessory"),
]
