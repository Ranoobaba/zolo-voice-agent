#
# Zolo — custom cursor sprite overlay.
#
# SPDX-License-Identifier: BSD 2-Clause License
#

"""A custom sprite that rides on top of the real macOS cursor.

Zolo drives the REAL system cursor via pyautogui, so we can't restyle the cursor
itself per-app. Instead this is a standalone, transparent, click-through,
always-on-top window that follows the cursor at ~120 Hz and draws a sprite on it.
Run it ALONGSIDE the bot (separate process) so it never blocks the voice loop.

Run it:
    # default built-in arrow sprite
    uv run python cursor_sprite.py
    # your own image (PNG with transparency looks best)
    ZOLO_CURSOR_SPRITE=~/zolo_pointer.png uv run python cursor_sprite.py
    # or pass the path / size as args
    uv run python cursor_sprite.py ~/zolo_pointer.png 56

Env / args:
    ZOLO_CURSOR_SPRITE   path to a PNG/JPG sprite (else a built-in arrow is drawn)
    ZOLO_CURSOR_SIZE     sprite size in points (default 44)
    ZOLO_CURSOR_HOTX/Y   hotspot offset from the sprite's TOP-LEFT, in points,
                         i.e. which point of the sprite sits on the cursor
                         (default 0,0 = the top-left tip, like a normal pointer)

Quit with Ctrl-C in the terminal.
"""

import os
import sys

import objc
from AppKit import (
    NSApplication,
    NSApplicationActivationPolicyAccessory,
    NSBackingStoreBuffered,
    NSBezierPath,
    NSColor,
    NSEvent,
    NSImage,
    NSImageScaleProportionallyUpOrDown,
    NSImageView,
    NSScreenSaverWindowLevel,
    NSWindow,
    NSWindowCollectionBehaviorCanJoinAllSpaces,
    NSWindowCollectionBehaviorFullScreenAuxiliary,
    NSWindowCollectionBehaviorStationary,
    NSWindowStyleMaskBorderless,
)
from Foundation import NSMakeRect, NSMakeSize, NSObject, NSPoint, NSTimer


def _arg(index: int, default: str) -> str:
    return sys.argv[index] if len(sys.argv) > index else default


SPRITE_PATH = os.getenv("ZOLO_CURSOR_SPRITE", _arg(1, "")).strip()
SIZE = float(os.getenv("ZOLO_CURSOR_SIZE", _arg(2, "44")))
HOTX = float(os.getenv("ZOLO_CURSOR_HOTX", "0"))
HOTY = float(os.getenv("ZOLO_CURSOR_HOTY", "0"))


def make_default_sprite(size: float) -> NSImage:
    """Draw a recognizable Zolo-purple arrow pointer (top-left tip)."""
    image = NSImage.alloc().initWithSize_(NSMakeSize(size, size))
    image.lockFocus()

    # Classic arrow outline in a unit square with a TOP-LEFT origin (y down).
    unit_points = [
        (0.00, 0.00),  # tip
        (0.00, 0.72),
        (0.20, 0.55),
        (0.33, 0.86),
        (0.46, 0.80),
        (0.33, 0.51),
        (0.54, 0.51),
    ]
    path = NSBezierPath.bezierPath()
    for i, (ux, uy) in enumerate(unit_points):
        # NSImage uses a bottom-left origin, so flip y.
        point = NSPoint(ux * size, (1.0 - uy) * size)
        if i == 0:
            path.moveToPoint_(point)
        else:
            path.lineToPoint_(point)
    path.closePath()

    NSColor.colorWithCalibratedRed_green_blue_alpha_(0.486, 0.361, 0.988, 1.0).setFill()  # #7C5CFC
    path.fill()
    NSColor.whiteColor().setStroke()
    path.setLineWidth_(max(1.5, size * 0.045))
    path.stroke()

    image.unlockFocus()
    return image


def load_sprite() -> NSImage:
    if SPRITE_PATH:
        expanded = os.path.expanduser(SPRITE_PATH)
        image = NSImage.alloc().initWithContentsOfFile_(expanded)
        if image is None:
            print(f"[zolo-cursor] couldn't load {expanded!r}; using built-in arrow")
        else:
            image.setSize_(NSMakeSize(SIZE, SIZE))
            return image
    return make_default_sprite(SIZE)


class CursorFollower(NSObject):
    """Repositions the overlay window onto the cursor every tick."""

    def initWithWindow_size_(self, window, size):
        self = objc.super(CursorFollower, self).init()
        if self is None:
            return None
        self.window = window
        self.size = size
        return self

    def tick_(self, _timer):
        # mouseLocation is in global screen coords with a bottom-left origin,
        # which matches setFrameOrigin_. Place the sprite's top-left (its tip)
        # on the cursor, shifted by the configured hotspot.
        loc = NSEvent.mouseLocation()
        origin = NSPoint(loc.x - HOTX, loc.y - self.size + HOTY)
        self.window.setFrameOrigin_(origin)


def main():
    app = NSApplication.sharedApplication()
    # Accessory = no Dock icon, never steals focus.
    app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)

    sprite = load_sprite()

    window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
        NSMakeRect(0, 0, SIZE, SIZE),
        NSWindowStyleMaskBorderless,
        NSBackingStoreBuffered,
        False,
    )
    window.setOpaque_(False)
    window.setBackgroundColor_(NSColor.clearColor())
    window.setHasShadow_(False)
    window.setIgnoresMouseEvents_(True)  # click-through
    window.setLevel_(NSScreenSaverWindowLevel)  # above normal windows
    window.setCollectionBehavior_(
        NSWindowCollectionBehaviorCanJoinAllSpaces
        | NSWindowCollectionBehaviorStationary
        | NSWindowCollectionBehaviorFullScreenAuxiliary
    )

    view = NSImageView.alloc().initWithFrame_(NSMakeRect(0, 0, SIZE, SIZE))
    view.setImage_(sprite)
    view.setImageScaling_(NSImageScaleProportionallyUpOrDown)
    window.setContentView_(view)
    window.orderFrontRegardless()

    follower = CursorFollower.alloc().initWithWindow_size_(window, SIZE)
    # ~120 Hz for smooth tracking; cheap (one setFrameOrigin per tick).
    NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
        1.0 / 120.0, follower, "tick:", None, True
    )

    print(
        f"[zolo-cursor] overlay running (size={SIZE:.0f}, "
        f"sprite={'custom' if SPRITE_PATH else 'built-in arrow'}). Ctrl-C to quit."
    )
    app.run()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
