"""ISO 16140-2:2016 Annex A (Table A.1 -- "Classification of samples and
their relevance for testing for various microorganisms") food category
taxonomy, and a normalizer that maps a mined report's free-text category
label onto it.

The 18 categories and their English/French names below are transcribed
directly from the project owner's own copy of Table A.1 (both language
editions supplied as a workbook) -- not reconstructed from memory or
guessed at. This is the actual fixed column set the project owner asked
the frontend's "tested food category" axis to use, instead of the ~108
distinct raw free-text strings mined reports actually use (which are full
of synonyms and near-duplicates -- "Dairy products" / "Milk & Dairy
products" / "Raw dairy products" / "Raw milk and dairy products" / ...
all really mean the same one or two of the categories below).

Real mined category labels don't reliably state Annex A's raw/ready-to-eat
split -- a report may just say "Meat products" or "Dairy products" with no
further qualifier, never mentioning which of the two related Annex A
categories (e.g. "Raw meat..." vs "Ready-to-eat, ready-to-reheat meat
products") it actually means. normalize_food_category() below can only be
as precise as its input allows:
  - An explicit qualifier (raw / pasteurised / ready-to-eat / heat-processed
    / cooked / ...) routes to the matching specific category.
  - A bare, unqualified food-family label (e.g. plain "Meat products")
    defaults to that family's raw/unprocessed Annex A category -- the more
    commonly tested starting matrix in these validation studies, and the
    same convention used consistently below for every raw/RTE-split family
    (dairy, meat, poultry, seafood, produce). This is a documented
    assumption, not a certainty -- it can silently be wrong for a specific
    report that happens to mean the RTE side without saying so.
  - A label naming no recognizable food family at all (e.g.
    "Miscellaneous", "Ingredients and specific products", or a truncated
    extraction artifact like a bare "Production") is left unclassified
    (None) rather than guessed -- callers should log these for review
    instead of silently absorbing them.
  - A label naming two food families at once (e.g. "Vegetables and
    seafood products") resolves to whichever family's rule is checked
    first below (documented per branch) -- picking one loses the other,
    a known limitation of any single-category assignment for a
    multi-family label.

New product/report category label -> add a new call site here, not a
schema change: the intent is that a genuinely new phrasing of an existing
Annex A category gets picked up by the keyword rules below, and anything
that doesn't is visibly unclassified rather than silently wrong.
"""
import re

ANNEX_A_CATEGORIES = [
    {"id": "raw_milk_dairy", "en": "Raw milk and dairy products",
     "fr": "Lait cru et produits laitiers"},
    {"id": "heat_processed_dairy", "en": "Heat-processed milk and dairy products",
     "fr": "Lait et produits laitiers traités à chaud"},
    {"id": "raw_meat", "en": "Raw meat and ready-to-cook meat products (except poultry)",
     "fr": "Viande crue et produits à base de viande prêts à cuisiner (sauf volaille)"},
    {"id": "rte_meat", "en": "Ready-to-eat, ready-to-reheat meat products",
     "fr": "Produits à base de viande prêts à réchauffer, prêts à consommer"},
    {"id": "raw_poultry", "en": "Raw poultry and ready-to-cook poultry products",
     "fr": "Volaille crue et produits à base de volaille prêts à cuire"},
    {"id": "rte_poultry", "en": "Ready-to-eat, ready-to-reheat meat poultry products",
     "fr": "Produits à base de volaille prêts à réchauffer, prêts à consommer"},
    {"id": "eggs", "en": "Eggs and egg products (derivates)",
     "fr": "Œufs et ovoproduits (dérivés)"},
    {"id": "raw_seafood", "en": "Raw and ready-to-cook fish and seafoods (unprocessed)",
     "fr": "Poissons et produits de la mer crus et prêts à cuire (non transformés)"},
    {"id": "rte_seafood", "en": "Ready-to-eat, ready-to-reheat fishery products",
     "fr": "Produits de la mer prêts à réchauffer, prêts à consommer"},
    {"id": "fresh_produce", "en": "Fresh produce and fruits",
     "fr": "Produits frais et fruits"},
    {"id": "processed_produce", "en": "Processed fruits and vegetables",
     "fr": "Fruits et légumes transformés"},
    {"id": "dried_produce", "en": "Dried cereals, fruits, nuts, seeds and vegetables",
     "fr": "Céréales, fruits, noix, graines et légumes déshydratés"},
    {"id": "infant_formula", "en": "Infant formula and infant cereals",
     "fr": "Lait et céréales infantiles"},
    {"id": "chocolate_bakery", "en": "Chocolate, bakery products and confectionary",
     "fr": "Chocolat, produits de pâtisserie et confiserie"},
    {"id": "composite_foods", "en": "Multi-component foods or meal components",
     "fr": "Aliments à multiples composants ou composants de repas"},
    {"id": "pet_food_feed", "en": "Pet food and animal feed",
     "fr": "Aliments pour animaux et animaux de compagnie"},
    {"id": "environmental_samples", "en": "Environmental samples (food or feed production)",
     "fr": "Échantillons environnementaux (production d'aliment ou d'aliment pour animaux)"},
    {"id": "primary_production_samples", "en": "Primary production samples (PPS)",
     "fr": "Échantillons de production primaire (ou PPS pour Primary Production Samples)"},
]

LABEL_BY_ID = {c["id"]: c["en"] for c in ANNEX_A_CATEGORIES}

# Checked in this order after "cooked"/"ready-to-eat"-style wording so a
# combined label like "Composite foods / Ready-to-eat and ready-to-reheat"
# still resolves as RTE-flavoured, but the check only matters within a
# branch that already knows which food family it's in.
_RTE_KEYWORDS = ("rte", "rtrh", "ready-to-eat", "ready to eat",
                  "ready-to-reheat", "ready to reheat", "cooked", "deli")


def _has(t: str, *keywords: str) -> bool:
    return any(k in t for k in keywords)


def normalize_food_category(raw: str):
    """Map a mined report's free-text category label to one of
    ANNEX_A_CATEGORIES's ids, or None if it names no recognizable food
    family. See the module docstring for the raw/RTE default-splitting
    rule and its limitations."""
    if not raw:
        return None
    t = raw.lower()

    # Checked ahead of the generic dairy rule below: these phrases always
    # co-occur with "milk powders" in real reports, which would otherwise
    # be caught by the dairy branch.
    if _has(t, "infant formula", "infant cereal"):
        return "infant_formula"
    if _has(t, "cocoa", "chocolate", "bakery", "confection"):
        return "chocolate_bakery"
    # Checked ahead of the general "environmental" rule: several real
    # labels read e.g. "Poultry primary production samples (PPS)".
    if _has(t, "primary production") or re.search(r'\bpps\b', t):
        return "primary_production_samples"
    if "environmental" in t:
        return "environmental_samples"
    if _has(t, "pet food", "animal feed", "feed stuff", "feed product"):
        return "pet_food_feed"
    # Checked ahead of the eggs/meat/poultry rules below: Annex A's own
    # name for this category is "composite", and several real labels pair
    # it with another family's name (e.g. "Egg products and composite",
    # "Composite foods / Ready-to-eat and ready-to-reheat") -- "composite"
    # wins in those cases since it's the more specific, defining word.
    if _has(t, "composite", "multicomponent", "multi-component", "meal component"):
        return "composite_foods"
    if _has(t, "dried", "dehydrat"):
        return "dried_produce"
    if "egg" in t:
        return "eggs"
    # Poultry is checked ahead of the general meat rule since it's Annex
    # A's own separate branch; a label naming both (e.g. "Meat and
    # poultry") resolves as poultry, losing the non-poultry-meat half.
    if _has(t, "poultry", "chicken", "turkey"):
        return "rte_poultry" if _has(t, *_RTE_KEYWORDS) else "raw_poultry"
    if _has(t, "meat", "beef", "pork"):
        return "rte_meat" if _has(t, *_RTE_KEYWORDS) else "raw_meat"
    if _has(t, "seafood", "fish", "fishery", "shellfish", "crustacean"):
        return "rte_seafood" if _has(t, *_RTE_KEYWORDS) else "raw_seafood"
    if _has(t, "dairy", "milk", "cheese", "yog"):
        if _has(t, "pasteuris", "pasteuriz", "heat process", "heat-process",
                 "sterilis", "steriliz", "uht"):
            return "heat_processed_dairy"
        return "raw_milk_dairy"
    if _has(t, "veget", "fruit", "produce", "salad"):
        if _has(t, "processed", "canned", "juice"):
            return "processed_produce"
        return "fresh_produce"
    return None
