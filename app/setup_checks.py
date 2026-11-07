"""What is installed, and exactly how to fix what isn't. Used by Setup.bat and the app's Setup panel."""
from __future__ import annotations

from . import downloads, paths, unity


def checks(deep: bool = False) -> dict:
    game = paths.game_dir()
    hub = paths.unity_hub()
    editor = paths.unity_editor(deep=deep)
    if editor and deep:
        paths.save_config({"unity_editor": editor})
    licence = paths.unity_licence()
    avail, total = paths.memory_mb()
    free = paths.free_gb(paths.DATA)
    inst = unity.install_status()
    items = [
        {"id": "game", "ok": bool(game), "label": "Nuclear Option",
         "detail": str(game) if game else "",
         "fix": "Install Nuclear Option from Steam, then press Check again. If it is installed somewhere Steam "
                "doesn't list, choose its folder with Change game folder.", "action": "pick-game"},
        {"id": "bepinex", "ok": paths.bepinex_ok(game), "label": "BepInEx 5 (mod loader)",
         "fix": "Press Install mods (the game must be closed). Or by hand: unzip BepInEx_win_x64_5.4.23.4.zip from "
                "github.com/BepInEx/BepInEx/releases into the Nuclear Option folder.",
         "action": "install-mods" if game else None},
        {"id": "nocustommaps", "ok": bool(paths.nocustommaps_dll(game)), "label": "NOCustomMaps (map loader)",
         "fix": "Press Install mods. Or by hand: put NOCustomMaps.dll from github.com/BatongMaster/NOCustomMaps/"
                "releases in Nuclear Option\\BepInEx\\plugins\\NOCustomMaps\\.",
         "action": "install-mods" if game else None},
        {"id": "hub", "ok": bool(hub), "label": "Unity Hub",
         "fix": "Press Get Unity Hub (it downloads Unity's installer and starts it), or install it from "
                "unity.com/download. Then press Check again.", "action": "get-hub"},
        {"id": "licence", "ok": bool(hub) and licence, "label": "Signed in to Unity Hub",
         "fix": "Open Unity Hub and sign in with a free Unity account; accept the free Unity Personal licence when "
                "asked. This is the one step Unity makes you do by hand. Then press Check again.",
         "action": "open-hub" if hub else None},
        {"id": "editor", "ok": bool(editor), "label": f"Unity {paths.UNITY_VERSION} (the game's version)",
         "detail": str(editor) if editor else ("Installing: " + (inst["lines"][-1] if inst["lines"] else "starting")
                                              if inst["running"] else ""),
         "fix": f"Press Install Unity (about 4 GB, 10-30 minutes). Or in Unity Hub: Installs > Install Editor > "
                f"Archive, version {paths.UNITY_VERSION}.",
         "action": "install-unity" if hub and licence and not inst["running"] else None,
         "busy": inst["running"]},
    ]
    notes = []
    if total < unity.MIN_RAM_MB:
        notes.append(f"This PC has {total / 1024:.0f} GB of memory. Maps can be prepared here, but the Unity step "
                     "needs about 8 GB: copy the Open Skies folder to a bigger PC to finish them.")
    if free < 15:
        notes.append(f"{free:.0f} GB free on the drive Open Skies uses. Each map needs about 10 GB while it builds.")
    if inst.get("ok") is False and not inst["running"]:
        notes.append("The last Unity install did not finish: " + (inst["lines"][-1] if inst["lines"] else "") +
                     f" (log: logs\\unity-install.log)")
    ready_build = all(i["ok"] for i in items if i["id"] in ("hub", "licence", "editor"))
    ready_install = all(i["ok"] for i in items if i["id"] in ("game", "bepinex", "nocustommaps"))
    return {"ready": ready_build and ready_install, "canBuild": ready_build, "canInstall": ready_install,
            "items": items, "notes": notes, "game": str(game) if game else None,
            "shippingCached": downloads.shipping_ready(), "shippingMB": downloads.SHIPPING_MB,
            "memory": {"freeMB": avail, "totalMB": total}, "freeGB": round(free, 1)}
