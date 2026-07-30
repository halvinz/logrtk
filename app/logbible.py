"""
logbible.py — Base de connaissances "Log Assistance".

Traduit les mots-clés des logs du robot en diagnostic lisible :
catégorie, signification, conclusion/solution, gravité.

Source : feuille "Log bible" (assistance) fournie par l'utilisateur.
`analyze_line(raw)` renvoie un dict {category, severity, meaning, conclusion}
ou None si la ligne ne correspond à aucune règle connue.
Gravités : "error" (rouge), "warn" (orange), "info" (gris).
"""

from __future__ import annotations

import re
from typing import Optional


WORKMODES = {
    "None": ("Aucun état (sous-tension)", "info"),
    "Idle": ("Au repos (attend une commande)", "info"),
    "Ready": ("Prêt au travail", "info"),
    "PrePoweroff": ("Tondeuse éteinte (auto ou manuelle)", "info"),
    "Error": ("Robot en ERREUR", "error"),
    "Charge": ("En charge", "info"),
    "PreStart": ("Va commencer le travail", "info"),
    "LeaveBase": ("Quitte la base", "info"),
    "Start": ("Démarre la tonte", "info"),
    "Return": ("Retourne à la station", "info"),
}

START_TYPES = {
    "Auto_start": "Démarrage depuis le planning",
    "Start+OK": "Démarrage depuis le clavier",
    "APP_Start": "Démarrage depuis l'application",
}

POWEROFF = [
    ("Shut down with no pin insert", "Extinction après 1 min sans saisie du code PIN"),
    ("Manual power off", "Extinction manuelle"),
    ("Auto power off", "Extinction automatique (20 min)"),
]

M3_ERRORS = {
    2: ("Tondeuse soulevée (capteur de levage)", "error",
        "Robot soulevé par quelqu'un, sinon vérifier capteur/roues avant"),
    5: ("Capteur de pluie déclenché", "warn",
        "Normal s'il pleut, sinon vérifier le capteur de pluie"),
    9: ("Erreur moteur de roue", "error",
        "Voir le détail « wheel motor fault » au même moment"),
    10: ("Capteur de blocage activé (> 10 s)", "error",
         "Robot bloqué : regarder l'endroit sur la carte"),
    11: ("Tondeuse à l'envers", "error",
         "Robot retourné, sinon défaut carte mère"),
}

WHEEL_FAULTS = {
    1: "Surintensité",
    2: "Pas de résistance au démarrage (câblage)",
    3: "Protection surchauffe",
    4: "Pas de connexion entre moteur et carte mère",
    5: "Erreur de communication avec la carte de gestion moteur",
    6: "Erreur de configuration de la carte de gestion moteur",
    7: "Capteurs à effet Hall manquants ou déconnectés",
}

BLADE_HEIGHT_ERRORS = {
    1: "Réglage de la hauteur de coupe bloqué",
    2: "Erreur encodeur",
    3: "Capteur Hall introuvable (aimant)",
    4: "Délai dépassé pour atteindre la hauteur souhaitée",
    5: "Erreur de limite de distance",
    6: "Hauteur définie non valide",
}

BUTTONS = {
    1: "Start", 2: "Maison", 3: "Retour", 4: "A", 5: "OK",
    6: "C", 7: "B", 8: "D", 14: "STOP",
}

_RE_WORKMODE = re.compile(r"Ui_inter_WorkMode_?\s*(\w+)", re.I)
_RE_START_TYPE = re.compile(r"start type is\s*[:\s]*([\w+]+)", re.I)
_RE_M3_ERROR = re.compile(r"m3 error\s*=\s*(\d+)", re.I)
_RE_WHEEL = re.compile(r"wheel motor fault\D*(\d+)", re.I)
_RE_BLADE_H = re.compile(r"getBladeHeight\D*(\d+)", re.I)
_RE_MAGNET_VAL = re.compile(r"magnetic info\D*(-?\d+)", re.I)
_RE_BUTTON = re.compile(r"button pressed is\W*(\d+)", re.I)


def _r(category, severity, meaning, conclusion=""):
    return {"category": category, "severity": severity,
            "meaning": meaning, "conclusion": conclusion}


def analyze_line(raw: str) -> Optional[dict]:
    """Analyse une ligne de log brute, renvoie un diagnostic ou None."""
    low = raw.lower()

    if "ui_inter_workmode" in low:
        m = _RE_WORKMODE.search(raw)
        if m:
            mode = m.group(1)
            for name, (meaning, sev) in WORKMODES.items():
                if mode.lower().startswith(name.lower()) and name != "None":
                    return _r("État du robot", sev, meaning)
            if mode.lower().startswith("none"):
                return _r("État du robot", "info", WORKMODES["None"][0])
        return None

    if "start type is" in low:
        m = _RE_START_TYPE.search(raw)
        if m:
            label = START_TYPES.get(m.group(1), f"Type de démarrage : {m.group(1)}")
            return _r("Démarrage", "info", label)
        return _r("Démarrage", "info", "Démarrage")

    if "power off" in low or "shut down" in low:
        for needle, label in POWEROFF:
            if needle.lower() in low:
                return _r("Extinction", "info", label)

    if "m3 error" in low:
        m = _RE_M3_ERROR.search(raw)
        if m:
            code = int(m.group(1))
            if code in M3_ERRORS:
                meaning, sev, concl = M3_ERRORS[code]
                return _r("Erreur M3", sev, meaning, concl)
            return _r("Erreur M3", "warn", f"M3 error = {code} (code non répertorié)")

    if "wheel motor fault" in low:
        m = _RE_WHEEL.search(raw)
        if m:
            code = int(m.group(1))
            if code == 0:
                return None  # pas d'erreur
            label = WHEEL_FAULTS.get(code, f"Code {code} non répertorié")
            return _r("Moteur de roue", "error", f"Défaut moteur de roue : {label}",
                      "Contrôler moteur/câblage selon le code")

    if "getbladeheight" in low:
        m = _RE_BLADE_H.search(raw)
        if m:
            code = int(m.group(1))
            if code == 0:
                return None  # pas d'erreur
            label = BLADE_HEIGHT_ERRORS.get(code, f"Code {code} non répertorié")
            return _r("Hauteur de coupe", "error", f"Erreur hauteur de coupe : {label}")

    if "blade setting changed" in low:
        return _r("Hauteur de coupe", "info",
                  "Hauteur de coupe modifiée par l'utilisateur")

    if "bump left" in low:
        return _r("Pare-chocs", "warn", "Choc à gauche",
                  "Obstacle à gauche ou capteur défectueux")
    if "bump right" in low:
        return _r("Pare-chocs", "warn", "Choc à droite",
                  "Obstacle à droite ou capteur défectueux")

    if "rain sensor changed to 1" in low or "rian sensor changed to 1" in low:
        return _r("Capteur de pluie", "warn", "Capteur de pluie déclenché",
                  "Normal s'il pleut, sinon vérifier le capteur")

    if "cannot find magnetic strip" in low or "over angle" in low:
        return _r("Bande magnétique", "error",
                  "Erreur de détection de la bande magnétique",
                  "Déclenchement de l'erreur à l'arrivée")
    if "magnetic info" in low:
        m = _RE_MAGNET_VAL.search(raw)
        if m and int(m.group(1)) < 1500:
            return _r("Bande magnétique", "warn",
                      f"Signal bande magnétique faible ({m.group(1)} < 1500)",
                      "> 1500 = OK, < 1500 = signal insuffisant")
        return None  # valeurs normales : pas la peine d'encombrer le diagnostic

    if "ultrasound bump" in low:
        return _r("OAS (ultrasons)", "info", "Détection d'obstacle par ultrasons",
                  "Apparitions répétées sans obstacle = défaut capteur")

    if "no find search path" in low:
        return _r("Navigation", "warn", "Chemin introuvable",
                  "Passage souvent trop étroit à cet endroit")

    if "escaping from trap" in low:
        return _r("Navigation", "warn", "Procédure d'échappement (robot coincé)")

    if "handlemode_dense_grass" in low:
        return _r("Tonte", "info", "Mode herbe dense activé")

    if "handlemode_continous_collision" in low or "handlemode_continuous_collision" in low:
        return _r("Navigation", "error", "Collisions répétées",
                  "Le robot reste en collision : vérifier l'endroit sur la carte")

    if "move_border" in low:
        return _r("Tonte", "info", "Tonte de bordure")

    if "err_rtk_float" in low:
        return _r("Signal RTK", "error", "Perte de signal RTK",
                  "Vérifier antenne/ciel dégagé ; possible coupure 4G (Level < 15000 hors station)")

    if "button pressed is" in low:
        m = _RE_BUTTON.search(raw)
        if m:
            code = int(m.group(1))
            label = BUTTONS.get(code, f"code {code}")
            return _r("Clavier", "info", f"Touche pressée : {label}")

    return None
