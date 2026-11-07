"""Strict mission checker. Rules come from the game's own JsonVersion 6 missions (ref/mission_fields_v6.json, built
by mission_fields.py from the 39 built-in and the v6 Workshop missions) plus the lessons of failed tests.

A mission must pass with zero errors before anyone plays it.
Usage: mission_lint.py <mission.json> <map build dir> [schema json]
"""
import json
import sys
from pathlib import Path

ORDERS = {"InOrder", "CompleteAll", "CompleteAny", "CompleteSome"}
# Field names exactly as the game's own v6 missions write them.
OBJECTIVE_FIELDS = {
    "None": set(),
    "DestroyUnits": {"completeOrder", "completeSomePercent", "targetUnits"},
    "SpotUnit": {"completeOrder", "completeSomePercent", "targetUnits"},
    "CaptureAirbase": {"completeOrder", "completeSomePercent", "targetAirbases"},
    "ReachWaypoints": {"completeOrder", "completeSomePercent", "completeOnEnterRange", "waypoints"},
    "ReachUnits": {"completeOrder", "completeSomePercent", "targets"},
    "CompleteOtherObjective": {"completeOrder", "completeSomePercent", "targetObjectives"},
    "WaitSeconds": {"seconds"},
    "DialogueBox": {"title", "body", "button", "factionOnly"},
    "SuccessfulSortie": {"minimumScore", "additive"},
}
OUTCOME_FIELDS = {
    "ShowMessage": {"Message", "PlaySound", "ObjectiveFactionOnly"},
    "StartObjective": {"objectivesToStart"},
    "SpawnUnit": {"UnitsToSpawn"},
    "StopOrCompleteObjective": {"options", "objectivesToStart"},
    "EndGame": {"endType", "endDelay"},
    "RemoveUnit": {"UnitsToRemove"},
    "RevealUnit": {"UnitsToReveal"},
    "ModifyAirbase": {"airbase", "faction", "disabled", "capturable", "captureDefense"},
    "ModifyEnvironment": {"timeOfDay", "weather", "cloudAltitude", "windSpeed", "windTurbulence", "windHeading"},
    "GiveScore": {"bothFactions", "playerFundsType", "playerFunds", "factionFundsType", "factionFunds", "playerScoreType",
                  "playerScore", "rankType", "rank", "factionScoreType", "factionScore"},
    "ModifyFaction": {"bothFactions", "excessFundsThreshold", "playerJoinAllowance", "playerTaxRate", "regularIncome",
                      "killReward", "preventDonation", "aiAircraftLimit", "reduceAIPerFriendlyPlayer", "addAIPerEnemyPlayer",
                      "warheadsReserve", "reserveAirframes", "extraReservesPerPlayer", "excessFundsDistributePercent",
                      "preventJoin"},
}
OBJECTIVE_BASE = {"Type", "UniqueName", "Faction", "DisplayName", "Hidden", "Outcomes"}
OUTCOME_BASE = {"Type", "UniqueName"}
TARGET_LIST = {"DestroyUnits": "targetUnits", "SpotUnit": "targetUnits", "CaptureAirbase": "targetAirbases",
               "CompleteOtherObjective": "targetObjectives", "ReachWaypoints": "waypoints", "ReachUnits": "targets"}


def lint(m, map_airbases, strict=True):
    """strict=True for our own missions; strict=False only to check the checker against other people's missions."""
    errors, warnings = [], []
    if m.get("JsonVersion") != 6:
        errors.append(f"JsonVersion {m.get('JsonVersion')}: write 6, the game's current layout")
    units = {u["UniqueName"]: (k, u) for k in ("aircraft", "vehicles", "ships", "buildings", "scenery", "containers",
                                                "missiles", "pilots")
             for u in m.get(k, []) or [] if "UniqueName" in u}
    objs = {o["UniqueName"]: o for o in m.get("objectives", [])}
    outs = {o["UniqueName"]: o for o in m.get("outcomes", [])}
    if len(objs) != len(m.get("objectives", [])) or len(outs) != len(m.get("outcomes", [])):
        errors.append("duplicate objective or outcome names")
    start = objs.get("Mission Start")
    if not start or not start.get("Hidden"):
        (errors if strict else warnings).append("no hidden 'Mission Start' objective")
    airbases = set(map_airbases) | {a["UniqueName"] for a in m.get("airbases", [])}
    owner = {a["UniqueName"]: a.get("faction") for a in m.get("airbases", [])}
    spawned_later = {u for o in outs.values() if o.get("Type") == "SpawnUnit" for u in o.get("UnitsToSpawn", [])}
    started = {o2 for o in outs.values() if o.get("Type") == "StartObjective" for o2 in o.get("objectivesToStart", [])}
    # Objectives that start together with the mission: started by an outcome of a Type-None objective.
    at_start = {o2 for n, o in objs.items() if o.get("Type") == "None" for oc in o.get("Outcomes", [])
                if outs.get(oc, {}).get("Type") == "StartObjective" for o2 in outs[oc].get("objectivesToStart", [])}

    for name, o in objs.items():
        t = o.get("Type")
        if t not in OBJECTIVE_FIELDS:
            errors.append(f"objective '{name}': unknown Type {t}")
            continue
        extra = set(o) - OBJECTIVE_BASE - OBJECTIVE_FIELDS[t]
        missing = (OBJECTIVE_BASE | OBJECTIVE_FIELDS[t]) - set(o)
        if extra:
            errors.append(f"objective '{name}' ({t}): fields the game does not use: {sorted(extra)}")
        if missing:
            errors.append(f"objective '{name}' ({t}): missing fields {sorted(missing)}")
        if "completeOrder" in o:
            if o["completeOrder"] not in ORDERS:
                errors.append(f"objective '{name}': completeOrder {o['completeOrder']} not one of {sorted(ORDERS)}")
            if o["completeOrder"] == "CompleteAny" and len(o.get(TARGET_LIST.get(t, ""), []) or []) > 1:
                warnings.append(f"objective '{name}': CompleteAny finishes on the FIRST of "
                                f"{len(o[TARGET_LIST[t]])} targets; use CompleteSome or CompleteAll if that is not meant")
            if o["completeOrder"] != "CompleteSome" and o.get("completeSomePercent") not in (0.5, None):
                warnings.append(f"objective '{name}': completeSomePercent is ignored unless completeOrder is CompleteSome")
        if t in TARGET_LIST:
            targets = o.get(TARGET_LIST[t]) or []
            if not targets:
                (errors if strict else warnings).append(
                    f"objective '{name}' ({t}): no targets, so it completes the moment it starts")
            if t in ("DestroyUnits", "SpotUnit"):
                bad = [u for u in targets if u not in units]
                if bad:
                    errors.append(f"objective '{name}': targets not in the mission: {bad[:5]}")
            if t == "CaptureAirbase":
                for a in targets:
                    if a not in airbases:
                        errors.append(f"objective '{name}': airbase '{a}' is not on the map")
                    elif o.get("Faction") and owner.get(a) == o.get("Faction"):
                        # Only certain when it starts with the mission; started later the base may have changed hands.
                        if name in at_start:
                            errors.append(f"objective '{name}': {o.get('Faction')} already owns '{a}', so it completes at once")
                        else:
                            warnings.append(f"objective '{name}': {o.get('Faction')} owns '{a}' at the start; it completes "
                                            f"the moment it starts unless the base has changed hands by then")
            if t == "CompleteOtherObjective":
                bad = [x for x in targets if x not in objs]
                if bad:
                    errors.append(f"objective '{name}': unknown target objectives {bad}")
        for oc in o.get("Outcomes", []):
            if oc not in outs:
                errors.append(f"objective '{name}': outcome '{oc}' does not exist")
        if name != "Mission Start" and name not in started:
            warnings.append(f"objective '{name}' is never started")

    for name, o in outs.items():
        t = o.get("Type")
        if t not in OUTCOME_FIELDS:
            errors.append(f"outcome '{name}': unknown Type {t}")
            continue
        extra = set(o) - OUTCOME_BASE - OUTCOME_FIELDS[t]
        missing = (OUTCOME_BASE | OUTCOME_FIELDS[t]) - set(o)
        if extra:
            errors.append(f"outcome '{name}' ({t}): fields the game does not use: {sorted(extra)}")
        if missing:
            errors.append(f"outcome '{name}' ({t}): missing fields {sorted(missing)}")
        for key in ("objectivesToStart",):
            for x in o.get(key, []):
                if x not in objs:
                    errors.append(f"outcome '{name}': objective '{x}' does not exist")
        for key in ("UnitsToSpawn", "UnitsToRemove", "UnitsToReveal"):
            for x in o.get(key, []):
                if x not in units:
                    errors.append(f"outcome '{name}': unit '{x}' does not exist")
        if t == "EndGame" and o.get("endType") not in ("Victory", "Defeat"):
            errors.append(f"outcome '{name}': endType {o.get('endType')}")

    # An EndGame reachable from an objective that completes immediately ends the mission at once.
    instant = {n for n, o in objs.items() if o.get("Type") == "None" and n != "Mission Start"}
    for n in instant:
        for oc in objs[n].get("Outcomes", []):
            if outs.get(oc, {}).get("Type") == "EndGame":
                (errors if strict else warnings).append(
                    f"objective '{n}' (Type None) completes at once and ends the game through '{oc}'")
    # Waypoints must name an existing objective or 'Unit Spawn' (a dangling name means the unit never moves).
    for k in ("vehicles", "ships"):
        for u in m.get(k, []) or []:
            for w in u.get("waypoints", []):
                if w.get("objective") not in objs and w.get("objective") != "Unit Spawn":
                    (errors if strict else warnings).append(
                        f"{u['UniqueName']}: waypoint waits for unknown objective '{w.get('objective')}'")
            # The game sends a unit to a waypoint when its objective starts and, of several waypoints sharing one
            # objective (or several 'Unit Spawn' ones), keeps only the first: the rest are never driven.
            seen = [w.get("objective") for w in u.get("waypoints", [])]
            dup = sorted({s for s in seen if seen.count(s) > 1})
            if dup:
                (errors if strict else warnings).append(
                    f"{u['UniqueName']}: {len(seen)} waypoints but they share trigger(s) {dup}; the game drives only "
                    f"the first of each, so the unit stops early (give each leg its own objective)")
            # An unheld vehicle steers itself to the nearest objective or enemy and ignores its route.
            if k == "vehicles" and seen and not u.get("holdPosition", False):
                (errors if strict else warnings).append(
                    f"{u['UniqueName']}: has waypoints but does not hold position, so the AI drives it wherever it "
                    f"likes from the start (set holdPosition true)")
            # A waypoint whose objective nothing ever starts is never driven.
            for s in set(seen) - {"Unit Spawn", "Mission Start"}:
                if s in objs and s not in started:
                    (errors if strict else warnings).append(
                        f"{u['UniqueName']}: waypoint trigger '{s}' is never started by any outcome")
    return errors, warnings


def main(mission_path, build_dir):
    m = json.loads(Path(mission_path).read_text(encoding="utf-8-sig"))
    ab = json.loads(Path(build_dir, "airbases.json").read_text(encoding="utf-8"))
    errors, warnings = lint(m, [a["uniqueName"] for a in ab])
    for e in errors:
        print("ERROR  ", e)
    for w in warnings:
        print("warning", w)
    print(f"{len(errors)} errors, {len(warnings)} warnings")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:]))
