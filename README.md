# BarManager

Drankvoorraad bijhouden per locatie door barcodes te scannen.

- Maak een **locatie** aan (thuisbar, kelder, café…) en geef eventueel andere gebruikers toegang.
- **Scan** flessen met de camera van je telefoon (of een USB/Bluetooth-handscanner) om ze toe
  te voegen of af te boeken.
- Een **onbekende barcode** wordt op de achtergrond op internet opgezocht: naam, merk, inhoud,
  alcoholpercentage en **foto's** worden automatisch ingevuld en gedownload.
- Elk product wordt automatisch in een **categorie** ingedeeld (whisky, rum, gin, wodka, jenever,
  likeur, bitter, wijn, bier, frisdrank & mixers, …). De voorraad staat per categorie gegroepeerd en
  je kunt filteren op categorie, zoeken, en filteren op "bijna op" / "op".
- Per product kun je een minimumvoorraad instellen; de voorraad exporteren als CSV.
- **Waarde**: prijs per fles, aankoopdatum en winkel per locatie; de totale waarde staat bij de locatie.
- **Cocktails**: alle recepten van [TheCocktailDB](https://www.thecocktaildb.com) (ruim 600, wekelijks
  bijgewerkt) en per locatie wat je nu kunt maken, wat je met één extra fles kunt maken en welke fles
  de meeste cocktails oplevert. Flessen worden op naam, land en categorie aan ingrediënten gekoppeld
  (Cointreau = triple sec, een Schotse whisky = scotch, een andere whisky als vervanger voor bourbon);
  een niet herkende fles kun je zelf aan een ingrediënt koppelen.
- **Flesinhoud bijhouden** (per locatie aan/uit): hoe vol de geopende fles is, hoe lang hij open
  staat en of hij nog goed is (cream liqueurs, vermout, wijn), plus **statistieken** over wat er
  gedronken en gekocht wordt.
- **Boodschappenlijst**: flessen onder het minimum, en bij flesinhoud bijhouden ook lege en bijna lege
  flessen, komen er vanzelf op; staat het bijhouden uit, dan zet je een fles er met een knop op.
  Inscannen haalt hem er weer af.
- **Barkaart voor gasten**: een openbare pagina (met QR-code) met wat er in de bar staat en welke
  cocktails je kunt maken, zonder aantallen of prijzen. Uit te zetten of te vernieuwen per locatie.

```
telefoon/browser ──https──► reverse proxy ──► BarManager (FastAPI + SQLite, /config)
                                                  │ onbekende barcode
                                                  ▼
                                Open Food Facts · UPCitemdb · (optioneel) Brave Search
```

## Opzoeken op internet

| Bron | Sleutel nodig | Wat |
|---|---|---|
| [Open Food Facts](https://world.openfoodfacts.org) | nee | De meeste dranken uit de Europese winkel: naam, merk, categorie, inhoud, alcohol, foto |
| [UPCitemdb](https://www.upcitemdb.com) (trial) | nee | Aanvulling, vooral internationale producten; max. ±100 opzoekingen per dag |
| [Brave Search API](https://brave.com/search/api/) | ja (`BM_BRAVE_API_KEY`, gratis tier) | Zoekt de barcode op het web als de databases hem niet kennen, en zoekt foto's op productnaam |
| [SearXNG](https://docs.searxng.org) (zelf gehost) | nee (`BM_SEARXNG_URL`) | Gratis alternatief voor Brave: webzoeken op barcode, foto's, en bronnen voor de AI |

Wordt een barcode nergens gevonden, dan staat het product als "Onbekend" in de voorraad. Vul je de
naam in, dan wordt de categorie bepaald en wordt automatisch naar foto's gezocht. Je kunt ook zelf
een foto maken of uploaden. Alle foto's worden opnieuw gecodeerd naar WebP en lokaal opgeslagen.

## Aanvullen met lokale AI (optioneel)

Met een [Ollama](https://ollama.com)-container ernaast (bijv. `qwen3:4b`, draait prima op een
Raspberry Pi 5) vult BarManager op de achtergrond lege velden aan van elk product waarvan de naam
bekend is: merk, land van herkomst, een korte (Engelse) omschrijving en, als de categorie nog
"Overig" is, de categorie. Eerst verzamelt BarManager bronnen over de fles (productdatabase,
Engelse Wikipedia en - met SearXNG of Brave - zoekresultaten op internet); het model mag alleen
feiten uit die bronnen gebruiken. Zonder bronnen schrijft het alleen een algemene omschrijving
van het soort drank, zonder namen, plaatsen of jaartallen. Wat jij of een productdatabase
invulde wordt nooit overschreven, en wat de AI invulde krijgt een paars **AI**-labeltje tot je
het zelf aanpast.

De AI verzint géén product bij een onbekende barcode: een taalmodel kent geen barcodes en zou
een fles verzinnen. Vul bij een onbekende barcode de naam in; de rest volgt dan vanzelf.

```yaml
  ollama:
    image: ollama/ollama:latest
    volumes:
      - ./appdata/ollama:/root/.ollama
    restart: unless-stopped
```

en bij barmanager `BM_OLLAMA_URL=http://ollama:11434`. Het model wordt de eerste keer zelf
gedownload (±2 GB). Op een Pi 5 duurt één product ongeveer een minuut.

## Camera

De camerascanner gebruikt de ingebouwde `BarcodeDetector` van de browser (Chrome/Android) en anders
[ZXing](https://github.com/zxing-js/library) (iPhone/Safari, Firefox). Browsers geven alleen
camera-toegang via **https**, dus zet BarManager achter een reverse proxy met TLS. Een handscanner
werkt altijd: die "typt" de barcode in het tekstveld.

## Installeren

Het image wordt bij elke push naar `main` gebouwd naar `ghcr.io/lexvdpoel/barmanager:latest`
(amd64 + arm64, dus ook voor een Raspberry Pi). Eerst draaien de tests; falen die, dan wordt er geen
image gepubliceerd. Het image is privé: log op de Docker-host eerst in met
`docker login ghcr.io -u GEBRUIKER -p GITHUB_TOKEN` (token met `read:packages`).

```bash
docker run -d --name barmanager -p 8080:8080 \
  -e BM_ADMIN_PASSWORD=verander-mij \
  -v /pad/naar/appdata/barmanager:/config \
  --restart unless-stopped ghcr.io/lexvdpoel/barmanager:latest
```

Of gebruik `docker-compose.yml`. Bij de eerste start wordt beheerder `admin` aangemaakt; zonder
`BM_ADMIN_PASSWORD` staat het gegenereerde wachtwoord in de containerlog.

### Instellingen (omgevingsvariabelen)

| Variabele | Standaard | |
|---|---|---|
| `BM_ADMIN_USERNAME` / `BM_ADMIN_PASSWORD` | `admin` / gegenereerd | Eerste beheerder, alleen bij een lege database |
| `BM_BRAVE_API_KEY` | leeg | Brave Search API voor webzoeken en foto's |
| `BM_UPCITEMDB` | `true` | UPCitemdb gebruiken |
| `BM_MAX_IMAGES` | `3` | Hoeveel foto's er automatisch per product worden opgehaald |
| `BM_LOOKUP_TIMEOUT` | `8` | Time-out per opzoekverzoek (seconden) |
| `BM_SEARXNG_URL` | leeg | Eigen SearXNG (met `json` in `search.formats`), bijv. `http://searxng:8080` |
| `BM_OLLAMA_URL` | leeg | Ollama-server voor AI-aanvulling, bijv. `http://ollama:11434` |
| `BM_OLLAMA_MODEL` | `qwen3:4b` | Welk model Ollama gebruikt |
| `BM_OLLAMA_TIMEOUT` | `600` | Maximale tijd per AI-antwoord (seconden) |
| `BM_SESSION_DAYS` | `30` | Hoe lang je ingelogd blijft |
| `BM_PORT` | `8080` | Poort in de container |
| `PUID` / `PGID` | `99` / `100` | Gebruiker waaronder de app draait (eigenaar van `/config`) |
| `TZ` | `Europe/Amsterdam` | Tijdzone |

In `/config` staan `barmanager.db` (SQLite) en `images/`. Zet de database bij voorkeur op lokale
opslag, niet op een netwerk- of clusterbestandssysteem.

## Ontwikkelen

```bash
python -m venv .venv
.venv/Scripts/pip install -e ".[dev]"     # Linux/macOS: .venv/bin/pip
.venv/Scripts/python -m pytest -q
BM_ADMIN_PASSWORD=test1234 .venv/Scripts/barmanager   # http://localhost:8080
```

| Map | Wat |
|---|---|
| `barmanager/web.py` | Pagina's (locaties, voorraad, producten, gebruikers) |
| `barmanager/api.py` | JSON voor de scanpagina en de +/−-knoppen |
| `barmanager/lookup.py` | Opzoeken van barcodes en foto's |
| `barmanager/categories.py` | Automatische indeling in categorieën |
| `barmanager/service.py` | Voorraadlogica en de achtergrond-opzoeker |
| `barmanager/static/scan.js` | Camerascanner |
