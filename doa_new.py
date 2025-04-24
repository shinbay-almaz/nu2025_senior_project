#!/usr/bin/env python3
"""
Wall‑E voice/manual controller with servo animations + sound effects
-------------------------------------------------------------------
Dependencies:
  pip install pvporcupine pvrhino sounddevice simpleaudio gpiozero pyusb pyserial numpy
"""

import sys, time, threading, usb.core, usb.util, serial, numpy as np, simpleaudio as sa
from gpiozero import AngularServo         # keeps your import even if unused
from tuning import Tuning                 # ReSpeaker 4‑mic array tuning
import sounddevice as sd
import pvrhino

# ───────────────────────────── Serial & array initialisation ────────────
arduino = serial.Serial('/dev/ttyUSB0', 115200)
time.sleep(2)

dev = usb.core.find(idVendor=0x2886, idProduct=0x0018)  # ReSpeaker 4‑mic USB PID

TIME_PER_CYCLE = 4.75      # seconds to do a full 360° turn
CYCLE_ANGLE    = 360

def send_command(cmd: str):
    """Write a one‑line command to the Arduino and log it."""
    arduino.write(cmd.encode())
    print(f"[SERIAL] → {cmd.strip()}")

def rotate_to_voice():
    """Turn Wall‑E toward the strongest sound source (ReSpeaker array)."""
    if not dev:
        print("[WARN] ReSpeaker array not found")
        return
    angle = (Tuning(dev).direction + 90) % CYCLE_ANGLE
    print("New target angle:", angle)
    if angle < 10:                               # already facing us
        return
    cw  = angle < 180                            # decide direction
    dur = (angle if cw else 360 - angle) / 360 * TIME_PER_CYCLE
    send_command(("a\n" if cw else "d\n"))
    time.sleep(dur)
    send_command("q\n")

# ───────────────────────────── Rhino intent engine ──────────────────────
rhino = pvrhino.create(
    access_key="zd6XrSoEhar44iE2FYMwhM+402s7rR1wkpn5UbyMivpCuG98eq5IFA==",
    context_path="model/apr_17.rhn",
)
sample_rate  = rhino.sample_rate
frame_length = rhino.frame_length

# ───────────────────────────── Sound helper ─────────────────────────────
def play_audio(path: str):
    print(path)
    """Fire‑and‑forget WAV playback (runs inside its own thread)."""
    try:
        sa.WaveObject.from_wave_file(path).play().wait_done()
    except Exception as e:
        print(f"[AUDIO] {e}")

SOUNDS = {                 # map intent → wav file
    "wave_your_hands": "./assets/hello_walle.wav",
    "i_love_you":      "./assets/eva.wav",
}

# ───────────────────────────── Animation definitions ────────────────────
ARDUINO   = "arduino"
RASPBERRY = "raspberry"

animations = {
    "raise_both_arms": (ARDUINO,  "t"),
    "how_are_you":     (ARDUINO,  "t"),
    "i_love_you":      (ARDUINO,  "i", "k"),
    "wave_your_hands": (ARDUINO,  "R100", "R70", "R100", "R70", "R0"),
    "who_am_I":        (RASPBERRY,),
    "walle":           (RASPBERRY,),
    "remember_me":     (RASPBERRY,),
}

NAMES = {"Sultan", "Rambo", "Alice", "Almas"}

# ───────────────────────────── Animation helper ────────────────────

def who_am_i():
    """
    1. tries to recognize person
    2. if doesn't know shows sad face
    3. if recognizes, plays audio with a greating + the person's name
    """
    return
    
def walle():
    """
    turns towards the person and raises his head
    """
    rotate_to_voice()
    # raise head as much as he can
    return
    
def remember_me():
    """
    ask what is your name? 
    listen for the name
    save to the db
    """
    return

# ───────────────────────────── Audio callback ───────────────────────────
def callback(indata, frames, time_info, status):
    if status:
        print(status)
    pcm = np.squeeze((indata * 32767).astype(np.int16))
    if rhino.process(pcm):
        inference = rhino.get_inference()
        if inference.is_understood:
            intent = inference.intent
            print("Recognised:", intent)

            if intent in NAMES:                  # just the name – ignore
                return

            # 1. direct single‑char command
            if intent not in animations:
                send_command(intent + '\n')
                time.sleep(2)
                send_command('q\n')
                return

            # 2. predefined animation
            if intent in SOUNDS:  
                # print("voice")               # kick off audio thread
                # sa.WaveObject.from_wave_file('./assets/tada.wav').play().wait_done()
                # play_audio1(SOUNDS[intent])
                threading.Thread(target=play_audio,
                                args=(SOUNDS[intent],),
                                daemon=True).start()

            seq = animations[intent]
            if seq[0] == ARDUINO:
                for s in seq[1:]:
                    send_command(s + '\n')
                    time.sleep(1)

                
        else:
            print("Command not understood.")

# ───────────────────────────── Manual helper ────────────────────────────
def read_command():
    cmd = input("Write a command ('r'=rotate, 'quit'=exit): ")
    if cmd == "quit":
        return False
    rotate_to_voice() if cmd == "r" else send_command(cmd + '\n')
    return True

# ───────────────────────────── Main entry point ─────────────────────────
def main():
    if len(sys.argv) < 2 or sys.argv[1] not in {"voice", "manual"}:
        print("Usage: python3 walle.py [voice|manual]")
        return

    try:
        if sys.argv[1] == "voice":
            with sd.InputStream(channels=1, samplerate=sample_rate,
                                blocksize=frame_length, dtype="float32",
                                callback=callback):
                print("Listening…  Ctrl‑C to stop")
                while True:
                    sd.sleep(1000)
        else:                                   # manual
            while read_command():
                pass
    except KeyboardInterrupt:
        pass
    finally:
        rhino.delete()
        arduino.close()
        print("Bye!")

if __name__ == "__main__":
    main()
