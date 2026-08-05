"""
states.py — Machine à états du robot, lue depuis les lignes de journal.

Ces fonctions vivaient dans `mapmodel`, qui a besoin de numpy pour la carte.
Elles n'en ont pas besoin, elles : les isoler ici permet de s'en servir sans
numpy — c'est ce qui rend `digest.py` exécutable tel quel sur un poste où
rien n'est installé. `mapmodel` les ré-exporte, les appelants ne changent pas.
"""

from __future__ import annotations

import re

# Le RTC du robot repart en 2017 après certaines coupures : dates à ignorer
MIN_VALID_YEAR = 2020

_RE_STATE = re.compile(r"Ui_Inter_(Sub_)?WorkMode_(\w+)", re.I)
_RE_RTK2_STATE = re.compile(r"\[(Work|Charge) State\]\s*\[\d+\]\s*(Enter|Exit)\s+(.+)", re.I)


def extract_state(raw: str) -> str | None:
    """État du robot déduit d'une ligne de log, sinon None.

    Deux machines à états selon la génération :
      RTK1  *Ui_Inter_WorkMode_Error# --> *Ui_Inter_WorkMode_PrePowerOff#
      RTK2  [Work State] [12] Enter Auto   /   [Charge State] [18] Exit Charge Doing
    Elles sont ramenées au même vocabulaire pour que la détection des
    trajets fonctionne à l'identique sur les deux formats.
    """
    low = raw.lower()
    if "workmode" in low:
        found = [name for _sub, name in _RE_STATE.findall(raw)
                 if name.lower() != "change"]
        return found[-1].lower() if found else None

    m = _RE_RTK2_STATE.search(raw)
    if not m:
        return None
    kind, action, name = m.group(1).lower(), m.group(2).lower(), m.group(3)
    name = name.strip().lower().replace(" ", "")
    if kind == "charge":
        if name.startswith("chargedoing"):
            # entrée en charge = arrivée à la station, sortie = départ
            return "charge" if action == "enter" else "prestart"
        return None
    if action != "enter":
        return None
    return {"auto": "start", "idle": "idle", "poweroff": "prepoweroff",
            "manualriding": "idle"}.get(name)


def state_timeline(lines) -> list:
    """[(ts, state_bas_de_casse, is_sub)] à partir des lignes de log."""
    events = []
    for l in lines:
        if l.ts is None or l.ts.year < MIN_VALID_YEAR:
            continue
        state = extract_state(l.raw)
        if state:
            events.append((l.ts, state, "sub_workmode" in l.raw.lower()))
    events.sort(key=lambda e: e[0])
    return events
