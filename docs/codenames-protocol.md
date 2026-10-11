# Codenames (codenames.game) socket protocol notes

Captured 2026-10-11 from a live room by joining as a spectator and recording a
full game (lobby → clues/guesses → assassin). Basis for a Codenames provider.

## Transport

- Socket.IO v4 (`EIO=4`), websocket transport, built on **boardgame.io** with a
  custom lobby layer.
- Lobby: `https://server-discord.gke.codenames.game` (default path `/socket.io`).
- Namespace: `/cno2` (classic). `/cnduet` is Duet; the server sends
  `wrongNamespace(<name>)` if we guess wrong.
- Room link: `https://codenames.game/r/<roomID>` e.g. `juzaf-hitos`.
- Client sends `sync-check` every ~5s; Engine.IO ping interval 10s.

## Joining a room

1. Connect to the lobby, namespace `/cno2`.
2. Generate our own credentials string (site uses three 8-letter CV words,
   e.g. `luzizobo-hutizaju-nalifamu`). **Persist it per room** - reusing it
   reclaims the same seat instead of adding a new player.
3. Emit `sync("default", null, <creds>, {playerName, playerImage: null,
   playerLocale: "en", roomID, credentials: <creds>})`.
   - `credentials: null` → `sync-error` "404 not found".
4. Lobby replies `updateServerURL("https://server-discord.gke.codenames.game/0")`.
   Reconnect to that origin with Socket.IO path `/0/socket.io` and repeat step 3.
5. Shard replies `createdMatch(roomID, matchID, playerID, creds, serverURL)`.
   Emit `sync(matchID, playerID, creds, {...same info...})`.
6. Shard replies `sync(matchID, {state, log, filteredMetadata, initialState,
   serverURL, servertime, gameName, setupData})` then
   `matchData(matchID, players, ...)`.

Joining does **not** put us on a team: we stay a spectator. Display names are
sanitised (brackets stripped: `Turn-Bot (spectator)` → `Turn-Bot spectator`).

## Incoming events

| event | args | meaning |
|---|---|---|
| `sync` | `matchID, {state, log, filteredMetadata, ...}` | full state |
| `patch` | `matchID, prevStateID, stateID, jsonPatchOps[], logEntries[], servertime` | RFC 6902 diff on state |
| `update` | `matchID, state, log` | full replacement (boardgame.io) |
| `matchData` | `matchID, players[], ...` | roster: `{id, name, isHost, isConnected}` |
| `sync-error` | `{matchID, error}` | bad match/creds |
| `room-timeout` | `{matchID}` | room expired |
| `updateServerURL` | `url` | move to shard |
| `playNextMatch` | `...` | host started a new game in same room |

If a `patch`'s `prevStateID` doesn't match ours, re-emit `sync`.

## State (`state.G`)

- `teams.{red,blue}.{spymasters,operatives}` → `{"p#<playerID>": true}`.
  Names come from `matchData` / `filteredMetadata` by id.
- `activeTeam`: `red`/`blue`/null. `activeRole`: `spymasters`/`operatives`/null.
- `startingTeam`, `guessLimit` (-1 after `endGuessing`), `cardsDealed`.
- `grid[25]`: `{word, color, revealed, tips}`. **`color` is null until
  revealed** for spectators - the bot never sees the key.
- `coverCards[]`: `{color, location: redDeck|blueDeck|...|grid}`; remaining per
  team = cover cards of that colour not yet `location == "grid"`.
- `gameover`: `{team: "blue", reason: "assassin" | ...}` (also `ctx.gameover`).
- `ctx.phase`: `lobby` → `game` → null (game over).

## Turn rules (what to ping)

- `activeRole == "spymasters"` → ping the spymaster(s) of `activeTeam`.
- `activeRole == "operatives"` → ping all operatives of `activeTeam`.
- Ping only when `(activeTeam, activeRole)` changes; guesses within a turn
  (`guessCard`, `tipCard`) don't change it.
- Clue word/number and giver come from the patch log entry:
  `action.payload.type == "giveClue"`, `args[0] == {word, number}`,
  `metadata.playerName`.

## Move names seen

`joinTeam`, `leaveTeam`, `startMatch`, `giveClue`, `tipCard` (operative
marks a card, no turn change), `guessCard`, `endGuessing`, plus framework
`endTurn`/`endPhase`.
