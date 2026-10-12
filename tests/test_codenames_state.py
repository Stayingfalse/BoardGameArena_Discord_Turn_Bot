from __future__ import annotations

import json
import random
from pathlib import Path

import pytest

from bga_turn.codenames.identity import DEFAULT_BOT_NAMES, generate_credentials, parse_bot_names, pick_bot_name
from bga_turn.codenames.jsonpatch import JsonPatchError, apply_patch
from bga_turn.codenames.links import build_room_url, extract_room_ids
from bga_turn.codenames.state import MatchTracker

# A full classic game recorded from a real room as a spectator, names anonymised:
# Alice (0) red operative, Bob (1) red spymaster, Carol (3) blue spymaster,
# Dave (4) blue operative, the bot is seat 2. Red starts; blue wins when Alice
# picks the assassin.
FIXTURE = Path(__file__).parent / "fixtures" / "codenames_game.jsonl"


def _events() -> list[tuple[str, list]]:
    return [(e["event"], e["args"]) for e in map(json.loads, FIXTURE.read_text("utf-8").splitlines())]


def _replay():
    """Feed the recording to a tracker; yield the view after every event."""
    tracker = None
    for event, args in _events():
        if event == "createdMatch":
            tracker = MatchTracker(args[1], bot_player_id=args[2])
        elif event == "sync":
            tracker.apply_sync(args[1])
        elif event == "patch":
            assert tracker.apply_patch(args[1], args[2], args[3], args[4])
        elif event == "matchData":
            tracker.apply_match_data(args[1])
        if tracker is not None and tracker.state is not None:
            yield tracker.view()


def test_replay_follows_every_turn_in_order():
    keys: list[str | None] = []
    for view in _replay():
        if not keys or keys[-1] != view.turn_key:
            keys.append(view.turn_key)
    assert keys == [
        None,  # lobby
        "red:spymasters",
        "red:operatives",
        "blue:spymasters",
        "blue:operatives",
        "red:spymasters",
        "red:operatives",
        "blue:spymasters",
        "blue:operatives",  # Dave ends guessing early...
        "red:spymasters",
        "red:operatives",
        None,  # ...and Alice finds the assassin
    ]


def test_players_to_act_are_the_spymaster_then_the_operatives():
    seen = {}
    for view in _replay():
        if view.turn_key and view.turn_key not in seen:
            seen[view.turn_key] = [view.player_name(pid) for pid in view.players_to_act()]
    assert seen == {
        "red:spymasters": ["Bob"],
        "red:operatives": ["Alice"],
        "blue:spymasters": ["Carol"],
        "blue:operatives": ["Dave"],
    }


def test_lobby_view_lists_rosters_and_leaves_out_the_bot():
    first = next(_replay())
    assert first.phase == "lobby"
    assert first.bot_player_id == "2"
    assert first.rosters["red"]["operatives"] == ["0"]
    assert "2" not in first.spectator_ids
    assert first.clue is None and first.turn_key is None


def test_clue_and_guesses_are_tracked_during_a_turn():
    views = list(_replay())
    first_guessing = next(v for v in views if v.turn_key == "red:operatives")
    assert (first_guessing.clue.word, first_guessing.clue.number) == ("CLUE 1", "4")
    assert (first_guessing.clue.team, first_guessing.clue.giver_name) == ("red", "Bob")
    assert first_guessing.guesses_left == 5  # the clue number plus one

    after_first_guess = next(v for v in views if v.last_guess is not None)
    assert (after_first_guess.last_guess.word, after_first_guess.last_guess.card_color) == ("BATTERY", "red")
    assert after_first_guess.last_guess.player_name == "Alice"

    # No clue is shown while a spymaster is thinking.
    assert all(v.clue is None for v in views if v.active_role == "spymasters")


def test_scores_count_down_and_the_finish_is_reported():
    views = list(_replay())
    started = next(v for v in views if v.phase == "in_progress")
    assert started.remaining == {"red": 9, "blue": 8}
    assert started.starting_team == "red"

    final = views[-1]
    assert final.phase == "finished"
    assert (final.winner_team, final.finish_reason) == ("blue", "assassin")
    assert final.turn_key is None and final.players_to_act() == []
    # Red found 3 of their own and blue guessed 1 red card for them.
    assert final.remaining == {"red": 5, "blue": 4}


def test_a_patch_that_skips_a_state_asks_for_a_resync():
    tracker = MatchTracker("m1")
    tracker.apply_sync({"state": {"_stateID": 5, "G": {}, "ctx": {}}})
    assert not tracker.apply_patch(6, 7, [{"op": "replace", "path": "/_stateID", "value": 7}], [])
    assert tracker.state_id == 5
    # A patch whose result disagrees with its own stateID is rejected too.
    assert not tracker.apply_patch(5, 6, [{"op": "replace", "path": "/_stateID", "value": 9}], [])
    assert tracker.apply_patch(5, 6, [{"op": "replace", "path": "/_stateID", "value": 6}], [])
    assert tracker.state_id == 6


def test_patching_before_any_sync_asks_for_a_resync():
    assert not MatchTracker("m1").apply_patch(0, 1, [], [])


def test_json_patch_operations():
    doc = {"a": {"b": 1}, "list": [1, 2]}
    apply_patch(
        doc,
        [
            {"op": "add", "path": "/a/p#1", "value": True},
            {"op": "replace", "path": "/a/b", "value": 2},
            {"op": "add", "path": "/list/1", "value": 9},
            {"op": "add", "path": "/list/-", "value": 3},
            {"op": "remove", "path": "/list/0"},
            {"op": "add", "path": "/esc~1aped~0", "value": "x"},
            {"op": "copy", "from": "/a/b", "path": "/c"},
            {"op": "move", "from": "/c", "path": "/d"},
            # Spectators get removals of keys they never had; these are ignored.
            {"op": "remove", "path": "/plugins/log/data/metadata"},
            {"op": "remove", "path": "/a/missing"},
        ],
    )
    assert doc == {"a": {"b": 2, "p#1": True}, "list": [9, 2, 3], "esc/aped~": "x", "d": 2}
    with pytest.raises(JsonPatchError):
        apply_patch({}, [{"op": "add", "path": "/no/parent", "value": 1}])
    with pytest.raises(JsonPatchError):
        apply_patch({}, [{"op": "explode", "path": "/a"}])


def test_room_links_are_found_in_messages():
    text = (
        "join https://codenames.game/r/juzaf-hitos please, or codenames.game/r/Abc-Def!"
        " (dup: <https://www.codenames.game/r/juzaf-hitos>) not https://codenames.game/rules"
    )
    assert extract_room_ids(text) == ["juzaf-hitos", "abc-def"]
    assert extract_room_ids("https://notcodenames.game/r/abc-def") == []
    assert build_room_url("juzaf-hitos") == "https://codenames.game/r/juzaf-hitos"


def test_credentials_look_like_the_sites_own():
    creds = generate_credentials(random.Random(1))
    words = creds.split("-")
    assert len(words) == 3 and all(len(word) == 8 and word.isalpha() for word in words)
    assert generate_credentials() != generate_credentials()


def test_bot_names_default_and_parse():
    assert pick_bot_name(rng=random.Random(0)) in DEFAULT_BOT_NAMES
    assert all(name.startswith("🤖 ") for name in DEFAULT_BOT_NAMES)
    assert parse_bot_names("🤖 One | 🤖 Two |") == ("🤖 One", "🤖 Two")
    assert parse_bot_names("") == DEFAULT_BOT_NAMES
    assert pick_bot_name(["🤖 Only"]) == "🤖 Only"
