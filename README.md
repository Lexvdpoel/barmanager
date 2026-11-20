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

Wordt een barcode nergens gevonden, dan staat het product als "Onbekend" in de voorraad. Vul je de
naam in, dan wordt de categorie bepaald en wordt automatisch naar foto's gezocht. Je kunt ook zelf
een foto maken of uploaden. Alle foto's worden opnieuw gecodeerd naar WebP en lokaal opgeslagen.

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
