# Architecture et plan d'implémentation

## Étape 1 — décisions

Le dépôt initial était vide. Le moteur et sa CLI ont précédé l'interface graphique.

| Module | Responsabilité |
| --- | --- |
| `manifest.py` | Format public, validation stricte, portabilité des chemins, sérialisation canonique |
| `io.py` | Streaming, stabilité des fichiers, parcours sans liens, publication exclusive, verrou |
| `engine.py` | Analyse, générations, identité des supports, plans, copie, contrôle, reprise, rapports |
| `control.py` | Pause, annulation et événements de progression sans dépendance Qt |
| `cli.py` | Choix utilisateur, plan affiché, autorisation explicite d'exécuter, exports |
| `gui.py` | Assistants Qt, worker QThread, catalogue local et présentation des rapports |
| `tools/verify_manifest.py` | Lecteur de secours autonome, bibliothèque standard uniquement |

Pas de SQLite : les journaux et rapports JSON persistants suffisent.
Un futur cache/catalogue local ne deviendra jamais une preuve d'intégrité.
L'autorité de référence est le manifeste indépendant attendu et chaque fichier effectivement relu.

## Invariants de sécurité

1. Le moteur n'ouvre jamais une source en écriture. Aucun fichier source ou archivé n'est supprimé.
2. Tout fichier définitif est publié avec une primitive refusant une destination préexistante.
3. Les fichiers existants sont relus intégralement ; la taille/date ne justifie aucun statut conforme.
4. Une copie temporaire est synchronisée puis relue avant publication ; tous les définitifs
   sont relus avant installation de la génération sur le support.
5. Aucune génération ne remplace un ancien manifeste ni une ancienne version du contenu.
6. Un marqueur du support correspondant à l'archive ET à l'UUID attendu précède les écritures
   ordinaires. L'initialisation explicite est limitée à un dossier vide dédié.
7. Les chemins provenant de JSON sont relatifs, validés, sans `..`, backslash, lecteur ou
   composant dangereux ; ils respectent le namespace réservé au contenu `data`.
8. Aucun lien symbolique, jonction, reparse point, socket, FIFO ou périphérique n'est suivi.
9. Toute interruption laisse une opération incomplète et/ou un bilan `not_verified`.
10. Une vérification immédiate ne prouve jamais une reconnexion ou une persistance matérielle.

## Copie et durabilité

Le plan lit les existants et les sources nécessaires, calcule le volume à copier, vérifie
la filiation et applique une marge d'espace libre (maximum de 16 Mio et 1 % des octets à copier).
Il ne peut pas réserver l'espace contre d'autres applications.

Après validation de l'identité et prise d'un verrou système consultatif : journal de départ
persistant, ouverture exclusive d'un temporaire **dans le dossier du fichier final**, copie
en blocs, empreinte du flux source, contrôle de stabilité du fichier, `flush` et `fsync`,
fermeture, nouvelle lecture du temporaire, comparaison SHA-256, puis renommage exclusif.
La racine est suivie par son couple périphérique/inode ; son remplacement provoque
un échec, et la relecture de fin de copie réutilise l'arbre déjà ouvert.
La publication utilise `renameatx_np(RENAME_EXCL)` sur macOS,
`renameat2(RENAME_NOREPLACE)` sur Linux et `os.rename` sous Windows.
Si la primitive ou son support par le système de fichiers est absent, l'opération s'arrête
en conservant le temporaire. Aucun repli vers un renommage écrasant ou un fichier final partiel.

Sous POSIX, une tentative de synchronisation des dossiers est faite. Une impossibilité
connue de synchroniser un dossier ne transforme pas `fsync` en garantie universelle.
Les nouveaux fichiers sont ensuite tous relus à leur nom définitif. Le rapport immédiat
et le manifeste sont publiés ; un événement de fin clôt le journal. Une coupure entre ces
étapes entraîne une reprise et un nouveau contrôle, pas une validation déduite du journal.
Les métadonnées JSON sont également relues avant et après leur publication.

Un contrôleur USB ou un HDD peut différer une écriture malgré les appels système.
exFAT ne dispose pas de journalisation ; des pertes de métadonnées sont possibles après
arrêt brutal. Les résultats décrivent uniquement les octets observés pendant le contrôle.

## Reprise

Les verrous consultatifs sont libérés par l'OS après crash. Le fichier de verrou peut
rester et ne représente pas à lui seul une opération en cours. Chaque opération possède
un UUID et une paire d'événements `.start.json` / `.finish.json`.

Une reprise est explicite et exige exactement le manifeste de l'opération interrompue.
Elle déplace les temporaires listés par un journal valide
vers le dossier réservé `.coldvault/residuals` sans les effacer. Elle ne fait pas confiance
aux checkpoints : elle relit tous les fichiers définitifs, conserve les conformes,
recopie seulement les absents et bloque devant un existant différent.
Les écritures d'erreur peuvent elles-mêmes échouer (disque débranché/plein) ; la
présence du journal de départ suffit alors à empêcher une reprise silencieuse.

## Mise à jour et supports

Les alias identifient des racines logiques indépendantes des chemins absolus. Chaque
modification crée une entrée dans une nouvelle génération ; les entrées historiques
restent dans le manifeste cumulatif. Les chemins sont `data/NNNNNN/ALIAS/relatif`.
Le SHA-256 du manifeste parent lie les générations. Un support ne reçoit pas une branche
divergente ou une génération précédente en écrasant son état.

Chaque HDD a son UUID logique, son nom et son archive dans `.coldvault/media.json`.
Les rapports portent UUID, génération, dates UTC, contexte et résultats. La dernière
génération contrôlée, la dernière copie et les contrôles complets se déduisent de cet
historique. Un catalogue JSON local conserve les associations et derniers états pour
l'accueil Qt. Le nom du support est choisi à son initialisation ; une interface de
renommage ultérieur du marqueur reste à construire.
Un marqueur cloné ne distingue pas matériellement deux disques : conserver un inventaire
indépendant et associer des noms physiques aux supports.

## Difficultés multiplateformes

- macOS : normalisation Unicode possible, casse souvent insensible, autorisations et
  caches externes ; noms Unicode conservés tels qu'observés, collisions NFC/casefold bloquées.
- Windows : caractères/noms réservés, lecteurs variables, jonctions et limites de chemin.
  Les reparse points sont refusés. La résistance aux remplacements concurrents hostiles
  nécessite ultérieurement des handles natifs Windows avec une gestion plus stricte.
  Les signatures de stabilité sont comparées exclusivement via `fstat`, y compris après
  réouverture sûre du chemin courant : `stat(path)` et `fstat(fd)` peuvent donner des
  significations différentes à `st_ctime_ns` ([CPython #157671](https://github.com/python/cpython/issues/157671)).
  Les contrôles de changement de contenu et d'identité restent actifs pendant lecture et copie.
- Linux : différences de systèmes de fichiers et disponibilité de `renameat2` ; échec sûr
  si la publication exclusive n'est pas disponible.
- exFAT : variations de tables de casse, horodatages et absence de journal ; les dates ne
  participent pas à une décision d'intégrité. Une compatibilité parfaite des noms n'est pas promise.
- Toutes plateformes : aucune garantie de cohérence globale entre fichiers sans snapshot.
  Garder les sources au repos ; les changements pendant un flux sont détectés par taille,
  dates haute résolution, inode et empreinte. Une mutation hostile restaurant les métadonnées
  dépasse le modèle de menace de ce jalon.

Références : [API Python `os`](https://docs.python.org/3.12/library/os.html),
[noms Windows](https://learn.microsoft.com/en-us/windows/win32/fileio/naming-a-file),
[rename Linux](https://man7.org/linux/man-pages/man2/rename.2.html),
[rename macOS](https://developer.apple.com/library/archive/documentation/System/Conceptual/ManPages_iPhoneOS/man2/rename.2.html).

## Jalons

1. **Architecture et format** : décisions ci-dessus, format v1 documenté.
2. **Moteur et CLI** : création, copie contrôlée, vérification indépendante, mises à jour
   additives, journaux et reprise. Premier périmètre livré.
3. **Tests** : fautes injectées et intégrations sur dossiers temporaires, CI trois OS.
   Un test réel non destructif sur un support exFAT dédié reste à faire manuellement.
4. **PySide6** : catalogue français et assistants fournis ; QThread/worker pour toute E/S,
   signaux transportant `Progress`, pause/annulation via `Control`. Aucun accès disque long
   sur le thread Qt. Parcours sources → analyse → support → plan → copie → contrôle,
   puis reconnexion déclarée et nouveau contrôle avec export du rapport.
   Ne déclarer une copie immédiatement contrôlée qu'après `Report.ok`, et une reconnexion
   que comme déclaration utilisateur tant qu'aucune preuve système suffisante n'existe.
5. **Distribution** : spec PyInstaller native et workflow manuel trois OS fournis.
   Build macOS ARM64 exécuté localement ; Windows/Linux doivent être validés par CI et
   sur machines cibles. Signature/notarisation non configurées.

Les tests Qt couvrent le worker, les plans bloqués, puis création → copie → vérification
avec reconnexion déclarée → mise à jour. Un échec de worker suivi d'une autre opération
a révélé un problème de cycle de vie natif : les connexions sont maintenant débranchées,
les références de traceback rompues et les dialogues détruits par `deleteLater` dans
le thread Qt. Les tests libèrent les widgets avant l'application ; ce cas est conservé
comme régression, avec contrôle du code de sortie du processus de test.

Les fonctions avancées (réplication HDD→HDD, cache SQLite, intégration d'éjection, suivi
matériel, réparation assistée) attendent la validation du moteur sur exFAT et de l'ergonomie.
