"""Canonicalize the free-text identity fields shared across sources:
target organism, manufacturer, expert laboratory, and detection technology.

Same problem food_categories.py already solves for food matrices, applied to
the other axes the frontend filters on. Each source (and each certificate
within a source) writes these names however its own paperwork happened to
phrase them, so the raw data carries many spellings of one real thing:

  - "Yeasts and Moulds" / "Yeasts and molds" / "Levures et Moisissures"
    are one organism written in British English, American English, and
    French.
  - "BIO-RAD" / "Bio-Rad" / "Bio-Rad Laboratories" are one company; so are
    the four spellings of bioMerieux and the four of Neogen.
  - "ADRIA" and "ADRIA Developpement" are one expert laboratory.

Left uncanonicalized these fragment every filter in the UI: a user picking
"Bio-Rad" from a manufacturer list silently gets 19 of that company's 24
methods, which is worse than useless for a comparison tool.

Design rules this module follows, and the reason for each:

1. Merge only what is genuinely the same thing. "Listeria spp." and
   "Listeria monocytogenes" look mergeable and are NOT -- genus-level
   detection and species-level detection are different validated claims,
   and a lab choosing a method needs to see the difference. Same for
   "Coliforms" vs "Escherichia coli". Spelling, language, punctuation and
   legal-entity suffixes are safe to merge; scope is not.

2. The manufacturer unit is the company you would buy from today, not the
   brand printed on the certificate. Oxoid and Life Technologies are both
   Thermo Fisher, and at the project owner's decision they are listed as
   Thermo Fisher Scientific rather than as separate entries -- a reader
   filtering by supplier wants all 19 of that company's methods, not 12
   under one heritage brand and 3 under another. The brand name still
   reaches the reader through the method's own commercial name (a "Thermo
   Scientific(TM) SureTect(TM)" assay says so on its face), which is where
   it is genuinely useful; a filter facet is not.

   Where a company is widely known under a name that differs from its
   current owner's, the owner stays in parentheses -- "MilliporeSigma
   (Merck)", "Solus Scientific (PerkinElmer)" -- so one entry still tells
   the reader both things.

3. Anything matching no rule passes through with light cleanup (whitespace,
   trailing punctuation) rather than being forced into a bucket, and
   callers can log it. A genuinely new manufacturer must show up as itself,
   not silently absorbed into whatever it resembles.
"""
import re
import unicodedata


def _fold(s: str) -> str:
    """Accent- and case-insensitive comparison key. 'bioMerieux',
    'bioMérieux' and 'BIOMERIEUX' must all hit the same rule, and the raw
    data really does contain all three shapes."""
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r'[^a-z0-9]+', '', s.lower())


def _tidy(s: str) -> str:
    """Collapse whitespace and strip the trailing punctuation these fields
    routinely carry ('bioMérieux SA_', 'STEC (... Escherichia coli )')."""
    s = re.sub(r'\s+', ' ', str(s)).strip()
    s = re.sub(r'\s+([)\]])', r'\1', s)          # "coli )" -> "coli)"
    return s.strip(" ,;_-")


# ---------------------------------------------------------------------------
# Target organisms
# ---------------------------------------------------------------------------
# (canonical label, [folded aliases]). Aliases are matched on the folded key,
# so case/accents/punctuation variants need not be listed separately.
_ORGANISM_RULES = [
    ("Yeasts and moulds", [
        "yeastsandmoulds", "yeastsandmolds", "yeastmould", "yeastmold",
        "levuresetmoisissures", "levuresmoisissures",
    ]),
    # ISO 4833 total viable count. "Total bacterial count" and "aerobic
    # mesophilic flora" are the same ISO 4833 enumeration under the names
    # the dairy sector and the French certificates respectively use for it.
    ("Total viable count (aerobic mesophilic flora)", [
        "totalviablecount", "totalbacterialcount",
        "aerobicmesophilicflorattotalviablecount",
        "aerobicmesophilicflora/totalviablecount",
        "aerobicmesophilicfloratotalviablecount",
        "floretotale", "totalplatecount", "totalaerobiccount",
    ]),
    ("Escherichia coli O157", ["ecolio157", "escherichiacolio157", "ecolio157h7", "escherichiacolio157h7"]),
    # Kept separate from E. coli O157 on purpose: STEC is a broader
    # virulence-gene-defined group (ISO/TS 13136), not the O157 serogroup.
    ("Shiga toxin-producing E. coli (STEC)", [
        "shigatoxinproducingecolistec", "stecshigatoxinproducingescherichiacoli",
        "stec", "ehec", "shigatoxinproducingescherichiacoli",
    ]),
    ("Escherichia coli", ["escherichiacoli", "ecoli"]),
    ("Listeria spp. and L. monocytogenes", [
        "listeriasppandlmonocytogenes", "listeriamonocytogeneslisteriaspp",
        "listeriasppandlisteriamonocytogenes", "listeriamonocytogenesandlisteriaspp",
    ]),
    ("Escherichia coli and coliforms", ["escherichiacolicoliforms", "escherichiacoliandcoliforms"]),
    ("Enterobacteriaceae and Cronobacter spp.", [
        "enterobacteriaceaecronobacterspp", "enterobacteriaceaeandcronobacterspp",
    ]),
    ("Salmonella spp. and Enterobacteriaceae", [
        "salmonellasppenterobacteriaceae", "salmonellasppandenterobacteriaceae",
    ]),
    ("Mesophilic lactic acid bacteria", [
        "bacterieslactiquesmesophiles", "mesophiliclacticacidbacteria", "lacticacidbacteria",
    ]),
    ("Coagulase-positive staphylococci", [
        "coagulasepositivestaphylococci", "staphylococcusaureus", "coagulasepositivestaphylococcus",
    ]),
    ("Enterobacteriaceae", ["enterobacteriaceae", "enterobacterales"]),
    ("Somatic cell count", ["somaticcellcount", "somaticcells"]),
    ("Salmonella spp.", ["salmonellaspp", "salmonella"]),
    ("Listeria monocytogenes", ["listeriamonocytogenes", "lmonocytogenes"]),
    ("Listeria spp.", ["listeriaspp", "listeria"]),
    ("Cronobacter spp.", ["cronobacterspp", "cronobacter", "enterobactersakazakii"]),
    ("Campylobacter spp.", ["campylobacterspp", "campylobacter"]),
    ("Bacillus cereus", ["bacilluscereus", "bacilluscereusgroup"]),
    ("Pseudomonas spp.", ["pseudomonasspp", "pseudomonas"]),
    ("Enterococcus spp.", ["enterococcusspp", "enterococcus"]),
    ("Coliforms", ["coliforms", "coliformes", "gasproducingcoliforms"]),
    ("Antibiotic residues", ["antibioticresidues", "residusdantibiotiques"]),
    ("Commercial sterility", ["commercialsterility", "sterilitecommerciale"]),
]

_ORGANISM_BY_ALIAS = {alias: label for label, aliases in _ORGANISM_RULES for alias in aliases}


def canonical_organism(raw):
    """One label per real target. Unknown names pass through tidied."""
    if not raw:
        return None
    return _ORGANISM_BY_ALIAS.get(_fold(raw)) or _tidy(raw)


# ---------------------------------------------------------------------------
# Manufacturers
# ---------------------------------------------------------------------------
# (canonical label, [folded-substring patterns]). Substring rather than exact
# match: these names carry unpredictable legal suffixes, addresses and
# parent-company parentheticals ("BioMérieux S.A. - Marcy L'Etoile",
# "Solus Scientific (part of PerkinElmer, Inc.)"), and enumerating every
# real permutation proved impossible -- the brand token is the stable part.
# Order matters: the first match wins, so more specific brands are listed
# before the parent corporations that would otherwise swallow them.
_MANUFACTURER_RULES = [
    ("bioMérieux", ["biomerieux"]),
    ("Bio-Rad", ["biorad"]),
    ("Neogen", ["neogen"]),
    ("Hygiena", ["hygiena"]),
    ("MilliporeSigma (Merck)", ["milliporesigma", "millaporesigma", "milliporesigma", "millipore"]),
    ("Solabia", ["solabia"]),
    ("Solus Scientific (PerkinElmer)", ["solusscientific"]),
    # Oxoid and Life Technologies are Thermo Fisher brands and are listed
    # as Thermo Fisher -- see rule 2. The typos are real and in the source
    # data ("Life Technologies Coporation", "Part of Termo Fisher").
    ("Thermo Fisher Scientific", [
        "thermofisher", "thermoscientific", "oxoid", "lifetechnologies",
        "termofisher",
    ]),
    ("Foss", ["fossanalytical", "foss"]),
    ("Bruker", ["bruker"]),
    ("Shimadzu Diagnostics", ["shimadzu"]),
    ("Kikkoman Biochemifa", ["kikkoman"]),
    ("JNC Corporation", ["jnccorporation"]),
    ("Gold Standard Diagnostics", ["goldstandarddiagnostics"]),
    ("Bentley Instruments", ["bentleyinstruments"]),
    ("CONGEN Biotechnologie", ["congen"]),
    ("R-Biopharm", ["rbiopharm"]),
    ("Charm Sciences", ["charmsciences"]),
    ("Check-Points", ["checkpoints"]),
    ("BioChek", ["biochek"]),
    ("Delta Instruments", ["deltainstruments"]),
    ("IDEXX", ["idexx"]),
    ("SY-LAB", ["sylab"]),
    ("HyServe", ["hyserve"]),
    ("DSM", ["dsmfood", "dsm"]),
    ("Autobio Diagnostics", ["autobio"]),
    ("Applied Food Diagnostics", ["appliedfooddiagnostics"]),
    ("ALS Life Sciences", ["alslifesciences"]),
    ("MCS Diagnostics", ["mcsdiagnostics"]),
    ("Microbial Systems", ["microbialsystems"]),
    ("SAN Group Biotech", ["sangroup"]),
]


def canonical_manufacturer(raw):
    """One label per real company. Unknown names pass through tidied."""
    if not raw:
        return None
    key = _fold(raw)
    for label, patterns in _MANUFACTURER_RULES:
        if any(p in key for p in patterns):
            return label
    return _tidy(raw)


# ---------------------------------------------------------------------------
# Expert laboratories
# ---------------------------------------------------------------------------
# The lab that ran the validation study, named on the summary report's cover
# page/header. Same substring-matching rationale as manufacturers: these
# appear with and without their legal form, their city, and their parent
# institute ("ADRIA", "ADRIA Developpement", "ADRIA Developpement ZA
# Creac'h Gwen 29000 Quimper" are one lab).
_EXPERT_LAB_RULES = [
    ("ADRIA Développement", ["adria"]),
    ("Microsept", ["microsept"]),
    ("ISHA", ["isha", "institutscientifiquedhygieneetdanalyse"]),
    ("Institut Pasteur de Lille", ["institutpasteurdelille", "iplsante", "ipl"]),
    ("ACTALIA", ["actalia"]),
    # Real labs confirmed by the first full extraction run over 238 reports;
    # "Inovalys site de Tours" and a bare "Inovalys" are one laboratory, so
    # the site suffix is folded away like any other spelling variant.
    ("Inovalys", ["inovalys"]),
    ("Labocea", ["labocea", "lda22"]),
    ("Eurofins", ["eurofins"]),
    ("CTCPA", ["ctcpa"]),
    ("Campden BRI", ["campdenbri", "campden"]),
    ("NIZO", ["nizo"]),
    ("TNO", ["tno"]),
    ("Q-lip", ["qlip"]),
    ("Wageningen Food Safety Research", ["wageningen", "rikilt"]),
    ("Fraunhofer IVV", ["fraunhofer"]),
    ("SGS", ["sgs"]),
    ("LRQA Nederland", ["lrqa"]),
]

# A capture that is not plausibly a laboratory name. The first real
# extraction run produced two of these from otherwise reasonable sentence
# patterns: "For" (a sentence fragment swallowed by a "...performed by"
# match) and "W. Jacobs-Reitsma" (a researcher credited in the study, not
# the lab that ran it). Rejecting them keeps a junk value out of a filter
# facet, where it is far more visible and more confusing than a blank.
_LAB_STOPWORDS = {
    "for", "the", "this", "these", "all", "both", "each", "one", "two",
    "a", "an", "and", "or", "of", "in", "on", "by", "with",
    "le", "la", "les", "un", "une", "des", "du", "de", "et", "par",
    "study", "etude", "report", "method", "test", "sample", "samples",
    "laboratory", "laboratoire", "expert",
}

# "W. Jacobs-Reitsma", "J.-P. Dupont": an initial followed by a surname is
# a person, and a person is never the answer to "which lab ran this".
_PERSON_NAME_RE = re.compile(r'^\s*(?:[A-Z]\.\s*){1,3}[A-Z][\w\'\-]+\s*$')


def _is_plausible_lab_name(name: str) -> bool:
    cleaned = _tidy(name)
    if len(cleaned) < 3:
        return False
    if cleaned.lower() in _LAB_STOPWORDS:
        return False
    if _PERSON_NAME_RE.match(cleaned):
        return False
    # A single short generic word ("For", "Study") is a capture artifact;
    # real one-word labs in this domain are acronyms (ISHA, TNO, CTCPA) or
    # long enough to be distinctive.
    if " " not in cleaned and cleaned.islower():
        return False
    return True


def canonical_expert_lab(raw):
    """One label per real laboratory.

    A name matching a known laboratory always wins, however the report
    phrased it. An unrecognized name is kept only if it plausibly IS a
    laboratory name -- see _is_plausible_lab_name -- and dropped otherwise,
    because a junk value in a filter facet is worse than no value.
    """
    if not raw:
        return None
    key = _fold(raw)
    for label, patterns in _EXPERT_LAB_RULES:
        if any(p in key for p in patterns):
            return label
    return _tidy(raw) if _is_plausible_lab_name(raw) else None


# ---------------------------------------------------------------------------
# Detection technology
# ---------------------------------------------------------------------------
# chromogenic_agar folds into culture_media at the project owner's explicit
# request: a chromogenic plate IS a culture medium, and splitting the two
# put the same physical product family in two different filter buckets.
_CATEGORY_ALIASES = {
    "chromogenic_agar": "culture_media",
}

CATEGORY_LABELS = {
    "culture_media": "Culture media",
    # "/ PCR" was too narrow once a report was actually read: 2015LR53
    # (RiboFlow) detects an rRNA sequence by nucleic acid hybridisation on a
    # lateral flow strip and states outright that "enzymatic amplification of
    # target sequence is not" required. It is molecular but it is not PCR, and
    # filing it under a label that says PCR would misinform the reader. The id
    # is left alone -- it appears in facets.json, in the Worker's tool enum and
    # in any filter a reader has bookmarked.
    "molecular_pcr": "Molecular (PCR / hybridisation)",
    "immunological_elisa": "Immunological / ELISA",
    "flow_cytometry": "Flow cytometry",
    "biochemical": "Biochemical",
    # Antibiotic-residue screening: a seeded agar whose indicator changes
    # colour unless a residue stops the organism growing. Not a technology for
    # detecting a microorganism at all -- the target of these methods is a drug
    # residue -- so folding them into "Culture media" would put them in front
    # of someone shopping for a Listeria method.
    "inhibition_assay": "Microbial inhibition",
    "other": "Other",
}

# Product families whose real detection principle is known, used to classify
# records whose source listing carries no technology field at all (MicroVal
# publishes none, so 40 of its certificates arrived as null -> "other").
# Each entry is a brand or model token confirmed against the real product,
# not a guess from the name alone; anything not listed here stays
# unclassified and is reported, per rule 3.
_TECHNOLOGY_KEYWORDS = [
    ("molecular_pcr", [
        # Real-time PCR kit lines.
        "foodproof", "food proof", "vetproof", "vet proof",
        "gene-up", "geneup", "surefast", "kylt", "iq-check", "iqcheck",
        "inviscreen", "bax", "assurance gds", "check & trace", "check and trace",
        # Neogen's "Molecular Detection Assay" is isothermal amplification --
        # not PCR strictly, but molecular nucleic-acid amplification, which
        # is what this bucket represents.
        "molecular detection assay", "mda 2", "mda2",
        "3plex", "taqman", "real-time pcr", "realtime pcr", " pcr",
    ]),
    ("flow_cytometry", [
        # Dairy instrument lines: all optical/flow counting of cells or
        # bacteria, not culture.
        "fossomatic", "bacsomatic", "bactoscan", "bactocount", "somascope",
        "d-count", "d‐count", "dcount", "somacount",
    ]),
    ("culture_media", [
        "simplate", "chromid", "precis", "petrifilm", "compact dry",
        "easy plate", "mc-media", "peelplate", "one plate", "rida count",
        "certablue", "agar", "plate count",
    ]),
    ("immunological_elisa", [
        "elisa", "vidas", "transia", "singlepath", "duopath", "rapidchek",
        "lateral flow", "immunoassay",
    ]),
    ("biochemical", [
        "api ", "vitek", "tempo", "soleris", "malthus", "bactometer",
    ]),
]

# Free-text evidence found inside the validation study itself. The project
# owner asked specifically for this: a report that describes a thermocycler
# is a molecular method whatever its product name suggests. Applied only
# when the name-based rules above found nothing, and only to a technology
# the evidence genuinely implies.
# ORDER IS SIGNIFICANT: the first category reaching two distinct hits wins.
# Every one of these reports describes a culture-based REFERENCE method, so
# agar and colony vocabulary is present in all of them -- which is why the
# culture entry is both last and deliberately narrow. Putting it earlier would
# reclassify every PCR method as culture media on the strength of the
# confirmation step it happens to describe.
_STUDY_TEXT_EVIDENCE = [
    # Most specific first. "growth inhibition" rather than bare "inhibition":
    # the EZ Check reports have a whole section called "PCR inhibition", and
    # bare "inhibition" would have handed them to this category.
    ("inhibition_assay", [
        "stearothermophilus", "growth inhibition", "inhibition de la croissance",
        "ph indicator", "indicateur de ph", "residus d'antibiotiques",
        "résidus d'antibiotiques", "antibiotic residues",
    ]),
    ("molecular_pcr", [
        "thermocycler", "thermal cycler", "cycleur thermique",
        "amplification", "dna extraction", "adn", "primer", "amorce",
        "real-time pcr", "rt-pcr", "qpcr", "nucleic acid",
        "isothermal amplification", "lysis buffer",
        # Added from the reports themselves. The rules missed the two EZ Check
        # certificates because the text spells out "polymerase chain reaction"
        # and "amplified" where the list only had "real-time pcr" and
        # "amplification" -- too literal, not too lax; the two-hit threshold
        # was never the problem.
        "polymerase chain reaction", "amplified",
        # Hybridisation without amplification (RiboFlow) is molecular too.
        "hybridis", "ribosomal rna", "rrna",
    ]),
    ("flow_cytometry", [
        "flow cytometr", "cytometrie en flux", "cytometry", "epifluorescence",
        "fluorescent labelling of cells", "optical counting",
    ]),
    ("immunological_elisa", [
        "elisa", "monoclonal antibod", "polyclonal antibod", "immunocapture",
        "immunoconcentration", "conjugate", "anticorps",
    ]),
    # Last and narrow, per the note above: only wording that describes the
    # ALTERNATIVE method's own culture principle, never the reference method's
    # plates. "colony count" and "agar" are deliberately absent -- they appear
    # in all 238 reports.
    ("culture_media", [
        "most probable number", "miniaturised", "miniaturized",
        "nombre le plus probable",
    ]),
]


def canonical_method_category(raw_category, commercial_name=None, study_text=None):
    """Best-known detection technology for a method.

    Precedence, strongest evidence first:
      1. an explicit category from the source (after folding chromogenic
         agar into culture media),
      2. the product family, when the brand's real principle is known,
      3. the validation study's own wording (thermocycler, antibody, ...).

    Returns None when nothing is known, so callers can report it instead of
    the data quietly claiming "other" as if that were a finding.
    """
    if raw_category and raw_category != "other":
        return _CATEGORY_ALIASES.get(raw_category, raw_category)

    name = (commercial_name or "").lower()
    if name:
        for category, keywords in _TECHNOLOGY_KEYWORDS:
            if any(k in name for k in keywords):
                return category

    text = (study_text or "").lower()
    if text:
        for category, keywords in _STUDY_TEXT_EVIDENCE:
            if sum(k in text for k in keywords) >= 2:
                # Two independent hits, not one: a single stray "amplification"
                # in a report's boilerplate is not evidence of a PCR method,
                # but a thermocycler AND a primer together is.
                return category

    # An explicit "other" from the source survives as "other" (a real
    # answer for antibiotic-residue inhibition tests); an absent one stays
    # None (genuinely unknown).
    return raw_category
