"""Report what a button sends, to diagnose a dead hotkey.

Watches BOTH the keyboard and the mouse for 30 seconds. Deliberately logs
only non-typing keys (function keys, modifiers, media/special keys) plus
mouse buttons, so normal typing is never recorded.
"""

import string
import time

import keyboard
import mouse

IGNORED = set(string.ascii_lowercase) | set(string.digits) | set(
    " `-=[]\\;',./"
) | {"space", "enter", "backspace", "tab"}

seen = []


def note(label: str) -> None:
    if label not in seen:
        seen.append(label)
        print(f"  detected: {label}")


def on_key(event) -> None:
    name = (event.name or "?").lower()
    if name in IGNORED or event.event_type != "down":
        return  # never record typed content
    note(f"KEY  {name}  (scan code {event.scan_code})")


def on_mouse(event) -> None:
    if isinstance(event, mouse.ButtonEvent) and event.event_type == "down":
        note(f"MOUSE  button '{event.button}'")


print("Press your function button several times now...")
print("(listening 30 seconds on keyboard AND mouse; typing is ignored)")
print("Move on to normal clicking only after you've tried the button.\n")
keyboard.hook(on_key)
mouse.hook(on_mouse)
time.sleep(30)
keyboard.unhook_all()
mouse.unhook_all()

print()
if seen:
    print("What Windows received:")
    for label in seen:
        print(f"  - {label}")
    print("\nIf your button is listed, it CAN be bound to dictation.")
else:
    print("NOTHING received from keyboard or mouse. The button is handled")
    print("inside the device firmware and cannot be used as a hotkey.")
print("\nClosing in 30 seconds...")
time.sleep(30)
