from __future__ import annotations

import os


def get_bot_lang() -> str:
    candidate = os.getenv("BOT_LANG", "EN").strip().upper()
    if candidate in {"EN", "FR"}:
        return candidate
    return "EN"


_MESSAGES: dict[str, dict[str, str]] = {
    "command_group_description": {
        "EN": "Board Game Arena commands",
        "FR": "Commandes Board Game Arena",
    },
    "command_link_member_description": {
        "EN": "Manually link a Discord member to a BGA player",
        "FR": "Lie manuellement un membre Discord a un joueur BGA",
    },
    "command_link_member_member": {
        "EN": "Discord member to link",
        "FR": "Membre Discord a lier",
    },
    "command_link_member_name": {
        "EN": "BGA name used in notifications",
        "FR": "Nom BGA a afficher dans les notifications",
    },
    "command_link_member_id": {
        "EN": "BGA player ID, found in the profile URL: boardgamearena.com/player?id=91713763",
        "FR": "ID joueur BGA, visible dans l'URL du profil : boardgamearena.com/player?id=91713763",
    },
    "command_unlink_member_description": {
        "EN": "Remove the BGA link for a Discord member",
        "FR": "Supprime le lien BGA d'un membre Discord",
    },
    "command_unlink_member_member": {
        "EN": "Discord member to unlink",
        "FR": "Membre Discord a delier",
    },
    "command_status_description": {
        "EN": "Show the known state of watched tables on this server",
        "FR": "Affiche l'etat connu des tables surveillees sur ce serveur",
    },
    "command_stats_description": {
        "EN": "Show server stats, or global stats with `global:true`",
        "FR": "Affiche les stats du serveur, ou les stats globales avec `global:true`",
    },
    "command_stats_global": {
        "EN": "When true, show global bot stats instead of this server only",
        "FR": "Si vrai, affiche les stats globales du bot au lieu de ce serveur seulement",
    },
    "error_manage_server_required_link": {
        "EN": "You need the `Manage Server` permission to link a Discord member to BGA.",
        "FR": "Il faut la permission `Manage Server` pour lier un membre Discord a un joueur BGA.",
    },
    "error_manage_server_required_unlink": {
        "EN": "You need the `Manage Server` permission to unlink a Discord member.",
        "FR": "Il faut la permission `Manage Server` pour delier un membre Discord.",
    },
    "error_need_bga_name_or_id": {
        "EN": "You must provide at least one `BgaName` or `BgaId` value.",
        "FR": "Il faut renseigner au moins un `NomBGA` ou un `IDBGA`.",
    },
    "error_invalid_bga_player_id": {
        "EN": "`bga_player_id` must be a valid numeric BGA player ID.",
        "FR": "`bga_player_id` doit etre un entier BGA valide.",
    },
    "error_command_server_only": {
        "EN": "This command must be used in a Discord server.",
        "FR": "Cette commande doit etre lancee dans un serveur Discord.",
    },
    "link_saved": {
        "EN": "Link saved for {member_mention}.\nBGA name: `{bga_name}`\nBGA ID: `{bga_id}`",
        "FR": "Lien enregistre pour {member_mention}.\nNom BGA : `{bga_name}`\nBGA ID : `{bga_id}`",
    },
    "link_missing_value_placeholder": {
        "EN": "will be auto-completed",
        "FR": "sera complete automatiquement",
    },
    "unlink_not_found": {
        "EN": "No BGA link exists for {member_mention}.",
        "FR": "Aucun lien BGA n'existe pour {member_mention}.",
    },
    "unlink_saved": {
        "EN": "BGA link removed for {member_mention}.",
        "FR": "Lien BGA supprime pour {member_mention}.",
    },
    "watch_init_active": {
        "EN": "already active",
        "FR": "deja actif",
    },
    "watch_init_waiting_event": {
        "EN": "state will be published on the next public websocket event",
        "FR": "publication de l'etat au prochain evenement websocket public",
    },
    "watch_init_waiting_players": {
        "EN": "waiting for enough players to join before turn tracking can start",
        "FR": "en attente de suffisamment de joueurs avant de lancer le suivi des tours",
    },
    "status_none": {
        "EN": "No BGA table is watched on this server.",
        "FR": "Aucune table BGA n'est surveillee sur ce serveur.",
    },
    "status_header": {
        "EN": "Watch status:",
        "FR": "Etat des watches :",
    },
    "status_embed_summary": {
        "EN": "{count} active watch(es) on this server.",
        "FR": "{count} watch(es) active(s) sur ce serveur.",
    },
    "status_card_title": {
        "EN": "📊 Table `{table_id}` • {game_name}",
        "FR": "📊 Table `{table_id}` • {game_name}",
    },
    "status_card_footer": {
        "EN": "Watch {index}/{total}",
        "FR": "Watch {index}/{total}",
    },
    "status_unknown": {
        "EN": "unknown",
        "FR": "inconnu",
    },
    "status_waiting_for_start": {
        "EN": "waiting for players before the game starts",
        "FR": "en attente de joueurs avant le debut de la partie",
    },
    "status_waiting_for": {
        "EN": "waiting for {mentions}",
        "FR": "a jouer pour {mentions}",
    },
    "status_waiting_no_link": {
        "EN": "players are waiting, but no matching Discord link is known",
        "FR": "des joueurs sont attendus, mais aucun lien Discord correspondant n'est connu",
    },
    "status_no_waiting": {
        "EN": "no player waiting",
        "FR": "aucun joueur attendu",
    },
    "stats_view_expired": {
        "EN": "This stats view is no longer active. Run `/bga stats` again.",
        "FR": "Cette vue de stats n'est plus active. Relance `/bga stats`.",
    },
    "stats_title_guild": {
        "EN": "{guild_name} — Stats",
        "FR": "{guild_name} — Stats",
    },
    "stats_title_global": {
        "EN": "Global Bot Stats",
        "FR": "Stats globales du bot",
    },
    "stats_this_server": {
        "EN": "this server",
        "FR": "ce serveur",
    },
    "stats_page_overview": {
        "EN": "Overview",
        "FR": "Vue d'ensemble",
    },
    "stats_page_leaders": {
        "EN": "Leaders",
        "FR": "Classements",
    },
    "stats_page_activity": {
        "EN": "Activity",
        "FR": "Activite",
    },
    "stats_scope_summary_guild": {
        "EN": "{tables} live table(s) across {channels} channel(s) • {recruiting} recruiting now",
        "FR": "{tables} table(s) actives dans {channels} salon(s) • {recruiting} en recrutement",
    },
    "stats_scope_summary_global": {
        "EN": "{tables} live table(s) across {channels} channel(s) • {recruiting} recruiting now globally",
        "FR": "{tables} table(s) actives dans {channels} salon(s) • {recruiting} en recrutement au global",
    },
    "stats_section_quick_numbers": {
        "EN": "Quick numbers",
        "FR": "Chiffres cles",
    },
    "stats_section_highlights": {
        "EN": "Highlights",
        "FR": "Temps forts",
    },
    "stats_section_top_games": {
        "EN": "Most played games",
        "FR": "Jeux les plus joues",
    },
    "stats_section_top_players": {
        "EN": "Most active players",
        "FR": "Joueurs les plus actifs",
    },
    "stats_section_top_recruiters": {
        "EN": "Most active recruiters",
        "FR": "Recruteurs les plus actifs",
    },
    "stats_section_activity": {
        "EN": "Activity breakdown",
        "FR": "Repartition de l'activite",
    },
    "stats_section_recent_pace": {
        "EN": "Recent pace (last 90 days)",
        "FR": "Rythme recent (90 derniers jours)",
    },
    "stats_section_top_channels": {
        "EN": "Most active channels",
        "FR": "Salons les plus actifs",
    },
    "stats_metric_games_tracked": {
        "EN": "Games tracked",
        "FR": "Parties suivies",
    },
    "stats_metric_live_tables": {
        "EN": "Live tables",
        "FR": "Tables actives",
    },
    "stats_metric_recruiting_now": {
        "EN": "Recruiting now",
        "FR": "En recrutement",
    },
    "stats_metric_linked_accounts": {
        "EN": "Linked accounts",
        "FR": "Comptes lies",
    },
    "stats_metric_channels": {
        "EN": "Tracked channels",
        "FR": "Salons suivis",
    },
    "stats_metric_avg_recruiting": {
        "EN": "Avg recruiting time",
        "FR": "Temps moyen de recrutement",
    },
    "stats_metric_avg_game": {
        "EN": "Avg game duration",
        "FR": "Duree moyenne d'une partie",
    },
    "stats_metric_avg_players": {
        "EN": "Avg players / game",
        "FR": "Joueurs moyens / partie",
    },
    "stats_metric_finished_games": {
        "EN": "Finished games",
        "FR": "Parties terminees",
    },
    "stats_metric_cancelled_games": {
        "EN": "Cancelled watches",
        "FR": "Watches annulees",
    },
    "stats_metric_unwatched_games": {
        "EN": "Unwatched manually",
        "FR": "Arretees manuellement",
    },
    "stats_metric_unique_players": {
        "EN": "Unique players seen",
        "FR": "Joueurs uniques vus",
    },
    "stats_metric_live_channels": {
        "EN": "Live channels right now",
        "FR": "Salons actifs maintenant",
    },
    "stats_metric_recruiters": {
        "EN": "Recruiters seen",
        "FR": "Recruteurs vus",
    },
    "stats_metric_guilds": {
        "EN": "Guilds configured",
        "FR": "Serveurs configures",
    },
    "stats_metric_forced_channels": {
        "EN": "Forced channels set",
        "FR": "Salons forces configures",
    },
    "stats_metric_busiest_day": {
        "EN": "Busiest day",
        "FR": "Jour le plus actif",
    },
    "stats_highlight_most_played": {
        "EN": "Most played game",
        "FR": "Jeu le plus joue",
    },
    "stats_highlight_most_active_player": {
        "EN": "Most active player",
        "FR": "Joueur le plus actif",
    },
    "stats_highlight_top_recruiter": {
        "EN": "Top recruiter",
        "FR": "Top recruteur",
    },
    "stats_highlight_busiest_day": {
        "EN": "📅 Busiest day: {day} ({count} finished)",
        "FR": "📅 Jour le plus actif : {day} ({count} terminees)",
    },
    "stats_none_available": {
        "EN": "No stats recorded yet.",
        "FR": "Aucune stat enregistree pour l'instant.",
    },
    "stats_game_count_line": {
        "EN": "{name} ({count})",
        "FR": "{name} ({count})",
    },
    "stats_player_record_line": {
        "EN": "{name} ({appearances} games, {wins} wins)",
        "FR": "{name} ({appearances} parties, {wins} victoires)",
    },
    "stats_recruiter_record_line": {
        "EN": "{recruiter} ({count} tables)",
        "FR": "{recruiter} ({count} tables)",
    },
    "stats_game_ranking_line": {
        "EN": "{rank}. {name} — {count}",
        "FR": "{rank}. {name} — {count}",
    },
    "stats_player_ranking_line": {
        "EN": "{rank}. {name} — {appearances} games / {wins} wins",
        "FR": "{rank}. {name} — {appearances} parties / {wins} victoires",
    },
    "stats_recruiter_ranking_line": {
        "EN": "{rank}. {recruiter} — {count} tables",
        "FR": "{rank}. {recruiter} — {count} tables",
    },
    "stats_channel_ranking_line": {
        "EN": "{rank}. {channel} — {count} games",
        "FR": "{rank}. {channel} — {count} parties",
    },
    "stats_recent_total_line": {
        "EN": "🎯 {count} game(s) finished in the last 90 days",
        "FR": "🎯 {count} partie(s) terminee(s) sur les 90 derniers jours",
    },
    "stats_recent_active_days_line": {
        "EN": "🗓 Activity was recorded on {count} day(s)",
        "FR": "🗓 Activite relevee sur {count} jour(s)",
    },
    "stats_recent_busiest_line": {
        "EN": "🔥 Peak day: {day} with {count} finished game(s)",
        "FR": "🔥 Pic d'activite : {day} avec {count} partie(s) terminee(s)",
    },
    "stats_masked_user": {
        "EN": "User •••{suffix}",
        "FR": "Utilisateur •••{suffix}",
    },
    "stats_masked_channel": {
        "EN": "Channel •••{suffix}",
        "FR": "Salon •••{suffix}",
    },
    "label_game": {
        "EN": "Game",
        "FR": "Jeu",
    },
    "label_table": {
        "EN": "Table",
        "FR": "Table",
    },
    "label_channel": {
        "EN": "Channel",
        "FR": "Salon",
    },
    "label_url": {
        "EN": "URL",
        "FR": "URL",
    },
    "label_status": {
        "EN": "Status",
        "FR": "Statut",
    },
    "label_seats": {
        "EN": "Seats",
        "FR": "Places",
    },
    "label_players_joined": {
        "EN": "Players joined",
        "FR": "Joueurs inscrits",
    },
    "label_state": {
        "EN": "State",
        "FR": "Etat",
    },
    "label_waiting_ids": {
        "EN": "Waiting IDs",
        "FR": "Waiting IDs",
    },
    "label_players_still_waiting": {
        "EN": "Players still waiting",
        "FR": "Joueurs encore attendus",
    },
    "label_current_turn": {
        "EN": "Current turn",
        "FR": "Tour en cours",
    },
    "label_all_players": {
        "EN": "Players",
        "FR": "Joueurs",
    },
    "label_winner": {
        "EN": "Winner",
        "FR": "Vainqueur",
    },
    "label_final_standings": {
        "EN": "Final standings",
        "FR": "Classement final",
    },
    "label_finish_reason": {
        "EN": "Finish reason",
        "FR": "Raison de fin",
    },
    "value_none": {
        "EN": "none",
        "FR": "aucun",
    },
    "value_unknown": {
        "EN": "unknown",
        "FR": "inconnu",
    },
    "error_empty_table_value": {
        "EN": "The table value is empty.",
        "FR": "La valeur de table est vide.",
    },
    "error_invalid_table_id": {
        "EN": "Could not extract a valid BGA table ID.",
        "FR": "Impossible d'extraire un ID de table BGA valide.",
    },
    "error_empty_table_url": {
        "EN": "The BGA table URL is empty.",
        "FR": "L'URL de table BGA est vide.",
    },
    "error_watch_requires_full_public_url": {
        "EN": "The `/bga watch` command requires the full public BGA table URL.",
        "FR": "La commande `/bga watch` exige l'URL publique complete de la table BGA.",
    },
    "error_url_missing_table_param": {
        "EN": "The BGA URL does not contain a valid `table=<id>` parameter.",
        "FR": "L'URL BGA ne contient pas de parametre `table=<id>` valide.",
    },
    "error_resolve_missing_request_token": {
        "EN": "Could not obtain the anonymous request token from the tableview page for table {table_id}.",
        "FR": "Impossible d'obtenir le jeton de requete anonyme depuis la page tableview pour la table {table_id}.",
    },
    "error_resolve_missing_game_server": {
        "EN": "Could not resolve the game server for table {table_id} (it may be private, finished, or not started yet).",
        "FR": "Impossible de resoudre le serveur de jeu pour la table {table_id} (elle est peut-etre privee, terminee ou pas encore commencee).",
    },
    "resolved_public_table": {
        "EN": "Resolved table {table_id} to /{gameserver}/{game_name}.",
        "FR": "Table {table_id} resolue vers /{gameserver}/{game_name}.",
    },
    "roster_fetch_failed": {
        "EN": "Could not fetch the authoritative player roster for table {table_id}: {error}. Falling back to page extraction.",
        "FR": "Impossible de recuperer le roster autoritaire des joueurs pour la table {table_id}: {error}. Repli sur l'extraction depuis la page.",
    },
    "error_url_missing_public_path": {
        "EN": "The BGA URL must contain the public game path, for example /15/sevenwondersdice?table=...",
        "FR": "L'URL BGA doit contenir le chemin public du jeu, par exemple /15/sevenwondersdice?table=...",
    },
    "error_url_missing_game_path": {
        "EN": "Could not extract the gameserver and game name from the BGA URL.",
        "FR": "Impossible d'extraire le gameserver et le nom du jeu depuis l'URL BGA.",
    },
    "error_load_tableinfos": {
        "EN": "Could not load tableinfos for table {table_id}: {error}",
        "FR": "Impossible de charger tableinfos pour la table {table_id}: {error}",
    },
    "error_tableinfos_http": {
        "EN": "Public tableinfos returned HTTP {status_code} for table {table_id}.",
        "FR": "tableinfos public renvoie HTTP {status_code} pour la table {table_id}.",
    },
    "error_tableinfos_invalid_json": {
        "EN": "Public tableinfos is not valid JSON for table {table_id}.",
        "FR": "tableinfos public n'est pas un JSON valide pour la table {table_id}.",
    },
    "error_tableinfos_unexpected": {
        "EN": "Unexpected public tableinfos payload for table {table_id}.",
        "FR": "tableinfos public inattendu pour la table {table_id}.",
    },
    "error_tableinfos_unexpected_payload": {
        "EN": "Unexpected public tableinfos payload for table {table_id}: status={status} exception={exception} error={error}",
        "FR": "Payload tableinfos public inattendu pour la table {table_id}: status={status} exception={exception} error={error}",
    },
    "error_tableinfos_missing_data": {
        "EN": "Public tableinfos does not contain a valid `data` block for table {table_id}.",
        "FR": "tableinfos public ne contient pas de bloc `data` valide pour la table {table_id}.",
    },
    "error_websocket_closed": {
        "EN": "Websocket connection closed: {error}",
        "FR": "Connexion websocket fermee: {error}",
    },
    "error_websocket_handshake_rejected": {
        "EN": "BGA websocket rejected the handshake for table {table_id}: HTTP {status_code} (likely a transient BGA/Cloudflare outage).",
        "FR": "BGA a rejete la poignee de main websocket pour la table {table_id} : HTTP {status_code} (probablement une coupure transitoire BGA/Cloudflare).",
    },
    "error_websocket_handshake_timeout": {
        "EN": "BGA websocket handshake timed out for table {table_id} on {websocket_url}.",
        "FR": "La poignee de main websocket BGA a expire pour la table {table_id} sur {websocket_url}.",
    },
    "error_load_public_page": {
        "EN": "Could not load public page {table_url}: {error}",
        "FR": "Impossible de charger la page publique {table_url}: {error}",
    },
    "error_public_page_http": {
        "EN": "The public page returned HTTP {status_code}.",
        "FR": "La page publique renvoie HTTP {status_code}.",
    },
    "error_missing_spectator_bootstrap": {
        "EN": "Could not extract the anonymous spectator identity from the public page.",
        "FR": "Impossible d'extraire l'identite spectateur anonyme depuis la page publique.",
    },
    "error_websocket_command_timeout": {
        "EN": "Timeout while waiting for websocket command {command_id}.",
        "FR": "Timeout sur la commande websocket {command_id}.",
    },
    "error_websocket_command_closed": {
        "EN": "Websocket closed while waiting for command {command_id}: {error}",
        "FR": "Connexion websocket fermee pendant la commande {command_id}: {error}",
    },
    "missing_discord_token": {
        "EN": "The DISCORD_TOKEN environment variable is required.",
        "FR": "La variable d'environnement DISCORD_TOKEN est obligatoire.",
    },
    "guild_sync": {
        "EN": "Slash commands synced for guild {guild_id} ({count} commands).",
        "FR": "Slash commands synchronisees sur la guilde {guild_id} ({count} commandes).",
    },
    "global_sync": {
        "EN": "Global slash commands synced ({count} commands).",
        "FR": "Slash commands globales synchronisees ({count} commandes).",
    },
    "global_cleanup_done": {
        "EN": "Deleted {count} stale global slash command(s) before guild sync.",
        "FR": "Suppression de {count} ancienne(s) slash command(s) globale(s) avant la sync guilde.",
    },
    "global_cleanup_skipped_no_guild": {
        "EN": "Skipping global slash command cleanup because DISCORD_GUILD_ID is not set.",
        "FR": "Nettoyage des slash commands globales ignore car DISCORD_GUILD_ID n'est pas renseigne.",
    },
    "bot_invite_url": {
        "EN": "Bot invite URL: {invite_url}",
        "FR": "URL d'invitation du bot : {invite_url}",
    },
    "tableinfos_status": {
        "EN": "Tableinfos {table_id} | status={status} | cancelled={cancelled} | time_end={time_end} | endgame_reason={endgame_reason} | finished={finished}",
        "FR": "Tableinfos {table_id} | status={status} | cancelled={cancelled} | time_end={time_end} | endgame_reason={endgame_reason} | finished={finished}",
    },
    "idle_tableinfos_check_failed": {
        "EN": "Idle tableinfos check failed for table {table_id}: {error}",
        "FR": "Le controle tableinfos en periode d'inactivite a echoue pour la table {table_id}: {error}",
    },
    "error_table_redirected_to_lobby": {
        "EN": "BGA redirected the public page to the table lobby ({final_url}); the table is no longer spectable.",
        "FR": "BGA a redirige la page publique vers le lobby ({final_url}); la table n'est plus observable.",
    },
    "table_unavailable_autounwatch": {
        "EN": "Table {table_id} is no longer publicly spectable (likely finished or private): {error}. Auto-unwatching.",
        "FR": "Table {table_id} n'est plus observable publiquement (probablement terminee ou privee) : {error}. Desactivation automatique de la surveillance.",
    },
    "invalid_json_frame": {
        "EN": "Ignored BGA websocket frame because JSON is invalid: {line}",
        "FR": "Trame websocket BGA ignoree car JSON invalide: {line}",
    },
    "worker_stopped": {
        "EN": "Websocket worker stopped for table {table_id}.",
        "FR": "Worker websocket stoppe pour la table {table_id}.",
    },
    "worker_started": {
        "EN": "Websocket worker started for table {table_id}.",
        "FR": "Worker websocket demarre pour la table {table_id}.",
    },
    "table_not_public": {
        "EN": "Table {table_id} is not publicly usable: {error}",
        "FR": "Table {table_id} non exploitable publiquement: {error}",
    },
    "websocket_error": {
        "EN": "BGA websocket error on table {table_id}: {error}",
        "FR": "Erreur websocket BGA sur la table {table_id}: {error}",
    },
    "unexpected_worker_error": {
        "EN": "Unexpected error in websocket worker for table {table_id}.",
        "FR": "Erreur inattendue dans le worker websocket de la table {table_id}.",
    },
    "table_finished_public": {
        "EN": "Table {table_id} detected as finished by the public stream.",
        "FR": "Table {table_id} detectee comme terminee par le flux public.",
    },
    "table_state": {
        "EN": "Table {table_id} | packet={packet_id} | waiting_ids={waiting_ids} | source={source} | details={details}",
        "FR": "Table {table_id} | packet={packet_id} | waiting_ids={waiting_ids} | source={source} | details={details}",
    },
    "table_finished_cleanup": {
        "EN": "Watches automatically removed for finished table {table_id}.",
        "FR": "Watchs supprimees automatiquement pour la table terminee {table_id}.",
    },
    "player_name_refresh_success": {
        "EN": "Refreshed missing player names from the public page for table {table_id} ({count} resolved).",
        "FR": "Les noms manquants ont ete completes depuis la page publique pour la table {table_id} ({count} resolu(s)).",
    },
    "player_name_refresh_failed": {
        "EN": "Could not refresh player names from the public page for table {table_id}: {error}",
        "FR": "Impossible de rafraichir les noms des joueurs depuis la page publique pour la table {table_id}: {error}",
    },
    "notification_sent": {
        "EN": "Notification sent for table {table_id} for IDs {waiting_ids}.",
        "FR": "Notification envoyee sur la table {table_id} pour les IDs {waiting_ids}.",
    },
    "notification_send_failed": {
        "EN": "Failed to send Discord notification for table {table_id} to channel {channel_id}: {error}",
        "FR": "Echec d'envoi Discord pour la table {table_id} sur le salon {channel_id}: {error}",
    },
    "invite_message_seats_value": {
        "EN": "{seats_taken}/{seats_total} joined — {seats_remaining} seat(s) remaining",
        "FR": "{seats_taken}/{seats_total} inscrits — {seats_remaining} place(s) restante(s)",
    },
    "invite_message_seats_unknown": {
        "EN": "{seats_taken} joined — remaining seats unknown",
        "FR": "{seats_taken} inscrits — places restantes inconnues",
    },
    "invite_message_footer": {
        "EN": "Turn notifications will replace this message automatically once the game starts.",
        "FR": "Les notifications de tour remplaceront automatiquement ce message quand la partie commencera.",
    },
    "button_join_table": {
        "EN": "Join table",
        "FR": "Rejoindre la table",
    },
    "button_link_bga_discord": {
        "EN": "🔗 Link your BGA & Discord",
        "FR": "🔗 Lier votre BGA & Discord",
    },
    "modal_link_bga_title": {
        "EN": "Link BGA & Discord Account",
        "FR": "Lier votre compte BGA & Discord",
    },
    "modal_link_bga_input_label": {
        "EN": "BGA Username or Player ID",
        "FR": "Pseudo BGA ou ID joueur",
    },
    "modal_link_bga_input_placeholder": {
        "EN": "e.g. Haurrus  or  91713763",
        "FR": "ex. Haurrus  ou  91713763",
    },
    "link_self_already_linked": {
        "EN": "You are already linked to BGA as `{bga_name}` (ID: `{bga_id}`).\nTo update the link use `/bga link-member` (requires Manage Server).",
        "FR": "Vous etes deja lie a BGA sous le nom `{bga_name}` (ID : `{bga_id}`).\nPour mettre a jour le lien, utilisez `/bga link-member` (necessite Gérer le serveur).",
    },
    "link_self_saved": {
        "EN": "✅ Linked! Your Discord account is now linked to BGA as `{bga_name}` (ID: `{bga_id}`).",
        "FR": "✅ Lien enregistre ! Votre compte Discord est maintenant lie a BGA sous le nom `{bga_name}` (ID : `{bga_id}`).",
    },
    "link_self_invalid_id": {
        "EN": "BGA Player ID must be a number. Enter your BGA username instead, or find your numeric ID in your BGA profile URL.",
        "FR": "L'ID joueur BGA doit etre un nombre. Entrez plutot votre pseudo BGA, ou trouvez votre ID numerique dans l'URL de votre profil BGA.",
    },
    "link_self_no_guild": {
        "EN": "This button can only be used inside a server.",
        "FR": "Ce bouton ne peut etre utilise qu'au sein d'un serveur.",
    },
    "lifecycle_in_progress_title": {
        "EN": "🎮 BGA Game in Progress",
        "FR": "🎮 Partie BGA en cours",
    },
    "turn_ping_prefix": {
        "EN": "Current turn: {mentions}",
        "FR": "Tour en cours : {mentions}",
    },
    "lifecycle_finished_title": {
        "EN": "🏆 BGA Game Finished",
        "FR": "🏆 Partie BGA terminee",
    },
    "invite_message_sent": {
        "EN": "Invite snapshot sent for table {table_id}.",
        "FR": "Snapshot d'invitation envoye pour la table {table_id}.",
    },
    "invite_message_send_failed": {
        "EN": "Failed to send invite snapshot for table {table_id} to channel {channel_id}: {error}",
        "FR": "Echec d'envoi du snapshot d'invitation pour la table {table_id} sur le salon {channel_id}: {error}",
    },
    "invite_message_updated": {
        "EN": "Invite snapshot updated for table {table_id}.",
        "FR": "Snapshot d'invitation mis a jour pour la table {table_id}.",
    },
    "invite_message_missing_update": {
        "EN": "Invite snapshot already missing while updating table {table_id}.",
        "FR": "Snapshot d'invitation deja introuvable pendant la mise a jour de la table {table_id}.",
    },
    "invite_message_update_failed": {
        "EN": "Failed to update invite snapshot for table {table_id}: {error}",
        "FR": "Echec de mise a jour du snapshot d'invitation pour la table {table_id}: {error}",
    },
    "turn_message_updated": {
        "EN": "Turn message updated for table {table_id} (waiting_ids={waiting_ids}).",
        "FR": "Message de tour mis a jour pour la table {table_id} (waiting_ids={waiting_ids}).",
    },
    "turn_message_missing_update": {
        "EN": "Turn message already missing while updating table {table_id}.",
        "FR": "Message de tour deja introuvable pendant la mise a jour pour la table {table_id}.",
    },
    "turn_message_update_failed": {
        "EN": "Failed to update Discord message for table {table_id}: {error}",
        "FR": "Echec de mise a jour du message Discord pour la table {table_id}: {error}",
    },
    "watch_message_deleted": {
        "EN": "{message_kind} message deleted for table {table_id}.",
        "FR": "Message {message_kind} supprime pour la table {table_id}.",
    },
    "watch_message_missing_delete": {
        "EN": "{message_kind} message already missing while deleting table {table_id}.",
        "FR": "Message {message_kind} deja introuvable pendant la suppression de la table {table_id}.",
    },
    "watch_message_delete_failed": {
        "EN": "Failed to delete {message_kind} Discord message for table {table_id}: {error}",
        "FR": "Echec de suppression du message Discord {message_kind} pour la table {table_id}: {error}",
    },
    "orphan_watch_message_deleted": {
        "EN": "Orphan {message_kind} message deleted for subscription {subscription_id}.",
        "FR": "Message {message_kind} orphelin supprime pour la souscription {subscription_id}.",
    },
    "orphan_watch_message_delete_failed": {
        "EN": "Failed to delete orphan {message_kind} message for subscription {subscription_id}: {error}",
        "FR": "Echec de suppression du message {message_kind} orphelin pour la souscription {subscription_id}: {error}",
    },
    "auto_watch_skipped": {
        "EN": "Skipping auto-watch for {table_reference} in channel {channel_id}: {error}",
        "FR": "Auto-watch ignoree pour {table_reference} dans le salon {channel_id} : {error}",
    },
    "auto_watch_registered": {
        "EN": "Auto-watch registered from a posted BGA link in guild {guild_id} channel {channel_id}.",
        "FR": "Auto-watch enregistree depuis un lien BGA poste dans le serveur {guild_id} salon {channel_id}.",
    },
    "stale_message_delete_failed": {
        "EN": "Failed to delete an old message for table {table_id} in channel {channel_id}: {error}",
        "FR": "Impossible de supprimer un ancien message de la table {table_id} dans le salon {channel_id}: {error}",
    },
    "channel_history_cleanup_failed": {
        "EN": "Failed to browse history for channel {channel_id} while cleaning table {table_id}: {error}",
        "FR": "Impossible de parcourir l'historique du salon {channel_id} pour nettoyer la table {table_id}: {error}",
    },
    "startup_cleanup": {
        "EN": "Startup cleanup: deleted {deleted_count} old message(s) for table {table_id}.",
        "FR": "Nettoyage de demarrage: {deleted_count} ancien(s) message(s) supprime(s) pour la table {table_id}.",
    },
    "channel_fetch_failed": {
        "EN": "Failed to resolve channel {channel_id} for table {table_id}: {error}",
        "FR": "Impossible de recuperer le salon {channel_id} pour la table {table_id}: {error}",
    },
    "channel_not_messageable": {
        "EN": "Channel {channel_id} does not accept messages for table {table_id}.",
        "FR": "Le channel {channel_id} n'accepte pas les messages pour la table {table_id}.",
    },
    "turn_message_content": {
        "EN": "{game_label}: {game_name}\n{table_label}: {table_id}\n{players_label}: {players}\n{url_label}: {table_url}",
        "FR": "{game_label} : {game_name}\n{table_label} : {table_id}\n{players_label} : {players}\n{url_label} : {table_url}",
    },
    "command_invocation": {
        "EN": "Slash command /{command} invoked by {user_name} ({user_id}) in guild={guild_id} channel={channel_id} params=[{params}]",
        "FR": "Commande slash /{command} appelee par {user_name} ({user_id}) dans serveur={guild_id} salon={channel_id} params=[{params}]",
    },
    "recruiting_only_unwatch": {
        "EN": "Recruiting-only mode: game started for table {table_id} (subscription {subscription_id}), recruiting message deleted and watch removed.",
        "FR": "Mode recrutement uniquement: partie demarree pour la table {table_id} (souscription {subscription_id}), message de recrutement supprime et watch retiree.",
    },
    "trigger_message_deleted": {
        "EN": "Deleted triggering BGA invite message in channel {channel_id} (guild {guild_id}).",
        "FR": "Message d'invitation BGA declencheur supprime dans le salon {channel_id} (serveur {guild_id}).",
    },
    "trigger_message_delete_failed": {
        "EN": "Failed to delete triggering BGA invite message in channel {channel_id}: {error}",
        "FR": "Echec de suppression du message d'invitation BGA declencheur dans le salon {channel_id}: {error}",
    },
    "trigger_message_delete_skipped_no_replacement": {
        "EN": "Skipped deleting triggering BGA invite message in channel {channel_id} (guild {guild_id}) because no replacement message was posted yet.",
        "FR": "Suppression du message d'invitation BGA declencheur ignoree dans le salon {channel_id} (serveur {guild_id}) car aucun message de remplacement n'a encore ete publie.",
    },
    "command_settings_description": {
        "EN": "Show or update per-server bot settings",
        "FR": "Afficher ou modifier les reglages du bot pour ce serveur",
    },
    "command_settings_recruiting_only": {
        "EN": "Recruiting-only mode: remove watch once the game starts",
        "FR": "Mode recrutement uniquement: retire le watch quand la partie commence",
    },
    "command_settings_delete_invite_message": {
        "EN": "Delete the Discord message that triggered auto-watch",
        "FR": "Supprimer le message Discord ayant declenche le watch automatique",
    },
    "command_settings_forced_channel": {
        "EN": "Force all bot notifications to this channel",
        "FR": "Forcer toutes les notifications du bot dans ce salon",
    },
    "command_settings_clear_forced_channel": {
        "EN": "Clear the forced channel override",
        "FR": "Supprimer l'override de salon force",
    },
    "settings_display": {
        "EN": (
            "**Server settings**\n"
            "• Recruiting-only mode: `{recruiting_only}`\n"
            "• Delete invite message: `{delete_invite_message}`\n"
            "• Forced channel: {forced_channel_id}"
        ),
        "FR": (
            "**Reglages du serveur**\n"
            "• Mode recrutement uniquement : `{recruiting_only}`\n"
            "• Supprimer le message d'invitation : `{delete_invite_message}`\n"
            "• Salon force : {forced_channel_id}"
        ),
    },
    "settings_saved": {
        "EN": (
            "✅ **Settings saved.**\n"
            "• Recruiting-only mode: `{recruiting_only}`\n"
            "• Delete invite message: `{delete_invite_message}`\n"
            "• Forced channel: {forced_channel_id}"
        ),
        "FR": (
            "✅ **Reglages sauvegard\u00e9s.**\n"
            "• Mode recrutement uniquement : `{recruiting_only}`\n"
            "• Supprimer le message d'invitation : `{delete_invite_message}`\n"
            "• Salon force : {forced_channel_id}"
        ),
    },
    "error_manage_server_required_settings": {
        "EN": "You need the **Manage Server** permission to change bot settings.",
        "FR": "Vous devez avoir la permission **Gérer le serveur** pour modifier les réglages du bot.",
    },
    "discord_rate_limited_retry": {
        "EN": "Discord rate-limited on {operation} for table {table_id}; retrying after {retry_after:.1f}s (attempt {attempt}/{max_retries}).",
        "FR": "Discord a limite le taux sur {operation} pour la table {table_id} ; nouvel essai apres {retry_after:.1f}s (tentative {attempt}/{max_retries}).",
    },
    "discord_rate_limit_exhausted": {
        "EN": "Discord rate-limit retries exhausted for {operation} on table {table_id}; dropping update.",
        "FR": "Tentatives epuisees apres limitation Discord sur {operation} pour la table {table_id} ; mise a jour abandonnee.",
    },
    "discord_queue_wait": {
        "EN": "Discord queue delaying {operation} on channel {channel_id} for {wait:.2f}s to avoid hitting limits.",
        "FR": "La file Discord retarde {operation} sur le salon {channel_id} pendant {wait:.2f}s pour eviter la limitation.",
    },
    "bga_rate_limited": {
        "EN": "BGA returned 429 for table {table_id}; waiting {retry_after}s before retry.",
        "FR": "BGA a retourne 429 pour la table {table_id} ; attente de {retry_after}s avant nouvel essai.",
    },
    "discord_io_paused": {
        "EN": "Discord I/O paused until rate-limit window clears ({wait:.1f}s remaining).",
        "FR": "Les operations Discord sont en pause jusqu'a la fin de la fenetre de limitation ({wait:.1f}s restantes).",
    },
    "bga_server_error_http": {
        "EN": "BGA returned HTTP {status_code} (server error).",
        "FR": "BGA a retourne HTTP {status_code} (erreur serveur).",
    },
    "bga_site_down": {
        "EN": "BGA appears to be down ({error_count}/{total_count} tables reporting server errors, including table {table_id}): {error}. Waiting before retry.",
        "FR": "BGA semble etre en panne ({error_count}/{total_count} tables signalent des erreurs serveur, dont la table {table_id}) : {error}. Attente avant nouvel essai.",
    },
    "bga_table_server_error": {
        "EN": "BGA server error on table {table_id} (consecutive: {consecutive}): {error}",
        "FR": "Erreur serveur BGA sur la table {table_id} (consecutif : {consecutive}) : {error}",
    },
    "bga_server_error_cleared": {
        "EN": "BGA server error resolved for table {table_id}; removing error banner from Discord.",
        "FR": "L'erreur serveur BGA pour la table {table_id} est resolue ; suppression de la banniere d'erreur Discord.",
    },
    "table_server_error_note": {
        "EN": "⚠️ BGA is reporting a server error for this table. The bot is retrying automatically.",
        "FR": "⚠️ BGA signale une erreur serveur pour cette table. Le bot reessaie automatiquement.",
    },
    "table_unexpected_error_note": {
        "EN": "⚠️ The bot encountered a repeated unexpected error on this table. It is still retrying automatically.",
        "FR": "⚠️ Le bot a rencontre une erreur inattendue repetee sur cette table. Il reessaie automatiquement.",
    },
    "command_recruiting_description": {
        "EN": "Show all tables currently recruiting players on this server",
        "FR": "Afficher toutes les tables en cours de recrutement sur ce serveur",
    },
    "recruiting_none": {
        "EN": "No tables are currently recruiting on this server.",
        "FR": "Aucune table n'est actuellement en recrutement sur ce serveur.",
    },
    "recruiting_header": {
        "EN": "🎲 Tables currently recruiting",
        "FR": "🎲 Tables en cours de recrutement",
    },
    "recruiting_summary": {
        "EN": "{count} table(s) currently recruiting.",
        "FR": "{count} table(s) en cours de recrutement.",
    },
    "recruiting_card_title": {
        "EN": "Table {table_id} — {game_name}",
        "FR": "Table {table_id} — {game_name}",
    },
    "recruiting_label_creator": {
        "EN": "👤 Created by",
        "FR": "👤 Créé par",
    },
    "recruiting_label_seats_free": {
        "EN": "💺 Seats free",
        "FR": "💺 Places libres",
    },
    "recruiting_label_players": {
        "EN": "🃏 Players",
        "FR": "🃏 Joueurs",
    },
    "recruiting_label_age": {
        "EN": "⏱ Open since",
        "FR": "⏱ Ouvert depuis",
    },
    "recruiting_age_minutes": {
        "EN": "{minutes}m",
        "FR": "{minutes}min",
    },
    "recruiting_age_hours": {
        "EN": "{hours}h {minutes}m",
        "FR": "{hours}h {minutes}min",
    },
    "recruiting_age_days": {
        "EN": "{days}d {hours}h",
        "FR": "{days}j {hours}h",
    },
    "recruiting_age_unknown": {
        "EN": "unknown",
        "FR": "inconnu",
    },
    "recruiting_seats_unknown": {
        "EN": "unknown",
        "FR": "inconnu",
    },
    "recruiting_players_none": {
        "EN": "none yet",
        "FR": "aucun pour l'instant",
    },
}


def tr(key: str, **kwargs: object) -> str:
    templates = _MESSAGES.get(key)
    if templates is None:
        return key
    template = templates.get(get_bot_lang()) or templates["EN"]
    return template.format(**kwargs)
