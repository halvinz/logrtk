# Robot Log Viewer

Visionneuse de logs pour robot (testé avec des exports de tondeuse Positec/Kress,
modèle KR172EA, mais fonctionne avec tout robot qui produit des logs au même
format `AAAA-MM-JJ HH:MM:SS.mmm:[NIVEAU] message`).

## Ce que ça fait

- Ouvre un dossier de logs exporté par le robot (le `.tar` que le robot génère)
  et détecte automatiquement les fichiers `..._MODEL.log`, `..._pos.log`,
  `..._boot.log`, `dmesg.txt`.
- Affiche les informations du robot (modèle, série, firmware, statistiques
  d'utilisation, planning).
- Reconstruit et affiche le trajet du robot à partir des positions SLAM
  loguées (dégradé bleu → rouge dans le temps, comme l'outil d'origine).
- Filtre par plage de dates.
- Visionneuse de logs avec recherche texte, filtre par niveau (INFO/WARN/ERROR)
  et par fichier source.

## Limite connue

Les fichiers `.plm` (carte / zones tondues) utilisés par l'outil d'origine
sont dans un format binaire propriétaire non documenté et ne sont pas
décodés ici — le fond de carte (zone verte tondue) n'est donc pas reproduit.
Le trajet du robot (le tracé bleu/rouge, la donnée la plus utile pour le
diagnostic) est lui entièrement reconstruit à partir des logs texte.

## Condensé pour analyse par IA (`digest.py`)

Un export de tondeuse fait de 40 000 à 800 000 lignes : aucun modèle de
langage n'analyse correctement ça. Le condensé en tire un résumé Markdown
de quelques pages, à coller dans ChatGPT, Claude ou tout autre assistant.

**Depuis l'application** (donc depuis le .exe) : onglet **Diagnostic**,
bouton **« Copier le condensé IA »** — le texte part dans le presse-papier,
il n'y a plus qu'à le coller dans l'assistant. Le bouton
« Enregistrer… » à côté produit le même contenu en fichier `.md`.

**En ligne de commande** :

```
python app/digest.py "C:\chemin\vers\LOGTOOL" -o condense.md
python app/digest.py export.zip --max-chars 20000
```

Il accepte les trois formats (dossier RTK1, dossier ou archive RTK2, page
HTML filaire) et **ne dépend que de la bibliothèque standard** : il tourne
avec un simple `python`, sans installer numpy ni PySide6.

Le condensé contient l'identité du robot, ce qu'il a fait, sa conclusion
locale, les incidents **regroupés en épisodes** (un message répété mille
fois est un incident, pas mille), un échantillon de lignes brutes par
incident, la chronologie des états — et surtout la liste des **erreurs que
la base de diagnostic ne reconnaît pas**, qui est précisément ce sur quoi
une analyse externe apporte quelque chose.

Ordres de grandeur constatés : 322 000 lignes → 11 000 caractères,
770 000 lignes → 12 000 caractères.

## Utilisation en développement (sans compiler)

```
pip install -r requirements.txt
python app/main.py
```

## Générer le .exe Windows

Sur une machine Windows avec Python 3.10+ installé (python.org, cocher
"Add python.exe to PATH" à l'installation) :

1. Copier tout le dossier `RobotLogViewer` sur la machine Windows.
2. Double-cliquer sur `build_exe.bat`.
3. Le fichier `RobotLogViewer.exe` apparaît dans `dist\`. Il est autonome,
   pas besoin de réinstaller Python pour l'utiliser ensuite.

## Structure du projet

```
RobotLogViewer/
├── app/
│   ├── main.py       point d'entrée
│   ├── gui.py        interface (PySide6 + matplotlib)
│   ├── parser.py     analyse des fichiers de logs (RTK1)
│   ├── rtk2.py       exports RTK2, wired.py  robots filaires
│   ├── logbible.py   base de diagnostic (message → cause)
│   ├── summary.py    résumé de comportement et conclusion
│   ├── states.py     machine à états (sans numpy, partagée avec digest)
│   └── digest.py     condensé Markdown pour analyse par IA (CLI)
├── requirements.txt
├── build_exe.bat      script de build Windows en un clic
└── README.md
```
