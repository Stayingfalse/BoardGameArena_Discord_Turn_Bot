# Bot Discord BGA self-host

[English version](README.md)

Bot Discord self-host pour Board Game Arena.

La fonction principale du bot est la **detection automatique des liens** : quand quelqu'un poste une URL de table BGA dans un salon Discord, le bot commence immediatement a surveiller cette table et publie un message de statut qu'il maintient a jour tout au long de la partie.

Aucun compte BGA, aucun mot de passe, aucun cookie — le bot ne lit que ce qu'un spectateur anonyme peut voir.

**Comment ca marche :**
1. Un joueur poste un lien de table BGA dans un salon Discord que le bot peut lire.
2. Le bot detecte le lien et commence a surveiller la table.
3. Un seul message est publie et mis a jour en direct au fil de trois etats : **Recrutement → En cours → Termine**.
4. Quand la partie se termine, le message est marque comme fini et la surveillance est retiree automatiquement.

Pour obtenir des mentions Discord dans les notifications de tour, les administrateurs peuvent lier des membres Discord a leurs comptes BGA avec `/bga link-member`. Les joueurs peuvent aussi se lier eux-memes en cliquant sur le bouton **Lier mon compte BGA & Discord** qui apparait sur le message de recrutement.

## Demarrage rapide

### Docker (recommande)

La methode la plus simple est Docker Compose. Le conteneur demarre automatiquement avec Docker et se relance en cas de crash.

```bash
cp .env.example .env
# Edite .env et renseigne au moins DISCORD_TOKEN
docker compose up -d
```

La base SQLite est dans un volume Docker nomme (`bga_data`) et survit aux redemarrages et mises a jour.

```bash
docker compose logs -f       # voir les logs
docker compose down          # arreter
docker compose up -d --build # reconstruire apres un changement de code
```

### Windows PowerShell

```powershell
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
# Edite .env, puis :
python -m bga_turn
```

### Linux / macOS

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
cp .env.example .env
# Edite .env, puis :
python -m bga_turn
```

## 1. Configuration

### Prerequis

- Docker et Docker Compose (recommande), **ou** Python 3.11 ou plus recent
- Un bot Discord cree dans le portail developpeur Discord avec le **Message Content Intent** active
- Le bot invite sur ton serveur Discord
- Au moins une table BGA accessible publiquement en mode spectateur

---

### Etape 1 — Creer le bot Discord

1. Va sur [discord.com/developers/applications](https://discord.com/developers/applications) et clique **New Application**.
2. Donne-lui un nom et cree-le.
3. Ouvre l'onglet **Bot** dans la barre laterale gauche.
4. Clique **Reset Token** (ou **Copy** si un token est deja affiche). C'est ton `DISCORD_TOKEN`. Garde-le secret — s'il est expose, regenere-le immediatement.
5. Dans le meme onglet **Bot**, descends jusqu'a **Privileged Gateway Intents** et active le **Message Content Intent**. C'est indispensable pour que le bot lise les liens postes et surveille automatiquement les tables.

> **Pourquoi le Message Content Intent est-il necessaire ?**
> Par defaut, Discord masque le contenu des messages aux bots. En activant cet intent, le bot peut lire le texte des messages et detecter automatiquement les liens BGA.

---

### Etape 2 — Inviter le bot sur ton serveur

La methode la plus simple est d'utiliser l'URL d'invitation que le bot affiche dans ses propres logs au demarrage. Lance le bot une premiere fois avec uniquement `DISCORD_TOKEN` renseigne et cherche la ligne :

```
Bot invite URL: https://discord.com/oauth2/authorize?client_id=...
```

Ouvre cette URL dans ton navigateur, choisis ton serveur et valide. Le bot aura exactement les permissions dont il a besoin.

**Alternative** : genere l'URL manuellement dans le portail developpeur :

1. Ouvre ton application dans le portail developpeur.
2. Va dans **Installation** (ou **OAuth2 > URL Generator** pour les anciens portails).
3. Dans **Guild Install**, selectionne les scopes : `bot` et `applications.commands`.
4. Dans **Bot Permissions**, selectionne :
   - `View Channels`
   - `Send Messages`
   - `Embed Links`
   - `Read Message History`
   - `Manage Messages` *(uniquement si `BGA_DELETE_INVITE_MESSAGE=1`)*
5. Copie l'URL generee, ouvre-la dans un navigateur et ajoute le bot a ton serveur.

---

### Etape 3 — Recuperer l'ID de serveur (`DISCORD_GUILD_ID`)

Renseigner `DISCORD_GUILD_ID` est optionnel mais fortement recommande pendant la configuration initiale : les slash commands apparaissent sur ton serveur quasi instantanement au lieu d'attendre la sync globale Discord (jusqu'a une heure).

1. Ouvre l'application Discord.
2. Va dans **Parametres utilisateur → Avance** et active le **Mode developpeur**.
3. Fais un clic droit sur l'icone ou le nom de ton serveur et clique **Copier l'ID du serveur**.
4. Colle cette valeur dans `DISCORD_GUILD_ID=...` dans ton `.env`.

Pour enregistrer les commandes sur plusieurs serveurs a la fois, renseigne `DISCORD_GUILD_ID` sous forme de liste d'IDs separes par des virgules (ex. `DISCORD_GUILD_ID=111222333,444555666`).

---

### Etape 4 — Configurer `.env`

Copie `.env.example` vers `.env` et renseigne au moins `DISCORD_TOKEN` :

```env
DISCORD_TOKEN=colle_ici_le_token_du_bot
DISCORD_GUILD_ID=colle_ici_l_id_du_serveur
```

Exemple complet avec toutes les variables expliquees :

```env
# --- Obligatoire ---
DISCORD_TOKEN=colle_ici_le_token_du_bot

# --- Recommande pendant la configuration ---
# ID du serveur Discord. Accelere l'enregistrement des slash commands.
DISCORD_GUILD_ID=colle_ici_l_id_du_serveur

# --- Comportement du bot ---
BGA_POLL_SECONDS=15             # Frequence de verification des tables (secondes)
BGA_DB_PATH=bga_bot.db          # Chemin du fichier SQLite
BGA_WS_URL=wss://ws-x1.boardgamearena.com/connection/websocket
LOG_LEVEL=INFO                  # DEBUG, INFO, WARNING, ERROR
BOT_LANG=EN                     # EN ou FR

# --- Fonctionnalites optionnelles ---
# Mettre a 1 pour ne publier que le message de recrutement et supprimer la watch quand la partie commence.
BGA_RECRUITING_ONLY=0

# Mettre a 1 pour supprimer le message d'origine contenant le lien BGA apres enregistrement de la watch.
# Necessite la permission Manage Messages.
BGA_DELETE_INVITE_MESSAGE=0

# ID du salon pour forcer toutes les notifications dans un seul salon,
# independamment du salon ou le lien a ete poste.
BGA_FORCED_CHANNEL_ID=

# Mettre a 1 une seule fois pour supprimer les anciennes slash commands globales, puis remettre a 0.
DISCORD_CLEAR_GLOBAL_COMMANDS=0

# Mettre a 1 pour reactiver le fallback HTTP legacy pour la detection de fin de partie.
BGA_ENABLE_TABLEINFOS_FALLBACK=0

# --- Tableau de bord web optionnel ---
DASHBOARD_ENABLED=0
DASHBOARD_PORT=8080
DASHBOARD_BASE_URL=http://localhost:8080
DISCORD_CLIENT_ID=
DISCORD_CLIENT_SECRET=
# Generer avec : python -c "import secrets; print(secrets.token_hex(32))"
DASHBOARD_SECRET_KEY=
```

#### Reference des variables `.env`

| Variable | Obligatoire | Defaut | Description |
|---|---|---|---|
| `DISCORD_TOKEN` | ✅ | — | Token du bot, onglet Bot du portail developpeur Discord |
| `DISCORD_GUILD_ID` | — | — | ID(s) de serveur separes par des virgules pour une sync rapide des commandes (recommande pendant la config) |
| `DISCORD_CLEAR_GLOBAL_COMMANDS` | — | `0` | Mettre a `1` une fois pour supprimer les slash commands globales obsoletes |
| `BGA_POLL_SECONDS` | — | `15` | Secondes entre deux ticks du scheduler |
| `BGA_DB_PATH` | — | `bga_bot.db` | Chemin du fichier SQLite |
| `BGA_WS_URL` | — | `wss://ws-x1.boardgamearena.com/connection/websocket` | URL du websocket public BGA |
| `BGA_ENABLE_TABLEINFOS_FALLBACK` | — | `0` | Reactive le fallback HTTP legacy pour la detection de fin de partie |
| `BGA_RECRUITING_ONLY` | — | `0` | Ne publier que le message de recrutement et supprimer la watch au demarrage de la partie |
| `BGA_DELETE_INVITE_MESSAGE` | — | `0` | Supprimer le message d'origine apres enregistrement de la watch |
| `BGA_FORCED_CHANNEL_ID` | — | — | Forcer toutes les notifications dans un salon specifique |
| `LOG_LEVEL` | — | `INFO` | Niveau de logs console (`DEBUG`, `INFO`, `WARNING`, `ERROR`) |
| `BOT_LANG` | — | `EN` | Langue des messages du bot (`EN` ou `FR`) |
| `DASHBOARD_ENABLED` | — | `0` | Active le tableau de bord web sur `DASHBOARD_PORT` |
| `DASHBOARD_PORT` | — | `8080` | Port du tableau de bord web |
| `DASHBOARD_BASE_URL` | — | `http://localhost:8080` | URL de base publique pour les redirections OAuth2 (HTTPS obligatoire en production) |
| `DISCORD_CLIENT_ID` | — | — | Client ID OAuth2 Discord (obligatoire si le dashboard est active) |
| `DISCORD_CLIENT_SECRET` | — | — | Client Secret OAuth2 Discord (obligatoire si le dashboard est active) |
| `DASHBOARD_SECRET_KEY` | — | — | Secret aleatoire pour la validation de l'etat auth (obligatoire si le dashboard est active) |

---

### Etape 5 — Lancer le bot

#### Recommande

```bash
python -m bga_turn
```

ou

```bash
bga-turn-bot
```

#### Lanceur de dev optionnel

```bash
python bot.py
```

Si `DISCORD_GUILD_ID` est renseigne (un seul ID ou une liste separee par des virgules), les slash commands sont synchronisees sur chacun de ces serveurs. Sinon, elles sont synchronisees globalement (peut prendre jusqu'a une heure pour apparaitre).

Si des slash commands en double apparaissent, mets `DISCORD_CLEAR_GLOBAL_COMMANDS=1` le temps d'un demarrage, puis remets `0`.

---

### Details du deploiement Docker

- `restart: unless-stopped` — demarrage automatique avec Docker, relance en cas de crash.
- `init: true` — forwarding correct des signaux et reaping des zombies.
- La base SQLite est dans le volume `bga_data` (monte sur `/data`). Pas besoin de renseigner `BGA_DB_PATH` dans `.env` — l'image utilise `/data/bga_bot.db` par defaut.

---

### Licence

Ce depot est distribue sous licence MIT. Voir `LICENSE`.

## 2. Comment fonctionne le bot

### Detection automatique des liens (fonctionnalite principale)

Le bot ecoute les messages dans tous les salons qu'il peut voir. Quand un message contient une URL de table BGA (ex. `https://boardgamearena.com/...?table=12345`), le bot :

1. Extrait l'ID de table depuis l'URL.
2. Recupere anonymement la page publique de la table pour identifier le jeu.
3. Ouvre une connexion websocket publique vers la table BGA.
4. Publie un message de statut dans le meme salon et le maintient a jour.

Le **Message Content Intent** doit etre active dans le portail developpeur Discord (voir Etape 1).

### Un seul message par table, trois etats de cycle de vie

Pour chaque table surveillee, le bot maintient exactement un message Discord et le met a jour en place au fil de la partie :

- **Etat 1 — Recrutement** : affiche le nom du jeu, les joueurs inscrits, les places disponibles et un bouton **Rejoindre**. Un bouton **Lier mon compte BGA & Discord** permet a n'importe quel membre de se lier lui-meme.
- **Etat 2 — En cours** : affiche a qui c'est le tour. Si ce joueur est lie a un compte Discord, il est @mentionne.
- **Etat 3 — Termine** : affiche le resultat final (vainqueur ou classement si disponible). La surveillance est automatiquement retiree.

### Bouton de liaison en libre-service

Les joueurs peuvent se lier sans intervention d'un administrateur en cliquant sur le bouton **Lier mon compte BGA & Discord** sur n'importe quel message de recrutement. Une fenetre modale s'affiche et demande leur pseudo BGA ou leur ID numerique.

### Enrichissement automatique des liens

Quand le bot voit un joueur agir sur une table surveillee, il complete automatiquement le nom BGA ou l'ID manquant pour les membres Discord lies. On peut lier quelqu'un avec juste un nom ou juste un ID, et le bot complete l'autre champ au fil du temps.

## 3. Commandes Discord

Toutes les commandes sont dans le groupe `/bga`.

### `/bga link-member`

Lie manuellement un membre Discord a un joueur BGA. Necessite `Manage Server` ou `Administrator`.

```text
/bga link-member @Membre Haurrus 91713763
```

- `bga_player_name` et `bga_player_id` sont tous les deux optionnels — renseigner au moins l'un des deux.
- L'ID joueur BGA est le nombre dans l'URL du profil BGA : `https://boardgamearena.com/player?id=91713763` → l'ID est `91713763`.
- Si seul le nom est fourni, le bot complete l'ID automatiquement la premiere fois qu'il voit ce joueur agir sur une table surveillee.

### `/bga unlink-member`

Supprime le lien BGA d'un membre Discord. Necessite `Manage Server` ou `Administrator`.

```text
/bga unlink-member @Membre
```

### `/bga status`

Affiche le dernier etat connu de toutes les tables surveillees sur le serveur courant (ephemere — visible uniquement par toi).

```text
/bga status
```

Affiche pour chaque watch : l'ID de table, le nom du jeu, le salon, a qui c'est le tour (IDs BGA) et l'etat interprete.

### `/bga settings`

Consulte ou modifie les parametres du bot pour ce serveur. Necessite `Manage Server` ou `Administrator`.

Lance sans argument pour voir les parametres actuels :

```text
/bga settings
```

| Option | Type | Description |
|---|---|---|
| `recruiting_only` | bool | Ne publier que le message de recrutement ; supprimer la watch quand la partie commence |
| `delete_invite_message` | bool | Supprimer le message d'origine contenant le lien BGA apres enregistrement de la watch (necessite Manage Messages) |
| `forced_channel` | salon | Forcer toutes les notifications dans ce salon, quel que soit l'endroit ou les liens sont postes |
| `clear_forced_channel` | bool | Supprimer le parametre de salon force |

Exemples :

```text
/bga settings recruiting_only:True
/bga settings delete_invite_message:True forced_channel:#notifications-bga
/bga settings clear_forced_channel:True
```

## 4. Guide de mise en service complet

### Minimal — detection automatique des liens uniquement

1. Cree le bot Discord, active le Message Content Intent, invite-le sur ton serveur (voir [Configuration](#1-configuration)).
2. Configure `.env` avec `DISCORD_TOKEN` et optionnellement `DISCORD_GUILD_ID`.
3. Lance le bot avec `python -m bga_turn` (ou Docker).
4. Poste un lien de table BGA dans un salon que le bot peut voir. Le bot commence a surveiller automatiquement.

### Avec mentions des joueurs

1. Realise les etapes ci-dessus.
2. Lie les membres Discord a leurs comptes BGA :
   ```text
   /bga link-member @MrHaurrus Haurrus 91713763
   ```
   Ou laisse les joueurs se lier eux-memes en cliquant sur le bouton **Lier mon compte BGA & Discord** sur le message de recrutement.
3. Desormais, quand c'est le tour d'un joueur lie, le bot le @mentionne dans le message de statut.

### Avec un salon de notifications dedie

Pour centraliser toutes les notifications dans un seul salon (ex. `#notifications-bga`), quel que soit le salon ou les liens sont postes :

```text
/bga settings forced_channel:#notifications-bga
```

## 5. Tableau de bord web optionnel

Le bot inclut un tableau de bord web optionnel permettant aux admins de serveur de gerer les parametres via une interface navigateur avec connexion OAuth2 Discord.

### Activer le tableau de bord

Ajoute les variables suivantes dans ton `.env` :

```env
DASHBOARD_ENABLED=1
DASHBOARD_PORT=8080
DASHBOARD_BASE_URL=https://ton-domaine.com   # HTTPS obligatoire en production
DISCORD_CLIENT_ID=ton_client_id_discord
DISCORD_CLIENT_SECRET=ton_client_secret_discord
DASHBOARD_SECRET_KEY=ton_secret_aleatoire    # python -c "import secrets; print(secrets.token_hex(32))"
```

Pour obtenir `DISCORD_CLIENT_ID` et `DISCORD_CLIENT_SECRET` :
1. Ouvre ton application dans le [portail developpeur Discord](https://discord.com/developers/applications).
2. Le **Client ID** est affiche sur la page **General Information**.
3. Va dans **OAuth2** et genere ou copie le **Client Secret**.
4. Dans **OAuth2 → Redirects**, ajoute `https://ton-domaine.com/auth/callback`.

Le tableau de bord est ensuite accessible sur `http://localhost:8080` (ou l'URL configuree).

## 6. Fonctionnement technique

### Architecture generale

Le bot repose sur trois couches :
- **Discord** — slash commands, evenements de messages et publication des messages
- **SQLite** — persistance des liens joueurs, des watches et des parametres serveur
- **API publique BGA** — bootstrap anonyme de la page de table et connexion websocket publique

### Flux reseau BGA

Le bot n'utilise pas de cookies, pas de session navigateur, pas de compte BGA.

1. **Lecture de la page publique** — telecharge l'URL de la table et extrait : identite spectateur anonyme (`user_id`, `archivemask`), noms de joueurs du bootstrap HTML, et etat initial du jeu si disponible.

2. **Connexion au websocket public** (`wss://ws-x1.boardgamearena.com/connection/websocket`) — rejoue le handshake BGA/Centrifugo (`connect`, `subscribe bgamsg`, `subscribe /table/t<TABLE_ID>`, etc.).

3. **Interpretation des evenements** — reconstruit les `waiting_ids` depuis les evenements websocket par ordre de priorite : `gameStateMultipleActiveUpdate`, `gameStateChange.active_player`, `yourturnack`, puis heuristiques publiques limitees. Detecte la fin de partie via `tableInfosChanged` status ou les signaux `tableDestroy`.

### Structure du projet

| Chemin | Role |
|---|---|
| `Dockerfile` | Image conteneur pour le deploiement Docker |
| `docker-compose.yml` | Service Compose avec redemarrage automatique et volume de donnees |
| `bot.py` | Lanceur de dev optionnel depuis la racine du depot |
| `src/bga_turn/app.py` | Point d'entree, chargement de l'environnement, demarrage du bot |
| `src/bga_turn/commands_bga.py` | Slash commands `/bga` et detecteur de liens `on_message` |
| `src/bga_turn/bga_client.py` | Reseau BGA public, parsing HTML, gestion websocket |
| `src/bga_turn/monitor.py` | Boucle de surveillance, cycle de vie des messages Discord, sync des joueurs suivis |
| `src/bga_turn/database.py` | Persistance SQLite |
| `src/bga_turn/dashboard.py` | Tableau de bord web aiohttp optionnel |
| `src/bga_turn/models.py` | Dataclasses metier |
| `src/bga_turn/utils.py` | Parsing URL, helpers JSON, utilitaires divers |
| `src/bga_turn/schema.sql` | Schema SQLite embarque dans le package |
| `pyproject.toml` | Metadata du package et point d'entree console |
| `.env.example` | Exemple de configuration locale |

### Points importants et limites

- Le bot ne fonctionne que sur des tables BGA accessibles publiquement en mode spectateur.
- Le bot est self-host — il doit tourner sur ta machine pour surveiller les tables.
- Le **Message Content Intent** doit etre active dans le portail developpeur Discord pour que la detection automatique des liens fonctionne.
- Sans cet intent, aucun lien n'est detecte et aucune table n'est surveillee automatiquement.
- Les noms de jeux affiches viennent du slug BGA ou du bootstrap public et ne sont pas toujours joliment formates.
- Le projet est distribue sans suite de tests unitaires ; la validation reste legere via le packaging et la compilation.
