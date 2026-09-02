"""Classification rules checked against the reports' own words.

The excerpts below are copied verbatim from run 33620688656 of
diagnose_unclassified_technology.yml, which printed what each of the six
unclassified reports says about its own detection principle. They are the
evidence the rules in taxonomy.py were written from, so keeping them here
turns "these six classify correctly" into something a later change has to
keep true rather than something that was true once on a Wednesday.

Two of these were traps worth preserving:

  * RiboFlow is named "...Flow" and detects rRNA, so both a name-based guess
    and a careless keyword list land on flow cytometry. The report says
    hybridisation on a lateral flow strip, explicitly WITHOUT amplification.
  * The EZ Check reports contain a section titled "PCR inhibition", which a
    bare "inhibition" keyword would read as an antibiotic-residue assay.

Run: python3 tests/test_technology_rules.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "pipeline"))
from taxonomy import CATEGORY_LABELS, canonical_method_category  # noqa: E402

# (certificate, commercial name, report excerpt, expected category)
CASES = [
    (
        "DSM 28/02-02/12", "Delvotest® T",
        "2.1. Principle of the method\n"
        "milk. The test is based on growth inhibition of Geobacillus stearothermophilus.\n"
        "The product contains a solid agar medium seeded with standardized number of spores of Geobacillus\n"
        "stearothermophilus with required nutrients for growth. The medium is colored by the pH indicator\n"
        "Milk samples are added into the test and are incubated at 64°C ± 2°C.\n"
        "Color of medium Results",
        "inhibition_assay",
    ),
    (
        "RBP 31/02-04/11", "Premi®Test",
        "1.1. Principe de la méthode alternative\n"
        "de viande). Le Premi®Test est basé sur l’inhibition de la croissance du Bacillus stearothermophilus\n"
        "var.calidolactis, bactérie très sensible à de nombreux antibiotiques et aux sulfamides. Des spores\n"
        "gélose au sein de laquelle se trouvent les spores de Bacillus stearothermophilus.\n"
        "les spores germent et se développent, entraînant l’acidification du milieu et un changement de couleur.\n"
        "d’antibiotiques, les spores ne se développent pas, elles sont inhibées par l’antibiotique",
        "inhibition_assay",
    ),
    (
        "2015LR53", "RiboFlow Listeria Twin Detection Kit",
        "ALOA Agar Listeria Ottavani & Agosti\nBHI Brain Heart Infusion broth\n"
        "CFU Colony Forming Units\nFB Fraser Broth\nNAg Nutrient Agar\nPlcm Palcam agar\n"
        "In short, with the alternative method, a Listeria monocytogenes-specific ribosomal RNA sequence and\n"
        "another rRNA sequence specific for all Listeria species are detected by a proprietary nucleic acid\n"
        "hybridisation protocol in a simple lateral flow assay format, using a crude cell extract from a (two-step)\n"
        "enriched culture. Tedious nucleic acid purification or enzymatic amplification of target sequence is not",
        "molecular_pcr",
    ),
    (
        "2015LR60", "AMP-6000 TMAC",
        "enumeration of microorganisms Part 1: Colony count at 30 degrees C by the pour plate technique\n"
        "- CFU Colony Forming Units\n- PCA Plate Count Agar\n"
        "The alternative method principle is based on on a miniaturised automated most probable number method.\n"
        "The AMP-6000 TMAC analysis system is a platform for determining the mesophilic aerobic colony count by\n"
        "mesophilic aerobic organisms are grown in a non-selective TMAC medium.\n"
        "The microtitre plates for the alternative method were incubated for the minimum time of 44 hours.",
        "culture_media",
    ),
    (
        "2025LR135", "EZ Check Listeria spp",
        "2.2 Alternative Method 6\nPrinciple 6\nPCR inhibition 17\nEnrichment broth storage 17\n"
        "Listeria and RAPID’ L. mono agar as well as ALOA.\n"
        "2. Incubation of agars at 37 ± 1°C for 24 ± 2 hours.\n"
        "utilizing both lysis procedures, on all PCR platforms, and with and without Free DNA\n"
        "all other instruments; therefore, this thermocycler was not evaluated in this validation\n"
        "detection of specific DNA sequences unique to Listeria spp. found in environmental\n"
        "samples and food products. Using real-time polymerase chain reaction (PCR), Listeria",
        "molecular_pcr",
    ),
    (
        "2025LR136", "EZ Check Listeria monocytogenes",
        "2.2 Alternative Method 6\nPrinciple 6\nPCR inhibition 16\n"
        "Listeria Special Broth II (LSB II) as the primary enrichment medium in an unpaired study\n"
        "2. Incubation of agars at 37 ± 1°C for 24 ± 2 hours.\n"
        "all other instruments; therefore, this thermocycler was not evaluated in this validation\n"
        "the detection of specific DNA sequences unique to Listeria monocytogenes found in\n"
        "(PCR), Listeria monocytogenes-specific DNA sequences are amplified and detected\n"
        "simultaneously by means of fluorescent probes. With the use of the iQ-Check Prep",
        "molecular_pcr",
    ),
]

# A source that states its own category must always beat the report text.
PRECEDENCE_CASES = [
    ("explicit category wins over text", "immunological_elisa", "Some Kit",
     "based on growth inhibition of Geobacillus stearothermophilus with spores",
     "immunological_elisa"),
    ("chromogenic still folds into culture media", "chromogenic_agar", "Some Agar", "",
     "culture_media"),
]

# Text that must NOT be enough to classify: one stray hit, or a reference
# method's culture vocabulary on its own.
NEGATIVE_CASES = [
    ("a lone thermocycler mention", "Mystery Kit",
     "The confirmation was performed on a thermocycler in the expert laboratory."),
    ("reference-method plates only", "Mystery Kit",
     "Colony count at 30 degrees C by the pour plate technique on Plate Count Agar, "
     "incubated 69 hours, CFU counted on agar."),
    ("PCR inhibition heading alone", "Mystery Kit",
     "5.4 PCR inhibition ... 17"),
]


def main() -> int:
    failures = []

    for cert, name, text, expected in CASES:
        got = canonical_method_category(None, name, text)
        ok = got == expected
        print(f"{'PASS' if ok else 'FAIL'}  {cert:<18} {name[:34]:<36} -> {got}")
        if not ok:
            failures.append(f"{cert}: expected {expected}, got {got}")

    for label, raw, name, text, expected in PRECEDENCE_CASES:
        got = canonical_method_category(raw, name, text)
        ok = got == expected
        print(f"{'PASS' if ok else 'FAIL'}  precedence: {label} -> {got}")
        if not ok:
            failures.append(f"{label}: expected {expected}, got {got}")

    for label, name, text in NEGATIVE_CASES:
        got = canonical_method_category(None, name, text)
        ok = got in (None, "other")
        print(f"{'PASS' if ok else 'FAIL'}  stays unclassified: {label} -> {got}")
        if not ok:
            failures.append(f"{label}: should not classify, got {got}")

    for _, _, _, expected in CASES:
        if expected not in CATEGORY_LABELS:
            failures.append(f"{expected} has no entry in CATEGORY_LABELS")

    print()
    if failures:
        print(f"=== {len(failures)} FAILURE(S) ===")
        for f in failures:
            print("  -", f)
        return 1
    print(f"=== all {len(CASES) + len(PRECEDENCE_CASES) + len(NEGATIVE_CASES)} checks passed ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
