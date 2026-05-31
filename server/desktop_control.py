#
# Zolo — desktop control backend.
#
# SPDX-License-Identifier: BSD 2-Clause License
#

"""Zolo's eyes and hands. Replaces the flower demo's mock_backend.py.

A DesktopController that the voice bot's tools call:
  - read_screen(): list interactive UI elements (id + role + label) from the
    macOS Accessibility tree of the FRONTMOST app's focused window (OS-wide).
    Pin to one app with ZOLO_TARGET_APP=Safari if you want to lock it down.
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

Hardening (from Cekura eval + an independent Codex review):
  - Live mode that was explicitly requested fails LOUD instead of silently
    no-op'ing into mock (Codex #9).
  - Reads are scoped to the focused window and drop disabled / off-screen
    controls so Zolo can't target things the user can't see (Codex #2).
  - Duplicate exact-label matches are treated as ambiguous, not first-pick
    (Codex #3).
  - Clicks re-read the element's CURRENT geometry and verify its role before
    acting; a moved/replaced element refuses instead of clicking stale
    coordinates (Codex #4).
  - Typing is blocked unless a text field is actually focused, so a batch that
    crossed a navigation can't type blind into the wrong place (Codex #5).
  - Committing controls (Book / Pay / Submit / Delete / ...) require an explicit
    confirmed=True; the plain click tool refuses them (Codex #1).
  - Shared element state is guarded by a lock because reads and actions run on
    different threads (Codex #7).
"""

import os
import threading
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
ATTR_ENABLED = "AXEnabled"
ATTR_FOCUSED_WINDOW = "AXFocusedWindow"
ATTR_MAIN_WINDOW = "AXMainWindow"
ATTR_FOCUSED_UI = "AXFocusedUIElement"

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

# Roles where the user is expected to type — used to gate type_text (Codex #5).
TEXT_ROLES = {"AXTextField", "AXTextArea", "AXSearchField", "AXComboBox"}

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

# Labels that indicate an irreversible / committing action. Clicking one of these
# requires an explicit confirmation (Codex #1) so a bad transcript or model slip
# can't submit/pay/delete in a single turn.
DESTRUCTIVE_KEYWORDS = (
    "book",
    "buy",
    "pay",
    "purchase",
    "checkout",
    "place order",
    "order now",
    "submit",
    "send",
    "confirm",
    "delete",
    "remove",
    "transfer",
    "withdraw",
)

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


def _is_destructive_label(label: str) -> bool:
    low = (label or "").lower()
    return any(keyword in low for keyword in DESTRUCTIVE_KEYWORDS)


class DesktopController:
    """Reads on-screen elements and drives the cursor/keyboard for the target app."""

    def __init__(self, target_app: str | None = None, mode: str | None = None):
        # OS-wide by default: target_app=None means "read/drive whatever app is
        # frontmost right now". Set ZOLO_TARGET_APP=Safari (or any app name) to
        # pin Zolo to a single app instead.
        pinned = target_app if target_app is not None else os.getenv("ZOLO_TARGET_APP")
        self.target_app = pinned or None

        # Was live mode EXPLICITLY demanded (mode arg or ZOLO_MODE), vs auto-selected
        # from ENV? An explicit live request must fail LOUD if it can't really drive the
        # machine — silently dropping to mock turns a live demo into a no-op (Codex #9).
        requested_mode = mode if mode is not None else os.getenv("ZOLO_MODE")
        if mode is None:
            mode = os.getenv("ZOLO_MODE") or ("live" if os.getenv("ENV") == "local" else "mock")
        self.mode = mode
        self._live_explicit = requested_mode == "live"

        # id -> element dict (with x/y/w/h and the live AX handle) from the most
        # recent read_screen. Guarded by _lock because read_screen runs on a worker
        # thread while actions run on another (Codex #7).
        self._elements_by_id: dict = {}
        self._lock = threading.Lock()
        # The app the most recent read_screen looked at. Clicks re-activate THIS
        # pid — not whatever happens to be frontmost at click time, which may have
        # drifted between the read and the action.
        self._active_pid = None
        self._active_app_name = None
        # Every intended action, for audit + so Cekura sees them in the transcript.
        self.action_log: list = []

        if self.mode == "live":
            self._enter_live_mode()

        logger.info(
            f"[zolo] DesktopController mode={self.mode} "
            f"target={self.target_app or 'frontmost (OS-wide)'}"
        )

    # --- live backend (lazy; never imported on the Linux cloud container) ----

    def _enter_live_mode(self):
        """Load live backends + verify Accessibility, or fail loudly if live was demanded.

        Codex #9: a silent fall-through to mock means actions still report success
        while nothing touches the real machine — a no-op demo. So when live was
        EXPLICITLY requested (ZOLO_MODE=live), refuse to start instead of pretending.
        Auto-selected live (ENV=local with no ZOLO_MODE) keeps the graceful fallback.
        """
        try:
            self._load_live_backends()
        except Exception as exc:
            reason = f"live backends could not load ({type(exc).__name__}: {exc})"
            if self._live_explicit:
                raise RuntimeError(
                    f"[zolo] ZOLO_MODE=live was requested but {reason}. Zolo will NOT control "
                    f"the real machine. Install the macOS deps (pyobjc, pyautogui) and grant "
                    f"Accessibility permission, or unset ZOLO_MODE to run in mock mode."
                ) from exc
            logger.warning(f"[zolo] {reason}; using mock mode")
            self.mode = "mock"
            return

        # Backends loaded — now make sure macOS actually trusts us for Accessibility.
        # Without it, AX reads silently return nothing and clicks go nowhere.
        if not self._ax_is_trusted():
            msg = (
                "[zolo] Accessibility permission is NOT granted, so Zolo can't read the screen "
                "or move the cursor. Grant it in System Settings > Privacy & Security > "
                "Accessibility (add your terminal/app), then restart."
            )
            if self._live_explicit:
                raise RuntimeError(msg)
            logger.warning(msg + " Falling back to mock mode.")
            self.mode = "mock"

    def _load_live_backends(self):
        import pyautogui
        from AppKit import (
            NSApplicationActivateIgnoringOtherApps,
            NSRunningApplication,
            NSWorkspace,
        )
        from ApplicationServices import (
            AXIsProcessTrusted,
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
        self._ax_is_trusted = AXIsProcessTrusted

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

    @staticmethod
    def _onscreen(position, size) -> bool:
        """Cheap visibility filter (Codex #2): drop zero-size and parked-offscreen controls.

        macOS hides offscreen windows at large negative coordinates, so we don't need
        exact multi-monitor bounds — just reject the degenerate / far-off cases.
        """
        x, y = position
        w, h = size
        if w <= 1 or h <= 1:
            return False
        if x < -5000 or y < -5000 or x > 30000 or y > 30000:
            return False
        return True

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

    def _activate_target(self, pid=None):
        """Bring the app from the last read_screen to the front so clicks/keys land in it."""
        if pid is None:
            pid = self._active_pid
        if pid is None:
            pid, _ = self._resolve_target()
        if pid is None:
            return
        app = self._NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
        if app is not None:
            app.activateWithOptions_(self._activate_flag)

    def _read_root(self, pid):
        """The subtree to walk: the app's focused/main window if any, else the app root.

        Scoping to the focused window (Codex #2) keeps background windows, palettes,
        and other apps' controls out of the element list.
        """
        app_el = self._ax_app_for_pid(pid)
        return (
            self._attr(app_el, ATTR_FOCUSED_WINDOW)
            or self._attr(app_el, ATTR_MAIN_WINDOW)
            or app_el
        )

    def _collect(self, element, out, depth=0, max_depth=20, budget=None):
        if budget is None:
            budget = [6000]
        if budget[0] <= 0 or depth > max_depth:
            return
        budget[0] -= 1
        role = self._attr(element, ATTR_ROLE)
        # Skip disabled controls (Codex #2): don't offer the model something it can't use.
        if role in INTERACTIVE_ROLES and self._attr(element, ATTR_ENABLED) is not False:
            position = self._decode_point(self._attr(element, ATTR_POSITION))
            size = self._decode_size(self._attr(element, ATTR_SIZE))
            label = (
                self._attr(element, ATTR_TITLE)
                or self._attr(element, ATTR_DESCRIPTION)
                or self._attr(element, ATTR_VALUE)
            )
            label = str(label or "").strip()
            if (
                position
                and size
                and self._onscreen(position, size)
                and (label or role in ALWAYS_KEEP_ROLES)
            ):
                out.append(
                    {
                        "role": role,
                        "label": label[:60],
                        "x": position[0],
                        "y": position[1],
                        "w": size[0],
                        "h": size[1],
                        "ax": element,  # live handle, for fresh re-read at click time (Codex #4)
                    }
                )
        for child in self._attr(element, ATTR_CHILDREN) or []:
            self._collect(child, out, depth + 1, max_depth, budget)

    # --- tool surface (called by the bot's function tools) -------------------

    def read_screen(self) -> dict:
        """Return the interactive elements on screen as {id, role, label}."""
        if self.mode == "mock":
            with self._lock:
                self._elements_by_id = {
                    e["id"]: {**e, "x": 200.0, "y": 120.0 * i, "w": 160.0, "h": 32.0}
                    for i, e in enumerate(MOCK_SCREEN, start=1)
                }
            return {
                "app": f"{self.target_app or 'frontmost app'} (mock)",
                "count": len(MOCK_SCREEN),
                "elements": [
                    {"id": e["id"], "role": e["role"], "label": e["label"]} for e in MOCK_SCREEN
                ],
            }

        pid, app_name = self._resolve_target()
        if pid is None:
            target = self.target_app or "the frontmost app"
            return {
                "app": app_name or self.target_app,
                "elements": [],
                "error": (
                    f"Couldn't find {target}. Ask the user to bring the app they want "
                    f"controlled to the front."
                ),
            }

        raw: list = []
        self._collect(self._read_root(pid), raw)

        new_map = {}
        payload = []
        for index, element in enumerate(raw[:40], start=1):
            element_id = f"e{index}"
            new_map[element_id] = element
            payload.append(
                {
                    "id": element_id,
                    "role": ROLE_SHORT.get(element["role"], element["role"]),
                    "label": element["label"],
                }
            )

        # Publish the new map + active pid atomically so an action on another thread
        # never sees a half-updated state (Codex #7).
        with self._lock:
            self._elements_by_id = new_map
            self._active_pid = pid
            self._active_app_name = app_name

        return {"app": app_name, "count": len(payload), "elements": payload}

    def _match(self, target: str):
        """Resolve a target (id or label) to (element_id, element) or (None, candidates).

        Caller must hold self._lock. Duplicate exact labels are AMBIGUOUS (Codex #3):
        we never silently pick the first one.
        """
        target_normalized = (target or "").strip().lower()
        if target in self._elements_by_id:
            return target, self._elements_by_id[target]

        exact = [
            (element_id, element)
            for element_id, element in self._elements_by_id.items()
            if target_normalized and element["label"].lower() == target_normalized
        ]
        if len(exact) == 1:
            return exact[0]
        if len(exact) > 1:
            return None, exact  # duplicate labels -> make the model disambiguate by id

        substring_matches = [
            (element_id, element)
            for element_id, element in self._elements_by_id.items()
            if target_normalized and target_normalized in element["label"].lower()
        ]
        if len(substring_matches) == 1:
            return substring_matches[0]
        return None, substring_matches

    def _click_point(self, element):
        """Re-read the element's CURRENT center + verify identity (Codex #4).

        Returns (point, source):
          - ((x, y), "fresh")            fresh geometry from the live AX handle
          - ((x, y), "snapshot")         mock / no handle: use the stored snapshot
          - ((x, y), "snapshot-fallback") handle present but geometry unreadable
          - (None, "gone-or-changed")    role changed / element vanished -> refuse
        """
        snapshot = (element["x"] + element["w"] / 2, element["y"] + element["h"] / 2)
        ax = element.get("ax")
        if ax is None:
            return snapshot, "snapshot"
        role = self._attr(ax, ATTR_ROLE)
        if role is None or role != element["role"]:
            return None, "gone-or-changed"
        position = self._decode_point(self._attr(ax, ATTR_POSITION))
        size = self._decode_size(self._attr(ax, ATTR_SIZE))
        if position and size and self._onscreen(position, size):
            return (position[0] + size[0] / 2, position[1] + size[1] / 2), "fresh"
        return snapshot, "snapshot-fallback"

    def _focused_is_text(self, pid) -> bool:
        """True unless we can POSITIVELY read a non-text focused element (Codex #5).

        Lenient on purpose: if focus can't be read we allow typing (don't block on
        uncertainty), but if the focused control is clearly a button/link/etc. we refuse.
        """
        if pid is None:
            return True
        try:
            focused = self._attr(self._ax_app_for_pid(pid), ATTR_FOCUSED_UI)
            if focused is None:
                return True
            role = self._attr(focused, ATTR_ROLE)
            if role is None:
                return True
            return role in TEXT_ROLES
        except Exception:
            return True

    def click(self, target: str, confirmed: bool = False) -> dict:
        with self._lock:
            element_id, resolved = self._match(target)
            active_pid = self._active_pid

        if element_id is None:
            candidates = [{"id": cid, "label": el["label"]} for cid, el in (resolved or [])][:6]
            hint = "Call read_screen and pick an exact id."
            if candidates:
                hint = "Several elements match — pick one by its exact id from the list."
            return {
                "ok": False,
                "reason": f"No single element matches '{target}'. {hint}",
                "candidates": candidates,
            }

        # Consent gate (Codex #1): committing controls require an explicit confirm.
        if _is_destructive_label(resolved["label"]) and not confirmed:
            return {
                "ok": False,
                "needs_confirmation": True,
                "id": element_id,
                "reason": (
                    f"'{resolved['label']}' looks like a committing action (it could submit, pay, "
                    f"send, book, or delete). Tell the user exactly what you're about to do, get a "
                    f"clear yes, then call confirm_and_click with this same target."
                ),
            }

        if self.mode == "mock":
            self.action_log.append({"action": "click", "target": target, "confirmed": confirmed})
            return {
                "ok": True,
                "did": f"clicked {resolved['label']!r}",
                "id": element_id,
                "mock": True,
            }

        point, source = self._click_point(resolved)
        if point is None:
            return {
                "ok": False,
                "stale": True,
                "reason": (
                    f"'{resolved['label']}' isn't where it was — the screen changed since I last "
                    f"looked. Let me read the screen again."
                ),
            }
        if source == "snapshot-fallback":
            logger.warning(
                f"[zolo] AX geometry unreadable for {resolved['label']!r}; using last-known position"
            )

        center_x, center_y = point
        self._activate_target(active_pid)
        self._pyautogui.moveTo(center_x, center_y, duration=0.3)
        self._pyautogui.click()
        self.action_log.append(
            {"action": "click", "id": element_id, "x": center_x, "y": center_y, "confirmed": confirmed}
        )
        return {"ok": True, "did": f"clicked {resolved['label']!r}", "id": element_id}

    def confirm_click(self, target: str) -> dict:
        """Confirmed path for committing actions (Codex #1). The bot exposes this as a
        SEPARATE tool the model may only call after the user says yes out loud."""
        return self.click(target, confirmed=True)

    def type_text(self, text: str) -> dict:
        if self.mode == "mock":
            self.action_log.append({"action": "type", "text": text})
            return {"ok": True, "did": f"typed '{text}'", "mock": True}

        with self._lock:
            active_pid = self._active_pid

        # Don't type blind into whatever has focus after a navigation (Codex #5).
        if not self._focused_is_text(active_pid):
            return {
                "ok": False,
                "no_focus": True,
                "reason": (
                    "No text field is focused, so typing would go nowhere useful. Click the field "
                    "first (or read the screen again) before typing."
                ),
            }

        self._activate_target(active_pid)
        self._pyautogui.write(text, interval=0.02)
        self.action_log.append({"action": "type", "text": text})
        return {"ok": True, "did": f"typed {text!r}"}

    def press_key(self, key: str) -> dict:
        if self.mode == "mock":
            self.action_log.append({"action": "press", "key": key})
            return {"ok": True, "did": f"pressed {key}", "mock": True}
        with self._lock:
            active_pid = self._active_pid
        self._activate_target(active_pid)
        self._pyautogui.press(key.strip().lower())
        self.action_log.append({"action": "press", "key": key})
        return {"ok": True, "did": f"pressed {key}"}

    def scroll(self, direction: str = "down", amount: int = 5) -> dict:
        clicks = amount if direction.strip().lower() == "up" else -amount
        if self.mode == "mock":
            self.action_log.append({"action": "scroll", "direction": direction, "amount": amount})
            return {"ok": True, "did": f"scrolled {direction}", "mock": True}
        with self._lock:
            active_pid = self._active_pid
        self._activate_target(active_pid)
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

        Safety: clicks inside a batch are never auto-confirmed, so a committing control
        (Book/Pay/Submit/...) stops the batch and forces the explicit confirm flow
        (Codex #1). A click whose target moved/vanished, or a type with no focused
        field, also stops the batch instead of acting blindly (Codex #4/#5).
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
