"""
portal.py — État MQTT du robot, tel que publié sur le portail.

Les logs du robot ne disent rien de la batterie : ni tension, ni
température, ni nombre de recharges. Ces valeurs vivent dans le message
d'état du portail, que le guide de dépannage appelle « journal d'activité ».

Ce module lit ce JSON et en tire les contrôles du guide :
tension supérieure à 15 V, température entre 0 et 40 °C, batterie
au-dessus de 30 % pour démarrer ou se mettre à jour.
"""

from __future__ import annotations

import json


# Seuils du guide de dépannage
BATT_VOLT_MIN = 15.0
BATT_TEMP_MIN, BATT_TEMP_MAX = 0, 40
BATT_PCT_MIN = 30
RSSI_FAIBLE = -100      # au-delà, la 4G ne passe plus correctement

FREQUENCES = {480: "trois fois par jour", 720: "deux fois par jour",
              1440: "une fois par jour", 2880: "tous les deux jours",
              4320: "tous les trois jours", 10000: "une fois par semaine"}


def _f(diag_cat, gravite, message, conseil="", keys=""):
    return {"category": diag_cat, "severity": gravite, "meaning": message,
            "conclusion": conseil, "keys": keys}


def read_status(path: str):
    """Lit un état du portail. Renvoie (infos, constats) ou None si le
    fichier n'a pas cette forme."""
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or "dat" not in data:
        return None
    return analyze(data)


def analyze(data: dict):
    dat = data.get("dat") or {}
    cfg = data.get("cfg") or {}
    infos, constats = [], []

    if dat.get("tm"):
        infos.append(f"État relevé le {dat['tm'][:10]} à {dat['tm'][11:19]} UTC")
    firmwares = [f"corps {dat['fw']}" for _ in (1,) if dat.get("fw")]
    if (dat.get("head") or {}).get("fw"):
        firmwares.append(f"tête {dat['head']['fw']}")
    if firmwares:
        infos.append("Firmware : " + ", ".join(firmwares))

    # --- batterie -----------------------------------------------------
    bt = dat.get("bt") or {}
    if bt:
        etat = "en charge" if bt.get("c") else "hors charge"
        infos.append(f"Batterie : {bt.get('p', '?')} %, {bt.get('v', '?')} V, "
                     f"{bt.get('t', '?')} °C, {etat}, "
                     f"{bt.get('nr', 0)} recharges")
        volts = bt.get("v")
        if isinstance(volts, (int, float)) and volts < BATT_VOLT_MIN:
            constats.append(_f(
                "Batterie", "error",
                f"Tension batterie faible ({volts} V, il en faut plus de "
                f"{BATT_VOLT_MIN:.0f})",
                "Tester au multimètre, nettoyer les contacts, remplacer si "
                "la valeur ne remonte pas",
                keys="batterie tension volt faible morte"))
        temp = bt.get("t")
        if isinstance(temp, (int, float)) and not (BATT_TEMP_MIN <= temp <= BATT_TEMP_MAX):
            constats.append(_f(
                "Batterie", "error",
                f"Température batterie hors plage ({temp} °C, admis "
                f"{BATT_TEMP_MIN} à {BATT_TEMP_MAX})",
                "Au-dessus de 40 °C il y a risque d'incendie : laisser "
                "refroidir puis remplacer si cela se reproduit",
                keys="batterie temperature chaude froide surchauffe"))
        pct = bt.get("p")
        if isinstance(pct, (int, float)) and pct < BATT_PCT_MIN:
            constats.append(_f(
                "Batterie", "warn",
                f"Batterie à {pct} %",
                f"Sous {BATT_PCT_MIN} % le robot ne démarre pas et refuse "
                "les mises à jour",
                keys="batterie faible charge demarre pas"))

    # --- signal et connectivité ---------------------------------------
    rssi = dat.get("rsi")
    reseau = ((dat.get("modules") or {}).get("4G") or {}).get("network") or {}
    if rssi is None:
        rssi = reseau.get("rssi")
    if rssi is not None:
        infos.append(f"Réseau : {dat.get('conn', '?')}, "
                     f"{reseau.get('mode', '')} {rssi} dBm".replace("  ", " "))
        if isinstance(rssi, (int, float)) and rssi <= RSSI_FAIBLE:
            constats.append(_f(
                "Réseau", "warn", f"Signal mobile très faible ({rssi} dBm)",
                "Sans 4G le robot perd la correction RTK : vérifier la "
                "couverture sur place et la carte SIM",
                keys="4g reseau signal faible sim antenne"))
    if (cfg.get("modules") or {}).get("4G", {}).get("enabled") == 0:
        constats.append(_f(
            "Réseau", "info", "La 4G est désactivée dans la configuration",
            keys="4g desactive configuration"))

    # --- RTK ----------------------------------------------------------
    rtk = dat.get("rtk") or {}
    for cle, nom in (("gps", "GPS / RTK"), ("network", "Réseau RTK"),
                     ("imu", "Centrale inertielle")):
        bloc = rtk.get(cle) or {}
        if bloc.get("error"):
            constats.append(_f(
                "Signal RTK", "error",
                f"{nom} en erreur (code {bloc['error']})",
                "Contrôler le dégagement du ciel, l'antenne et la couverture "
                "des stations de référence",
                keys="rtk gps signal erreur position antenne"))

    # --- pluie --------------------------------------------------------
    pluie = dat.get("rain") or {}
    if pluie.get("s"):
        constats.append(_f("Capteur de pluie", "info",
                           "Capteur de pluie mouillé",
                           keys="pluie capteur mouille"))
    if pluie.get("cnt"):
        constats.append(_f(
            "Capteur de pluie", "info",
            f"Report de tonte pour pluie en cours ({pluie['cnt']} min)",
            keys="pluie report delai attente"))

    # --- modules ------------------------------------------------------
    for nom, bloc in ((n, b) for n, b in
                      ((dat.get("modules") or {})).items()):
        if isinstance(bloc, dict) and bloc.get("error"):
            constats.append(_f("Modules", "warn",
                               f"Module {nom} en erreur (code {bloc['error']})",
                               keys="module erreur"))

    # --- réglages -----------------------------------------------------
    zones = ((cfg.get("rtk") or {}).get("zs")) or []
    for zone in zones:
        z = zone.get("cfg") or {}
        freq = (z.get("sc") or {}).get("freq")
        hauteur = ((z.get("modules") or {}).get("EA") or {}).get("h")
        detail = []
        if freq is not None:
            detail.append(FREQUENCES.get(freq, f"toutes les {freq} min"))
        if hauteur is not None:
            detail.append(f"hauteur {hauteur} mm")
        if detail:
            infos.append(f"Zone {zone.get('id', '?')} : " + ", ".join(detail))

    retard = cfg.get("rd")
    if retard is not None:
        infos.append(f"Report de pluie réglé : {retard} min")

    return infos, constats
