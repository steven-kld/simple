"""The actuation layer: take a coordinate, click it the way a hand would.

pyautogui drives X11's XTEST extension, so these are real OS-level input
events - isTrusted in the browser, indistinguishable from a physical mouse.
"""

import math
import random
import time

import pyautogui

# Slam the cursor into the top-left corner to abort a run in progress.
pyautogui.FAILSAFE = True
# We do our own timing; pyautogui's own 0.1 s pause would double it.
pyautogui.PAUSE = 0

FailSafe = pyautogui.FailSafeException

MAX_ARC = 80


def screen_size():
    return pyautogui.size()


def move(x, y):
    """Arc the cursor to (x, y) along a quadratic Bezier with jittered timing."""
    sx, sy = pyautogui.position()
    distance = math.hypot(x - sx, y - sy)

    # Both the arc and the step count scale with travel, or a 40 px nudge would
    # swing out as wide as a cross-screen move.
    arc = min(MAX_ARC, distance * 0.15)
    steps = max(4, min(25, round(distance / 40) + 4))

    cx = (sx + x) / 2 + random.uniform(-arc, arc)
    cy = (sy + y) / 2 + random.uniform(-arc, arc)

    for i in range(steps + 1):
        t = i / steps
        bx = (1 - t) ** 2 * sx + 2 * (1 - t) * t * cx + t**2 * x
        by = (1 - t) ** 2 * sy + 2 * (1 - t) * t * cy + t**2 * y
        pyautogui.moveTo(int(round(bx)), int(round(by)))
        time.sleep(random.uniform(0.005, 0.015))


def click(x, y):
    move(x, y)
    time.sleep(random.uniform(0.01, 0.05))  # a hand settles before pressing
    pyautogui.click()
