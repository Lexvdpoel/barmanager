# Changelog

All notable changes to BarManager. The image `ghcr.io/lexvdpoel/barmanager` is tagged with each version;
`latest` follows `main`.

## 0.7.0 (2026-11-22)

### Added
- **English and Dutch.** Every page, message, the scanner and the guest bar menu are in both languages.
  Switch with NL / EN at the top (or on the Account page); the default follows your browser.
- The website [barmanager.club](https://barmanager.club), with screenshots in both languages.
- Code of Conduct, contributing guide, security policy, issue and discussion templates.

### Changed
- The CSV export uses English column names when the app is in English.
- Cocktail amounts use English units (tsp, dash) in English, and stay in cl.

## 0.6.8 (2026-11-22)

### Added
- **Cocktails**: more than 600 recipes from TheCocktailDB, refreshed weekly. Per location: what you can
  make, what is one ingredient away, and which bottle would unlock the most new drinks. Click an
  ingredient to see the right bottles first, then substitutes, and link a bottle by hand.
- **Kitchen & mixers**: tick the juices, sodas, syrups, fruit and spices you have; recipes then show
  exactly what you are missing.
- **Fill levels** (on or off per location): how full the open bottle is, how long it has been open and
  whether it is still good (cream liqueurs, vermouth, wine).
- **Shopping list**: bottles below their minimum, empty bottles and an almost empty last bottle are added
  automatically; with fill levels off, a button on the product page.
- **Statistics**: litres and bottles per month, what is drunk most, value, bottles bought.
- **Value**: price, purchase date and shop per bottle; value per location and in the CSV export.
- **Bar menu for guests**: a public page with QR code showing the bottles and the cocktails you can make.
- New category **Aquavit**.

### Changed
- Smarter matching of bottles to ingredients: aged and dark rum only count as a substitute for light rum,
  rakia and grappa for brandy, blue curaçao for triple sec, and flavoured whisky liqueurs never count as
  real whisky.
- Vermouth is no longer sorted under port and sherry.
- The AI writes countries in English and no longer puts an alcoholic drink in a soft-drink category.
- Letters like ø, æ and ß are recognised in names (Arvesølvet, Weißbier).

## 0.5.2 (2026-11-22)

### Changed
- Photos from product databases are always kept, but web photos come first.
- The scanner pauses after each bottle (tap "Scan the next bottle"), so nothing is counted twice; a louder
  beep that also works on phones.

## 0.5.0 (2026-11-21)

### Added
- Unknown barcodes: the AI reads web search results (SearXNG) to name the bottle.
- Alcohol percentage and "also counts as" categories are filled in from the sources.
- Web photos first, product database photos as extras.

## 0.4.0 (2026-11-21)

### Added
- **Local AI** through Ollama fills in brand, country, description and category, grounded in sources
  (product databases, English Wikipedia, web search). It never guesses numbers.
- SearXNG support for web search.

## 0.3.0 (2026-11-21)

### Added
- More sources (Wikipedia, OpenGTINDB), automatic re-lookup of unknown barcodes, deleting products.
- More than one category per product, and the category Additives & garnish.
- The scanner reads vertical barcodes; the mobile layout fits the screen.

## 0.1.0 (2026-11-21)

- First version: stock per location by scanning barcodes, automatic lookup with photos and categories,
  users and access per location, CSV export.
