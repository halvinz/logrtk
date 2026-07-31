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
import unicodedata
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

# Les changements d'état réels ressemblent à :
#   [--UI--]Action_Ui_Inter_WorkMode_Change:  *Ui_Inter_WorkMode_Error# --> *Ui_Inter_WorkMode_PrePowerOff#
# On garde donc la DERNIÈRE occurrence (l'état d'arrivée) et on ignore le mot "Change".
_RE_WORKMODE = re.compile(r"Ui_Inter_(?:Sub_)?WorkMode_(\w+)", re.I)
_RE_START_TYPE = re.compile(r"start type is\s*[:\s]*([\w+]+)", re.I)
_RE_M3_ERROR = re.compile(r"m3 error\s*=\s*(\d+)", re.I)
_RE_WHEEL = re.compile(r"wheel motor fault\D*(\d+)", re.I)
_RE_BLADE_H = re.compile(r"getBladeHeight\D*(\d+)", re.I)
_RE_MAGNET_VAL = re.compile(r"magnetic info\D*(-?\d+)", re.I)
_RE_BUTTON = re.compile(r"button pressed is\W*(\d+)", re.I)
_RE_RTK_FLAG = re.compile(r"RTK flag\s*(\d)\s*->\s*(\d)", re.I)
_RE_ESC_MODE = re.compile(r"handle_?mode\s*:\s*(?:ES_HANDLEMODE_)?(\w+)", re.I)
_RE_ESC_TYPE = re.compile(r"handle_type\s*:\s*(?:ES_TYPE_)?(\w+)", re.I)

# Procédures d'échappement : le robot s'est retrouvé piégé et se dégage.
# Le mode dit COMMENT il se dégage, le type dit CE QUI l'a piégé.
ESCAPE_MODES = {
    "SEARCH_FAIL": ("Blocage / échappement",
                    "Le robot ne trouve plus de chemin (piégé)", "error"),
    "SEARCHFAIL": ("Blocage / échappement",
                   "Le robot ne trouve plus de chemin (piégé)", "error"),
    "WHEEL_SLIP": ("Patinage", "Patinage des roues", "warn"),
    "CONTINUOUS_COLLISION": ("Blocage / échappement", "Collisions répétées", "error"),
    "CONTINOUS_COLLISION": ("Blocage / échappement", "Collisions répétées", "error"),
    "SERIES_BUMP": ("Blocage / échappement", "Chocs à répétition", "warn"),
    "DENSE_GRASS": ("Tonte", "Mode herbe dense activé", "info"),
}

ESCAPE_TYPES = {
    "SLIP": "patinage",
    "BORDER": "bloqué en bordure",
    "BUMP": "choc",
    "SERIES_BUMP": "chocs répétés",
    "OBSTACLE": "obstacle",
    "THREE_OBS": "obstacle rencontré trois fois",
    "WHEEL_STALL": "roue bloquée",
    "SERIES_WHEEL_STALL": "roues bloquées à répétition",
    "MOTOR_OVERHEAT": "surchauffe du moteur",
    "RAISE": "robot soulevé",
    "NORMAL": "",
}

# Causes qui méritent une alerte rouge même si l'échappement réussit
ESCAPE_SERIOUS = {"MOTOR_OVERHEAT", "RAISE", "SERIES_WHEEL_STALL", "WHEEL_STALL"}
_RE_SLIP = re.compile(r"slip is\s*([\d.]+)", re.I)
_RE_ISLAND = re.compile(r"island_vec num\s*=\s*(\d+)", re.I)


# Mots en français courant rattachés à chaque catégorie : ils rendent la
# recherche de panne possible alors que les logs, eux, sont en anglais.
CATEGORY_KEYS = {
    "État du robot": "etat mode marche arret veille",
    "Démarrage": "demarrage depart planning app clavier",
    "Extinction": "extinction arret eteint",
    "Erreur M3": "erreur alarme m3 defaut panne",
    "Moteur de roue": "moteur roue avance recule bloque surintensite surchauffe cablage hall",
    "Hauteur de coupe": "hauteur coupe lame tonte reglage encodeur",
    "Pare-chocs": "choc bump pare-chocs collision obstacle heurte tape",
    "Capteur de pluie": "pluie humide rain capteur",
    "Bande magnétique": "bande magnetique magnetic guidage fil cable",
    "OAS (ultrasons)": "ultrason oas obstacle detection capteur",
    "Navigation": "navigation chemin passage etroit coince bloque piege trap echappement collision",
    "Patinage": "patinage patine glisse derape roues pente humide slip",
    "Tonte": "tonte tondre bordure herbe dense",
    "Signal RTK": "rtk gps gnss signal satellite antenne ombre 4g reseau position",
    "Clavier": "touche bouton clavier appui",
    "Carte": "carte zone ilot interdit obstacle bordure",
}


def _r(category, severity, meaning, conclusion="", keys=""):
    """`keys` : mots supplémentaires en français courant pour retrouver la
    panne dans la recherche (le log, lui, est en anglais)."""
    all_keys = " ".join(filter(None, (CATEGORY_KEYS.get(category, ""), keys)))
    return {"category": category, "severity": severity, "meaning": meaning,
            "conclusion": conclusion, "keys": all_keys}


# --------------------------------------------------------------------------
# Robots RTK2 : pas de « Log bible », mais chaque ligne porte sa gravité et
# son module. On s'en sert directement, en écartant la plomberie logicielle
# (bus de messages, threads système) qui n'apprend rien sur la tondeuse.
# --------------------------------------------------------------------------
TECH_TAGS = {
    "DDS", "MSG", "SYS", "IPC", "MAJOR_IPC", "UTILS", "LOG", "MEM",
    "DATABUS", "RTPS", "SHM", "TIMER", "THREAD",
}

TAG_LABELS = {
    "MAPPING": "Cartographie",
    "MAPCOM": "Carte",
    "TASK": "Tâche en cours",
    "PLAN_BRIGE": "Navigation",
    "PLAN": "Navigation",
    "PLANNER": "Navigation",
    "MOWER": "Tonte",
    "BLOCK": "Obstacle",
    "PERCEP": "Perception",
    "HMI": "Interface",
    "NCM": "Réseau",
    "NCMCOM": "Réseau",
    "NTRIPCOM": "Signal RTK",
    "IOT": "Connectivité",
    "M3": "Carte électronique",
    "FI": "Sécurité",
    "SENSOR": "Capteurs",
    "SLAM": "Localisation",
    "UCM": "Pilotage",
    "SSM": "États",
    "EM": "Énergie",
}


def analyze_rtk2_line(level: str, tag: str, text: str) -> Optional[dict]:
    """Diagnostic générique pour les robots RTK2, faute de bible dédiée :
    on remonte les lignes que le robot lui-même signale en WARN ou ERROR."""
    if level not in ("WARN", "ERROR"):
        return None
    tag = (tag or "").upper()
    if tag in TECH_TAGS:
        return None
    category = TAG_LABELS.get(tag, tag.title() if tag else "Robot")
    message = " ".join(text.split())[:150]
    return _r(category, "error" if level == "ERROR" else "warn", message,
              keys=tag.lower())


def normalize(s: str) -> str:
    """Minuscules sans accents, pour une recherche tolérante."""
    s = unicodedata.normalize("NFD", s.lower())
    return "".join(c for c in s if unicodedata.category(c) != "Mn")


def search_terms(query: str) -> list:
    """Mots utiles d'une recherche : les articles et petits mots (« un »,
    « de », « la ») sont écartés, sinon « évite un piège » ne trouverait
    rien alors que l'utilisateur a écrit une phrase naturelle."""
    return [w for w in normalize(query).split() if len(w) > 2]


# Ce que l'on peut taper dans la recherche de panne (affiché à l'utilisateur)
SEARCH_CATALOG = [
    ("Robot coincé", "évite un piège, coincé, bloqué, échappement, trap"),
    ("Patinage", "patine, glisse, slip, roues qui tournent"),
    ("Chocs", "choc, bump, pare-chocs, collision, obstacle"),
    ("Robot soulevé", "soulevé, levage, à l'envers, retourné"),
    ("Pluie", "pluie, rain, capteur de pluie"),
    ("Moteur de roue", "moteur, roue, surintensité, surchauffe, câblage, hall"),
    ("Hauteur de coupe", "hauteur, lame, coupe, encodeur, blade"),
    ("Signal RTK / GNSS", "rtk, gps, signal, satellite, zone d'ombre"),
    ("Bande magnétique", "bande, magnétique, magnetic, guidage"),
    ("Navigation", "chemin introuvable, passage étroit, bordure, navigation"),
    ("Ultrasons (OAS)", "ultrason, oas, détection obstacle"),
    ("États et démarrage", "démarrage, extinction, charge, erreur, planning"),
]


# États dans lesquels le robot est à sa base : certaines valeurs y sont
# normalement basses et ne doivent pas déclencher d'alerte.
AT_STATION = ("charge", "checkcharge", "precheckinstation", "docking",
              "idle", "ready", "none", "prepoweroff")


def analyze_line(raw: str, state: Optional[str] = None) -> Optional[dict]:
    """Analyse une ligne de log brute, renvoie un diagnostic ou None.

    `state` est l'état du robot au moment de cette ligne (workmode courant) :
    il sert aux règles qui ne valent qu'en dehors de la station de charge.
    """
    low = raw.lower()

    if "workmode" in low:
        found = [m for m in _RE_WORKMODE.findall(raw) if m.lower() != "change"]
        if not found:
            return None
        mode = found[-1].lower()          # état d'arrivée
        sub = "sub_workmode" in low
        for name, (meaning, sev) in WORKMODES.items():
            if mode.startswith(name.lower()):
                label = f"{meaning} (sous-état)" if sub else meaning
                return _r("État du robot", "info" if sub else sev, label)
        return None

    if "start type is" in low:
        m = _RE_START_TYPE.search(raw)
        if not m or m.group(1).upper().startswith("NO_START"):
            return None
        return _r("Démarrage", "info",
                  START_TYPES.get(m.group(1), f"Type de démarrage : {m.group(1)}"))

    if "power off" in low or "shut down" in low:
        for needle, label in POWEROFF:
            if needle.lower() in low:
                return _r("Extinction", "info", label)

    if "m3 error" in low:
        m = _RE_M3_ERROR.search(raw)
        if m:
            code = int(m.group(1))
            if code == 0:
                return None  # retour à la normale
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
    if "magneticstart" in low or "startfollowstripe" in low:
        return _r("Bande magnétique", "info",
                  "Début du guidage par la bande magnétique")
    if "magneticstop" in low:
        return _r("Bande magnétique", "info",
                  "Fin du guidage par la bande magnétique")

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

    if "escaping from trap" in low or "escape" in low:
        m_mode = _RE_ESC_MODE.search(raw)
        m_type = _RE_ESC_TYPE.search(raw)
        if not m_mode and "escaping from trap" not in low:
            return None
        mode = m_mode.group(1).upper() if m_mode else ""
        cat, meaning, sev = ESCAPE_MODES.get(
            mode, ("Blocage / échappement", "Procédure d'échappement (robot piégé)", "warn")
        )
        cause = ""
        if m_type:
            code = m_type.group(1).upper()
            cause = ESCAPE_TYPES.get(code, code.lower().replace("_", " "))
            if code in ESCAPE_SERIOUS:
                sev = "error"
        # « Patinage des roues — cause : patinage » n'apprend rien : on
        # n'ajoute la cause que si elle n'est pas déjà dans le libellé.
        if cause and normalize(cause) not in normalize(meaning):
            meaning = f"{meaning} — cause : {cause}"
        return _r(cat, sev, meaning,
                  "Le robot s'est retrouvé piégé et tente de se dégager — "
                  "voir l'endroit exact sur la carte",
                  keys="evite piege trappe sort coince immobilise degage echappement escape")

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

    # Perte / retour du signal RTK : information fiable et sans ambiguïté.
    m = _RE_RTK_FLAG.search(raw)
    if m:
        before, after = m.group(1), m.group(2)
        if before == "1" and after == "0":
            return _r("Signal RTK", "error", "Perte du signal RTK",
                      "Zone d'ombre, antenne masquée ou coupure 4G — voir l'endroit sur la carte")
        if before == "0" and after == "1":
            return _r("Signal RTK", "info", "Signal RTK retrouvé")
        return None

    if "shadow" in low and "gnss" in low:
        return _r("Signal RTK", "warn", "Zone d'ombre GNSS détectée",
                  "Réception satellite dégradée à cet endroit")

    if "slip is" in low:
        m = _RE_SLIP.search(raw)
        # l'erreur de fusion est loguée en continu : on n'alerte qu'au-delà du seuil
        if m and float(m.group(1)) >= 0.5:
            return _r("Patinage", "warn",
                      f"Patinage important (erreur de fusion {m.group(1)})",
                      "Roues qui glissent : sol humide, pente ou herbe haute")
        return None
    if "wheel_slip" in low or "es_type_slip" in low:
        return _r("Patinage", "error", "Patinage : procédure d'échappement déclenchée",
                  "Le robot patine et tente de se dégager — voir l'endroit sur la carte")

    if "island_vec num" in low:
        m = _RE_ISLAND.search(raw)
        if m:
            return _r("Carte", "info",
                      f"{m.group(1)} îlot(s) / zone(s) interdite(s) dans la carte")
    if "inner border size" in low:
        return _r("Carte", "info", raw.split("]")[-1].strip())

    if "button pressed is" in low:
        m = _RE_BUTTON.search(raw)
        if m:
            code = int(m.group(1))
            label = BUTTONS.get(code, f"code {code}")
            return _r("Clavier", "info", f"Touche pressée : {label}")

    return None
