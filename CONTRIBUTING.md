# Contributing to BarManager

Thank you for wanting to help! BarManager keeps track of a home bar by scanning barcodes, and every bit of
help makes it better: a bug report, an idea, a translation, a better ingredient match or a fix.

Please read the [Code of Conduct](CODE_OF_CONDUCT.md) first. Everyone who takes part is expected to follow
it.

## Questions, ideas and bugs

- **A question** ("how do I ...?"): ask it in the
  [Q&A of Discussions](https://github.com/Lexvdpoel/barmanager/discussions/categories/q-a).
- **An idea** for a feature: post it in
  [Ideas](https://github.com/Lexvdpoel/barmanager/discussions/categories/ideas). Upvote the ideas you want;
  the most wanted ones are picked up first.
- **A bug**: open a [bug report](https://github.com/Lexvdpoel/barmanager/issues/new/choose). Give the
  version (bottom of the Account page, or `/healthz`), how you run it (Docker on which machine), your
  browser and phone, and the steps that lead to the problem. The container log helps a lot.
- **A barcode that is not found or a bottle in the wrong category**: that is a bug too. Give the barcode
  and what the bottle is.
- **A security problem**: do not post it in public. Report it privately through
  [private vulnerability reporting](https://github.com/Lexvdpoel/barmanager/security/advisories/new); see
  [SECURITY.md](SECURITY.md).

## Before you write code

For anything bigger than a small fix, open an idea or an issue first and say what you plan to do. That way
we can agree on the approach before you spend time on it, and two people do not build the same thing.

## Setting up

You need Python 3.11 or newer (the image uses 3.12).

```sh
git clone https://github.com/Lexvdpoel/barmanager.git
cd barmanager
python -m venv .venv
.venv/bin/pip install -e ".[dev]"                  # Windows: .venv\Scripts\pip
BM_ADMIN_PASSWORD=test1234 .venv/bin/barmanager    # http://localhost:8080
```

The camera only works over https or on `localhost`. To test the scanner on a phone, put the dev server
behind a reverse proxy with a certificate, or type barcodes into the text field.

## Tests

```sh
.venv/bin/python -m pytest -q
```

The tests never go online: `tests/conftest.py` fakes Open Food Facts, UPCitemdb, Wikipedia, SearXNG,
TheCocktailDB and Ollama. New behaviour needs a test; a fix needs a test that failed before it.

## How the code is written

- **Every text the user sees goes through `_()`** (templates and Python) or `BM.t()` (JavaScript), with
  the Dutch text as the key and the English translation in `barmanager/locale_en.py`.
  `tests/test_i18n.py` fails when a translation is missing, and checks that English pages contain no
  Dutch.
- The AI may only fill in facts it found in the sources, never numbers from memory, and never a product
  for an unknown barcode. Keep it that way.
- Matching bottles to cocktail ingredients lives in `barmanager/cocktails.py` (`SPECS`). A new rule there
  comes with a test in `tests/test_features.py`.
- Keep the pages usable with one hand on a phone, and without colour as the only signal.
- No new runtime dependencies without a good reason: the image runs on a Raspberry Pi.

## Pull requests

- One change per pull request, with a clear title and what it changes for the user.
- Fill in the checklist in the pull request template.
- The build (tests, then the Docker image) must be green.
- The website (`site/`) has both an English and a Dutch text for everything.

By contributing you agree that your contribution is licensed under the [MIT License](LICENSE) of the
project.
