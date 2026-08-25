"""Fixture data for itel collector tests.

itel-india.com is a client-rendered SPA (see collectors/itel.py module
docstring) — `ItelFetcher` returns already-structured data extracted by a
headless browser, not raw HTML, so there is no HTML file to fixture here.
These dicts/tuples are the fixture: the exact shape `page.evaluate()`
returns in production, captured against the live site on 2026-08-18 (card
text) and re-derived from a real captured DOM snippet (spec rows).
"""

FEATURE_PHONE_CARDS = [
    {"href": "/product/super-guru-4g", "text": "Super Guru 4G"},
    {"href": "/product/super-guru-4g", "text": "1,799"},  # short duplicate anchor (price link)
    {"href": "/product/it2165c", "text": "newit2165C"},
    {"href": "/product/ace-3-heera", "text": "newAce 3 Heera"},
    # a slug seen ONLY as a short/empty-ish duplicate anchor still resolves
    # via its longer sibling anchor elsewhere in the list
    {"href": "/product/flip-one", "text": "Flip One"},
    {"href": "/product/flip-one", "text": ""},
    # real bug caught live 2026-08-18: on the actual site this single
    # anchor's text concatenates name + bullet-point blurb + price with NO
    # separator at all — "longest text wins" alone would pick this whole
    # blob as the model name. The spec table's own "Model Name" row (see
    # ACE_2_HEERA_SPEC_ROWS) is what must win instead.
    {
        "href": "/product/ace-2-heera",
        "text": 'Ace 2 Heera1.77" Big Display | 1000mAh battery | '
                "Bluetooth | Auto Call Recording | Wireless FM with Recording1,109",
    },
]

SMARTPHONE_CARDS = [
    {"href": "/product/zeno-100-pro", "text": "Zeno 100 Pro"},
    {"href": "/product/a100-pro", "text": "A100 Pro"},
    # deliberately conflicting: it2165c also appears on the smartphones
    # listing in this fixture set — super-guru-4g stays clean so it can be
    # the un-ambiguous happy-path case elsewhere
    {"href": "/product/it2165c", "text": "newit2165C"},
]

SUPER_GURU_4G_SPEC_ROWS = [
    ("Model", "Super Guru 4G"),
    ("Colors", "Black, Blue, Light Green"),
    ("Display", '5.09 cm(2")'),
    ("Battery", "1000 mAH"),
    ("Language Support", "13 (English, Hindi, Gujarati)"),
    ("Phonebook", "2000"),
    ("SMS", "500"),
    ("Kingvoice", "Yes"),
    ("Torch", "Yes"),
    ("LetsChat", "Yes"),
    ("SAR Value", "1.141W/kg 1g Head Tissue"),
]

# it2165c and ace-3-heera: page loads but has a thinner General tab (no
# Kingvoice/LetsChat rows) — realistic incomplete-but-present case.
IT2165C_SPEC_ROWS = [
    ("Model", "it2165C"),
    ("Display", '2" Big Display'),
    ("Battery", "1200mAh"),
]

# real spec-table shape observed live for this product family: the label
# is "Model Name", not "Model" — the collector must check both.
ACE_2_HEERA_SPEC_ROWS = [
    ("Model Name", "Ace 2 Heera"),
    ("Dimension", "114*49*14.2mm"),
    ("SIM Type", "2-Mini"),
]
