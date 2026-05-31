#
# Zolo — desktop control backend.
#
# SPDX-License-Identifier: BSD 2-Clause License
#

"""Zolo's eyes and hands. Replaces the flower demo's mock_backend.py.

A DesktopController that the voice bot's tools call:
  - read_screen(): list interactive UI elements (id + role + label) from the
    macOS Accessibility tree of the FRONTMOST app (OS-wide). Pin to one app
    with ZOLO_TARGET_APP=Safari if you want to lock it down.
  - click(target) / type_text(text) / press_key(key) / scroll(direction): drive
    the REAL cursor and keyboard via pyautogui.

Two modes (env ZOLO_MODE; auto-selected if unset):
  - "live": real Accessibility reads + real cursor control. For the local demo
    (set ENV=local).
  - "mock": returns a canned screen and only LOGS intended actions — no real
    input. For Cekura test runs and the Linux Pipecat Cloud container, where
    there is no Mac screen to drive. Auto-selected when ENV != local.

pyobjc / pyautogui are imported lazily inside live mode only, so this module
imports cleanly on the Linux cloud container, which has neither.
"""

import os
import time

from loguru import logger

# kAXValueType* integer constants (stable across pyobjc versions).
KAX_VALUE_CGPOINT = 1
KAX_VALUE_CGSIZE = 2

ATTR_ROLE = "AXRole"
ATTR_TITLE = "AXTitle"
ATTR_DESCRIPTION = "AXDescription"
ATTR_VALUE = "AXValue"
ATTR_CHILDREN = "AXChildren"
ATTR_POSITION = "AXPosition"
ATTR_SIZE = "AXSize"

INTERACTIVE_ROLES = {
    "AXButton",
    "AXLink",
    "AXTextField",
    "AXTextArea",
    "AXCheckBox",
    "AXRadioButton",
    "AXPopUpButton",
    "AXComboBox",
    "AXMenuItem",
    "AXSearchField",
}

# Friendly short names for the LLM (it never sees the AX prefix).
ROLE_SHORT = {
    "AXButton": "button",
    "AXLink": "link",
    "AXTextField": "textfield",
    "AXTextArea": "textarea",
    "AXCheckBox": "checkbox",
    "AXRadioButton": "radio",
    "AXPopUpButton": "dropdown",
    "AXComboBox": "combobox",
    "AXMenuItem": "menuitem",
    "AXSearchField": "searchfield",
}

# Always-typed roles even when their AX label is empty (the value is the content).
ALWAYS_KEEP_ROLES = {"AXTextField", "AXSearchField", "AXTextArea"}

# Canned screen used in mock mode (a generic restaurant-booking page) so Cekura
# can run scenarios deterministically without a real browser.
MOCK_SCREEN = [
    {"id": "e1", "role": "searchfield", "label": "Search restaurants"},
    {"id": "e2", "role": "dropdown", "label": "Party size"},
    {"id": "e3", "role": "textfield", "label": "Date"},
    {"id": "e4", "role": "textfield", "label": "Time"},
    {"id": "e5", "role": "button", "label": "Find a table"},
    {"id": "e6", "role": "textfield", "label": "Your name"},
    {"id": "e7", "role": "button", "label": "Book"},
]


class DesktopController:
    """Reads on-screen elements and drives the cursor/keyboard for the target app."""

    def __init__(self, target_app: str | None = None, mode: str | None = None):
        # OS-wide by default: target_app=None means "read/drive whatever app is
        # frontmost right now". Set ZOLO_TARGET_APP=Safari (or any app name) to
        # pin Zolo to a single app instead.
        pinned = target_app if target_app is not None else os.getenv("ZOLO_TARGET_APP")
        self.target_app = pinned or None

        if mode is None:
            mode = os.getenv("ZOLO_MODE") or ("live" if os.getenv("ENV") == "local" else "mock")
        self.mode = mode

        # id -> element dict (with x/y/w/h) from the most recent read_screen.
        self._elements_by_id: dict = {}
        # The app the most recent read_screen looked at. Clicks re-activate THIS
        # pid — not whatever happens to be frontmost at click time, which may have
        # drifted between the read and the action.
        self._active_pid = None
        self._active_app_name = None
        # Every intended action, for audit + so Cekura sees them in the transcript.
        self.action_log: list = []

        if self.mode == "live":
            try:
                self._load_live_backends()
            except Exception as exc:
                logger.warning(f"[zolo] live backends unavailable ({exc}); using mock mode")
                self.mode = "mock"

        logger.info(
            f"[zolo] DesktopController mode={self.mode} "
            f"target={self.target_app or 'frontmost (OS-wide)'}"
        )

    # --- live backend (lazy; never imported on the Linux cloud container) ----

    def _load_live_backends(self):
        import pyautogui
        from AppKit import (
            NSApplicationActivateIgnoringOtherApps,
            NSRunningApplication,
            NSWorkspace,
        )
        from ApplicationServices import (
            AXUIElementCopyAttributeValue,
            AXUIElementCreateApplication,
            AXValueGetValue,
        )

        pyautogui.FAILSAFE = True  # slam cursor to a screen corner to abort a runaway
        self._pyautogui = pyautogui
        self._NSWorkspace = NSWorkspace
        self._NSRunningApplication = NSRunningApplication
        self._activate_flag = NSApplicationActivateIgnoringOtherApps
        self._ax_copy = AXUIElementCopyAttributeValue
        self._ax_app_for_pid = AXUIElementCreateApplication
        self._ax_value_get = AXValueGetValue

    def _attr(self, element, name):
        error_code, value = self._ax_copy(element, name, None)
        return value if error_code == 0 else None

    def _decode_point(self, ax_value):
        if ax_value is None:
            return None
        ok, point = self._ax_value_get(ax_value, KAX_VALUE_CGPOINT, None)
        return (point.x, point.y) if ok else None

    def _decode_size(self, ax_value):
        if ax_value is None:
            return None
        ok, size = self._ax_value_get(ax_value, KAX_VALUE_CGSIZE, None)
        return (size.width, size.height) if ok else None

    def _resolve_target(self):
        """Return (pid, app_name) for the app Zolo should read/drive right now.

        Pinned mode (ZOLO_TARGET_APP set): always that named app.
        OS-wide mode (default): the frontmost app — so Zolo drives whatever
        window the user is actually looking at, across the whole OS.
        """
        workspace = self._NSWorkspace.sharedWorkspace()
        if self.target_app:
            for app in workspace.runningApplications():
                if (app.localizedName() or "") == self.target_app:
                    return app.processIdentifier(), app.localizedName()
            return None, self.target_app
        front = workspace.frontmostApplication()
        if front is not None:
            return front.processIdentifier(), front.localizedName()
        return None, None

    def _activate_target(self):
        """Bring the app from the last read_screen to the front so clicks/keys land in it."""
        pid = self._active_pid
        if pid is None:
            pid, _ = self._resolve_target()
        if pid is None:
            return
        app = self._NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
        if app is not None:
            app.activateWithOptions_(self._activate_flag)

    def _collect(self, element, out, depth=0, max_depth=20, budget=None):
        if budget is None:
            budget = [6000]
        if budget[0] <= 0 or depth > max_depth:
            return
        budget[0] -= 1
        role = self._attr(element, ATTR_ROLE)
        if role in INTERACTIVE_ROLES:
            position = self._decode_point(self._attr(element, ATTR_POSITION))
            size = self._decode_size(self._attr(element, ATTR_SIZE))
            label = (
                self._attr(element, ATTR_TITLE)
                or self._attr(element, ATTR_DESCRIPTION)
                or self._attr(element, ATTR_VALUE)
            )
            label = str(label or "").strip()
            if position and size and (label or role in ALWAYS_KEEP_ROLES):
                out.append(
                    {
                        "role": role,
                        "label": label[:60],
                        "x": position[0],
                        "y": position[1],
                        "w": size[0],
                        "h": size[1],
                    }
                )
        for child in self._attr(element, ATTR_CHILDREN) or []:
            self._collect(child, out, depth + 1, max_depth, budget)

    # --- tool surface (called by the bot's function tools) -------------------

    def read_screen(self) -> dict:
        """Return the interactive elements on screen as {id, role, label}."""
        if self.mode == "mock":
            self._elements_by_id = {
                e["id"]: {**e, "x": 200.0, "y": 120.0 * i, "w": 160.0, "h": 32.0}
                for i, e in enumerate(MOCK_SCREEN, start=1)
            }
            return {
                "app": f"{self.target_app or 'frontmost app'} (mock)",
                "count": len(MOCK_SCREEN),
                "elements": [{"id": e["id"], "role": e["role"], "label": e["label"]} for e in MOCK_SCREEN],
            }

        pid, app_name = self._resolve_target()
        if pid is None:
            target = self.target_app or "the frontmost app"
            return {
                "app": app_name or self.target_app,
                "elements": [],
                "error": f"Couldn't find {target}. Ask the user to bring the app they want controlled to the front.",
            }

        # Remember which app this read looked at so later clicks re-activate IT,
        # even if the user tabs away before the action fires.
        self._active_pid = pid
        self._active_app_name = app_name

        raw: list = []
        self._collect(self._ax_app_for_pid(pid), raw)

        self._elements_by_id = {}
        payload = []
        for index, element in enumerate(raw[:40], start=1):
            element_id = f"e{index}"
            self._elements_by_id[element_id] = element
            payload.append(
                {
                    "id": element_id,
                    "role": ROLE_SHORT.get(element["role"], element["role"]),
                    "label": element["label"],
                }
            )
        return {"app": app_name, "count": len(payload), "elements": payload}

    def _match(self, target: str):
        """Resolve a target (id or label) to (element_id, element) or (None, candidates)."""
        target_normalized = (target or "").strip().lower()
        if target in self._elements_by_id:
            return target, self._elements_by_id[target]
        for element_id, element in self._elements_by_id.items():
            if element["label"].lower() == target_normalized:
                return element_id, element
        substring_matches = [
            (element_id, element)
            for element_id, element in self._elements_by_id.items()
            if target_normalized and target_normalized in element["label"].lower()
        ]
        if len(substring_matches) == 1:
            return substring_matches[0]
        return None, substring_matches

    def click(self, target: str) -> dict:
        if self.mode == "mock":
            self.action_log.append({"action": "click", "target": target})
            return {"ok": True, "did": f"clicked '{target}'", "mock": True}

        element_id, resolved = self._match(target)
        if element_id is None:
            candidates = [{"id": cid, "label": el["label"]} for cid, el in (resolved or [])][:6]
            return {
                "ok": False,
                "reason": f"No single element matches '{target}'. Call read_screen and pick an exact id or label.",
                "candidates": candidates,
            }

        center_x = resolved["x"] + resolved["w"] / 2
        center_y = resolved["y"] + resolved["h"] / 2
        self._activate_target()
        self._pyautogui.moveTo(center_x, center_y, duration=0.3)
        self._pyautogui.click()
        self.action_log.append({"action": "click", "id": element_id, "x": center_x, "y": center_y})
        return {"ok": True, "did": f"clicked {resolved['label']!r}", "id": element_id}

    def type_text(self, text: str) -> dict:
        if self.mode == "mock":
            self.action_log.append({"action": "type", "text": text})
            return {"ok": True, "did": f"typed '{text}'", "mock": True}
        self._activate_target()
        self._pyautogui.write(text, interval=0.02)
        self.action_log.append({"action": "type", "text": text})
        return {"ok": True, "did": f"typed {text!r}"}

    def press_key(self, key: str) -> dict:
        if self.mode == "mock":
            self.action_log.append({"action": "press", "key": key})
            return {"ok": True, "did": f"pressed {key}", "mock": True}
        self._activate_target()
        self._pyautogui.press(key.strip().lower())
        self.action_log.append({"action": "press", "key": key})
        return {"ok": True, "did": f"pressed {key}"}

    def scroll(self, direction: str = "down", amount: int = 5) -> dict:
        clicks = amount if direction.strip().lower() == "up" else -amount
        if self.mode == "mock":
            self.action_log.append({"action": "scroll", "direction": direction, "amount": amount})
            return {"ok": True, "did": f"scrolled {direction}", "mock": True}
        self._activate_target()
        self._pyautogui.scroll(clicks)
        self.action_log.append({"action": "scroll", "direction": direction, "amount": amount})
        return {"ok": True, "did": f"scrolled {direction}"}

    def do_actions(self, steps) -> dict:
        """Run a list of on-screen actions in order, stopping at the first real failure.

        Lets the LLM finish a multi-step on-screen task in ONE turn instead of one
        slow round-trip per action. Each step is a dict:
          {"action": "click",  "target": "<id or label>"}
          {"action": "type",   "text": "<text>"}
          {"action": "press",  "key": "<key>"}
          {"action": "scroll", "direction": "up"|"down", "amount": <int>}
        """
        if not isinstance(steps, list):
            return {"ok": False, "error": "steps must be a list of action objects"}

        results = []
        for index, step in enumerate(steps, start=1):
            if not isinstance(step, dict):
                results.append({"step": index, "ok": False, "error": "step must be an object"})
                return {"ok": False, "completed": index - 1, "results": results}

            action = str(step.get("action", "")).strip().lower()
            if action == "click":
                outcome = self.click(step.get("target", ""))
            elif action == "type":
                outcome = self.type_text(step.get("text", ""))
            elif action == "press":
                outcome = self.press_key(step.get("key", ""))
            elif action == "scroll":
                outcome = self.scroll(step.get("direction", "down"), int(step.get("amount", 5)))
            else:
                outcome = {"ok": False, "error": f"unknown action '{action}'"}

            results.append({"step": index, **outcome})
            if outcome.get("ok") is False:
                return {"ok": False, "completed": index - 1, "stopped_at": index, "results": results}
            if self.mode == "live":
                time.sleep(0.12)  # let the UI settle between actions (click -> focus -> type)

        return {"ok": True, "completed": len(steps), "results": results}
