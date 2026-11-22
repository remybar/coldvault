# ColdVault

Gestionnaire d'archives froides, libre, hors ligne. **Version initiale 0.1**.
Il prépare des manifestes SHA-256, copie sans écraser, relit les copies et conserve
toutes les générations. Une interface PySide6 en français et une CLI utilisent le
même moteur. Les opérations disque s'exécutent hors du thread graphique.

Python ≥ 3.12. Aucune dépendance d'exécution du moteur. Aucun compte cloud, base de
données propriétaire, compression ou conteneur requis pour récupérer les fichiers.
Les tests locaux utilisent uniquement des dossiers temporaires, jamais de vrais HDD.
Cette version doit encore être éprouvée sur des supports exFAT de test avant utilisation
pour des archives irremplaçables.

## Installer et tester

Installer [uv](https://docs.astral.sh/uv/getting-started/installation/), puis :

```sh
uv sync --locked --group dev
uv run coldvault --help
uv run pytest
uv run ruff check .
uv run ruff format --check .
```

Pour l'interface et les tests Qt :

```sh
uv sync --locked --extra gui --group dev
uv run --extra gui coldvault-gui
QT_QPA_PLATFORM=offscreen uv run --extra gui pytest
```

La dernière commande utilise la syntaxe macOS/Linux ; sous PowerShell, définir d'abord
`$env:QT_QPA_PLATFORM = 'offscreen'`. Sans l'extra `gui`, seuls les tests Qt sont ignorés.
Seul le composant officiel **PySide6-Essentials** est installé : QtCore, QtGui et QtWidgets,
sans QtWebEngine ni les modules supplémentaires inutilisés.

Un bundle macOS ARM64 local est générable avec le [packaging natif](docs/distribution.md).
Dans le dépôt de travail, `dist/ColdVault.app` peut être ouvert depuis le Finder après
le build. Les builds Windows/Linux doivent être produits et testés sur leurs OS respectifs.

## Utilisation graphique

1. **Créer une archive** : ajouter plusieurs dossiers et leurs noms logiques, choisir
   un nouveau fichier manifeste indépendant, puis laisser calculer les SHA-256.
2. **Copier sur un disque** : confirmer les sources, sélectionner un support connu ou
   initialiser explicitement un dossier vide sur le HDD, examiner le plan, puis copier.
3. **Vérifier un disque** : sélectionner son dossier et exporter le rapport. Après
   éjection/rebranchement effectués avec le système, cocher la déclaration correspondante.
4. **Mettre à jour** : préparer une génération additive puis refaire la copie et le contrôle
   sur chaque support. Les anciennes versions sont conservées automatiquement.
5. **Exporter** : copier le manifeste courant et les derniers rapports connus dans un
   dossier indépendant. L'historique complet reste dans `.coldvault/reports` sur les supports.

Le panneau d'accueil présente les derniers états connus, pas une attestation actuelle
de connexion ou d'intégrité. Un catalogue JSON local mémorise chemins sources, manifestes,
UUID des supports et derniers rapports dans le dossier de données utilisateur choisi par Qt.
Sa perte n'empêche pas d'ouvrir un manifeste et de vérifier une archive sans sources.
Pour déplacer un support vers une autre lettre/chemin, sélectionner le support **connu**
puis choisir son nouveau dossier ; l'UUID attendu reste identique et sera contrôlé.

## Parcours complet

Créer préalablement un dossier local `exports` et un **dossier vide dédié** sur chaque HDD.
Les exemples de chemins sont à adapter ; Windows accepte les mêmes options avec
des chemins tels que `E:\Famille` et `C:\Users\...`.

```sh
uv run coldvault create --name Famille \
  --source 'Photos=/Users/moi/Pictures/Archives' \
  --source 'Documents=/Users/moi/Documents/Archives' \
  --output exports/famille-g1.json

uv run coldvault media-init --manifest exports/famille-g1.json \
  --destination /Volumes/HDD-A/Famille --label 'Chez les beaux-parents'
```

L'initialisation retourne un `media_id` à conserver dans votre inventaire indépendant.
Elle ne partitionne ni ne formate le disque. Une archive logique peut être associée
à plusieurs supports, initialisés séparément avec des UUID distincts.
Ce marqueur est une identité **logique**, copiable, sans attestation matérielle.
Ne pas cloner le dossier `.coldvault` pour créer un deuxième support.

```sh
# Lecture seule : affiche le plan, les volumes, l'espace libre et les blocages.
uv run coldvault copy --manifest exports/famille-g1.json \
  --source 'Photos=/Users/moi/Pictures/Archives' \
  --source 'Documents=/Users/moi/Documents/Archives' \
  --destination /Volumes/HDD-A/Famille --media-id UUID-A

# Même commande, avec --execute pour effectuer le plan et le contrôle intégral.
uv run coldvault copy --manifest exports/famille-g1.json \
  --source 'Photos=/Users/moi/Pictures/Archives' \
  --source 'Documents=/Users/moi/Documents/Archives' \
  --destination /Volumes/HDD-A/Famille --media-id UUID-A --execute
```

Le résultat signifie **vérification immédiate terminée, reconnexion encore à effectuer**.
Éjecter avec le système d'exploitation, débrancher, rebrancher, puis :

```sh
uv run coldvault verify --manifest exports/famille-g1.json \
  --destination /Volumes/HDD-A/Famille --media-id UUID-A \
  --after-reconnection --record-on-media --report exports/controle-a-g1.json
```

`--after-reconnection` est une déclaration explicite de l'utilisateur, enregistrée comme
`user_declaration`. ColdVault ne mesure pas une reconnexion physique.
Sans cette option, le rapport porte le contexte `standalone`.
Sans `--record-on-media`, la vérification n'écrit rien sur le HDD.
Répéter l'initialisation, la copie et la vérification pour le HDD B avec son propre UUID.

Les fichiers de `exports` sont indépendants du disque source et peuvent être copiés
manuellement sur Google Drive ou ailleurs. Aucun écrasement d'export n'est autorisé.
Les rapports contiennent des noms de fichiers et des erreurs d'E/S : choisir leur lieu
de stockage en conséquence. Aucun contenu de fichier ni identifiant de compte n'est journalisé.

## Vérifier des années plus tard

Les sources ne sont pas nécessaires :

```sh
uv run coldvault verify --manifest exports/famille-g1.json \
  --destination /Volumes/HDD-A/Famille --report exports/controle-2030.json
```

Le manifeste est également disponible dans `.coldvault/manifests` sur chaque support
ayant terminé sa copie. Utiliser la dernière génération attendue, conservée indépendamment.
Un manifeste décrit un état, il n'authentifie pas cryptographiquement son auteur.
La perte du marqueur n'empêche pas le contrôle en lecture seule avec un manifeste indépendant.

Statuts des fichiers : `verified`, `missing`, `size_mismatch`, `hash_mismatch`,
`io_error`, `extra`, `temporary`, `not_verified`. Une empreinte différente indique une
non-conformité ; sa cause n'est pas déduite. Un fichier supplémentaire ou un temporaire
résiduel rend le bilan global non conforme. `.coldvault` est réservé aux métadonnées
et n'est pas compté comme contenu supplémentaire.

Codes de sortie : 0 contrôle conforme / préparation réussie ; 1 contrôle non conforme ;
2 opération impossible ; 130 interruption. Le plan seul ne constitue pas une vérification finale.

## Ajouter des fichiers et des versions

```sh
uv run coldvault update --manifest exports/famille-g1.json \
  --source 'Photos=/Users/moi/Pictures/Archives' \
  --source 'Documents=/Users/moi/Documents/Archives' \
  --output exports/famille-g2.json
```

Toutes les sources courantes sont relues par SHA-256. Conserver les mêmes alias ; une
racine devenue vide doit rester fournie. La commande affiche les nouveaux fichiers,
modifiés, inchangés et absents des sources. **Elle prépare un candidat**, sans déclarer
la mise à jour terminée. Examiner ce candidat, puis utiliser `copy` avec ce manifeste,
son plan et `--execute` sur chaque support, et refaire la procédure de reconnexion.
Un fichier inchangé garde son chemin antérieur. Une modification crée un nouveau chemin
dans `data/000002/...`. Les versions anciennes et fichiers disparus des sources restent référencés.
Le manifeste candidat est installé sur le disque uniquement après contrôle complet.

La copie exige la génération parente sur le support ; conserver et appliquer les
générations dans l'ordre. Copier une génération récente vers un nouveau disque avec
des versions historiques absentes des sources n'est pas encore pris en charge : il
faudra un mode de réplication entre supports. Ne pas modifier les manifestes pour contourner ce contrôle.

## Interruption et reprise

Ctrl+C demande une annulation coopérative entre blocs de 1 Mio. Une E/S bloquée par
l'OS peut retarder sa prise en compte. L'API Python propose aussi pause/reprise via `Control`.
Après arrêt, relancer le plan puis `copy ... --execute --resume`.
La reprise conserve les temporaires connus dans `.coldvault/residuals`, sans supprimer
leur contenu. Elle recalcule les empreintes des fichiers définitifs et recopie les
fichiers absents. Elle ne reprend pas un flux au milieu d'un fichier.
Les temporaires inconnus sont signalés ; leur déplacement relève d'une décision manuelle.
Un fichier définitif non conforme bloque la copie : le conserver ailleurs pour enquête
avant une nouvelle tentative, sans écrasement automatique.

Les journaux sont dans `.coldvault/operations`, les rapports dans `.coldvault/reports`.
Une erreur d'écriture peut empêcher l'enregistrement du rapport ; le journal de départ
et les temporaires restent à examiner. Aucune suppression automatique de fichier archivé ou source.

## Restauration et contrôle manuel sans ColdVault

Ouvrir le dossier `data` avec un gestionnaire de fichiers. Chaque génération contient
ses ajouts/versions, dans les sous-dossiers correspondant aux alias sources. Une version
plus récente d'un même `logical_path` possède la plus grande `revision`. Les fichiers
supprimés des sources sont volontairement conservés ; il n'existe pas de restauration
automatique d'un « miroir courant » dans cette version.

Pour vérifier un manifeste **de confiance** avec uniquement Python standard :

```sh
python3 tools/verify_manifest.py /chemin/manifeste.json /chemin/Famille
```

Le script autonome [tools/verify_manifest.py](tools/verify_manifest.py) ne dépend pas du
paquet ColdVault. Il lit en streaming, vérifie les tailles et SHA-256, refuse les
traversées et liens, et signale les fichiers supplémentaires. Pour Windows, utiliser `python`.
Conserver ce script et la [spécification publique](docs/manifest-v1.md) avec les exports.

## Limites de ce jalon

- Dossiers vides, liens symboliques, jonctions, fichiers spéciaux, ACL, attributs étendus,
  resource forks et autres flux Windows ne sont pas archivés. Les liens/fichiers spéciaux
  bloquent explicitement la préparation ; les dossiers vides sont omis.
- Noms Windows réservés, collisions casse/Unicode et chemins trop longs sont refusés,
  sans renommage automatique. La politique est conservatrice et ne modélise pas toutes
  les tables de casse exFAT ; une collision réelle supplémentaire échoue sans écrasement.
- Sous Windows, ne pas laisser un autre programme modifier l'arbre pendant l'opération.
  Les contrôles de jonctions ne garantissent pas la résistance à un adversaire local
  remplaçant simultanément les dossiers. Sous POSIX, les E/S sont ancrées par descripteurs.
- Les sources doivent être au repos. Ce logiciel ne réalise pas de snapshot du système de fichiers.
- `fsync`, renommage et relecture immédiate ne constituent pas une garantie contre une
  perte d'alimentation, un cache matériel non fiable ou une défaillance USB/exFAT.
  Conserver plusieurs copies, éjecter proprement et vérifier après reconnexion.
- Le catalogue est local et limité aux derniers rapports ; il ne remplace pas l'historique
  sur chaque support. L'export graphique des rapports d'une copie immédiate demande que
  le HDD soit encore accessible ; les contrôles ultérieurs exportent directement leur rapport.
- Pas de détection matérielle, éjection intégrée, réparation automatique, verrou distribué,
  signature de manifestes ou distribution signée. Une seule instance graphique par profil
  utilisateur est recommandée pour éviter des mises à jour concurrentes du catalogue local.

Voir [l'architecture et le plan](docs/architecture.md) et [la distribution prévue](docs/distribution.md).

Licence MIT.
