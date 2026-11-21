"""Automatic categorisation of drinks.

Three passes, the first one that matches wins:
1. category tags from Open Food Facts (most reliable),
2. product name / description / category text,
3. well-known brand names.
Text from web search results is noisy, so for that a vote count is used instead.
"""

import re
import unicodedata

# key, label, colour (used for the badges)
CATEGORIES: list[tuple[str, str, str]] = [
    ("whisky", "Whisky", "#b8741a"),
    ("rum", "Rum", "#8c4a2f"),
    ("gin", "Gin", "#3d8f8a"),
    ("vodka", "Wodka", "#5b7fb8"),
    ("tequila", "Tequila & mezcal", "#9a9a2e"),
    ("brandy", "Cognac & brandy", "#a0522d"),
    ("jenever", "Jenever & korenwijn", "#7a8f3d"),
    ("likeur", "Likeur", "#b0458a"),
    ("bitter", "Bitter & amaro", "#a33b3b"),
    ("aperitief", "Vermout & aperitief", "#d0663a"),
    ("port", "Port & sherry", "#7b2d4a"),
    ("wijn", "Wijn", "#8e2443"),
    ("bubbels", "Mousserende wijn", "#c9a227"),
    ("bier", "Bier", "#d49a1f"),
    ("cider", "Cider", "#7fa33a"),
    ("mixer", "Frisdrank & mixers", "#2f8fc4"),
    ("siroop", "Siroop", "#c43d6b"),
    ("sap", "Sap", "#e08a1e"),
    ("additief", "Additieven & garnering", "#5f7f6a"),
    ("overig", "Overig", "#7a8494"),
]
KEYS = [c[0] for c in CATEGORIES]
LABELS = {k: label for k, label, _ in CATEGORIES}
COLORS = {k: color for k, _, color in CATEGORIES}
ORDER = {k: i for i, k in enumerate(KEYS)}
DEFAULT = "overig"


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(ch for ch in text if not unicodedata.combining(ch)).lower()
    text = re.sub(r"[^a-z0-9&']+", " ", text)
    return " ".join(text.split())


def _pattern(words: list[str]) -> re.Pattern:
    alts = sorted({re.escape(normalize(w)) for w in words}, key=len, reverse=True)
    return re.compile(r"(?<![a-z0-9])(?:" + "|".join(alts) + r")(?![a-z0-9])")


# Order matters: more specific rules first (e.g. "ginger beer" before beer,
# "cream liqueur" before whisky, "port wine" before wine).
_RULE_WORDS: list[tuple[str, list[str]]] = [
    ("additief", ["cocktail bitters", "aromatic bitters", "orange bitters", "angostura", "peychaud",
                  "peychaud's", "cocktail cherries", "cocktail cherry", "cocktailkersen", "maraschino cherries",
                  "luxardo cherries", "cocktail olives", "cocktail onions", "garnish", "garnering", "foamer",
                  "cocktail foamer", "aquafaba", "egg white", "eiwit", "fruit puree", "vruchtenpuree",
                  "lime cordial", "cordial", "rimming salt", "rimming sugar", "cocktail mix", "sour mix",
                  "margarita mix", "bloody mary mix", "cocktail additive", "additief", "additieven"]),
    ("mixer", ["bitter lemon", "ginger ale", "ginger beer", "tonic water", "tonic waters",
               "indian tonic", "tonics"]),
    ("bubbels", ["champagne", "champagnes", "prosecco", "proseccos", "cava", "cremant",
                 "sparkling wine", "sparkling wines", "mousserende wijn", "schuimwijn", "sekt",
                 "spumante", "asti", "vin mousseux", "vins mousseux", "franciacorta"]),
    ("port", ["port wine", "port wines", "porto", "portwijn", "port", "tawny", "sherry", "sherries",
              "madeira", "marsala", "jerez", "fino", "oloroso", "fortified wine", "fortified wines",
              "versterkte wijn", "pedro ximenez"]),
    ("bitter", ["amaro", "amari", "bitters", "kruidenbitter", "krauterlikor", "krauterlikore",
                "herbal liqueur", "herbal liqueurs", "beerenburg", "beerenburger", "fernet",
                "bitter", "bitter aperitif", "kruidenlikeur"]),
    ("aperitief", ["vermouth", "vermouths", "vermout", "wermut", "aperitif", "aperitifs",
                   "aperitivo", "aperitief", "spritz", "quinquina"]),
    ("likeur", ["liqueur", "liqueurs", "likeur", "likeuren", "likor", "likore", "liquore", "licor",
                "cream liqueur", "cream liqueurs", "amaretto", "limoncello", "triple sec",
                "curacao", "advocaat", "eierlikeur", "eggnog", "sambuca", "schnapps", "creme de",
                "anisette", "ouzo", "pastis", "anise", "bessenjenever", "coffee liqueur",
                "likorette", "likorettes", "likeurtje", "likeurtjes", "shooter", "shooters",
                "shotjes", "fruitlikeur", "kruidenlikeur"]),
    ("whisky", ["whisky", "whiskies", "whiskey", "whiskeys", "bourbon", "bourbons", "scotch",
                "single malt", "blended malt", "rye whiskey", "tennessee whiskey", "malt whisky"]),
    ("jenever", ["jenever", "jenevers", "genever", "genevers", "korenwijn", "graanjenever",
                 "jonge jenever", "oude jenever", "genievre", "corenwyn"]),
    ("gin", ["gin", "gins", "london dry", "sloe gin", "dry gin"]),
    ("vodka", ["vodka", "vodkas", "wodka", "wodkas", "wodka's"]),
    ("rum", ["rum", "rums", "rhum", "rhums", "ron", "cachaca", "spiced rum", "dark rum",
             "white rum"]),
    ("tequila", ["tequila", "tequilas", "mezcal", "mezcals", "mescal", "agave spirit"]),
    ("brandy", ["cognac", "cognacs", "brandy", "brandies", "armagnac", "calvados", "grappa",
                "pisco", "vieux", "weinbrand", "eau de vie", "eaux de vie", "metaxa"]),
    ("cider", ["cider", "ciders", "cidre", "cidres", "sidra", "perry", "appelcider"]),
    ("bier", ["beer", "beers", "bier", "bieren", "biere", "bieres", "lager", "lagers", "pils",
              "pilsner", "pilsener", "ipa", "ale", "ales", "stout", "stouts", "porter", "weizen",
              "weissbier", "witbier", "tripel", "dubbel", "quadrupel", "bock", "bokbier", "radler",
              "trappist", "speciaalbier", "blond bier"]),
    ("wijn", ["wine", "wines", "wijn", "wijnen", "wein", "weine", "vin", "vins", "vino", "vini",
              "red wine", "white wine", "rose wine", "rode wijn", "witte wijn", "rose", "merlot",
              "cabernet", "chardonnay", "sauvignon", "pinot", "rioja", "bordeaux", "chianti",
              "shiraz", "syrah", "malbec", "riesling", "tempranillo", "primitivo", "zinfandel"]),
    ("siroop", ["syrup", "syrups", "siroop", "siropen", "sirop", "sirops", "sirup", "grenadine",
                "ranja", "limonadesiroop"]),
    ("sap", ["juice", "juices", "sap", "sappen", "jus", "nectar", "nectars", "smoothie",
             "fruit juice", "vruchtensap"]),
    ("mixer", ["tonic", "soda", "sodas", "cola", "colas", "lemonade", "lemonades", "limonade",
               "frisdrank", "frisdranken", "soft drink", "soft drinks", "energy drink",
               "energy drinks", "mineral water", "mineral waters", "sparkling water",
               "carbonated drinks", "spuitwater", "bruisend water", "mineraalwater"]),
]
_RULES = [(cat, _pattern(words)) for cat, words in _RULE_WORDS]

_BRAND_WORDS: list[tuple[str, list[str]]] = [
    ("bubbels", ["moet", "moet & chandon", "veuve clicquot", "dom perignon", "freixenet", "mumm",
                 "laurent perrier", "taittinger", "bollinger", "piper heidsieck", "codorniu",
                 "martini asti", "martini prosecco", "ruinart", "pommery", "nicolas feuillatte"]),
    ("jenever", ["ketel 1", "hooghoudt", "bokma", "olifant", "filliers", "rutte", "zuidam",
                 "notaris", "schiedam", "wenneker jenever", "bols genever", "boomsma jenever"]),
    ("whisky", ["johnnie walker", "jameson", "jack daniel", "jack daniels", "jack daniel's",
                "jim beam", "glenfiddich", "glenlivet", "glenmorangie", "macallan", "laphroaig",
                "lagavulin", "ardbeg", "talisker", "chivas", "chivas regal", "ballantine",
                "ballantine's", "j&b", "famous grouse", "bushmills", "maker's mark", "makers mark",
                "wild turkey", "four roses", "bulleit", "monkey shoulder", "dewar's", "dewars",
                "grant's", "tullamore", "jura", "highland park", "oban", "dalwhinnie",
                "auchentoshan", "bowmore", "aberlour", "balvenie", "nikka", "suntory", "hibiki",
                "yamazaki", "woodford reserve", "buffalo trace", "teeling", "redbreast",
                "glen grant", "cardhu", "caol ila", "bruichladdich", "port charlotte",
                "springbank", "kilchoman", "dalmore", "glendronach", "glenallachie", "tomatin",
                "writers tears", "powers", "evan williams", "knob creek", "woodford",
                "jim beam", "canadian club", "crown royal", "label 5", "william lawson's",
                "william lawsons", "clan campbell", "teacher's", "johnnie"]),
    ("rum", ["bacardi", "captain morgan", "havana club", "kraken", "sailor jerry", "diplomatico",
             "appleton", "mount gay", "plantation", "zacapa", "el dorado", "bumbu", "don papa",
             "pampero", "brugal", "matusalem", "stroh", "goslings", "wray & nephew", "pyrat",
             "flor de cana", "botucal", "kingston 62", "sierra madre rum", "santa teresa",
             "dictador", "clement", "trois rivieres", "j bally", "damoiseau", "black tot"]),
    ("gin", ["gordon's", "gordons", "bombay sapphire", "bombay", "tanqueray", "hendrick's",
             "hendricks", "beefeater", "monkey 47", "bulldog", "roku", "the botanist",
             "sipsmith", "malfy", "aviation", "plymouth", "citadelle", "gin mare", "nordes",
             "brockmans", "bobby's", "bobbys", "copperhead", "elephant", "silent pool",
             "seagram's gin", "larios", "puerto de indias"]),
    ("vodka", ["absolut", "smirnoff", "grey goose", "belvedere", "ketel one", "stolichnaya",
               "russian standard", "finlandia", "eristoff", "skyy", "tito's", "titos", "ciroc",
               "zubrowka", "wyborowa", "sobieski", "poliakov", "puschkin", "danzka", "pravda",
               "beluga", "chopin", "nemiroff", "stoli", "vodka 42 below", "42 below"]),
    ("tequila", ["jose cuervo", "cuervo", "patron", "olmeca", "sierra", "don julio", "casamigos",
                 "espolon", "el jimador", "herradura", "sauza", "del maguey", "montelobos",
                 "1800 tequila", "cazcabel", "volcan de mi tierra", "clase azul", "corralejo"]),
    ("brandy", ["hennessy", "remy martin", "martell", "courvoisier", "camus", "metaxa", "otard",
                "vecchia romagna", "st remy", "asbach", "torres 10", "torres 5", "fundador",
                "soberano", "osborne veterano", "chabasse", "pere magloire"]),
    ("additief", ["angostura", "peychaud", "peychaud's", "fee brothers", "the bitter truth",
                  "ms better's", "bob's bitters", "luxardo maraschino cherries", "amarena fabbri"]),
    ("bitter", ["campari", "jagermeister", "fernet branca", "ramazzotti", "averna",
                "montenegro", "underberg", "unicum", "cynar",
                "sonnema", "hoppe beerenburg", "boomsma beerenburg", "chartreuse", "becherovka",
                "killepitsch", "jagerbomb"]),
    ("aperitief", ["aperol", "martini", "cinzano", "noilly prat", "lillet", "dubonnet", "carpano",
                   "select aperitivo", "cocchi", "punt e mes", "antica formula", "suze",
                   "pimm's", "pimms", "st raphael", "byrrh"]),
    ("port", ["sandeman", "taylor's", "taylors", "graham's", "grahams", "dow's", "cockburn's",
              "cockburns", "offley", "ferreira", "croft", "kopke", "niepoort", "tio pepe",
              "harveys", "warre's", "fonseca", "ramos pinto", "noval", "lustau"]),
    ("likeur", ["baileys", "kahlua", "malibu", "cointreau", "grand marnier", "disaronno",
                "licor 43", "frangelico", "tia maria", "southern comfort", "drambuie", "chambord",
                "passoa", "safari", "pisang ambon", "kwai feh", "bols", "de kuyper", "galliano",
                "sheridan's", "sheridans", "st germain", "st-germain", "midori", "archers",
                "dooley's", "dooleys", "mozart", "warninks", "coebergh", "giffard", "marie brizard",
                "pernod", "ricard", "xuxu", "pisco sour", "luxardo", "amarula", "kahlua",
                "fireball", "goldschlager", "berentzen", "olmeca altos", "tequila rose", "licor beirao",
                "mandarine napoleon", "strega", "benedictine", "kuemmerling", "flugel",
                "sourz", "bacardi breezer", "boswandeling", "schrobbelèr", "schrobbeler"]),
    ("bier", ["heineken", "amstel", "grolsch", "hertog jan", "bavaria", "jupiler", "la chouffe",
              "duvel", "leffe", "westmalle", "la trappe", "corona", "desperados", "kirin",
              "budweiser", "peroni", "estrella", "affligem", "hoegaarden", "guinness",
              "brand pils", "texels", "brouwerij 't ij", "uiltje", "kompaan", "lagunitas",
              "brewdog", "chimay", "karmeliet", "westvleteren", "rochefort", "orval", "palm",
              "warsteiner", "beck's", "becks", "carlsberg", "tuborg", "stella artois",
              "paulaner", "erdinger", "weihenstephaner", "gulpener", "lindemans", "kwak",
              "brugse zot", "kasteel", "bud", "san miguel", "mort subite", "liefmans"]),
    ("cider", ["strongbow", "magners", "somersby", "savanna", "apple bandit", "kopparberg",
               "rekorderlig", "bulmers", "stassen", "aspall"]),
    ("mixer", ["schweppes", "fever tree", "fever-tree", "fentimans", "thomas henry",
               "coca cola", "coca-cola", "pepsi", "sprite", "fanta", "7up", "7 up", "red bull",
               "royal club", "chaudfontaine", "spa", "bar le duc", "three cents", "double dutch",
               "london essence", "1724 tonic", "east imperial", "franklin & sons", "aqua monaco",
               "rivella", "dr pepper", "monster energy", "fritz kola", "lipton ice tea",
               "fuze tea", "sourcy", "san pellegrino", "perrier"]),
    ("sap", ["appelsientje", "minute maid", "tropicana", "granini", "innocent", "coolbest",
             "hero", "dubbelfrisss", "dubbelfris", "ocean spray"]),
    ("siroop", ["monin", "1883", "routin", "teisseire", "karvan cevitam", "roosvicee",
                "slimpie", "da vinci gourmet", "torani"]),
]
_BRANDS = [(cat, _pattern(words)) for cat, words in _BRAND_WORDS]


def _first_rule(text: str, rules) -> str | None:
    text = normalize(text)
    if not text:
        return None
    for cat, pattern in rules:
        if pattern.search(text):
            return cat
    return None


def _vote(text: str) -> str | None:
    text = normalize(text)
    if not text:
        return None
    counts: dict[str, int] = {}
    for cat, pattern in _RULES:
        n = len(pattern.findall(text))
        if n:
            counts[cat] = counts.get(cat, 0) + n
    if not counts:
        return None
    return max(counts, key=lambda c: (counts[c], -ORDER[c]))


def classify(tags: list[str] | None = None, texts: list[str] | None = None,
             brand: str = "", web_texts: list[str] | None = None) -> str:
    """The main category."""
    return classify_all(tags, texts, brand, web_texts)[0]


# Spirits that, flavoured (Jim Beam Peach, Jack Daniel's Honey, ...), are also a liqueur.
SPIRITS = ("whisky", "rum", "gin", "vodka", "tequila", "brandy", "jenever")
_FLAVOUR = _pattern(["peach", "perzik", "honey", "honing", "apple", "appel", "cinnamon", "kaneel", "fire",
                     "vanilla", "vanille", "cherry", "kers", "coconut", "kokos", "caramel", "karamel",
                     "salted caramel", "raspberry", "framboos", "strawberry", "aardbei", "passion fruit",
                     "mango", "chocolate", "chocolade", "coffee", "koffie", "cream", "maple", "toffee",
                     "pineapple", "ananas", "blackberry", "braam", "red stag", "flavoured", "flavored"])
_LIQUEUR = dict(_RULES[i] for i in range(len(_RULES)) if _RULES[i][0] == "likeur")["likeur"]
_ALSO = {"port": ["wijn"], "bubbels": ["wijn"]}
# Tags that are too generic to say anything about a second category.
_GENERIC_TAGS = re.compile(r"(?<![a-z0-9])eaux? de vie(?![a-z0-9])")


def _hits(text: str, rules) -> list[str]:
    text = normalize(text)
    return [cat for cat, pattern in rules if text and pattern.search(text)]


def classify_all(tags: list[str] | None = None, texts: list[str] | None = None,
                 brand: str = "", web_texts: list[str] | None = None) -> list[str]:
    """Main category first, followed by any other categories the drink also belongs to.

    A drink can belong to two categories, e.g. Jim Beam Peach is both whisky and liqueur,
    a port is also a wine."""
    tags = [t.split(":", 1)[-1] for t in (tags or [])]
    texts = [t for t in (texts or []) if t]
    main = (
        _first_rule(" | ".join(tags), _RULES)
        or _first_rule(" | ".join(texts), _RULES)
        or _first_rule(" | ".join([brand, *texts]), _BRANDS)
        or _vote(" | ".join(web_texts or []))
        or _first_rule(" | ".join(web_texts or []), _BRANDS)
        or DEFAULT
    )
    if main == DEFAULT:
        return [main]
    tag_text = _GENERIC_TAGS.sub(" ", normalize(" | ".join(tags)))
    own = normalize(" | ".join([brand, *texts]))
    found = set(_hits(tag_text, _RULES)) | set(_hits(own, _RULES)) | set(_hits(own, _BRANDS))
    extra: set[str] = set()
    spirits = [c for c in SPIRITS if c in found]
    if main == "likeur":
        # Whisky liqueur, rum-based liqueur, ...: also list it under the spirit.
        extra.update(spirits[:1])
    elif main in SPIRITS:
        flavoured = _FLAVOUR.search(own) or _FLAVOUR.search(tag_text)
        if "likeur" in found and (_LIQUEUR.search(own) or _LIQUEUR.search(tag_text) or flavoured):
            extra.add("likeur")
        elif flavoured and main in ("whisky", "rum", "brandy", "tequila"):
            extra.add("likeur")
    extra.update(_ALSO.get(main, []))
    extra.discard(main)
    return [main, *sorted(extra, key=lambda c: ORDER[c])]


def label(key: str) -> str:
    return LABELS.get(key, LABELS[DEFAULT])


def valid(key: str) -> bool:
    return key in LABELS
