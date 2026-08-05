"""
digest.py — Condensé d'un journal de tondeuse, à coller dans un LLM.

Un export de robot pèse des dizaines de milliers de lignes, dont l'immense
majorité est de la répétition. Aucun modèle n'analyse correctement ça : il
faut lui donner le signal, pas le bruit. Ce module produit un condensé
Markdown de quelques pages qui tient dans n'importe quelle conversation.

Ce qu'il retient, dans l'ordre d'utilité :

1. l'identité du robot et ce qu'il a fait (résumé déjà produit par `summary`) ;
2. les incidents **regroupés en épisodes** — un message répété mille fois en
   une minute est un incident, pas mille ;
3. les erreurs **que la Log bible ne reconnaît pas** : le logiciel sait déjà
   conclure sur les autres, c'est ici que l'analyse d'un modèle apporte
   quelque chose ;
4. un échantillon de lignes brutes par incident, comme pièces à conviction.

Aucune dépendance hors bibliothèque standard : le module tourne tel quel sur
un poste sans numpy ni PySide6.

Usage :
    python app/digest.py "C:\\...\\LOGTOOL"
    python app/digest.py "C:\\...\\export.zip" -o condense.md --max-chars 20000
"""

from __future__ import annotations

import argparse
import os
import sys
from collections import Counter
from datetime import timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from logbible import analyze_line, analyze_rtk2_line          # noqa: E402
from parser import (load_session_from_archive,                # noqa: E402
                    load_session_from_folder, load_session_from_html,
                    load_session)
from states import extract_state, state_timeline, MIN_VALID_YEAR  # noqa: E402
from summary import describe                                   # noqa: E402
from wired import (analyze_wired_line, looks_like_wired,       # noqa: E402
                   state_timeline as wired_states)

# Fenêtre de regroupement des occurrences d'un même message (comme l'onglet
# Diagnostic du logiciel : GROUP_SECONDS dans gui.py).
GROUP_SECONDS = 120

# Combien de lignes brutes joindre par incident : assez pour que le modèle
# voie la forme exacte du message, pas assez pour noyer le condensé.
SAMPLES_PER_EVENT = 2


# --------------------------------------------------------------------------
# Chargement
# --------------------------------------------------------------------------
def load_any(path: str):
    """Charge un export quel que soit son emballage (dossier, archive, HTML)."""
    if os.path.isdir(path):
        return load_session_from_folder(path)
    if path.lower().endswith((".zip", ".tar", ".gz", ".tgz")):
        return load_session_from_archive(path)
    if looks_like_wired(path) or path.lower().endswith((".html", ".htm")):
        return load_session_from_html(path)
    return load_session(path=path)


# --------------------------------------------------------------------------
# Incidents
# --------------------------------------------------------------------------
def build_events(session) -> tuple:
    """(events, échantillons) — même logique de regroupement que l'onglet
    Diagnostic du logiciel.

    `events` garde la forme attendue par `summary.describe` :
    [debut, fin, diag, nombre, brut]. Les lignes d'exemple voyagent à côté,
    dans une liste alignée sur les index, pour ne pas altérer ce contrat.
    """
    burst = timedelta(seconds=GROUP_SECONDS)
    events = []
    samples = []
    state = None
    for l in session.lines:
        if l.ts is None or l.ts.year < MIN_VALID_YEAR:
            continue
        # On passe l'état PRÉCÉDENT : sur une ligne de changement d'état,
        # c'est lui qui dit d'où vient le robot (Retour → Erreur).
        found = extract_state(l.raw)
        precedent = state
        if found:
            state = found

        if session.fmt == "wired":
            diag = analyze_wired_line(l.level, l.text)
        else:
            diag = analyze_line(l.raw, precedent)
        if diag is None and session.fmt == "rtk2":
            diag = analyze_rtk2_line(l.level, l.tag, l.text)
        if diag is None:
            continue

        if events and events[-1][2]["meaning"] == diag["meaning"] \
                and (l.ts - events[-1][1]) <= burst:
            events[-1][1] = l.ts
            events[-1][3] += 1
        else:
            events.append([l.ts, l.ts, diag, 1, l.raw])
            samples.append([])
        if len(samples[-1]) < SAMPLES_PER_EVENT:
            samples[-1].append(l.raw.strip()[:300])
    return events, samples


def group_by_meaning(events, samples) -> list:
    """Un incident par libellé : épisodes, occurrences, première/dernière vue.

    C'est la vue qui compte pour un diagnostic : « 43 épisodes de patinage
    répartis sur 3 jours » dit ce que « 12 000 lignes » ne dit pas.

    Les libellés qui ne diffèrent que par un nombre sont regroupés — cinq
    lignes « région n° 1..5 » ou « 931 tr/min », « 1581 tr/min » décrivent un
    seul phénomène et rempliraient sinon tout le tableau. Le libellé affiché
    reste celui de la première occurrence, avec le nombre de variantes.
    """
    import re

    par_libelle = {}
    for (debut, fin, diag, count, _raw), ech in zip(events, samples):
        clef = re.sub(r"\d+", "#", diag["meaning"])
        e = par_libelle.get(clef)
        if e is None:
            par_libelle[clef] = {
                "meaning": diag["meaning"], "category": diag["category"],
                "severity": diag["severity"], "conclusion": diag.get("conclusion", ""),
                "episodes": 1, "occurrences": count, "variantes": {diag["meaning"]},
                "premier": debut, "dernier": fin, "samples": list(ech),
            }
        else:
            e["episodes"] += 1
            e["occurrences"] += count
            e["variantes"].add(diag["meaning"])
            e["premier"] = min(e["premier"], debut)
            e["dernier"] = max(e["dernier"], fin)
            if len(e["samples"]) < SAMPLES_PER_EVENT:
                e["samples"].extend(ech[:SAMPLES_PER_EVENT - len(e["samples"])])

    ordre = {"error": 0, "warn": 1, "info": 2, "ok": 3}
    return sorted(par_libelle.values(),
                  key=lambda e: (ordre.get(e["severity"], 9), -e["episodes"]))


def unknown_errors(session, limit=15) -> list:
    """Lignes graves que la Log bible ne reconnaît pas.

    Le logiciel sait déjà conclure sur les incidents catalogués ; ce sont
    ceux-là, les inconnus, qu'il vaut la peine de soumettre à un modèle.
    Les lignes sont normalisées (nombres remplacés par #) avant comptage,
    sinon un même message paraît unique à chaque occurrence.
    """
    import re

    compteur = Counter()
    exemple = {}
    niveau_vu = {}
    for l in session.lines:
        # Après certaines coupures le RTC du robot repart en 2017 : ces dates
        # fausseraient la colonne « première vue ».
        if l.ts is None or l.ts.year < MIN_VALID_YEAR:
            continue
        grave = (l.level or "").upper().startswith(("E", "W", "F")) or \
            any(mot in l.text.lower() for mot in ("error", "fail", "fault",
                                                  "timeout", "exception"))
        if not grave:
            continue
        if session.fmt == "wired":
            connu = analyze_wired_line(l.level, l.text)
        else:
            connu = analyze_line(l.raw, None) or (
                analyze_rtk2_line(l.level, l.tag, l.text)
                if session.fmt == "rtk2" else None)
        if connu is not None:
            continue
        motif = re.sub(r"\d+", "#", l.text.strip())[:200]
        if not motif:
            continue
        compteur[motif] += 1
        exemple.setdefault(motif, (l.ts, l.raw.strip()[:300]))
        # Le niveau le plus grave vu pour ce motif : le firmware étiquette
        # parfois en ERROR des messages anodins, un FATAL mérite d'être vu
        # d'abord même s'il est cent fois moins fréquent.
        rang = {"F": 0, "E": 1, "A": 2, "W": 3}
        lv = (l.level or "?").upper()[:1]
        if rang.get(lv, 8) < rang.get(niveau_vu.get(motif, "?"), 9):
            niveau_vu[motif] = lv

    # Tri par fréquence, pas par niveau : ce firmware étiquette « FATAL » de
    # la plomberie USB et « ERROR » des messages de routine. Le volume dit
    # mieux ce qui sature réellement le journal ; le niveau reste affiché
    # pour que le lecteur en tienne compte.
    return [{"motif": m, "count": n, "premier": exemple[m][0],
             "brut": exemple[m][1], "niveau": niveau_vu.get(m, "?")}
            for m, n in compteur.most_common(limit)]


def file_stats(session) -> list:
    """Volume par fichier source et par gravité : donne au modèle l'échelle
    du journal, et signale un fichier anormalement bavard."""
    par_source = Counter(l.source for l in session.lines)
    par_niveau = Counter((l.level or "?").upper()[:1] for l in session.lines)
    return [par_source.most_common(8), par_niveau.most_common()]


# --------------------------------------------------------------------------
# Rendu
# --------------------------------------------------------------------------
def _ts(d, avec_annee: bool = False) -> str:
    """Horodatage court. L'année n'est affichée que si le journal en couvre
    plusieurs : sans elle, « 01/05 » puis « 08/04 » donnent l'impression
    d'une fin antérieure au début alors que deux années les séparent."""
    if not d:
        return "?"
    return d.strftime("%d/%m/%Y %H:%M:%S" if avec_annee else "%d/%m %H:%M:%S")


def render(session, path: str, max_chars: int = 15000) -> str:
    """Condensé Markdown, tronqué proprement au budget demandé."""
    events, samples = build_events(session)
    incidents = group_by_meaning(events, samples)
    states = (wired_states if session.fmt == "wired" else state_timeline)(session.lines)
    lignes_resume, conclusion, gravite = describe(session, events, states)
    inconnus = unknown_errors(session)
    sources, niveaux = file_stats(session)

    # Un export peut couvrir plusieurs années (robot rangé l'hiver, journal
    # jamais purgé) : dans ce cas toutes les dates portent leur année.
    annees = {e["premier"].year for e in incidents} | {e["dernier"].year for e in incidents}
    annees |= {u["premier"].year for u in inconnus}
    annees |= {ts.year for ts, _s, _sub in states}
    multi = len(annees) > 1

    def ts(d):
        return _ts(d, multi)

    o = []
    a = o.append

    a("# Condensé de journal — tondeuse robot")
    a("")
    a("Extrait automatiquement d'un export de tondeuse Positec/Kress par "
      "RobotLogViewer. Les incidents sont **regroupés en épisodes** : un "
      "message répété en rafale compte pour un épisode, avec son nombre "
      "d'occurrences entre parenthèses.")
    a("")

    # --- Identité
    a("## Robot")
    a("")
    ident = [("Modèle", session.model), ("N° de série", session.serial),
             ("Firmware", session.firmware), ("Export", session.export_date),
             ("Format de log", session.fmt),
             ("Temps de travail total", session.total_work_time),
             ("Distance totale", session.total_distance),
             ("Temps de lame", session.blade_running_time),
             ("Recharges", session.battery_recharged),
             ("Longueur de bordure", session.boundary_length)]
    for k, v in ident:
        if v:
            a(f"- **{k}** : {v}")
    a(f"- **Fichier analysé** : {os.path.basename(path)}")
    a(f"- **Lignes de journal** : {len(session.lines)} · positions "
      f"enregistrées : {len(session.track)}")
    if session.schedule:
        a(f"- **Planning** : {' | '.join(session.schedule[:7])}")
    a("")

    # --- Comportement + conclusion locale
    a("## Ce qu'a fait le robot")
    a("")
    for l in lignes_resume:
        a(f"- {l}")
    a("")
    a(f"**Conclusion du logiciel ({gravite})** : {conclusion}")
    a("")

    # --- Incidents catalogués
    a("## Incidents identifiés")
    a("")
    if incidents:
        a("| Gravité | Catégorie | Incident | Épisodes | Occurrences | Première | Dernière |")
        a("|---|---|---|---|---|---|---|")
        for e in incidents[:25]:
            libelle = e["meaning"].replace("|", "\\|")
            autres = len(e["variantes"]) - 1
            if autres:
                libelle += f" _(+{autres} variante{'s' if autres > 1 else ''})_"
            a(f"| {e['severity']} | {e['category']} | {libelle} | "
              f"{e['episodes']} | {e['occurrences']} | {ts(e['premier'])} | "
              f"{ts(e['dernier'])} |")
        a("")
        a("### Lignes brutes correspondantes")
        a("")
        for e in incidents[:8]:
            a(f"**{e['meaning']}** ({e['category']}, {e['episodes']} épisodes)")
            if e["conclusion"]:
                a(f"> Piste connue : {e['conclusion']}")
            a("```")
            for s in e["samples"][:SAMPLES_PER_EVENT]:
                a(s)
            a("```")
            a("")
    else:
        a("_Aucun incident catalogué sur cette période._")
        a("")

    # --- Inconnus : le vrai apport d'une analyse externe
    a("## Erreurs non reconnues par la base de diagnostic")
    a("")
    a("Ces messages sont graves d'après le robot mais ne correspondent à "
      "aucune règle connue du logiciel. **C'est ici qu'une analyse externe "
      "est utile.** Les nombres sont remplacés par `#` pour regrouper les "
      "variantes d'un même message.")
    a("")
    if inconnus:
        a("Classées par volume. Niveaux tels que le firmware les inscrit "
          "(F = fatal, E = error, A = alert, W = warning) : **peu fiables** — "
          "on trouve des `FATAL` sur de la plomberie USB et des `ERROR` sur "
          "des messages de routine. À pondérer par le libellé, pas par le "
          "niveau seul.")
        a("")
        a("| Niveau | Occurrences | Première | Message (normalisé) |")
        a("|---|---|---|---|")
        for u in inconnus:
            motif = u["motif"].replace("|", "\\|")
            a(f"| {u['niveau']} | {u['count']} | {ts(u['premier'])} | `{motif}` |")
        a("")
        a("### Exemples bruts")
        a("")
        a("```")
        for u in inconnus[:6]:
            a(u["brut"])
        a("```")
        a("")
    else:
        a("_Aucune : toutes les erreurs du journal sont déjà cataloguées._")
        a("")

    # --- Chronologie
    a("## Chronologie des états")
    a("")
    if states:
        a("```")
        precedent = None
        montres = 0
        for ts_etat, st, _sub in states:
            if st == precedent:
                continue
            a(f"{ts(ts_etat)}  {st}")
            precedent = st
            montres += 1
            if montres >= 40:
                a(f"... ({len(states)} changements d'état au total)")
                break
        a("```")
    else:
        a("_Aucun changement d'état lisible._")
    a("")

    # --- Volumétrie
    a("## Volumétrie")
    a("")
    a("- Par fichier : " + ", ".join(f"{s} ({n})" for s, n in sources))
    a("- Par gravité : " + ", ".join(f"{lv} ({n})" for lv, n in niveaux))
    a("")

    a("---")
    a("")
    a("**Question posée au modèle** : à partir de ces éléments, quelle est "
      "la cause la plus probable du comportement du robot, et quelles "
      "vérifications faire en priorité sur le terrain ? Signale en "
      "particulier ce que les erreurs non reconnues ci-dessus peuvent "
      "indiquer.")

    texte = "\n".join(o)
    if len(texte) > max_chars:
        coupe = texte[:max_chars].rsplit("\n", 1)[0]
        texte = coupe + f"\n\n_[condensé tronqué à {max_chars} caractères — " \
                        f"relancer avec --max-chars pour la version complète]_"
    return texte


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Condense un export de tondeuse en un résumé Markdown "
                    "à coller dans un LLM.")
    ap.add_argument("chemin", help="dossier d'export, archive .zip/.tar, "
                                   "page HTML filaire ou fichier .log")
    ap.add_argument("-o", "--sortie", help="fichier de sortie (défaut : stdout)")
    ap.add_argument("--max-chars", type=int, default=15000,
                    help="budget de caractères (défaut 15000)")
    args = ap.parse_args(argv)

    if not os.path.exists(args.chemin):
        print(f"Chemin introuvable : {args.chemin}", file=sys.stderr)
        return 2

    try:
        session = load_any(args.chemin)
    except Exception as e:                     # noqa: BLE001
        print(f"Lecture impossible : {e}", file=sys.stderr)
        return 1

    if not session.lines:
        print("Aucune ligne de journal lisible dans cet export.", file=sys.stderr)
        return 1

    texte = render(session, args.chemin, args.max_chars)

    if args.sortie:
        with open(args.sortie, "w", encoding="utf-8") as f:
            f.write(texte)
        print(f"Condensé écrit dans {args.sortie} "
              f"({len(texte)} caractères, {len(session.lines)} lignes analysées)")
    else:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        print(texte)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
