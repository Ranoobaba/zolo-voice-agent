"""
spike.py - Zolo "can it even click" gate.

Proves two things BEFORE we build the agent, so we don't discover a blocker at 5 PM:
  1. SIGHT - can we read on-screen UI elements (labels + coordinates) from the
             macOS Accessibility tree, for Safari?
  2. HANDS - do we have permission to drive the real cursor, and does an AX
             coordinate actually map to the right spot on this (Retina) Mac?

Run it:
    cd ~/Zolo/yc-voice-agents-hackathon/server
    uv run python spike.py        # (or: source .venv/bin/activate; python spike.py)

Before running: open Safari with a normal page (e.g. https://www.google.com)
and make sure a Safari window is visible on screen.
"""

import time

import pyautogui
from AppKit import NSWorkspace
from ApplicationServices import (
    AXIsProcessTrusted,
    AXIsProcessTrustedWithOptions,
    AXUIElementCopyAttributeValue,
    AXUIElementCreateApplication,
    AXValueGetValue,
)

# kAXValueType* integer constants (stable across pyobjc versions).
KAX_VALUE_CGPOINT = 1
KAX_VALUE_CGSIZE = 2

# Attribute names as plain strings (avoids version-specific constant symbols).
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


def get_attribute(element, attribute_name):
    error_code, value = AXUIElementCopyAttributeValue(element, attribute_name, None)
    return value if error_code == 0 else None


def decode_point(ax_value):
    if ax_value is None:
        return None
    succeeded, point = AXValueGetValue(ax_value, KAX_VALUE_CGPOINT, None)
    return (point.x, point.y) if succeeded else None


def decode_size(ax_value):
    if ax_value is None:
        return None
    succeeded, size = AXValueGetValue(ax_value, KAX_VALUE_CGSIZE, None)
    return (size.width, size.height) if succeeded else None


def label_for(element):
    return (
        get_attribute(element, ATTR_TITLE)
        or get_attribute(element, ATTR_DESCRIPTION)
        or get_attribute(element, ATTR_VALUE)
    )


def collect_interactive_elements(element, found, depth=0, max_depth=20, budget=[6000]):
    if budget[0] <= 0 or depth > max_depth:
        return
    budget[0] -= 1
    role = get_attribute(element, ATTR_ROLE)
    if role in INTERACTIVE_ROLES:
        position = decode_point(get_attribute(element, ATTR_POSITION))
        size = decode_size(get_attribute(element, ATTR_SIZE))
        if position and size:
            found.append(
                {
                    "role": role,
                    "label": str(label_for(element) or "")[:50],
                    "x": position[0],
                    "y": position[1],
                    "w": size[0],
                    "h": size[1],
                }
            )
    for child in get_attribute(element, ATTR_CHILDREN) or []:
        collect_interactive_elements(child, found, depth + 1, max_depth, budget)


def find_safari_pid():
    for app in NSWorkspace.sharedWorkspace().runningApplications():
        if (app.localizedName() or "") == "Safari":
            return app.processIdentifier()
    return None


def main():
    print("== Zolo spike ==\n")

    # 1) HANDS - permission gate (covers both AX reads and real cursor control).
    if not AXIsProcessTrusted():
        print("Accessibility permission: MISSING")
        print("  Fix: System Settings > Privacy & Security > Accessibility >")
        print("       add your terminal (Terminal or iTerm), toggle it ON, then re-run.")
        return
    print("Accessibility permission: GRANTED")

    # 2) SIGHT - read Safari's accessibility tree.
    safari_pid = find_safari_pid()
    if safari_pid is None:
        print("\nSafari is not running. Open Safari with a page, then re-run.")
        return

    print("\nReading Safari's accessibility tree...")
    ax_app = AXUIElementCreateApplication(safari_pid)
    found = []
    collect_interactive_elements(ax_app, found)
    print(f"Interactive elements found: {len(found)}\n")
    for index, element in enumerate(found[:20]):
        print(
            f"  [{index:2}] {element['role']:13} {element['label']!r:42} "
            f"@ ({element['x']:.0f},{element['y']:.0f}) {element['w']:.0f}x{element['h']:.0f}"
        )

    if not found:
        print("\nSIGHT FAILED: no labeled web elements.")
        print("  Make sure a real Safari page is open and visible, then re-run.")
        return

    # 3) HANDS - move the real cursor to one element's center (coordinate-mapping test).
    target = next(
        (e for e in found if e["role"] in ("AXTextField", "AXSearchField")),
        found[0],
    )
    center_x = target["x"] + target["w"] / 2
    center_y = target["y"] + target["h"] / 2
    print(
        f"\nIn 4 seconds the real cursor flies to the center of "
        f"{target['role']} {target['label']!r} at ({center_x:.0f},{center_y:.0f})."
    )
    print("Watch your pointer. (No click - uncomment the click line below to test that too.)")
    time.sleep(4)
    pyautogui.moveTo(center_x, center_y, duration=0.6)
    # pyautogui.click()   # <- uncomment to ALSO test a real click on that element

    print("\nIf the pointer landed exactly on that element: SIGHT + HANDS + coordinate")
    print("mapping all work, and approach A' is green. If it landed at roughly HALF the")
    print("distance, tell Claude - there's a Retina 2x scale factor to apply. Done.")


if __name__ == "__main__":
    main()
