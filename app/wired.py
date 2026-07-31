"""
wired.py — Journaux des robots filaires (KR101E et apparentés).

Troisième format rencontré, et le plus inhabituel : une page HTML. Chaque
entrée y est un paragraphe

    #00042 - 31/07/2026 11:35:16 - <span class="i06s02l00">…texte…</span>

et la classe CSS ne sert pas qu'à la décoration : sa couleur porte la
gravité, définie dans la feuille de style du fichier. Rouge pour une
erreur, orange pour une alerte, vert pour un état normal. On la lit donc
au lieu de deviner.

Les codes E1 à E10 du guide de dépannage n'apparaissent pas tels quels
dans le journal : ils y figurent sous leur forme anglaise (« wire
missing », « Upside Down », « trapped sensor timeout »…). La table de
correspondance ci-dessous fait le lien avec le code et la marche à suivre.
"""

from __future__ import annotations

import os
import re
import html as _html
from datetime import datetime

TS_FMT = "%d/%m/%Y %H:%M:%S"

_RE_STYLE_RULE = re.compile(r"([^{}]+)\{([^{}]+)\}")
_RE_COLOR = re.compile(r"color\s*:\s*([^;]+)", re.I)
_RE_CLASS = re.compile(r"\.(i\d+s\d+l\d+)")
_RE_ENTRY = re.compile(
    r"#(\d+)\s*-\s*(\d{2}/\d{2}/\d{4} [\d:]+)\s*-\s*"
    r"<span class=\"?(i\d+s\d+l\d+)\"?[^>]*>(.*?)</span>", re.S)
_RE_TAG = re.compile(r"<[^>]+>")

ROUGE = {"red", "crimson", "#fa5858", "#610b0b", "#59020e", "#ff0000"}
ORANGE = {"#ffa800", "ffa800", "#996600", "#e6bf00", "orange"}

# Valeurs relevées au fil des messages, pour la synthèse
# Les relevés apparaissent sous la forme « 23.0C 18.49 Volt » ou
# « 29.7C 18.62V 0mA » : on exige le couple, sinon n'importe quel nombre
# suivi d'un V serait pris pour une tension.
_RE_TEMP_VOLT = re.compile(r"(-?[\d.]+)\s*C\s+([\d.]+)\s*V", re.I)
_RE_VOLT_SEUL = re.compile(r"battery voltage:\s*([\d.]+)\s*V", re.I)


def looks_like_wired(path: str) -> bool:
    """Vrai si le fichier est un journal HTML de robot filaire."""
    if not path.lower().endswith((".html", ".htm")):
        return False
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            debut = f.read(4000)
    except OSError:
        return False
    return "<style>" in debut and re.search(r"i\d+s\d+l\d+", debut) is not None


def _severities(style: str) -> dict:
    """Gravité de chaque classe, déduite de sa couleur."""
    out = {}
    for selecteur, corps in _RE_STYLE_RULE.findall(style):
        m = _RE_COLOR.search(corps)
        if not m:
            continue
        couleur = m.group(1).strip().lower()
        if couleur in ROUGE:
            niveau = "ERROR"
        elif couleur in ORANGE:
            niveau = "WARN"
        else:
            niveau = "INFO"
        for nom in _RE_CLASS.findall(selecteur):
            out[nom] = niveau
    return out


def load(path: str, session) -> None:
    """Charge un journal HTML dans `session`."""
    from parser import LogLine

    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        contenu = f.read()

    debut_style = contenu.find("<style>")
    fin_style = contenu.find("</style>")
    gravites = _severities(contenu[debut_style:fin_style]) if debut_style >= 0 else {}

    fichier = os.path.basename(path)
    session.fmt = "wired"
    for _num, ts_txt, classe, brut in _RE_ENTRY.findall(contenu):
        texte = _html.unescape(_RE_TAG.sub("", brut)).strip()
        if not texte:
            continue
        try:
            ts = datetime.strptime(ts_txt, TS_FMT)
        except ValueError:
            ts = None
        session.lines.append(
            LogLine(ts, gravites.get(classe, "INFO"), texte,
                    f"{ts_txt} - {texte}", fichier, classe)
        )
    session.files_loaded.append(fichier)
    _identity(session)


def _identity(session) -> None:
    """Modèle et versions de firmware, annoncés au démarrage du robot."""
    versions = {}
    for l in session.lines:
        m = re.match(r"\[([^\]]+)\](?:\[([^\]]+)\])? - Firmware version: (.+)",
                     l.text)
        if m:
            nom = m.group(2) or m.group(1)
            versions.setdefault(nom, m.group(3).strip())
        m = re.search(r"\[Battery\] Model detected: (\S+)", l.text)
        if m and not session.model:
            session.model = m.group(1)
    if versions:
        session.firmware = ", ".join(f"{k} {v}" for k, v in list(versions.items())[:4])
    dates = [l.ts for l in session.lines if l.ts]
    if dates:
        session.export_date = max(dates).strftime("%d/%m/%Y %H:%M")


def battery_readings(lines) -> tuple:
    """(tensions, températures) relevées dans les messages."""
    volts, temps = [], []
    for l in lines:
        m = _RE_TEMP_VOLT.search(l.text)
        if m:
            t, v = float(m.group(1)), float(m.group(2))
            if -20 < t < 90:
                temps.append(t)
            if 5 < v < 40:
                volts.append(v)
            continue
        m = _RE_VOLT_SEUL.search(l.text)
        if m:
            v = float(m.group(1))
            if 5 < v < 40:
                volts.append(v)
    return volts, temps


# États du robot filaire, pour la même synthèse que les modèles RTK
ETATS = [
    ("grass cutting", "start"),
    ("start sequence", "prestart"),
    ("following wire", "start"),
    ("searching wire", "prestart"),
    ("at home", "charge"),
    ("in idle state", "idle"),
    ("shutdown", "prepoweroff"),
]


def state_timeline(lines) -> list:
    """[(date, état, False)] compatible avec la synthèse."""
    out = []
    for l in lines:
        if l.ts is None:
            continue
        bas = l.text.lower()
        for motif, etat in ETATS:
            if motif in bas:
                if not out or out[-1][1] != etat:
                    out.append((l.ts, etat, False))
                break
    return out


# --------------------------------------------------------------------------
# Correspondance entre les messages du journal et le guide de dépannage
# --------------------------------------------------------------------------
REGLES = [
    ("outside wire timeout", "E1", "Navigation", "error",
     "Robot hors limites (fil non retrouvé)",
     "Vérifier le câble périmétrique et sa connexion à la station, "
     "sa résistance (3-10 Ω, 13-20 Ω sur Mega) et l'absence d'interférences",
     "fil cable perimetrique hors limites e1"),
    ("wire missing", "E1", "Navigation", "error",
     "Fil périmétrique manquant",
     "LED de la station : vert clignotant lent = câble défectueux. "
     "Contrôler le câble, les connecteurs et la résistance",
     "fil cable perimetrique manquant e1"),
    ("upside down", "E6", "Sécurité", "error",
     "Tondeuse à l'envers",
     "Pente supérieure à 35° interdite ; sinon contrôler le système de "
     "roues et les capteurs de levage",
     "envers renverse pente inclinaison e6"),
    ("trapped sensor timeout", "E4", "Blocage / échappement", "error",
     "Robot piégé (capteur bloqué plus de 10 s)",
     "Terrain sans trous, roues et couvercle flottant dégagés, vis serrées, "
     "aimants en place",
     "piege trappe bloque e4"),
    ("trapped recovery procedure", "E4", "Blocage / échappement", "warn",
     "Procédure de dégagement",
     "Répétée souvent : chercher l'obstacle ou le trou sur le terrain",
     "piege degagement recuperation e4"),
    ("safety trap", "E4", "Blocage / échappement", "warn",
     "Capteur de sécurité déclenché",
     "", "securite capteur trap e4"),
    ("power-off - lift", "E5", "Sécurité", "warn",
     "Coupe arrêtée : robot soulevé",
     "Les roues avant doivent bouger librement d'environ 1,5 cm ; "
     "vérifier l'aimant de la roue avant",
     "souleve levage e5"),
    ("power-off - wire missing", "E1", "Navigation", "error",
     "Coupe arrêtée : fil périmétrique perdu", "", "fil coupe arret e1"),
    ("power-off - low battery", None, "Batterie", "warn",
     "Coupe arrêtée : batterie faible", "", "batterie faible coupe"),
    ("power-off - trap timer", "E4", "Blocage / échappement", "warn",
     "Coupe arrêtée : robot piégé", "", "piege coupe arret e4"),
    ("[emergency charge] shutdown", "E7", "Batterie", "error",
     "Arrêt en charge d'urgence",
     "Le robot s'est éteint faute de tension : tester la batterie au "
     "multimètre (plus de 15 V) et contrôler la station",
     "batterie urgence arret tension e7"),
    ("shutdown: battery low", None, "Batterie", "warn",
     "Arrêt sur batterie vide", "", "batterie vide arret"),
    ("low current: retry", "E7", "Batterie", "error",
     "Charge en échec (courant insuffisant)",
     "Nettoyer les broches du robot et de la station, contrôler la LED de "
     "la station, les connecteurs et le câble de batterie, puis essayer "
     "une autre batterie",
     "charge batterie station courant e7 recharge"),
    ("wheel blocked", "E2", "Moteur de roue", "error",
     "Moteur de roue bloqué",
     "Roues propres et dégagées, légère résistance à la rotation, "
     "connecteurs sans corrosion",
     "roue moteur bloque e2"),
    ("blade blocked", "E3", "Moteur de coupe", "error",
     "Moteur de coupe bloqué",
     "Disque, lames et arbre propres ; relever la hauteur de coupe et "
     "redescendre progressivement",
     "lame disque coupe bloque e3"),
    ("rain", "F1", "Capteur de pluie", "info",
     "Report de tonte pour pluie",
     "Délai automatique de 180 min après séchage ; nettoyer le capteur "
     "à la brosse fine",
     "pluie report delai f1"),
]


# Messages colorés en rouge par le firmware alors qu'ils décrivent une
# marche normale : sans ce tri, ils écraseraient les vraies pannes.
BENINS = (
    "[net sm]", "mqtt", "blade power-on", "charge end by base",
    "[file system]", "[can-bus]", "firmware version", "peripheral init",
    "power-off - stop request", "power-off - home", "menu idle timer",
    "[bootloader]", "model detected", "in idle state",
)

# Tension minimale admise par le guide de dépannage
VOLT_MIN = 15.0


def analyze_wired_line(level: str, text: str):
    """Diagnostic d'une ligne de journal filaire."""
    bas = text.lower()
    for motif, code, categorie, gravite, message, action, cles in REGLES:
        if motif in bas:
            titre = f"{code} — {message}" if code else message
            return {"category": categorie, "severity": gravite,
                    "meaning": titre, "conclusion": action, "keys": cles}
    if any(b in bas for b in BENINS):
        return None
    if level in ("ERROR", "WARN"):
        return {"category": "Robot",
                "severity": "error" if level == "ERROR" else "warn",
                "meaning": " ".join(text.split())[:150], "conclusion": "",
                "keys": ""}
    return None
