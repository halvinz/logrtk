"""
summary.py — Ce qu'a fait le robot, et ce qui cloche.

L'onglet Diagnostic liste les incidents un par un ; encore faut-il en tirer
une lecture d'ensemble. Ce module résume le comportement (tonte, charges,
extinctions, erreurs) puis propose une conclusion, en remontant du symptôme
le plus fréquent vers sa cause probable.
"""

from __future__ import annotations

import re
from datetime import timedelta

from logbible import FREQUENCES


def _plural(n, singulier, pluriel=None):
    return f"{n} {singulier if n <= 1 else (pluriel or singulier + 's')}"


def _duree(delta: timedelta) -> str:
    total = int(delta.total_seconds())
    if total < 60:
        return f"{total} s"
    heures, reste = divmod(total, 3600)
    minutes = reste // 60
    if heures:
        return f"{heures} h {minutes:02d}"
    return f"{minutes} min"


def behaviour(states) -> dict:
    """Compte ce que le robot a fait, à partir de sa machine à états."""
    out = {"tonte": timedelta(0), "sessions": 0, "charges": 0,
           "extinctions": 0, "erreurs": 0, "sorties": 0, "debut": None,
           "fin": None}
    debut_tonte = None
    for ts, state, _sub in states:
        if out["debut"] is None:
            out["debut"] = ts
        out["fin"] = ts
        if state.startswith("start"):
            if debut_tonte is None:
                debut_tonte = ts
                out["sessions"] += 1
        else:
            if debut_tonte is not None:
                out["tonte"] += ts - debut_tonte
                debut_tonte = None
            if state.startswith("charge"):
                out["charges"] += 1
            elif state.startswith("prepoweroff"):
                out["extinctions"] += 1
            elif state.startswith("error"):
                out["erreurs"] += 1
            elif state.startswith(("prestart", "leavebase")):
                out["sorties"] += 1
    return out


_RE_FREQ = re.compile(r"freq:\[(\d+)\]")


def _frequence(session) -> str:
    """Fréquence de tonte réglée dans les préréglages, en clair."""
    if not session:
        return ""
    vues = []
    for l in reversed(session.lines):
        m = _RE_FREQ.search(l.raw)
        if m:
            vues.append(int(m.group(1)))
            if len(vues) >= 3:
                break
    if not vues:
        return ""
    minutes = vues[0]
    return FREQUENCES.get(minutes, f"toutes les {minutes} minutes")


def _compte(events, categorie=None, gravite=None, episodes=False) -> int:
    """Nombre d'occurrences, ou d'épisodes distincts si `episodes`.

    Certains messages sont répétés des milliers de fois par seconde ; c'est
    le nombre d'épisodes qui dit si un problème est réellement récurrent.
    """
    total = 0
    for _ts, _fin, diag, count, _raw in events:
        if categorie and diag["category"] != categorie:
            continue
        if gravite and diag["severity"] != gravite:
            continue
        total += 1 if episodes else count
    return total


def _pire(events, exclure=()):
    """Événement le plus révélateur : l'erreur la plus fréquente, sinon
    l'alerte la plus fréquente. `exclure` écarte les libellés qui ne font que
    répéter la conclusion."""
    for gravite in ("error", "warn"):
        candidats = [e for e in events if e[2]["severity"] == gravite
                     and not any(mot in e[2]["meaning"].lower()
                                 for mot in exclure)]
        if candidats:
            return max(candidats, key=lambda e: e[3])
    return None


def describe(session, events, states, coverage=None, periode=None) -> tuple:
    """(lignes de comportement, conclusion, gravité de la conclusion).

    `events` : incidents déjà regroupés [debut, fin, diag, nombre, brut].
    `states` : chronologie des états. `coverage` : (tondu m², total m²).
    `periode` : (début, fin) affichés, sinon déduits des états — ces derniers
    portent parfois des dates aberrantes laissées par l'horloge du robot.
    """
    b = behaviour(states)
    lignes = []

    debut, fin = periode if periode else (b["debut"], b["fin"])
    if debut and fin:
        lignes.append(f"Période analysée : du {debut:%d/%m/%Y %H:%M} "
                      f"au {fin:%d/%m/%Y %H:%M}")
    if b["sessions"]:
        lignes.append(f"Tonte : {_plural(b['sessions'], 'session')}, "
                      f"{_duree(b['tonte'])} au total")
    else:
        lignes.append("Tonte : le robot n'a jamais démarré de tonte")
    if coverage and coverage[1]:
        tondu, total = coverage
        lignes.append(f"Surface : {tondu:.0f} m² tondus sur {total:.0f} m² "
                      f"({100 * tondu / total:.0f} %)")
    if b["charges"]:
        lignes.append(f"Station : {_plural(b['charges'], 'mise')} en charge, "
                      f"{_plural(b['sorties'], 'sortie')} de base")
    planning = _frequence(session)
    if planning:
        lignes.append(f"Planning : {planning}")
    if b["extinctions"]:
        lignes.append(f"Extinctions : {b['extinctions']}")
    if b["erreurs"]:
        lignes.append(f"Passages en erreur : {b['erreurs']}")

    # On compte des épisodes distincts, pas des lignes de log : un message
    # répété mille fois en une minute ne vaut pas mille problèmes. C'est
    # aussi ce que la conclusion compare, pour rester cohérente avec
    # les chiffres affichés juste au-dessus.
    def episodes(cat, sev=None):
        return _compte(events, cat, sev, episodes=True)

    n_rtk = episodes("Signal RTK", "error") + episodes("Localisation", "error")
    n_patine = episodes("Patinage")
    n_choc = episodes("Pare-chocs")
    n_blocage = episodes("Blocage / échappement")

    detail = [f"{nom} {n}" for n, nom in
              ((n_patine, "patinage"), (n_choc, "chocs"),
               (n_blocage, "blocages"), (n_rtk, "pertes de position")) if n]
    if detail:
        lignes.append("Incidents (épisodes distincts) : " + ", ".join(detail))

    conclusion, gravite = _conclure(session, events, b, n_rtk, n_patine,
                                    n_choc, n_blocage)
    return lignes, conclusion, gravite


def _conclure(session, events, b, n_rtk, n_patine, n_choc, n_blocage):
    """Règles ordonnées : du blocage total au fonctionnement normal."""
    positions = len(session.track) if session else 0
    heures = b["tonte"].total_seconds() / 3600

    def cadence(n):
        """« soit N par heure de tonte » — un nombre brut ne dit rien sans
        la durée sur laquelle il s'est produit."""
        if heures < 0.25:
            return ""
        return f", soit {n / heures:.0f} par heure de tonte"

    if positions == 0:
        # inutile de citer « ne se localise pas » comme cause de l'absence de
        # position : c'est le même constat. On cherche ce qui l'explique.
        pire = _pire(events, exclure=("se localiser", "global pose"))
        cause = f" Cause la plus probable : {pire[2]['meaning']}." if pire else ""
        return ("Le robot n'a enregistré aucune position : il ne s'est pas "
                "localisé et n'a donc pas pu tondre." + cause), "error"

    if b["sessions"] == 0:
        if b["charges"]:
            return ("Le robot s'est rechargé mais n'a jamais lancé de tonte "
                    "sur cette période : vérifier le planning et les "
                    "conditions de démarrage."), "error"
        return ("Aucune tonte sur cette période."), "warn"

    n_station = _compte(events, "Station de charge", episodes=True)
    n_bande = _compte(events, "Bande magnétique", episodes=True)
    faible = (" Le signal de la bande magnétique est faible : bande trop "
              "enfouie, posée à l'envers, ou bande de zone interdite utilisée "
              "par erreur." if n_bande else
              " Vérifier le niveau de la station et son installation.")
    if n_station >= 3:
        return (f"Le robot n'arrive pas à rentrer à sa station "
                f"({n_station} échecs de retour).{faible}"), "error"
    if n_bande >= 3:
        return (f"Signal de bande magnétique insuffisant "
                f"({n_bande} relevés sous le seuil de 1000) : le robot "
                "risque de ne plus retrouver sa station. Bande trop enfouie, "
                "posée à l'envers, ou bande de zone interdite."), "warn"

    if n_rtk and n_rtk >= max(10, n_patine, n_choc):
        return (f"Problème de localisation dominant ({n_rtk} pertes de "
                "position) : contrôler l'antenne, le dégagement du ciel et "
                "la couverture 4G du terrain."), "error"

    if n_patine and n_patine >= max(10, n_choc, n_blocage):
        return (f"Le robot patine beaucoup ({n_patine} épisodes"
                f"{cadence(n_patine)}) : terrain en pente, herbe humide ou "
                "roues usées. Repérez les endroits sur la carte pour cibler "
                "la zone en cause."), "warn"

    if n_blocage >= 10:
        return (f"Le robot se retrouve souvent piégé ({n_blocage} procédures "
                f"d'échappement{cadence(n_blocage)}) : cherchez le passage "
                "étroit ou l'obstacle responsable sur la carte."), "warn"

    if n_choc >= 20:
        return (f"Chocs fréquents ({n_choc} épisodes{cadence(n_choc)}) : "
                "obstacle mal cartographié ou pare-chocs sensible."), "warn"

    if b["erreurs"]:
        pire = _pire(events)
        detail = f" Le plus fréquent : {pire[2]['meaning']}." if pire else ""
        return (f"Le robot est passé {b['erreurs']} fois en erreur." + detail), "warn"

    return "Aucun problème majeur relevé sur cette période.", "ok"
