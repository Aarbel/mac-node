# mac-node — nœud navigateur résidentiel Hermes (macOS)

## Qu’est-ce que c’est ?

**mac-node** est un petit agent qui s’installe sur le Mac de Clément, rejoint le tailnet Tailscale existant, et expose une API HTTP sécurisée permettant au serveur Hermes (sur le VPS) de piloter un vrai navigateur Chromium depuis l’IP résidentielle. Son rôle principal est de vérifier, à chaque connexion, si un export de données Meta/Instagram a eu lieu dans les **14 derniers jours** ; sinon, de en demander un nouveau ; puis, lorsque l’export est prêt, de télécharger l’archive ZIP et de la déposer dans `~/.hermes-mac/dumps/` pour ingestion côté serveur.

## Installation

Sur le Mac (macOS uniquement), une seule commande après clonage du dépôt :

```bash
git clone <url-du-repo> mac-node && cd mac-node && bash install.sh
```

Le script est **idempotent** et **sans sudo** : il crée `~/.hermes-mac/` (venv Python, profil Playwright, état, jeton), installe `playwright` et Chromium, enregistre un **LaunchAgent** (`com.hermes.macnode`) qui lance `node.py --on-login` au démarrage de session, et vérifie Tailscale.

## Connexion Instagram (une fois)

Après l’installation, ouvrez une session Instagram dans le profil persistant du nœud :

```bash
~/.hermes-mac/venv/bin/python ig_export.py login
```

Une fenêtre **visible** s’ouvre sur la page de connexion Instagram. Connectez-vous normalement ; le script attend jusqu’à 5 minutes la présence du cookie `sessionid`, puis ferme le navigateur. **C’est la seule étape manuelle** requise pour l’authentification.

## Modèle de sécurité

| Couche | Comportement |
|--------|----------------|
| **Réseau** | L’API n’accepte que les clients dont l’IP appartient au CGNAT Tailscale (`100.64.0.0/10`). Toute autre adresse reçoit **403**. |
| **Authentification** | En-tête `Authorization: Bearer <jeton>` comparé en temps constant au fichier `~/.hermes-mac/token` (32 octets aléatoires, créé une seule fois). Jeton invalide → **401**. |
| **Tâches** | Seuls des noms de tâches explicites sont acceptés (`ig_state`, `ig_request_export`, `ig_download`, `browse`, `ig_login`). |

Le jeton n’apparaît jamais dans les logs. Le nœud n’écrit que sous `~/.hermes-mac/` et dans le répertoire du dépôt.

## Règle des 14 jours

Au démarrage (`--on-login`), le nœud appelle `ig_state` pour lire la date du dernier export Meta. Si la date est **inconnue** ou **plus vieille que 14 jours**, et qu’aucune exportation n’est déjà **en cours**, il lance `ig_request_export` (Messages uniquement, JSON, toute la période). Une notification macOS résume l’action (meilleur effort). L’état est persisté dans `state.json`.

## Fichiers utiles

| Chemin | Rôle |
|--------|------|
| `~/.hermes-mac/token` | Jeton Bearer pour le serveur Hermes |
| `~/.hermes-mac/state.json` | Dernière vérification, date d’export, pending, dernière erreur |
| `~/.hermes-mac/dumps/` | Archives ZIP téléchargées (`YYYYMMDD_ig_export.zip`) |
| `~/.hermes-mac/node.log` | Journal des requêtes HTTP et événements |
| `~/.hermes-mac/profile/` | Profil Chromium persistant (cookies Instagram) |

Exemple de `state.json` :

```json
{
  "last_export_at": "2026-10-01",
  "last_check_at": "2026-10-31T12:00:00+00:00",
  "pending_request": false,
  "last_error": null
}
```

## API (rappel)

- `GET /health` — santé, Playwright, session Instagram, IP Tailscale  
- `GET /status` — vue de `state.json` + liste des dumps  
- `POST /task` — `{"name":"...", "params":{...}}` (timeout tâche 300 s)

URL typique après install : `http://<ip-tailscale>:8791` (ou un autre port choisi automatiquement — voir ci-dessous).

## Ports

Lors de l’installation, `install.sh` choisit le **premier port libre** parmi `8791`, `8792`, `8793`, `8794`, `8899` et `9990`, l’enregistre dans `~/.hermes-mac/port` (fichier mode `600`) et configure le LaunchAgent avec `--port <valeur>`. Si tous ces ports sont occupés, le script s’arrête avec un message d’erreur.

Pour **forcer** un port sans réinstaller :

| Priorité | Source |
|----------|--------|
| 1 | argument `--port` de `node.py` |
| 2 | variable d’environnement `HERMES_MAC_PORT` |
| 3 | première ligne de `~/.hermes-mac/port` (entier 1024–65535) |
| 4 | défaut **8791** |

Si le nœud ne peut pas écouter (port déjà pris au démarrage), il quitte avec le **code de sortie 3** et indique de relancer `install.sh`, qui reprend un port libre et recharge le LaunchAgent.

## Désinstallation

```bash
launchctl unload ~/Library/LaunchAgents/com.hermes.macnode.plist
rm -rf ~/.hermes-mac
rm ~/Library/LaunchAgents/com.hermes.macnode.plist
```

## Limites connues

- Le Mac doit être **allumé**, **connecté au tailnet**, et l’utilisateur **connecté à la session macOS** pour que le LaunchAgent et le navigateur fonctionnent.
- L’interface Meta (Accounts Center) **change souvent** : les sélecteurs couvrent le français et l’anglais ; si une étape échoue, le nœud renvoie **quelle étape** a échoué et **quels libellés** ont été tentés — il n’improvise pas de clics.
- La confirmation par **mot de passe** Meta ne peut pas être automatisée : le flux renvoie `needs_password: true` et l’opérateur termine dans la fenêtre ouverte.
- Les tests CI tournent sur Linux **sans** Playwright ; le Mac cible doit exécuter `install.sh` pour le navigateur.

## Développement / tests

```bash
python3 -m unittest discover -s tests -v
```

Les tests mockent l’auth et le dispatch ; aucun navigateur n’est requis.
