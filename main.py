#!/usr/bin/env python3
"""
Wall‑E controller + face recognition + robust audio input
---------------------------------------------------------
Usage examples
  $ python3 walle.py voice           # auto‑detect mic
  $ python3 walle.py voice 2         # force PortAudio device 2
  $ python3 walle.py --list-devices  # just list available devices
  $ python3 walle.py manual
"""
# ───────────────────────── stdlib ─────────────────────────────────────────
import sys, time, threading, queue, usb.core, usb.util, serial
from pathlib import Path

# ───────────────────────── 3rd‑party ─────────────────────────────────────
import numpy as np, simpleaudio as sa, sounddevice as sd, pvrhino
from gpiozero import AngularServo         # keeps your import even if unused
from tuning import Tuning                 # ReSpeaker 4‑mic array tuning

# face‑recognition worker (unchanged)
from face_recognizer import FaceRecognitionThread

# ───────────────────────── serial & HW init ──────────────────────────────
arduino = serial.Serial('/dev/ttyUSB0', 115200)
time.sleep(2)

dev = usb.core.find(idVendor=0x2886, idProduct=0x0018)      # ReSpeaker 4‑mic

TIME_PER_CYCLE = 4.75
CYCLE_ANGLE    = 360

def send_command(cmd: str):
    arduino.write(cmd.encode())
    print(f"[SERIAL] → {cmd.strip()}")

def rotate_to_voice():
    if not dev:
        print("[WARN] ReSpeaker array not found")
        return
    angle = (Tuning(dev).direction + 270) % CYCLE_ANGLE
    print("New target angle:", angle)
    if angle < 10:
        return
    cw  = angle < 180
    dur = (angle if cw else 360 - angle) / 360 * TIME_PER_CYCLE
    send_command(("a\n" if cw else "d\n"))
    time.sleep(dur)
    send_command("q\n")

# ─────────────────────────── Rhino ───────────────────────────────────────
rhino = pvrhino.create(
    access_key="zd6XrSoEhar44iE2FYMwhM+402s7rR1wkpn5UbyMivpCuG98eq5IFA==",
    context_path=str(Path("models") / "apr_17.rhn"),
)
RHINO_RATE   = rhino.sample_rate        # 16 000 Hz
RHINO_FRAMES = rhino.frame_length       # 512 samples

# ──────────────────────── queues / globals ───────────────────────────────
shared_queue = queue.Queue()     # camera <‑‑> voice logic
intent_queue = queue.Queue()     # audio callback → worker
face_recognizer = FaceRecognitionThread(shared_queue=shared_queue)

ARDUINO = "arduino"
RASPBERRY = "raspberry"
flag = False                     # waiting for a new face‑name?

names = {"Almas","Rambo","Sula","Amir","Askar","Aru","Dias","Ali",
         "Islam","Mansur","Marlen","Amina","Diana","Alina","Sofia",
         "Bota","Yussuf","Timur","Arman","Abay","Lucas","Leo","Eve",
         "John","Mike","Adnan","Michael","Alex"}

sounds = {
    "wave_your_hands": "./assets/hello_walle.wav",
    "i_love_you":      "./assets/eva.wav",
    "raise_both_arms": "./assets/tada.wav",
    "who_am_I":        "./assets/loading_recognition.wav",
}

def play_audio(path: str):
    try:
        sa.WaveObject.from_wave_file(path).play().wait_done()
    except Exception as e:
        print(f"[AUDIO] {e}")

def walle():
    global flag
    rotate_to_voice()
    for i in range(50, 100, 10):
        # send_command(f"B{i}\n")
        face_recognizer.trigger()
        res = shared_queue.get()["result"]
        if res == "not_recognized":
            flag = True
        shared_queue.task_done()
        break

animations = {
    "raise_both_arms": (ARDUINO,  "R100", "L100", "R0", "L0"),
    "how_are_you":     (ARDUINO,  "R100", "L100"),
    "i_love_you":      (ARDUINO,  "i", "k"),
    "wave_your_hands": (ARDUINO,  "R100", "R70", "R100", "R70", "R0"),
    "who_am_I":        (RASPBERRY,),
    "walle":           (RASPBERRY, walle),
}

# ───────────────────────── intent logic ──────────────────────────────────
def handle_intent(intent: str):
    global flag
    print("Recognised:", intent)

    if flag:                       # expect the speaker’s name
        if intent in names:
            shared_queue.put({"name": intent})
            flag = False
        else:
            print("Say your name")
        return

    if intent in names:            # ignore random names otherwise
        return

    if intent not in animations:   # single‑char direct command
        send_command(intent + '\n')
        time.sleep(2)
        send_command('q\n')
        return

    if intent in sounds:           # play WAV asynchronously
        threading.Thread(target=play_audio,
                         args=(sounds[intent],),
                         daemon=True).start()

    seq = animations[intent]
    if seq[0] == ARDUINO:
        for s in seq[1:]:
            send_command(s + '\n'); time.sleep(1)
    else:
        seq[1]()                   # call its Raspberry handler

# ───────────────────────── audio callback ────────────────────────────────
def audio_callback(indata, frames, time_info, status):
    if status:                          # XRUN, overflow, etc.
        print("[AUDIO]", status, file=sys.stderr)
    pcm = np.squeeze(indata)            # already int16
    if rhino.process(pcm):
        inf = rhino.get_inference()
        if inf.is_understood:
            intent_queue.put(inf.intent)
        else:
            print("Command not understood.")

# ───────────────────── intent‑worker thread ⬇ ⬇ ⬇ ────────────────────────
def intent_worker():
    while True:
        item = intent_queue.get()
        if item is None:
            break
        try:
            handle_intent(item)
        except Exception as exc:
            print(f"[WORKER] {exc}")

# ───────────────────────── helper: list devices ──────────────────────────
def list_devices():
    print("\nPortAudio devices:")
    for idx, dev in enumerate(sd.query_devices()):
        io = []
        if dev['max_input_channels']  > 0: io.append("in")
        if dev['max_output_channels'] > 0: io.append("out")
        print(f"  {idx:2d}: {dev['name']}  ({', '.join(io)})")
    print()

# ─────────────────────────────── main() ──────────────────────────────────
def main():
    if len(sys.argv) == 2 and sys.argv[1] == "--list-devices":
        list_devices(); return

    if len(sys.argv) < 2 or sys.argv[1] not in {"voice", "manual"}:
        print("Usage: python3 walle.py [voice|manual] [device#]")
        print("       python3 walle.py --list-devices")
        return

    stream_device = None
    if len(sys.argv) >= 3:          # user supplied a device number
        stream_device = int(sys.argv[2])

    if sys.argv[1] == "voice":
        if stream_device is None:
            # choose the first device that has input channels
            for idx, d in enumerate(sd.query_devices()):
                if d['max_input_channels'] > 0:
                    stream_device = idx
                    break
        print(f"Using PortAudio input device #{stream_device}")
        list_devices()

    try:
        face_recognizer.start()
        threading.Thread(target=intent_worker, daemon=True).start()

        if sys.argv[1] == "voice":
            with sd.InputStream(device=stream_device,
                                channels=1,
                                samplerate=RHINO_RATE,
                                blocksize=RHINO_FRAMES,
                                dtype='int16',
                                callback=audio_callback):
                print("Listening…  Ctrl‑C to stop")
                while True:
                    time.sleep(1)

        else:  # manual mode
            while True:
                try:
                    cmd = input("> ").strip()
                except EOFError:
                    break
                if cmd == "quit": break
                rotate_to_voice() if cmd == "r" else send_command(cmd[0] + '\n')

    except KeyboardInterrupt:
        pass
    finally:
        intent_queue.put(None)          # stop worker
        face_recognizer.stop(); face_recognizer.join()
        rhino.delete(); arduino.close()
        print("Bye!")

# ─────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    main()
