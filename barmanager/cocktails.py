"""Cocktails: recipes from TheCocktailDB and "what can I make with what is in the bar".

Every ingredient of a recipe gets a *spec* that says how a bottle in the bar can supply it:

* ``basis``  - fresh things you are assumed to have (ice, lemon, sugar, mint, eggs, ...);
* ``mixer``  - juices, sodas and syrups; assumed present unless the user switches that off;
* ``drank``  - bottles: matched against the products in stock by name/brand/country and category.

A match is either exact ("Cointreau" for "Triple sec") or a substitute ("Highland Park" for
"Bourbon": a whisky, just not the right kind). Users can override per ingredient: "always in the
house", or link specific products.
"""

from __future__ import annotations

import logging
import math
import re
import string
import threading
import time
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

import httpx

from . import categories, images
from .db import Database

log = logging.getLogger("barmanager.cocktails")

API = "https://www.thecocktaildb.com/api/json/v1/1/"
SYNC_EVERY = 7 * 86400


def norm(text: str) -> str:
    return categories.normalize(text)


# ---- ingredient specs ----------------------------------------------------------------------------
@dataclass(frozen=True)
class Spec:
    kind: str                       # basis | mixer | drank
    words: tuple = ()               # any of these phrases in name/brand/country = exact match
    cats: tuple = ()                # ... and the product must be in one of these categories
    all_cats: bool = False          # every product in `cats` is an exact match
    sub_words: tuple = ()           # substitutes: phrases
    sub_cats: tuple = ()            # substitutes: categories
    not_words: tuple = ()           # a product with one of these is only a substitute (dark rum for light rum)


def D(words=(), cats=(), sub_words=(), sub_cats=(), all_cats=False, not_words=()) -> Spec:
    return Spec("drank", tuple(words), tuple(cats), all_cats, tuple(sub_words), tuple(sub_cats), tuple(not_words))


MIXER_CATS = ("sap", "mixer", "siroop", "additief")


def M(words=(), cats=MIXER_CATS) -> Spec:
    return Spec("mixer", tuple(words), tuple(cats))


BASIS = Spec("basis")

_basis = """sugar, powdered sugar, brown sugar, demerara sugar, lemon, lime, lemon juice, lime juice,
fresh lemon juice, fresh lime juice, lemon peel, lime peel, orange, orange peel, orange spiral, blood orange,
ice, water, milk, coffee, espresso, tea, hot chocolate, egg, egg white, egg yolk, cream, light cream,
heavy cream, whipping cream, whipped cream, half and half, condensed milk, coconut milk, yoghurt, butter,
mint, nutmeg, cinnamon, cloves, ginger, cardamom, allspice, coriander, cumin seed, pepper, black pepper,
cayenne pepper, red chili flakes, salt, celery salt, asafoetida, vanilla, vanilla extract, almond flavoring,
caramel coloring, honey, maple syrup, corn syrup, sugar syrup, simple syrup, olive, olive brine,
maraschino cherry, cherry, cherries, pineapple, apple, banana, mango, kiwi, papaya, strawberries, blackberries,
figs, cucumber, fruit, rosemary, thyme, lavender, rose, chocolate, cocoa powder, oreo cookie, marshmallows,
vanilla ice cream, sherbet, jello, tabasco sauce, hot sauce, worcestershire sauce, soy sauce, soda water,
club soda, carbonated water, kool aid"""

_whisky_scotch = ("scotch", "single malt", "scotland", "schotland", "highland", "speyside", "islay", "glen*",
                  "johnnie walker", "chivas", "ballantine's", "ballantines", "famous grouse", "j&b", "dewar's",
                  "laphroaig", "ardbeg", "lagavulin", "talisker", "macallan", "bowmore", "balvenie", "aberlour",
                  "dalmore", "oban", "auchentoshan", "monkey shoulder", "grant's")
_whisky_us = ("bourbon", "kentucky", "tennessee", "united states", "usa", "verenigde staten", "jim beam",
              "jack daniel's", "jack daniels", "wild turkey", "four roses", "maker's mark", "buffalo trace",
              "woodford", "bulleit", "evan williams", "knob creek", "old forester", "elijah craig")
_whisky_irish = ("irish", "ireland", "ierland", "jameson", "bushmills", "tullamore", "paddy", "powers",
                 "redbreast", "teeling", "writers tears")
# Aged, dark and spiced rums: fine in a Mojito if need be, but not "light rum".
_DARK_RUM = ("dark", "black", "negra", "negro", "navy", "kraken", "myers's", "goslings", "zwart*", "donker*",
             "spiced", "spice*", "anejo", "aged", "year*", "years", "jaar", "anos", "reserva", "reserve",
             "gold", "oro", "dorado", "ambre", "amber", "extra viejo", "xo", "solera", "captain morgan",
             "sailor jerry", "diplomatico", "zacapa", "don papa", "el dorado", "appleton", "plantation",
             "7", "12", "15", "21", "23")
_EAU_DE_VIE = ("raki", "rakia", "rakija", "rakiya", "grappa", "pisco", "eau de vie", "slivovitz", "sljivovica",
               "palinka", "tuica", "tsipouro", "zivania", "kirsch*", "obstler", "poire williams", "williams",
               "schnaps", "schnapps", "orujo", "marc", "chacha", "arak")
_orange = ("triple sec", "cointreau", "grand marnier", "curacao", "orange liqueur", "sinaasappellikeur",
           "combier", "mandarine napoleon", "dry orange", "orange curacao")
_vermouth = ("vermouth", "vermout", "wermut", "martini", "noilly prat", "dolin", "cinzano", "carpano",
             "cocchi", "punt e mes", "lillet", "dubonnet")
_anise = ("pastis", "ricard", "pernod", "anis", "anisette", "ouzo", "sambuca", "absinthe", "absint", "raki",
          "arak", "anise")
_cream = ("baileys", "irish cream", "sheridan's", "sheridans", "carolans", "cream liqueur", "roomlikeur",
          "amarula", "kerrygold")
_bitters = ("angostura", "aromatic bitters", "bitters", "peychaud's", "peychaud", "orange bitters")

SPECS: dict[str, Spec] = {k.strip(): BASIS for k in _basis.replace("\n", " ").split(",") if k.strip()}
SPECS.update({
    # gin & vodka
    "gin": D(cats=["gin"], all_cats=True),
    "sloe gin": D(["sloe gin", "sloe", "sleedoorn*", "schlehen*"]),
    "vodka": D(cats=["vodka"], all_cats=True),
    "absolut vodka": D(["absolut"], ["vodka"], sub_cats=["vodka"]),
    "absolut citron": D(["citron", "lemon", "citroen", "limon"], ["vodka"], sub_cats=["vodka"]),
    "absolut kurant": D(["kurant", "currant", "bes"], ["vodka"], sub_cats=["vodka"]),
    "absolut peppar": D(["peppar", "pepper"], ["vodka"], sub_cats=["vodka"]),
    "peach vodka": D(["peach", "perzik"], ["vodka"], sub_cats=["vodka"]),
    "lime vodka": D(["lime", "limoen"], ["vodka"], sub_cats=["vodka"]),
    "raspberry vodka": D(["raspberry", "framboos", "razz"], ["vodka"], sub_cats=["vodka"]),
    "vanilla vodka": D(["vanilla", "vanille"], ["vodka"], sub_cats=["vodka"]),
    "cranberry vodka": D(["cranberry"], ["vodka"], sub_cats=["vodka"]),
    "everclear": D(["everclear", "grain alcohol", "neutral", "spiritus"], sub_cats=["vodka"]),
    "grain alcohol": D(["everclear", "grain alcohol", "neutral", "spiritus"], sub_cats=["vodka"]),
    # rum
    "rum": D(cats=["rum"], all_cats=True),
    "light rum": D(cats=["rum"], all_cats=True, sub_cats=["rum"], not_words=_DARK_RUM),
    "white rum": D(cats=["rum"], all_cats=True, sub_cats=["rum"], not_words=_DARK_RUM),
    "gold rum": D(cats=["rum"], all_cats=True),
    "dark rum": D(["dark", "black", "navy", "kraken", "myers's", "goslings", "negrita", "zwart*", "donker*",
                   "spiced", "anejo", "aged", "jamaica*"], ["rum"], sub_cats=["rum"]),
    "blackstrap rum": D(["blackstrap", "black", "dark", "kraken"], ["rum"], sub_cats=["rum"]),
    "spiced rum": D(["spiced", "captain morgan", "sailor jerry", "kraken", "spice*"], ["rum"], sub_cats=["rum"]),
    "anejo rum": D(["anejo", "aged", "reserva", "year*", "jaar", "old"], ["rum"], sub_cats=["rum"]),
    "151 proof rum": D(["151", "overproof", "over proof", "navy strength"], ["rum"], sub_cats=["rum"]),
    "bacardi limon": D(["limon", "lemon", "citrus"], ["rum"], sub_cats=["rum"]),
    "malibu rum": D(["malibu", "coconut", "kokos*"], sub_words=["coconut", "kokos*"]),
    "cachaca": D(["cachaca", "cachaça"], sub_cats=["rum"]),
    # whisky
    "whiskey": D(cats=["whisky"], all_cats=True),
    "whisky": D(cats=["whisky"], all_cats=True),
    "blended whiskey": D(cats=["whisky"], all_cats=True),
    "scotch": D(_whisky_scotch, ["whisky"], sub_cats=["whisky"]),
    "blended scotch": D(_whisky_scotch, ["whisky"], sub_cats=["whisky"]),
    "johnnie walker": D(["johnnie walker"], sub_words=_whisky_scotch, sub_cats=["whisky"]),
    "islay single malt scotch": D(["islay", "laphroaig", "ardbeg", "lagavulin", "bowmore", "bruichladdich",
                                   "caol ila", "kilchoman", "bunnahabhain", "port charlotte", "peat*"],
                                  ["whisky"], sub_cats=["whisky"]),
    "bourbon": D(_whisky_us, ["whisky"], sub_cats=["whisky"]),
    "tennessee whiskey": D(_whisky_us, ["whisky"], sub_cats=["whisky"]),
    "jack daniels": D(["jack daniel's", "jack daniels"], sub_words=_whisky_us, sub_cats=["whisky"]),
    "jim beam": D(["jim beam"], sub_words=_whisky_us, sub_cats=["whisky"]),
    "wild turkey": D(["wild turkey"], sub_words=_whisky_us, sub_cats=["whisky"]),
    "rye whiskey": D(["rye", "rogge"], ["whisky"], sub_cats=["whisky"]),
    "irish whiskey": D(_whisky_irish, ["whisky"], sub_cats=["whisky"]),
    "crown royal": D(["crown royal", "canadian", "canada"], ["whisky"], sub_cats=["whisky"]),
    "yukon jack": D(["yukon jack"], sub_words=["whisky liqueur", "honey"], sub_cats=["likeur"]),
    # agave, brandy
    "tequila": D(cats=["tequila"], all_cats=True),
    "mezcal": D(["mezcal", "mescal"], ["tequila"], sub_cats=["tequila"]),
    # Clear fruit spirits (rakia, grappa, pisco...) are in the brandy category, but a recipe that asks
    # for brandy means an oak-aged one: they only count as a substitute.
    "brandy": D(cats=["brandy"], all_cats=True, sub_cats=["brandy"], not_words=_EAU_DE_VIE),
    "cognac": D(["cognac", "hennessy", "remy martin", "martell", "courvoisier"], ["brandy"], sub_cats=["brandy"]),
    "applejack": D(["applejack", "calvados", "apple brandy", "appelbrandewijn"], sub_cats=["brandy"]),
    "apple brandy": D(["applejack", "calvados", "apple brandy", "appelbrandewijn"], sub_cats=["brandy"]),
    "pisco": D(["pisco"], sub_cats=["brandy"]),
    # orange liqueurs
    "triple sec": D(_orange),
    "cointreau": D(["cointreau"], sub_words=_orange),
    "grand marnier": D(["grand marnier"], sub_words=_orange),
    "orange curacao": D(["curacao"], sub_words=_orange),
    "blue curacao": D(["blue curacao", "blauwe curacao", "curacao blue", "bols blue"], sub_words=_orange),
    # coffee, cream, nut
    "kahlua": D(["kahlua", "tia maria", "coffee liqueur", "koffielikeur", "mr black", "borghetti", "espresso liqueur"]),
    "coffee liqueur": D(["kahlua", "tia maria", "coffee liqueur", "koffielikeur", "mr black", "borghetti"]),
    "tia maria": D(["tia maria"], sub_words=["kahlua", "coffee liqueur", "koffielikeur"]),
    "coffee brandy": D(["coffee brandy", "kahlua", "tia maria", "coffee liqueur", "koffielikeur"]),
    "baileys irish cream": D(["baileys", "irish cream"], sub_words=_cream),
    "irish cream": D(["baileys", "irish cream", "carolans", "sheridan's", "sheridans"], sub_words=_cream),
    "amaretto": D(["amaretto", "disaronno", "di saronno"]),
    "frangelico": D(["frangelico", "hazelnut", "hazelnoot*", "noisette"]),
    "advocaat": D(["advocaat", "eierlikeur", "eierlikor", "egg liqueur", "verpoorten"]),
    "godiva liqueur": D(["godiva"], sub_words=["chocolate liqueur", "chocoladelikeur", "creme de cacao", "cacao"]),
    "chocolate liqueur": D(["chocolate", "chocolade*", "cacao", "godiva", "mozart"], ["likeur"]),
    "creme de cacao": D(["creme de cacao", "cacao"], sub_words=["chocolate liqueur", "chocoladelikeur", "godiva"]),
    "white creme de cacao": D(["creme de cacao", "cacao"], sub_words=["chocolate liqueur", "chocoladelikeur"]),
    "dark creme de cacao": D(["creme de cacao", "cacao"], sub_words=["chocolate liqueur", "chocoladelikeur"]),
    # vermouth & aperitifs
    "sweet vermouth": D(["rosso", "sweet", "rouge", "red", "rood", "carpano", "antica formula", "punt e mes",
                         "di torino"], ["aperitief"], sub_words=_vermouth, sub_cats=["aperitief"]),
    "rosso vermouth": D(["rosso", "sweet", "rouge", "red", "rood", "carpano", "punt e mes"], ["aperitief"],
                        sub_words=_vermouth, sub_cats=["aperitief"]),
    "dry vermouth": D(["dry", "secco", "noilly prat", "dolin"], ["aperitief"], sub_words=_vermouth,
                      sub_cats=["aperitief"]),
    "vermouth": D(_vermouth, ["aperitief"], sub_cats=["aperitief"]),
    "lillet blanc": D(["lillet"], sub_words=_vermouth, sub_cats=["aperitief"]),
    "lillet": D(["lillet"], sub_words=_vermouth, sub_cats=["aperitief"]),
    "dubonnet rouge": D(["dubonnet"], sub_words=_vermouth, sub_cats=["aperitief"]),
    "campari": D(["campari"], sub_words=["bitter", "aperitivo"], sub_cats=["bitter", "aperitief"]),
    "aperol": D(["aperol"], sub_words=["aperitivo", "select"], sub_cats=["aperitief"]),
    "amaro montenegro": D(["montenegro"], sub_cats=["bitter"]),
    "jagermeister": D(["jagermeister"], sub_cats=["bitter"]),
    "rumple minze": D(["rumple minze", "peppermint schnapps"], sub_words=["menthe", "peppermint", "pepermunt*"]),
    # fruit & herbal liqueurs
    "galliano": D(["galliano"]),
    "midori melon liqueur": D(["midori", "melon", "meloen*"], ["likeur"]),
    "melon liqueur": D(["midori", "melon", "meloen*"], ["likeur"]),
    "peach schnapps": D(["peach", "perzik*", "peche", "peachtree", "pfirsich*"], ["likeur"]),
    "peachtree schnapps": D(["peach", "perzik*", "peche", "peachtree", "pfirsich*"], ["likeur"]),
    "peach brandy": D(["peach", "perzik*", "peche", "pfirsich*"], ["likeur", "brandy"]),
    "southern comfort": D(["southern comfort"]),
    "chambord raspberry liqueur": D(["chambord"], sub_words=["raspberry", "framboos", "frambozen*", "framboise",
                                                             "himbeer*"], sub_cats=["likeur"]),
    "raspberry liqueur": D(["chambord", "raspberry", "framboos", "frambozen*", "framboise", "himbeer*"], ["likeur"]),
    "creme de cassis": D(["cassis", "blackcurrant", "zwarte bes*"], ["likeur", "siroop"]),
    "creme de mure": D(["mure", "blackberry", "braam", "bramen*"], ["likeur"]),
    "blackberry brandy": D(["blackberry", "braam", "bramen*", "mure"], ["likeur", "brandy"]),
    "strawberry liqueur": D(["strawberry", "aardbei*", "fraise", "erdbeer*"], ["likeur"]),
    "strawberry schnapps": D(["strawberry", "aardbei*", "fraise", "erdbeer*"], ["likeur"]),
    "blueberry schnapps": D(["blueberry", "bosbes*", "myrtille", "blaubeer*"], ["likeur"]),
    "butterscotch schnapps": D(["butterscotch", "caramel", "karamel"], ["likeur"]),
    "banana liqueur": D(["banana", "banaan", "banane", "bananen*", "pisang"], ["likeur"]),
    "creme de banane": D(["banana", "banaan", "banane", "bananen*", "pisang"], ["likeur"]),
    "pisang ambon": D(["pisang"], sub_words=["banana", "banaan", "banane"]),
    "coconut liqueur": D(["coconut", "kokos*", "malibu", "coco"], ["likeur", "rum"]),
    "kiwi liqueur": D(["kiwi"], ["likeur"]),
    "passoa": D(["passoa", "passion fruit liqueur", "passievrucht*", "maracuja"], ["likeur"]),
    "st germain": D(["st germain", "st-germain", "elderflower", "vlierbloesem*", "holunder*"], ["likeur"]),
    "maraschino liqueur": D(["maraschino", "maraskino"], ["likeur"]),
    "cherry heering": D(["heering", "cherry", "kers*", "kirsch*", "griotte"], ["likeur"]),
    "cherry brandy": D(["cherry", "kers*", "kirsch*", "heering", "griotte"], ["likeur", "brandy"]),
    "cherry liqueur": D(["cherry", "kers*", "kirsch*", "heering", "griotte"], ["likeur"]),
    "apricot brandy": D(["apricot", "abrikoos", "abrikozen*", "abricot", "marille*", "aprikose*"], ["likeur", "brandy"]),
    "benedictine": D(["benedictine", "b&b"]),
    "green chartreuse": D(["chartreuse"]),
    "yellow chartreuse": D(["chartreuse"]),
    "drambuie": D(["drambuie"], sub_words=["whisky liqueur", "honey"], sub_cats=["likeur"]),
    "white creme de menthe": D(["creme de menthe", "menthe", "peppermint", "pepermunt*", "mint liqueur"]),
    "green creme de menthe": D(["creme de menthe", "menthe", "peppermint", "pepermunt*", "mint liqueur"]),
    "goldschlager": D(["goldschlager", "cinnamon", "kaneel"], ["likeur"]),
    "hot damn": D(["hot damn", "fireball", "cinnamon", "kaneel"], ["likeur", "whisky"]),
    "firewater": D(["firewater", "fireball", "cinnamon"], ["likeur", "whisky"]),
    "apfelkorn": D(["apfelkorn", "appelkorn", "apfel", "appel*", "apple"], ["likeur", "jenever"]),
    "falernum": D(["falernum"]),
    "sambuca": D(["sambuca"], sub_words=_anise),
    "black sambuca": D(["sambuca"], sub_words=_anise),
    "anis": D(_anise),
    "anisette": D(_anise),
    "ouzo": D(["ouzo"], sub_words=_anise),
    "pernod": D(["pernod"], sub_words=_anise),
    "ricard": D(["ricard"], sub_words=_anise),
    "absinthe": D(["absinthe", "absint"], sub_words=_anise),
    "wormwood": D(["absinthe", "absint", "wormwood", "alsem"]),
    # wine, beer
    "red wine": D(["red", "rood", "rode", "rouge", "rosso", "tinto", "merlot", "cabernet", "shiraz", "syrah",
                   "pinot noir", "malbec", "tempranillo", "rioja", "chianti", "primitivo"], ["wijn"],
                  sub_cats=["wijn"]),
    "white wine": D(["white", "wit", "witte", "blanc", "bianco", "blanco", "chardonnay", "sauvignon", "riesling",
                     "pinot grigio", "verdejo", "chenin"], ["wijn"], sub_cats=["wijn"]),
    "wine": D(cats=["wijn"], all_cats=True),
    "champagne": D(["champagne"], ["bubbels"], sub_cats=["bubbels"]),
    "prosecco": D(["prosecco"], ["bubbels"], sub_cats=["bubbels"]),
    "port": D(["port", "porto", "portwijn"], ["port"], sub_cats=["port"]),
    "ruby port": D(["ruby"], ["port"], sub_cats=["port"]),
    "sherry": D(["sherry", "jerez", "fino", "oloroso", "amontillado", "pedro ximenez", "manzanilla"], ["port"],
                sub_cats=["port"]),
    "beer": D(cats=["bier"], all_cats=True),
    "lager": D(["lager", "pils", "pilsner", "pilsener"], ["bier"], sub_cats=["bier"]),
    "corona": D(["corona"], sub_cats=["bier"]),
    "guinness stout": D(["guinness", "stout"], ["bier"], sub_cats=["bier"]),
    "zima": D(["zima"], sub_cats=["bier", "cider"]),
    "cider": D(cats=["cider"], all_cats=True),
    # bitters (small bottles, but real bottles)
    "angostura bitters": D(["angostura"], sub_words=_bitters, sub_cats=["additief"]),
    "bitters": D(_bitters, ["additief", "bitter"], sub_cats=["additief"]),
    "orange bitters": D(["orange bitters", "orange"], ["additief"], sub_words=_bitters),
    "peach bitters": D(["peach bitters", "peach"], ["additief"], sub_words=_bitters),
    "peychaud bitters": D(["peychaud's", "peychaud"], sub_words=_bitters),
    # mixers: juices, sodas, syrups
    "orange juice": M(["orange", "sinaasappel*", "jus d'orange"]),
    "pineapple juice": M(["pineapple", "ananas*"]),
    "cranberry juice": M(["cranberry", "cranberries"]),
    "apple juice": M(["apple", "appel*"]),
    "grapefruit juice": M(["grapefruit", "pompelmoes"]),
    "tomato juice": M(["tomato", "tomaten*"]),
    "grape juice": M(["grape", "druiven*"]),
    "passion fruit juice": M(["passion", "passievrucht*", "maracuja"]),
    "cherry juice": M(["cherry", "kersen*"]),
    "pomegranate juice": M(["pomegranate", "granaatappel*"]),
    "fruit juice": M(["juice", "sap", "vruchtensap"]),
    "peach nectar": M(["peach", "perzik*"]),
    "apricot nectar": M(["apricot", "abrikoos", "abrikozen*"]),
    "lemonade": M(["lemonade", "limonade"]),
    "pink lemonade": M(["pink lemonade", "lemonade", "limonade"]),
    "limeade": M(["limeade", "lime"]),
    "ginger ale": M(["ginger ale", "gingerale"]),
    "ginger beer": M(["ginger beer", "gemberbier"]),
    "coca cola": M(["cola", "coca cola", "coke"]),
    "pepsi cola": M(["cola", "pepsi"]),
    "7 up": M(["7up", "7 up", "seven up", "sprite", "lemon lime"]),
    "sprite": M(["sprite", "7up", "7 up", "lemon lime"]),
    "lemon lime soda": M(["sprite", "7up", "7 up", "lemon lime"]),
    "tonic water": M(["tonic"]),
    "bitter lemon": M(["bitter lemon"]),
    "root beer": M(["root beer"]),
    "dr pepper": M(["dr pepper"]),
    "mountain dew": M(["mountain dew"]),
    "fresca": M(["fresca", "grapefruit"]),
    "grape soda": M(["grape", "druif*"]),
    "sarsaparilla": M(["sarsaparilla", "root beer"]),
    "carbonated soft drink": M(["soda", "frisdrank", "cola", "sprite"]),
    "surge": M(["surge", "mountain dew"]),
    "iced tea": M(["ice tea", "iced tea", "ijsthee"]),
    "schweppes russchian": M(["russchian", "schweppes"]),
    "fruit punch": M(["punch"]),
    "grenadine": M(["grenadine"]),
    "orgeat syrup": M(["orgeat", "amandel*", "almond"]),
    "rosemary syrup": M(["rosemary", "rozemarijn"]),
    "raspberry syrup": M(["raspberry", "framboos", "frambozen*"]),
    "vanilla syrup": M(["vanilla", "vanille"]),
    "pineapple syrup": M(["pineapple", "ananas"]),
    "coconut syrup": M(["coconut", "kokos*"]),
    "mint syrup": M(["mint", "munt"]),
    "honey syrup": M(["honey", "honing"]),
    "ginger syrup": M(["ginger", "gember*"]),
    "passion fruit syrup": M(["passion", "passievrucht*", "maracuja"]),
    "agave syrup": M(["agave"]),
    "sirup of roses": M(["rose", "rozen*"]),
    "chocolate syrup": M(["chocolate", "chocolade*"]),
    "elderflower cordial": M(["elderflower", "vlierbloesem*"]),
    "blackcurrant cordial": M(["blackcurrant", "cassis", "zwarte bes*"]),
    "roses sweetened lime juice": M(["lime cordial", "rose's", "roses lime"]),
    "sweet and sour": M(["sweet and sour", "sour mix"]),
    "sour mix": M(["sour mix", "sweet and sour"]),
    "daiquiri mix": M(["daiquiri"]),
    "pina colada mix": M(["pina colada", "colada"]),
    "cream of coconut": M(["cream of coconut", "coco lopez", "coconut cream", "kokosroom"]),
})

_STOP = {"liqueur", "schnapps", "creme", "de", "of", "the", "fresh", "sirup"}
_MIXWORDS = {"juice", "syrup", "cordial", "nectar", "soda", "mix", "lemonade", "cola"}
_SPIRITS = {"rum": "rum", "vodka": "vodka", "gin": "gin", "whiskey": "whisky", "whisky": "whisky",
            "tequila": "tequila", "brandy": "brandy", "cognac": "brandy", "scotch": "whisky", "bourbon": "whisky"}


def spec_for(name: str) -> Spec:
    key = norm(name)
    if key in SPECS:
        return SPECS[key]
    toks = key.split()
    if not toks:
        return BASIS
    if _MIXWORDS & set(toks):
        rest = [t for t in toks if t not in _MIXWORDS and t not in _STOP]
        return M([" ".join(rest)] if rest else [key])
    for t in toks:
        if t in _SPIRITS and len(toks) > 1:
            return D([key], [_SPIRITS[t]], sub_cats=[_SPIRITS[t]])
    rest = [t for t in toks if t not in _STOP] or toks
    if {"liqueur", "schnapps", "creme"} & set(toks):
        return D([" ".join(rest)], ["likeur"])
    return D([key])


def _phrase_re(phrases) -> re.Pattern | None:
    parts = []
    for ph in phrases:
        prefix = ph.endswith("*")
        ph = norm(ph.rstrip("*"))
        if ph:
            parts.append(re.escape(ph) + ("" if prefix else r"(?![a-z0-9])"))
    if not parts:
        return None
    return re.compile(r"(?<![a-z0-9])(?:" + "|".join(parts) + ")")


_RE_CACHE: dict[tuple, re.Pattern | None] = {}


def _hit(phrases, hay: str) -> bool:
    if not phrases:
        return False
    rx = _RE_CACHE.get(phrases)
    if rx is None and phrases not in _RE_CACHE:
        rx = _RE_CACHE[phrases] = _phrase_re(phrases)
    return bool(rx and rx.search(hay))


def product_hay(p) -> str:
    return norm(" ".join(str(p[k] or "") for k in ("name", "brand", "country")))


def product_cats(p) -> set[str]:
    return {p["category"], *[c for c in (p["extra_categories"] or "").split(",") if c]}


BASE_SPIRITS = {"whisky", "rum", "gin", "vodka", "tequila", "brandy", "jenever"}


def match(spec: Spec, p) -> str | None:
    """'exact', 'sub' or None: can this product stand in for the ingredient?"""
    if spec.kind == "basis":
        return None
    hay, cats = p["_hay"], p["_cats"]
    if "likeur" in cats:
        # Southern Comfort / Jim Beam Peach are liqueurs that also count as whisky in the bar,
        # but they cannot stand in for real whisky (or rum, gin...) in a recipe.
        cats = cats - BASE_SPIRITS
    demoted = bool(spec.not_words) and _hit(spec.not_words, hay)
    if not demoted:
        if spec.all_cats and cats & set(spec.cats):
            return "exact"
        if spec.words and _hit(spec.words, hay) and (not spec.cats or cats & set(spec.cats)):
            return "exact"
    if spec.sub_words:
        if _hit(spec.sub_words, hay) and (not spec.sub_cats or cats & set(spec.sub_cats)):
            return "sub"
    elif spec.sub_cats and cats & set(spec.sub_cats):
        return "sub"
    return None


# ---- availability --------------------------------------------------------------------------------
def stock(db: Database, location_ids: list[int] | None) -> list[dict]:
    """Products with at least one bottle in these locations (None = everywhere)."""
    where, args = "", []
    if location_ids is not None:
        if not location_ids:
            return []
        where = f"WHERE i.location_id IN ({','.join('?' * len(location_ids))})"
        args = list(location_ids)
    rows = db.all(f"""SELECT p.*, SUM(i.quantity) AS qty,
                        (SELECT filename FROM product_images pi WHERE pi.product_id = p.id
                         ORDER BY position, id LIMIT 1) AS image
                      FROM inventory i JOIN products p ON p.id = i.product_id {where}
                      GROUP BY p.id HAVING SUM(i.quantity) > 0""", *args)
    out = []
    for r in rows:
        d = dict(r)
        d["_hay"], d["_cats"] = product_hay(d), product_cats(d)
        out.append(d)
    return out


@dataclass
class Status:
    state: str              # basis | altijd | ok | vervanger | mist
    kind: str
    products: list


class Matcher:
    def __init__(self, db: Database, products: list[dict], assume_mixers: bool = True):
        self.products = products
        self.assume_mixers = assume_mixers
        self.pantry = pantry_mode(db)
        self.always = {r["ingredient"] for r in db.all("SELECT ingredient FROM ingredient_prefs WHERE always=1")}
        self.links: dict[str, set[int]] = {}
        for r in db.all("SELECT ingredient, product_id FROM ingredient_links"):
            self.links.setdefault(r["ingredient"], set()).add(r["product_id"])
        self._cache: dict[str, Status] = {}

    def resolve(self, name: str) -> Status:
        key = norm(name)
        if key in self._cache:
            return self._cache[key]
        spec = spec_for(key)
        linked = self.links.get(key, set())
        exact = [p for p in self.products if p["id"] in linked or match(spec, p) == "exact"]
        subs = [p for p in self.products if p not in exact and match(spec, p) == "sub"]
        if exact:
            st = Status("ok", spec.kind, exact)
        elif canon(key) in self.always:
            st = Status("altijd", spec.kind, [])
        elif key in TRIVIAL:
            st = Status("basis", spec.kind, [])
        elif self.pantry and spec.kind in ("basis", "mixer"):
            st = Status("mist", spec.kind, [])       # not ticked on the kitchen page
        elif spec.kind == "basis" or (spec.kind == "mixer" and self.assume_mixers):
            st = Status("basis", spec.kind, [])
        elif subs:
            st = Status("vervanger", spec.kind, subs)
        else:
            st = Status("mist", spec.kind, [])
        self._cache[key] = st
        return st


# ---- kitchen & mixers ("pantry") -------------------------------------------------------------------
# Once the user fills in the pantry page, fresh ingredients, juices, sodas and syrups only count
# when ticked there (or when a matching bottle is in stock). Before that everything is assumed.
TRIVIAL = {"ice", "water"}
# Different recipe words for the same thing in the kitchen: one tick covers them all.
ALIAS = {"club soda": "soda water", "carbonated water": "soda water", "pepsi cola": "coca cola",
         "sprite": "7 up", "lemon lime soda": "7 up", "simple syrup": "sugar syrup",
         "fresh lemon juice": "lemon juice", "fresh lime juice": "lime juice", "cherries": "cherry",
         "whipping cream": "heavy cream", "sour mix": "sweet and sour", "black pepper": "pepper"}


def canon(key: str) -> str:
    return ALIAS.get(key, key)
PANTRY_GROUPS = [
    ("sap", "Sappen"), ("fris", "Frisdrank & bruiswater"), ("siroop", "Siropen & mixen"),
    ("fruit", "Fruit, groente & garnering"), ("zuivel", "Zuivel & ei"), ("zoet", "Suiker & zoet"),
    ("kruiden", "Kruiden & smaakmakers"), ("koffie", "Koffie & thee"), ("overig", "Overig"),
]
_GROUP_WORDS = [
    ("koffie", ["coffee", "espresso", "tea", "hot chocolate", "iced tea"]),
    ("siroop", ["syrup", "sirup", "cordial", "grenadine", "sweet and sour", "sour mix", "mix", "cream of coconut"]),
    ("sap", ["juice", "nectar", "lemonade", "limeade"]),
    ("fris", ["cola", "tonic", "ginger ale", "ginger beer", "7 up", "sprite", "soda", "carbonated", "root beer",
              "dr pepper", "mountain dew", "fresca", "sarsaparilla", "surge", "russchian", "fruit punch",
              "bitter lemon", "kool aid"]),
    ("zuivel", ["milk", "cream", "egg", "yoghurt", "butter", "ice cream", "sherbet", "half and half"]),
    ("zoet", ["sugar", "honey", "chocolate", "cocoa", "oreo", "marshmallow", "jello", "vanilla"]),
    ("kruiden", ["mint", "nutmeg", "cinnamon", "cloves", "ginger", "cardamom", "allspice", "coriander", "cumin",
                 "pepper", "chili", "salt", "asafoetida", "rosemary", "thyme", "lavender", "rose", "tabasco",
                 "hot sauce", "worcestershire", "soy sauce", "flavoring", "coloring"]),
    ("fruit", ["lemon", "lime", "orange", "pineapple", "apple", "banana", "mango", "kiwi", "papaya", "strawberr",
               "blackberr", "figs", "cucumber", "fruit", "cherr", "olive", "peel", "spiral"]),
]
NL = {
    "sugar": "suiker", "powdered sugar": "poedersuiker", "brown sugar": "bruine suiker",
    "demerara sugar": "demerarasuiker", "lemon": "citroen", "lime": "limoen", "lemon juice": "citroensap",
    "lime juice": "limoensap", "fresh lemon juice": "vers citroensap", "fresh lime juice": "vers limoensap",
    "lemon peel": "citroenschil", "lime peel": "limoenschil", "orange": "sinaasappel",
    "orange peel": "sinaasappelschil", "orange spiral": "sinaasappelkrul", "blood orange": "bloedsinaasappel",
    "milk": "melk", "coffee": "koffie", "espresso": "espresso", "tea": "thee", "hot chocolate": "warme chocolademelk",
    "egg": "ei", "egg white": "eiwit", "egg yolk": "eidooier", "cream": "room", "light cream": "koffieroom",
    "heavy cream": "slagroom (vloeibaar)", "whipping cream": "slagroom", "whipped cream": "slagroom (geklopt)",
    "half and half": "half melk, half room", "condensed milk": "gecondenseerde melk",
    "coconut milk": "kokosmelk", "yoghurt": "yoghurt", "butter": "boter", "mint": "munt",
    "nutmeg": "nootmuskaat", "cinnamon": "kaneel", "cloves": "kruidnagel", "ginger": "gember",
    "cardamom": "kardemom", "allspice": "piment", "coriander": "koriander", "cumin seed": "komijnzaad",
    "pepper": "peper", "black pepper": "zwarte peper", "cayenne pepper": "cayennepeper",
    "red chili flakes": "chilivlokken", "salt": "zout", "celery salt": "selderijzout",
    "vanilla": "vanille", "vanilla extract": "vanille-extract", "almond flavoring": "amandelaroma",
    "honey": "honing", "maple syrup": "ahornsiroop", "corn syrup": "maissiroop", "sugar syrup": "suikersiroop",
    "simple syrup": "suikersiroop", "olive": "olijf", "olive brine": "olijvennat",
    "maraschino cherry": "cocktailkers", "cherry": "kers", "cherries": "kersen", "pineapple": "ananas",
    "apple": "appel", "banana": "banaan", "mango": "mango", "kiwi": "kiwi", "papaya": "papaja",
    "strawberries": "aardbeien", "blackberries": "bramen", "figs": "vijgen", "cucumber": "komkommer",
    "fruit": "fruit", "rosemary": "rozemarijn", "thyme": "tijm", "lavender": "lavendel", "rose": "roos",
    "chocolate": "chocolade", "cocoa powder": "cacaopoeder", "oreo cookie": "oreokoekje",
    "marshmallows": "marshmallows", "vanilla ice cream": "vanille-ijs", "sherbet": "sorbetijs", "jello": "gelatinepudding",
    "tabasco sauce": "tabasco", "hot sauce": "hete saus", "worcestershire sauce": "worcestershiresaus",
    "soy sauce": "sojasaus", "soda water": "spuitwater / bruiswater",
    "orange juice": "sinaasappelsap", "pineapple juice": "ananassap", "cranberry juice": "cranberrysap",
    "apple juice": "appelsap", "grapefruit juice": "grapefruitsap", "tomato juice": "tomatensap",
    "grape juice": "druivensap", "passion fruit juice": "passievruchtsap", "cherry juice": "kersensap",
    "pomegranate juice": "granaatappelsap", "fruit juice": "vruchtensap", "peach nectar": "perziknectar",
    "apricot nectar": "abrikozennectar", "lemonade": "citroenlimonade", "pink lemonade": "roze limonade",
    "limeade": "limoenlimonade", "ginger ale": "ginger ale", "ginger beer": "gemberbier", "coca cola": "cola",
    "pepsi cola": "cola", "7 up": "7-Up / Sprite", "guava juice": "guavesap", "lime juice cordial": "limoensiroop", "root beer": "root beer", "grape soda": "druivenfrisdrank", "carbonated soft drink": "frisdrank", "daiquiri mix": "daiquiri-mix", "pina colada mix": "piña colada-mix",
    "tonic water": "tonic", "bitter lemon": "bitter lemon", "iced tea": "ijsthee", "grenadine": "grenadine",
    "orgeat syrup": "amandelsiroop (orgeat)", "raspberry syrup": "frambozensiroop", "vanilla syrup": "vanillesiroop",
    "coconut syrup": "kokossiroop", "mint syrup": "muntsiroop", "honey syrup": "honingsiroop",
    "ginger syrup": "gembersiroop", "passion fruit syrup": "passievruchtsiroop", "agave syrup": "agavesiroop",
    "rosemary syrup": "rozemarijnsiroop", "pineapple syrup": "ananassiroop", "sirup of roses": "rozensiroop",
    "chocolate syrup": "chocoladesiroop", "elderflower cordial": "vlierbloesemsiroop",
    "blackcurrant cordial": "zwarte-bessensiroop", "roses sweetened lime juice": "limoensiroop (Rose's)",
    "sweet and sour": "sweet & sour mix", "sour mix": "sour mix", "cream of coconut": "kokosroom",
    "kool aid": "Kool-Aid (limonadepoeder)", "fruit punch": "vruchtenpunch",
}


def pantry_mode(db: Database) -> bool:
    r = db.one("SELECT value FROM kv WHERE key='pantry'")
    return bool(r and r["value"] == "1")


def pantry_group(key: str) -> str:
    for group, words in _GROUP_WORDS:
        if any(re.search(r"(?<![a-z])" + re.escape(w), key) for w in words):
            return group
    return "overig"


def pantry_items(db: Database) -> list[dict]:
    """Every fresh ingredient, juice, soda and syrup the recipes use, with how often."""
    counts: dict[str, dict] = {}
    for r in db.all("SELECT name, COUNT(DISTINCT cocktail_id) AS n FROM cocktail_ingredients GROUP BY name"):
        key = norm(r["name"])
        spec = spec_for(key)
        if spec.kind == "drank" or key in TRIVIAL or not key:
            continue
        key = canon(key)
        d = counts.setdefault(key, {"key": key, "name": r["name"], "n": 0, "kind": spec.kind})
        d["n"] += r["n"]
    have = {r["ingredient"]: r["always"] for r in db.all("SELECT ingredient, always FROM ingredient_prefs")}
    mode = pantry_mode(db)
    for d in counts.values():
        d["nl"] = NL.get(d["key"], "")
        d["group"] = pantry_group(d["key"])
        # Before the page was ever saved everything counted as present, so start from there.
        d["have"] = bool(have.get(d["key"])) if mode else True
    return sorted(counts.values(), key=lambda d: -d["n"])


def all_cocktails(db: Database) -> list[dict]:
    rows = [dict(r) for r in db.all("SELECT * FROM cocktails ORDER BY name COLLATE NOCASE")]
    ings: dict[str, list] = {}
    for r in db.all("SELECT * FROM cocktail_ingredients ORDER BY cocktail_id, pos"):
        ings.setdefault(r["cocktail_id"], []).append(dict(r))
    for c in rows:
        c["ingredients"] = ings.get(c["id"], [])
    return rows


def evaluate(cocktail: dict, matcher: Matcher) -> dict:
    missing, subs, used = [], [], []
    seen, bottles = set(), 0
    for ing in cocktail["ingredients"]:
        key = norm(ing["name"])
        if key in seen:
            continue
        seen.add(key)
        st = matcher.resolve(ing["name"])
        bottles += st.kind == "drank"
        if st.state == "mist":
            missing.append(ing["name"])
        elif st.state == "vervanger":
            subs.append(ing["name"])
        if st.products:
            used.extend(p["id"] for p in st.products)
    return {**cocktail, "missing": missing, "subs": subs, "uses": set(used), "bottles": bottles,
            "boozy": bottles > 0 and cocktail.get("alcoholic") != "Non alcoholic",
            "can": not missing, "almost": len(missing) == 1}


def evaluate_all(db: Database, products: list[dict], assume_mixers: bool = True) -> list[dict]:
    m = Matcher(db, products, assume_mixers)
    return [evaluate(c, m) for c in all_cocktails(db)]


def ingredients_for_product(db: Database, product) -> set[str]:
    """Normalised ingredient names this product can supply (exact or as substitute)."""
    p = dict(product)
    p["_hay"], p["_cats"] = product_hay(p), product_cats(p)
    linked = {r["ingredient"] for r in db.all("SELECT ingredient FROM ingredient_links WHERE product_id=?", p["id"])}
    out = set(linked)
    for r in db.all("SELECT DISTINCT name FROM cocktail_ingredients"):
        if match(spec_for(r["name"]), p):
            out.add(norm(r["name"]))
    return out


# ---- measures ------------------------------------------------------------------------------------
_NUM = r"(?:\d+\s+\d+/\d+|\d+/\d+|\d*[.,]\d+|\d+)"
_MEASURE = re.compile(r"^\s*(" + _NUM + r")\s*(?:-\s*(" + _NUM + r"))?\s*"
                      r"(oz|ounces?|shots?|jiggers?|cl|ml|tsp|tblsp|tbsp|dash(?:es)?|cups?|parts?|cubes?)?\b(.*)$",
                      re.I)
_UNIT_NL = {"tsp": "tl", "tblsp": "el", "tbsp": "el", "dash": "scheutje", "dashes": "scheutjes", "cup": "kop",
            "cups": "kop", "part": "deel", "parts": "delen", "cube": "blokje", "cubes": "blokjes"}


def _num(s: str) -> float:
    s = s.strip().replace(",", ".")
    if " " in s:
        a, b = s.split(None, 1)
        return float(a) + float(Fraction(b))
    return float(Fraction(s)) if "/" in s else float(s)


def _fmt(v: float) -> str:
    v = math.floor(v * 2 + 0.5) / 2
    return f"{v:g}".replace(".", ",")


def measure_nl(measure: str) -> str:
    """'1 1/2 oz' -> '4,5 cl'; shots and jiggers too. Other measures stay as they are."""
    m = _MEASURE.match(measure or "")
    if not m:
        return (measure or "").strip()
    unit = (m.group(3) or "").lower()
    rest = m.group(4).strip()
    try:
        lo = _num(m.group(1))
        hi = _num(m.group(2)) if m.group(2) else None
    except (ValueError, ZeroDivisionError):
        return measure.strip()
    factor = {"oz": 3, "ounce": 3, "ounces": 3, "shot": 4, "shots": 4, "jigger": 4.5, "jiggers": 4.5,
              "cl": 1}.get(unit)
    if unit == "ml":
        factor = 0.1
    if factor:
        txt = _fmt(lo * factor) + (f"–{_fmt(hi * factor)}" if hi else "") + " cl"
    else:
        num = m.group(1).strip() + (f"–{m.group(2).strip()}" if m.group(2) else "")
        txt = (num + " " + _UNIT_NL.get(unit, unit)).strip()
    return (txt + " " + rest).strip()


# ---- shelf life after opening (used by the fill-level tracking) ----------------------------------
SHELF_DAYS_BY_CAT = {"aperitief": 60, "port": 90, "wijn": 5, "bubbels": 3, "bier": 2, "cider": 3, "sap": 7,
                     "mixer": 5, "siroop": 180}
_CREAM = _phrase_re(list(_cream) + ["cream", "room", "creme liqueur", "advocaat", "eierlikeur", "eierlikor",
                                    "egg liqueur", "verpoorten"])


def default_shelf_days(p) -> int | None:
    """How long an opened bottle stays good, None = (practically) unlimited, like most spirits."""
    hay = norm(" ".join(str(p[k] or "") for k in ("name", "brand", "description")))
    if p["category"] == "likeur" or "likeur" in (p["extra_categories"] or ""):
        if _CREAM.search(norm(f"{p['name']} {p['brand']}")) or "cream liqueur" in hay:
            return 180
    if p["category"] == "port" and re.search(r"(?<![a-z])(fino|manzanilla)(?![a-z])", hay):
        return 14
    return SHELF_DAYS_BY_CAT.get(p["category"])


# ---- sync from TheCocktailDB -------------------------------------------------------------------
def _store_drink(db: Database, d: dict, now: float) -> None:
    db.run("""INSERT INTO cocktails(id, name, category, alcoholic, glass, iba, instructions, thumb_url, updated_at)
              VALUES (?,?,?,?,?,?,?,?,?)
              ON CONFLICT(id) DO UPDATE SET name=excluded.name, category=excluded.category,
                alcoholic=excluded.alcoholic, glass=excluded.glass, iba=excluded.iba,
                instructions=excluded.instructions,
                image = CASE WHEN thumb_url = excluded.thumb_url THEN image ELSE '' END,
                thumb_url=excluded.thumb_url, updated_at=excluded.updated_at""",
           d["idDrink"], (d.get("strDrink") or "").strip()[:150], d.get("strCategory") or "",
           d.get("strAlcoholic") or "", d.get("strGlass") or "", d.get("strIBA") or "",
           (d.get("strInstructions") or "").strip(), d.get("strDrinkThumb") or "", now)
    db.run("DELETE FROM cocktail_ingredients WHERE cocktail_id=?", d["idDrink"])
    pos = 0
    for i in range(1, 16):
        name = (d.get(f"strIngredient{i}") or "").strip()
        if name:
            db.run("INSERT INTO cocktail_ingredients(cocktail_id, pos, name, measure) VALUES (?,?,?,?)",
                   d["idDrink"], pos, name[:80], (d.get(f"strMeasure{i}") or "").strip()[:60])
            pos += 1


def sync(db: Database, settings, client_factory, pause: float = 0.05) -> dict:
    """Fetch every cocktail TheCocktailDB has. The free API caps search results at 25 per letter,
    so ids are also collected via the category/glass/type/ingredient filters and looked up one by one."""
    now = time.time()
    drinks: dict[str, dict] = {}
    ids: set[str] = set()
    with client_factory() as c:
        failed = 0

        def get(endpoint: str, **params):
            """One API call; the free API answers 429 when asked too fast, so wait and retry."""
            nonlocal failed
            for attempt in range(5):
                r = c.get(API + endpoint, params=params, timeout=20)
                if r.status_code == 429 or r.status_code >= 500:
                    wait = float(r.headers.get("retry-after") or 0) or 10 * 2 ** attempt
                    time.sleep(min(wait, 120) if pause else 0)
                    continue
                r.raise_for_status()
                if pause:
                    time.sleep(pause)
                try:
                    data = r.json()
                except ValueError:
                    return []
                res = (data or {}).get("drinks")
                return res if isinstance(res, list) else []
            failed += 1
            log.info("TheCocktailDB %s %s: blijft te druk, overgeslagen", endpoint, params)
            return []

        def store(batch) -> None:
            with db.tx():
                for d in batch:
                    _store_drink(db, d, now)

        for ch in string.ascii_lowercase + string.digits:
            for d in get("search.php", f=ch):
                drinks[d["idDrink"]] = d
        if not drinks:
            raise httpx.HTTPError("TheCocktailDB gaf geen recepten terug")
        store(drinks.values())          # keep what we have, even if the rest fails
        ids |= set(drinks)
        for kind, field in (("c", "strCategory"), ("g", "strGlass"), ("a", "strAlcoholic"),
                            ("i", "strIngredient1")):
            for v in get("list.php", **{kind: "list"}):
                if v.get(field):
                    ids |= {x["idDrink"] for x in get("filter.php", **{kind: v[field]}) if x.get("idDrink")}
        have = {r["id"] for r in db.all("SELECT id FROM cocktails")}
        extra = []
        for i in sorted(ids - set(drinks) - have):
            for d in get("lookup.php", i=i):
                drinks[d["idDrink"]] = d
                extra.append(d)
            if len(extra) >= 25:
                store(extra)
                extra = []
        store(extra)
        images_added = 0
        target = Path(settings.image_dir) / "cocktails"
        for r in db.all("SELECT id, thumb_url FROM cocktails WHERE image='' AND thumb_url != ''"):
            try:
                data = images.fetch(c, r["thumb_url"] + "/medium", settings.max_image_bytes)
                name = images.store(data, str(target))
            except Exception as exc:  # noqa: BLE001
                log.info("cocktailfoto %s overgeslagen: %s", r["id"], exc)
                continue
            db.run("UPDATE cocktails SET image=? WHERE id=?", "cocktails/" + name, r["id"])
            images_added += 1
            if pause:
                time.sleep(pause / 2)
    total = db.one("SELECT COUNT(*) AS n FROM cocktails")["n"]
    if failed > 10:
        # Mostly rate-limited: keep what we got, but try again in an hour instead of next week.
        raise httpx.HTTPError(f"{failed} verzoeken mislukt; {total} recepten opgehaald, later opnieuw")
    db.run("INSERT INTO kv(key, value) VALUES ('cocktails_synced', ?) "
           "ON CONFLICT(key) DO UPDATE SET value=excluded.value", str(now))
    log.info("cocktails bijgewerkt: %d recepten (%d opgehaald, %d foto's)", total, len(drinks), images_added)
    return {"total": total, "fetched": len(drinks), "images": images_added}


def last_sync(db: Database) -> float | None:
    r = db.one("SELECT value FROM kv WHERE key='cocktails_synced'")
    return float(r["value"]) if r else None


class CocktailSync:
    """Background thread: fetch the recipes at first start and then once a week."""

    def __init__(self, db: Database, settings, lookup):
        self.db, self.settings, self.lookup = db, settings, lookup
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self.busy = False
        self.error = ""

    def start(self) -> None:
        if self.settings.lookup_inline:
            return
        self._thread = threading.Thread(target=self._run, name="cocktail-sync", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=5)

    def now(self) -> None:
        if self.settings.lookup_inline:
            self.run_once()
        else:
            self.db.run("DELETE FROM kv WHERE key='cocktails_synced'")
            self._wake.set()

    def run_once(self) -> None:
        self.busy, self.error = True, ""
        try:
            sync(self.db, self.settings, self.lookup.client, 0 if self.settings.lookup_inline else 0.6)
        except (httpx.HTTPError, OSError) as exc:
            self.error = str(exc) or type(exc).__name__
            log.warning("cocktails ophalen mislukt: %s", self.error)
        except Exception as exc:  # noqa: BLE001 - the thread must keep running
            self.error = str(exc) or type(exc).__name__
            log.exception("cocktails ophalen mislukt")
        finally:
            self.busy = False

    def _run(self) -> None:
        if self._stop.wait(20):          # let the app start first
            return
        while not self._stop.is_set():
            last = last_sync(self.db)
            if last is None or time.time() - last > SYNC_EVERY:
                self.run_once()
                if self.error:            # try again in an hour
                    self._wake.wait(3600)
                    self._wake.clear()
                    continue
            self._wake.wait(3600)
            self._wake.clear()
