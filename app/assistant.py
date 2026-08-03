"""
assistant.py — Diagnostic à partir du message du client.

On y colle le courriel reçu ; le module en tire les symptômes décrits, les
confronte à ce que montrent les journaux chargés, et propose des causes
classées avec la marche à suivre des guides de dépannage.

Deux principes ont guidé les règles :

1. Ce que le client a déjà fait ou déjà écarté ne doit pas lui être
   reproposé. « J'ai remplacé les moteurs de roue », « j'ai essayé un autre
   robot sur la même station » : ces phrases éliminent des pistes, et c'est
   souvent là que se trouve l'information la plus utile.
2. Une piste que les journaux contredisent est signalée comme telle plutôt
   que passée sous silence.
"""

from __future__ import annotations

import re
import unicodedata


def _norm(texte: str) -> str:
    texte = unicodedata.normalize("NFD", (texte or "").lower())
    texte = "".join(c for c in texte if unicodedata.category(c) != "Mn")
    # « j'ai » devient « j ai » : sans cela les tournures les plus courantes
    # d'un message de client échappent aux motifs.
    texte = texte.replace("'", " ").replace("’", " ")
    # Un client passe à la ligne au lieu de ponctuer : on marque la coupure,
    # sinon deux phrases n'en font plus qu'une et les extraits débordent.
    texte = re.sub(r"[\r\n]+", " ; ", texte)
    return re.sub(r"[ \t]+", " ", texte)


# --------------------------------------------------------------------------
# Familles de robots, d'après la référence citée dans le message
# --------------------------------------------------------------------------
# Découpage des séries confirmé par le service : filaires de KR100 à KR130,
# RTK première génération en 16x, 17x, 23x et 24x, RTK deuxième génération
# à partir de KR25x.
FAMILLES = [
    (r"kr\s?1[0-3]\d", "filaire"),
    (r"kr\s?(16|17|23|24)\d", "rtk1"),
    (r"kr\s?2[5-9]\d", "rtk2"),
    (r"\brtk\s?2\b|\bseconde generation\b", "rtk2"),
    (r"\brtk\b", "rtk1"),
    (r"\bfilaire\b|\bperimetrique\b", "filaire"),
]

NOM_FAMILLE = {"filaire": "robot filaire", "rtk1": "robot RTK première génération",
               "rtk2": "robot RTK deuxième génération"}


def detecter_famille(texte: str):
    """(famille, référence citée) d'après le message."""
    bas = _norm(texte)
    modele = re.search(r"\bkr\s?\d{3}\s?[a-z]{0,2}\b", bas)
    for motif, famille in FAMILLES:
        if re.search(motif, bas):
            return famille, (modele.group(0).upper().replace(" ", "")
                             if modele else "")
    return "", (modele.group(0).upper().replace(" ", "") if modele else "")


# --------------------------------------------------------------------------
# Symptômes reconnus
# --------------------------------------------------------------------------
SYMPTOMES = [
    {
        "id": "e1",
        "code": "E1",
        "familles": ("filaire",),
        "motifs": ("e1", "fil manquant", "hors limite", "wire missing",
                   "sort de la zone", "depasse la limite"),
        "label": "Fil périmétrique perdu ou robot hors limites",
        "categories": ("Navigation",),
        "causes": [
            ("Câble périmétrique coupé, abîmé ou mal connecté",
             "Contrôler le câble et ses connecteurs (corrosion, coupures) et "
             "mesurer la résistance : 3 à 10 Ω, 13 à 20 Ω sur Mega"),
            ("Station de recharge défaillante",
             "LED de la station : vert clignotant lent = câble défectueux, "
             "rouge = problème de chargeur. Faire une boucle de test"),
            ("Interférences extérieures",
             "Robot voisin, clôture pour chien, canalisations ou câbles "
             "enterrés à proximité"),
            ("Réception du signal défectueuse côté robot",
             "Si la station est déjà mise hors de cause, suspecter la carte "
             "mère du robot"),
        ],
    },
    {
        "id": "e2",
        "code": "E2",
        "familles": ("filaire",),
        "motifs": ("e2", "moteur de roue", "moteurs de roue", "roue bloquee",
                   "wheel blocked"),
        "label": "Moteur de roue bloqué",
        "categories": ("Moteur de roue",),
        "causes": [
            ("Roues encrassées ou entravées",
             "Roues propres, légère résistance à la rotation, rien de desserré"),
            ("Connecteurs ou câbles corrodés entre moteurs et carte mère",
             "Contrôler l'état des connexions"),
            ("Carte mère défectueuse", "Remplacer la carte si le reste est sain"),
        ],
    },
    {
        "id": "rond",
        "code": "",
        "familles": (),
        "motifs": ("tourne en rond", "tournant en rond", "tourne sur lui",
                   "ne roule pas droit", "tourne en cercle", "en rond"),
        "label": "Le robot tourne en rond",
        "categories": ("Moteur de roue",),
        "causes": [
            ("Moteur de roue défectueux ou aimant manquant",
             "Tourner les roues à la main : sans résistance, le moteur est "
             "probablement cassé"),
            ("Câbles des moteurs gauche et droit intervertis",
             "Vérifier que chaque moteur est relié à son connecteur"),
            ("Odométrie faussée",
             "Contrôler les capteurs de roue et leur propreté"),
        ],
    },
    {
        "id": "e3",
        "code": "E3",
        "familles": (),
        "motifs": ("e3", "lame", "moteur de coupe", "disque de coupe",
                   "blade"),
        "label": "Problème de moteur de coupe",
        "categories": ("Moteur de coupe", "Hauteur de coupe"),
        "causes": [
            ("Disque, lames ou arbre encombrés",
             "Nettoyer, puis relever la hauteur de coupe et redescendre "
             "progressivement"),
            ("Herbe trop haute ou trop dense", "Prévoir plus de séances au début"),
        ],
    },
    {
        "id": "e4",
        "code": "E4",
        "familles": (),
        # « bloqué sur le code erreur » ne veut pas dire que le robot est
        # coincé physiquement : on exige un contexte de blocage réel.
        "motifs": ("e4", "piege", "coince", "trapped", "bloque dans",
                   "bloque contre", "bloque sous", "s enlise", "immobilise"),
        "label": "Robot piégé",
        "categories": ("Blocage / échappement",),
        "causes": [
            ("Terrain accidenté ou obstacle",
             "Trous, bosses, passages étroits"),
            ("Couvercle flottant entravé",
             "Vis serrées, aimants en place, jeu correct"),
            ("Capteurs de choc défectueux",
             "Tester dans le menu de diagnostic"),
        ],
    },
    {
        "id": "e5",
        "code": "E5",
        "familles": (),
        "motifs": ("e5", "souleve", "levage", "lifted"),
        "label": "Robot soulevé",
        "categories": ("Sécurité", "Erreur M3"),
        "causes": [
            ("Roues avant entravées",
             "Elles doivent monter et descendre d'environ 1,5 cm"),
            ("Aimant de roue avant manquant ou à l'envers", "Contrôler et remonter"),
        ],
    },
    {
        "id": "e6",
        "code": "E6",
        "familles": (),
        "motifs": ("e6", "a l envers", "renverse", "upside down", "pente"),
        "label": "Robot à l'envers",
        "categories": ("Sécurité",),
        "causes": [
            ("Pente supérieure à 35°", "Limite mécanique de la machine"),
            ("Capteurs de levage défectueux",
             "Menu de diagnostic, entrées : cercles noirs quand le robot est levé"),
        ],
    },
    {
        "id": "charge",
        "code": "E7",
        "familles": (),
        "motifs": ("ne recharge pas", "ne charge pas", "ne se recharge pas",
                   "probleme de charge", "e7", "pas de charge",
                   "ne charge plus", "recharge pas"),
        "label": "La recharge ne se fait pas",
        "categories": ("Batterie",),
        "causes": [
            ("Broches de contact encrassées ou oxydées",
             "Nettoyer celles du robot et de la station"),
            ("Chargeur ou tête de station défectueux",
             "Contrôler la LED de la station, essayer un autre chargeur"),
            ("Batterie en fin de vie",
             "Tester au multimètre : plus de 15 V au repos"),
            ("Carte mère du robot",
             "À suspecter si la station est déjà mise hors de cause"),
        ],
    },
    {
        "id": "charge_allume",
        "code": "",
        "familles": (),
        "motifs": ("que s il est eteint", "s il est eteint", "eteint il charge",
                   "allume il ne charge pas", "quand il est eteint"),
        "label": "Charge uniquement robot éteint",
        "categories": ("Batterie",),
        "causes": [
            ("Consommation interne ou court-circuit qui empêche la charge "
             "quand l'électronique est active",
             "Signe fort d'un défaut de la carte mère ou du circuit de charge "
             "du robot, la station étant capable de charger par ailleurs"),
        ],
    },
    {
        "id": "station",
        "code": "E8",
        "familles": (),
        "motifs": ("ne rentre pas", "ne retrouve pas sa station",
                   "ne trouve pas la station", "e8", "ne rentre plus"),
        "label": "La station n'est pas retrouvée",
        "categories": ("Station de charge", "Bande magnétique"),
        "causes": [
            ("Station mal posée ou pas de niveau", "Vérifier l'assise et la fixation"),
            ("Signal de guidage insuffisant",
             "Sur RTK, valeur de bande magnétique supérieure à 1000"),
        ],
    },
    {
        "id": "pluie",
        "code": "F1",
        "familles": (),
        "motifs": ("pluie", "f1", "capteur de pluie"),
        "label": "Report de tonte pour pluie",
        "categories": ("Capteur de pluie",),
        "causes": [
            ("Délai de pluie en cours", "180 min par défaut après séchage"),
            ("Capteur encrassé ou humide",
             "Nettoyer à la brosse fine ; valeur normale 4095 à sec"),
        ],
    },
    {
        "id": "zone",
        "code": "",
        "familles": ("rtk1", "rtk2"),
        "motifs": ("zone inaccessible", "ne va pas dans", "n atteint pas",
                   "passage trop etroit", "ne passe pas", "chemin"),
        "label": "Une zone n'est pas atteinte",
        "categories": ("Navigation",),
        "causes": [
            ("Passage trop étroit ou absent",
             "Au moins 1,2 m de large, 1,5 m recommandé, et 15 m au maximum"),
            ("Chemin entre zones non créé",
             "Créer un chemin zone à zone, ou vers la station"),
            ("Signal insuffisant dans le passage",
             "Pas plus de 1 à 2 m sans signal RTK"),
        ],
    },
    {
        "id": "carte",
        "code": "",
        "familles": ("rtk1", "rtk2"),
        "motifs": ("erreur de carte", "carte ne se telecharge", "map error",
                   "carte ne se charge", "probleme de carte",
                   "recartographier", "cartographie"),
        "label": "Problème de carte",
        "categories": ("Carte", "Cartographie"),
        "causes": [
            ("RTK non attribué à la carte",
             "Carte active, Détails, Station de recharge, Modifier, RTK"),
            ("Mauvaise couverture pendant la cartographie",
             "Refaire la carte en évitant les zones d'ombre"),
            ("Liaison tête / robot",
             "Vérifier la connexion physique et dans le portail"),
        ],
    },
    {
        "id": "batterie",
        "code": "",
        "familles": (),
        "motifs": ("batterie", "ne tient pas la charge", "autonomie",
                   "se decharge", "s eteint tout seul"),
        "label": "Problème de batterie",
        "categories": ("Batterie",),
        "causes": [
            ("Batterie en fin de vie",
             "Tester au multimètre : plus de 15 V et plus de 0 A. "
             "Sur RTK, remplacer si le SOH est sous 10 %"),
            ("Contacts encrassés ou corrodés",
             "Nettoyer les broches du robot et de la station"),
            ("Température hors plage",
             "Entre 0 et 40 °C sur RTK, 0 et 50 °C sur filaire"),
        ],
    },
    {
        "id": "irregulier",
        "code": "",
        "familles": (),
        "motifs": ("tond mal", "coupe irreguliere", "manque des zones",
                   "oublie des zones", "ne tond pas partout", "herbe haute",
                   "coupe inegale"),
        "label": "Tonte irrégulière ou zones oubliées",
        "categories": ("Tonte", "Moteur de coupe"),
        "causes": [
            ("Fréquence de tonte insuffisante",
             "Régler une fois par jour ou tous les deux jours"),
            ("Lames émoussées", "Les remplacer"),
            ("Cartographie perfectible",
             "Zones interdites trop proches du bord, passages sous 1,5 m"),
        ],
    },
    {
        "id": "position",
        "code": "",
        "familles": ("rtk1", "rtk2"),
        "motifs": ("pas de position", "perd le signal", "signal rtk",
                   "gps", "ne se localise pas", "rtk flottant"),
        "label": "Perte de position RTK",
        "categories": ("Signal RTK", "Localisation"),
        "causes": [
            ("Zone d'ombre, arbres ou bâtiments", "Dégager la vue du ciel"),
            ("Couverture 4G insuffisante", "Sans 4G, pas de correction RTK"),
            ("Calibration ou carte à refaire", "Voir les erreurs de calibration"),
        ],
    },
]

# Tournures indiquant qu'une piste a déjà été traitée ou écartée
_RE_DEJA_FAIT = re.compile(
    r"j ?ai (?:deja )?(remplace|change|nettoye|teste|essaye|verifie|refait)"
    r"[^.;\n]{0,60}", re.I)
_RE_ECARTE = re.compile(
    r"(?:autre|second|deuxieme) (?:robot|tondeuse|batterie|chargeur|station|tete)"
    r"[^.;\n]{0,80}(?:pas de (?:souci|probleme)|fonctionne|marche|ok|rien)"
    r"|(?:pas de (?:souci|probleme)|fonctionne bien|marche bien)"
    r"[^.;\n]{0,40}(?:autre|meme) (?:robot|station|chargeur)", re.I)


def _deja_traite(texte_norm: str) -> list:
    faits = [m.group(0).strip() for m in _RE_DEJA_FAIT.finditer(texte_norm)]
    faits += [m.group(0).strip() for m in _RE_ECARTE.finditer(texte_norm)]
    # une même phrase est souvent captée par les deux expressions
    uniques = []
    for f in faits:
        if not any(f in autre or autre in f for autre in uniques):
            uniques.append(f)
    return uniques


def _mise_hors_cause(texte_norm: str) -> set:
    """Éléments que le client a testés et qui fonctionnent."""
    hors = set()
    if _RE_ECARTE.search(texte_norm):
        if re.search(r"station|chargeur|base", texte_norm):
            hors.add("station")
    if re.search(r"j ?ai (?:deja )?(?:remplace|change)[^.;\n]{0,40}"
                 r"(moteur|roue)", texte_norm):
        hors.add("moteurs de roue")
    if re.search(r"j ?ai (?:deja )?(?:remplace|change)[^.;\n]{0,40}batterie",
                 texte_norm):
        hors.add("batterie")
    return hors


# Mots trop courants pour distinguer un cas d'un autre
_VIDES = {"robot", "tondeuse", "client", "probleme", "solution", "cause",
          "pour", "dans", "avec", "elle", "cette", "être", "etre", "faire",
          "plus", "pas", "que", "qui", "sur", "les", "des", "une", "est",
          "son", "sa", "ses", "vous", "nous", "leur", "bien", "tout", "tous",
          "peut", "faut", "voir", "puis", "mais", "par", "aux", "car"}


def _mots(texte: str) -> set:
    return {m for m in re.findall(r"[a-z0-9]{3,}", _norm(texte))
            if m not in _VIDES}


def chercher_cas(message: str, famille: str = "", limite: int = 5) -> list:
    """Cas de la base de dépannage qui ressemblent le plus au message.

    Comparaison par mots communs : la base est écrite dans la langue des
    techniciens, la même que celle des clients, ce qui suffit à rapprocher
    « il ne rentre pas à sa base » de « Robot ne rentre pas à sa base ».
    """
    try:
        from base_aide import CAS
    except ImportError:
        return []

    mots_message = _mots(message)
    if not mots_message:
        return []

    resultats = []
    for titre, fam, corps in CAS:
        if fam and famille and fam != famille:
            continue
        # Le document source colle parfois les mots (« Lesultrasonsne
        # fonctionnentplus ») : on cherche donc le mot dans la chaîne plutôt
        # que dans une liste de jetons.
        titre_norm, corps_norm = _norm(titre), _norm(corps)
        communs_titre = {m for m in mots_message if m in titre_norm}
        communs_corps = {m for m in mots_message if m in corps_norm}
        # le titre décrit le symptôme : il pèse davantage que le corps
        score = 3 * len(communs_titre) + len(communs_corps)
        if fam == famille and famille:
            score += 1
        # au moins deux mots du symptôme en commun : en dessous, on remonte
        # des cas sans rapport
        if score >= 6 and len(communs_titre) >= 2:
            resultats.append((score, titre, fam, corps))
    resultats.sort(key=lambda r: -r[0])
    return resultats[:limite]


def analyser(message: str, session=None, events=None) -> dict:
    """Diagnostic structuré. `events` : incidents regroupés du diagnostic."""
    bas = _norm(message)
    famille, reference = detecter_famille(message)

    releves = []
    for s in SYMPTOMES:
        # « familles » vide = symptôme commun à toutes les gammes
        limites = s.get("familles") or ()
        if limites and famille and famille not in limites:
            continue
        if any(m in bas for m in s["motifs"]):
            releves.append(dict(s))

    # ce que les journaux disent de chaque symptôme
    par_categorie = {}
    for e in (events or []):
        diag = e[2]
        if diag["severity"] == "info":
            continue
        par_categorie[diag["category"]] = par_categorie.get(diag["category"], 0) + e[3]

    for s in releves:
        s_total = sum(par_categorie.get(c, 0) for c in s["categories"])
        s["_journaux"] = s_total

    hors_cause = _mise_hors_cause(bas)
    return {
        "famille": famille,
        "reference": reference,
        "symptomes": releves,
        "cas_similaires": chercher_cas(message, famille),
        "deja_fait": _deja_traite(bas),
        "hors_cause": hors_cause,
        "journaux_charges": bool(session and session.lines),
        "categories_journaux": par_categorie,
    }


def rediger(rapport: dict) -> str:
    """Met le diagnostic en forme, en HTML pour l'affichage."""
    out = []
    ref = rapport["reference"]
    famille = NOM_FAMILLE.get(rapport["famille"], "")
    entete = " — ".join(x for x in (ref, famille) if x)
    if entete:
        out.append(f"<p><b>Machine :</b> {entete}</p>")
    elif rapport["symptomes"]:
        out.append("<p style='color:#b36b00'>Modèle non identifié : précisez "
                   "la référence (KR101E, KR172E, KR260ES…) pour un "
                   "diagnostic adapté à la gamme.</p>")

    if not rapport["symptomes"] and not rapport.get("cas_similaires"):
        out.append("<p>Aucun symptôme connu n'a été reconnu dans ce message. "
                   "Reformulez-le ou citez le code d'erreur affiché.</p>")
        return "".join(out)

    if rapport["deja_fait"]:
        out.append("<p><b>Déjà fait par le client</b> (à ne pas redemander) :<ul>")
        for f in rapport["deja_fait"]:
            out.append(f"<li>{f}</li>")
        out.append("</ul></p>")

    out.append("<p><b>Symptômes relevés</b></p>")
    for s in rapport["symptomes"]:
        # les codes E ne valent que pour les robots filaires : les afficher
        # sur un RTK induirait le technicien en erreur
        code = s.get("code") if rapport["famille"] == "filaire" else ""
        titre = f"{code} — {s['label']}" if code else s["label"]
        out.append(f"<p style='margin-top:8px'><b>{titre}</b>")
        if rapport["journaux_charges"]:
            n = s.get("_journaux", 0)
            if n:
                out.append(f"<br><span style='color:#c62828'>Confirmé par les "
                           f"journaux : {n} occurrence(s).</span>")
            else:
                out.append("<br><span style='color:#b36b00'>Rien dans les "
                           "journaux chargés sur ce point — vérifier que la "
                           "période couvre l'incident.</span>")
        out.append("<ul>")
        for cause, action in s["causes"]:
            marque = ""
            for elt in rapport["hors_cause"]:
                if elt.split()[0][:5] in _norm(cause):
                    marque = (" <i style='color:#2e7d32'>— déjà écarté par le "
                              "client</i>")
            out.append(f"<li>{cause}{marque}<br>"
                       f"<span style='color:#555'>{action}</span></li>")
        out.append("</ul></p>")

    if rapport["hors_cause"]:
        out.append("<p><b>Pistes écartées par les essais du client :</b> "
                   + ", ".join(sorted(rapport["hors_cause"])) + ".</p>")

    cas = rapport.get("cas_similaires") or []
    if cas:
        out.append("<p style='margin-top:10px'><b>Cas déjà résolus dans la "
                   "base de dépannage</b></p><ul>")
        for _score, titre, fam, corps in cas:
            marque = f" <i>({NOM_FAMILLE.get(fam, fam)})</i>" if fam else ""
            out.append(f"<li><b>{titre}</b>{marque}<br>"
                       f"<span style='color:#555'>{corps}</span></li>")
        out.append("</ul>")

    out.append(_conclusion(rapport))
    return "".join(out)


def _conclusion(rapport: dict) -> str:
    """Synthèse : ce qui reste quand on retire ce qui a été écarté."""
    ids = {s["id"] for s in rapport["symptomes"]}
    hors = rapport["hors_cause"]
    texte = ""

    if "charge_allume" in ids:
        texte = ("La machine se recharge éteinte mais pas allumée : le "
                 "problème vient du robot, pas de la station. Un défaut du "
                 "circuit de charge ou de la carte mère est de loin le plus "
                 "probable.")
    elif "e1" in ids and "station" in hors:
        texte = ("Le code E1 persiste alors que la station a été mise hors de "
                 "cause par substitution : la réception du signal de fil côté "
                 "robot devient la piste principale, donc la carte mère.")
    elif "e1" in ids:
        texte = ("Commencer par départager le câble et la station : une boucle "
                 "de test tranche en quelques minutes.")
    elif ids:
        texte = ("Reprendre les causes ci-dessus dans l'ordre, en sautant ce "
                 "que le client a déjà contrôlé.")

    if "charge_allume" in ids and "e1" in ids:
        texte = ("Deux symptômes convergent vers la carte mère du robot : le "
                 "code E1 malgré une station saine, et une charge qui ne "
                 "fonctionne qu'appareil éteint. Contrôler la carte mère "
                 "(corrosion, traces de brûlure) avant toute autre pièce.")

    if not texte:
        return ""
    return (f"<p style='margin-top:10px; padding:6px; background:#eef4ff'>"
            f"<b>Conclusion :</b> {texte}</p>")
