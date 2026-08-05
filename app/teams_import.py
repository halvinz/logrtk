"""
teams_import.py — Transformer un canal Teams collé en cas de dépannage.

Six mois d'échanges d'équipe contiennent des diagnostics que la base d'aide
n'a pas. Ce module part du texte **copié-collé** depuis Teams (aucune API,
aucun accès à demander : on ne lit que ce que l'on a déjà le droit de lire),
en tire les fils de discussion, ne garde que les échanges techniques, et
produit les triplets (symptôme, gamme, solution) de `base_aide.py`.

Trois principes :

1. **Anonymat.** Les noms d'auteurs servent à reconstituer qui répond à qui,
   puis sont jetés : la base d'aide décrit des pannes, pas des personnes.
2. **Prudence.** Un fil n'est retenu que s'il ressemble à un problème suivi
   d'une réponse. Le reste (logistique, blagues, « merci ») est écarté.
3. **Rien n'est écrit automatiquement dans la base.** La sortie est un CSV
   à relire : une solution fausse dans la base d'aide coûte plus cher qu'une
   solution absente.

Usage :
    python app/teams_import.py canal.txt --preview      # vérifier la lecture
    python app/teams_import.py canal.txt -o cas.csv
    python app/teams_import.py canal.txt --python       # tuples prêts à coller
"""

from __future__ import annotations

import argparse
import csv
import os
import re
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from assistant import detecter_famille          # noqa: E402
from logbible import normalize                  # noqa: E402

# --------------------------------------------------------------------------
# Lecture du texte collé
# --------------------------------------------------------------------------
# Teams colle sous des formes variables selon le client (web, bureau, mobile)
# et la langue. On accepte les plus courantes plutôt que d'en imposer une.
_DATES = (
    "%d/%m/%Y %H:%M", "%d/%m/%Y, %H:%M", "%d/%m/%y %H:%M",
    "%Y-%m-%d %H:%M", "%d %B %Y %H:%M",
)

# « Marie Dupont 12/03/2026 09:14 » ou « Marie Dupont  09:14 »
_RE_ENTETE = re.compile(
    r"^(?P<auteur>[^\d\n]{2,60}?)\s{1,}"
    r"(?P<date>(?:\d{1,2}[/-]\d{1,2}[/-]\d{2,4}[,]?\s+)?\d{1,2}[:h]\d{2})\s*$"
)
# Ligne d'auteur seule, la date sur la ligne suivante
_RE_AUTEUR_SEUL = re.compile(r"^(?P<auteur>[A-ZÀ-Ý][^\d\n]{1,59})$")
_RE_DATE_SEULE = re.compile(
    r"^(?:le\s+)?(?P<date>\d{1,2}[/-]\d{1,2}[/-]\d{2,4}[,]?\s+\d{1,2}[:h]\d{2}"
    r"|\d{1,2}[:h]\d{2})\s*$", re.I)

# Habillage de l'interface que le presse-papier emporte avec le texte
_BRUIT = re.compile(
    r"^(répondre|reply|réagir|aimer|j'aime|modifié|edited|traduire|translate|"
    r"\d+\s*(réponses?|replies?)|voir le fil|see more|afficher la suite|"
    r"nouvelle conversation|a réagi.*|réactions?\s*:?\s*\d*|"
    # ligne de réactions : uniquement des émojis/symboles et leur compteur
    r"[\U0001F300-\U0001FAFF←-⯿️\s\d]+)\s*$", re.I)


def _parse_date(brut: str):
    brut = brut.replace("h", ":").replace(",", "").strip()
    for fmt in _DATES:
        try:
            return datetime.strptime(brut, fmt)
        except ValueError:
            continue
    # heure seule : on garde l'ordre relatif, pas la date
    try:
        return datetime.strptime(brut, "%H:%M")
    except ValueError:
        return None


def parse_paste(texte: str) -> list:
    """[{auteur, date, texte}] à partir du canal collé.

    Tolérant par nécessité : le presse-papier de Teams n'a pas de format
    stable. Tout ce qui n'est reconnu ni comme en-tête ni comme habillage
    est rattaché au message en cours — mieux vaut un message trop long
    qu'une réponse perdue.
    """
    messages = []
    courant = None
    lignes = texte.replace("\r\n", "\n").split("\n")
    i = 0
    while i < len(lignes):
        ligne = lignes[i].strip()
        i += 1
        if not ligne or _BRUIT.match(ligne):
            continue

        entete = _RE_ENTETE.match(ligne)
        auteur = date = None
        if entete:
            auteur = entete.group("auteur").strip()
            date = _parse_date(entete.group("date"))
        else:
            # auteur sur une ligne, date sur la suivante
            seul = _RE_AUTEUR_SEUL.match(ligne)
            if seul and i < len(lignes):
                suite = _RE_DATE_SEULE.match(lignes[i].strip())
                if suite:
                    auteur = seul.group("auteur").strip()
                    date = _parse_date(suite.group("date"))
                    i += 1

        if auteur:
            if courant and courant["texte"].strip():
                messages.append(courant)
            courant = {"auteur": auteur, "date": date, "texte": ""}
        elif courant is not None:
            courant["texte"] += ligne + " "
        # avant le premier en-tête reconnu : on ignore

    if courant and courant["texte"].strip():
        messages.append(courant)
    for m in messages:
        m["texte"] = re.sub(r"\s+", " ", m["texte"]).strip()
    return messages


# --------------------------------------------------------------------------
# Regroupement en fils
# --------------------------------------------------------------------------
# Un canal collé perd l'indentation des réponses : on reconstitue les fils
# par proximité. Deux messages séparés de plus de 3 h traitent rarement du
# même incident.
SEUIL_FIL_MINUTES = 180


def group_threads(messages: list) -> list:
    """[[messages]] — un fil par échange présumé."""
    fils = []
    courant = []
    for m in messages:
        if not courant:
            courant = [m]
            continue
        prec = courant[-1]
        ecart = None
        if m["date"] and prec["date"]:
            ecart = abs((m["date"] - prec["date"]).total_seconds()) / 60
        # Sans date exploitable, on s'en remet au changement d'auteur seul :
        # deux messages consécutifs du même auteur restent un même propos.
        if ecart is not None and ecart > SEUIL_FIL_MINUTES:
            fils.append(courant)
            courant = [m]
        else:
            courant.append(m)
    if courant:
        fils.append(courant)
    return fils


# --------------------------------------------------------------------------
# Sélection des échanges techniques
# --------------------------------------------------------------------------
_TECHNIQUE = re.compile(
    r"\bkr\s?\d{3}|\be(?:rr(?:eur|or))?\s?\d{1,2}\b|\bf1\b|\brtk\b|\bslam\b|"
    r"\bstation\b|\bbatterie\b|\bfirmware\b|\bcarte m[eè]re\b|\bantenne\b|"
    r"\bp[eé]rim[eé]trique\b|\blame\b|\broue\b|\bmapping\b|\bbackend\b|"
    r"\bpare[- ]chocs?\b|\bcapteur\b|\bmoteur\b|\bcharge\b|\btonte\b|\btondeuse\b",
    re.I)

# Un symptôme s'énonce rarement sous forme de question : dans la base d'aide
# existante, la plupart sont déclaratifs (« L'écran n'affiche plus rien »).
# On reconnaît donc aussi les négations et les tournures d'anomalie.
_QUESTION = re.compile(
    r"\?|\bprobl[eè]me\b|\bpanne\b|\berreur\b|\bbloqu[eé]|\bcomment\b|"
    r"\bpourquoi\b|\bqui a d[eé]j[aà]\b|\bhs\b|\bd[eé]faut\b|"
    # « ne ... plus/pas », y compris avec pronom élidé (« ne s'allume pas »)
    r"\bne\s+[\w'’]+(?:\s+[\w'’]+)?\s+(?:plus|pas|rien)\b|"
    r"\bn'(?:arrive|affiche|allume|a plus|est plus)\b|"
    r"\bimpossible\b|\brefuse de\b|\bn'y arrive pas\b|"
    r"\baffiche\s+(?:une\s+)?(?:erreur|e\s?\d|f1)\b|"
    r"\btourne en rond\b|\bs'arr[eê]te\b|\bse coupe\b|\bsurchauff|"
    r"\ben boucle\b|\bne veut pas\b|\bplante\b", re.I)

_REPONSE = re.compile(
    r"\bv[eé]rifi|\bremplac|\bchange|\bnettoy|\bcontr[oô]l|\bfaut\b|\bil faut\b|"
    r"\bessaie|\bessaye|\bsolution\b|\bc'est\b|\bcause\b|\bmets?\b|\bmettre\b|"
    r"\bmaj\b|\bmise [aà] jour\b|\bd[eé]saparie|\breinitialis|\br[eé]initialis",
    re.I)

_POLITESSE = re.compile(
    r"^(merci|ok|super|parfait|bonjour|bonsoir|salut|d'accord|nickel|top|"
    r"bien re[cç]u|👍|\W)+$", re.I)


def _est_technique(texte: str) -> bool:
    return bool(_TECHNIQUE.search(texte)) and len(texte) > 25


def extract_cases(fils: list) -> list:
    """[(symptôme, gamme, solution, contexte)] — candidats à la base d'aide.

    Un cas retenu = un message qui expose un problème technique, suivi d'au
    moins une réponse d'une autre personne qui ressemble à une action.
    """
    cas = []
    for fil in fils:
        # le problème : premier message technique qui pose une question
        idx_pb = None
        for i, m in enumerate(fil):
            if _est_technique(m["texte"]) and _QUESTION.search(m["texte"]):
                idx_pb = i
                break
        if idx_pb is None:
            continue
        probleme = fil[idx_pb]

        reponses = []
        for m in fil[idx_pb + 1:]:
            if m["auteur"] == probleme["auteur"]:
                continue                      # l'auteur qui se relance
            if _POLITESSE.match(m["texte"]) or len(m["texte"]) < 15:
                continue
            if _REPONSE.search(m["texte"]) or _est_technique(m["texte"]):
                reponses.append(m["texte"])
        if not reponses:
            continue

        famille, modele = detecter_famille(probleme["texte"] + " " + " ".join(reponses))
        cas.append({
            "symptome": _resumer(probleme["texte"]),
            "gamme": famille,
            "solution": " ".join(reponses)[:1500],
            "modele": modele,
            "date": probleme["date"].strftime("%d/%m/%Y") if probleme["date"] else "",
        })
    return cas


# Politesses et tournures d'accroche : la base d'aide veut « Le robot affiche
# E4 », pas « Salut, j'ai un client qui me dit que le robot affiche E4 ».
_ACCROCHE = re.compile(
    r"^(?:salut|bonjour|bonsoir|hello|coucou|hey|les gars|team)\s*[,!.]*\s*"
    r"|^(?:j'ai|jai)\s+(?:un|une)\s+(?:souci|probl[eè]me|cas)\s*[:,]?\s*"
    r"|^(?:quelqu'un|qqun)\s+(?:a[- ]t[- ]il|a)\s+d[eé]j[aà]\s+vu\s*[:,]?\s*"
    r"|^(?:petite question|question)\s*[:,]?\s*", re.I)


def _resumer(texte: str, limite: int = 160) -> str:
    """Première phrase utile, tronquée : le symptôme sert de clé de recherche."""
    texte = texte.strip()
    precedent = None
    while precedent != texte:                 # « Salut, j'ai un souci : ... »
        precedent = texte
        texte = _ACCROCHE.sub("", texte).strip()
    coupe = re.split(r"(?<=[.!?])\s+", texte)[0]
    if len(coupe) < 20 and len(texte) > len(coupe):
        coupe = texte
    coupe = coupe.strip(" ,;:-–")
    return (coupe[:1].upper() + coupe[1:])[:limite].strip()


# --------------------------------------------------------------------------
# Dédoublonnage contre la base existante
# --------------------------------------------------------------------------
def _cas_existants() -> list:
    try:
        from base_aide import CAS
    except Exception:                          # noqa: BLE001
        return []
    return [normalize(c[0]) for c in CAS]


def dedupe(cas: list) -> tuple:
    """(nouveaux, ignorés) — un symptôme déjà décrit n'est pas réimporté."""
    def mots_utiles(phrase: str) -> set:
        return {m.strip(".,;:!?") for m in phrase.split() if len(m) > 3}

    connus = [mots_utiles(k) for k in _cas_existants()]
    nouveaux, ignores = [], []
    vus = []
    for c in cas:
        clef = normalize(c["symptome"])
        mots = mots_utiles(clef)

        def proche(autre: set) -> bool:
            """Recouvrement rapporté au plus court des deux libellés : sans
            cela « Le robot ne s'allume pas », déjà présent en 4 mots, ne
            serait jamais reconnu dans une phrase de Teams plus bavarde."""
            if not mots or not autre:
                return False
            commun = len(mots & autre)
            return commun >= 2 and commun / min(len(mots), len(autre)) >= 0.6

        doublon = any(proche(k) for k in connus) or any(proche(v) for v in vus)
        if doublon:
            ignores.append(c)
        else:
            vus.append(mots)
            nouveaux.append(c)
    return nouveaux, ignores


# --------------------------------------------------------------------------
# Sorties
# --------------------------------------------------------------------------
def write_csv(cas: list, chemin: str) -> None:
    with open(chemin, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["symptome", "gamme", "solution", "modele_cite", "date"])
        for c in cas:
            w.writerow([c["symptome"], c["gamme"], c["solution"], c["modele"], c["date"]])


def to_python(cas: list) -> str:
    """Tuples au format de `base_aide.CAS`, prêts à coller après relecture."""
    out = []
    for c in cas:
        s = c["symptome"].replace("'", "\\'")
        sol = c["solution"].replace("'", "\\'")
        out.append(f"    ('{s}', '{c['gamme']}',\n     '{sol}'),")
    return "\n".join(out)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Transforme un canal Teams copié-collé en cas de dépannage.")
    ap.add_argument("fichier", help="fichier texte contenant le canal collé")
    ap.add_argument("-o", "--sortie", help="CSV de sortie")
    ap.add_argument("--python", action="store_true",
                    help="affiche les tuples au format base_aide.CAS")
    ap.add_argument("--preview", action="store_true",
                    help="montre ce qui a été lu, sans rien produire")
    ap.add_argument("--tout", action="store_true",
                    help="n'écarte pas les cas déjà présents dans base_aide")
    args = ap.parse_args(argv)
    # La console Windows est en cp1252 : sans cela, une flèche ou un accent
    # suffit à faire échouer l'affichage.
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    try:
        with open(args.fichier, "r", encoding="utf-8", errors="ignore") as f:
            texte = f.read()
    except OSError as e:
        print(f"Lecture impossible : {e}", file=sys.stderr)
        return 1

    messages = parse_paste(texte)
    fils = group_threads(messages)
    cas = extract_cases(fils)
    nouveaux, ignores = (cas, []) if args.tout else dedupe(cas)

    if args.preview:
        print(f"Messages reconnus : {len(messages)}")
        print(f"Fils reconstitués : {len(fils)}")
        print(f"Cas candidats     : {len(cas)}  "
              f"(nouveaux {len(nouveaux)}, déjà connus {len(ignores)})")
        print()
        if not messages:
            print("Aucun message reconnu — le format collé diffère de ceux "
                  "attendus. Collez-moi une dizaine de lignes pour caler la "
                  "lecture.")
            return 1
        print("--- 3 premiers messages lus ---")
        for m in messages[:3]:
            d = m["date"].strftime("%d/%m %H:%M") if m["date"] else "?"
            print(f"[{d}] {m['auteur']} : {m['texte'][:120]}")
        print()
        print("--- 3 premiers cas extraits ---")
        for c in nouveaux[:3]:
            print(f"• {c['symptome']}")
            print(f"  gamme : {c['gamme'] or '(indéterminée)'}")
            print(f"  → {c['solution'][:160]}")
        return 0

    if args.python:
        print(to_python(nouveaux))
        return 0

    sortie = args.sortie or "cas_teams.csv"
    write_csv(nouveaux, sortie)
    print(f"{len(nouveaux)} cas écrits dans {sortie} "
          f"({len(ignores)} déjà connus, écartés ; {len(messages)} messages lus)")
    print("À relire avant de les verser dans base_aide.py.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
