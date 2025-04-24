import sys, time, threading, usb.core, usb.util, serial, numpy as np, simpleaudio as sa
from gpiozero import AngularServo         # keeps your import even if unused
from tuning import Tuning                 # ReSpeaker 4‑mic array tuning
import sounddevice as sd
import pvrhino
from facerecog.recognize_face_old import FaceRecognitionThread
import queue

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

shared_queue = queue.Queue()
face_recognizer = FaceRecognitionThread(shared_queue=shared_queue)

ARDUINO = "arduino"
RASPBERRY = "raspberry"

def walle():
    global flag
    rotate_to_voice()
    for i in range(50, 100, 10):
        send_command("B" + i + "\n")
        face_recognizer.trigger()
        response = shared_queue.get()
        if response["result"] == "not_detected":
            continue

        if response["result"] == "not_recognized":
            flag = True

        break


animations = {
    "raise_both_arms": (ARDUINO,  "R100", "L100", "R0", "L0"),
    "how_are_you":     (ARDUINO,  "R100", "L100"),
    "i_love_you":      (ARDUINO,  "i", "k"),
    "wave_your_hands": (ARDUINO,  "R100", "R70", "R100", "R70", "R0"),
    "who_am_I":        (RASPBERRY,),
    "walle":           (RASPBERRY, walle),
}

flag = False

names = [
    "Almas", "Rambo", "Sula", "Amir", "Askar", "Aru", "Dias", "Ali", 
    "Islam", "Mansur", "Marlen", "Amina", "Diana", "Alina", "Sofia", 
    "Bota", "Yussuf", "Timur", "Arman", "Abay", "Lucas", "Leo", "Eve", 
    "John", "Mike", "Adnan", "Michael", "Alex"
]

# ───────────────────────────── Sound helper ─────────────────────────────
def play_audio(path: str):
    print(path)
    """Fire‑and‑forget WAV playback (runs inside its own thread)."""
    try:
        sa.WaveObject.from_wave_file(path).play().wait_done()
    except Exception as e:
        print(f"[AUDIO] {e}")

sounds = {                 # map intent → wav file
    "wave_your_hands": "./assets/hello_walle.wav",
    "i_love_you":      "./assets/eva.wav",
    "raise_both_arms": "./assets/tada.wav",
    "who_am_I":        "./assets/loading_recognition.wav",
}

def callback(indata, frames, time_info, status):
    global flag
    if status:
        print(status)
    pcm = np.squeeze((indata * 32767).astype(np.int16))
    if rhino.process(pcm):
        inference = rhino.get_inference()
        if inference.is_understood:
            intent = inference.intent
            print("Recognised:", intent)
            if flag:
                if intent in names:                  # just the name – ignore
                    shared_queue.put({"name": intent})
                    flag = False
                else:
                    print("Say your name")
            else:
                if intent in names:
                    return
                # 1. direct single‑char command TODO FIX
                if intent not in animations:
                    send_command(intent + '\n')
                    time.sleep(2)
                    send_command('q\n')
                    return

                # 2. predefined animation
                if intent in sounds:  
                    threading.Thread(target=play_audio,
                                    args=(sounds[intent],),
                                    daemon=True).start()

                seq = animations[intent]
                if seq[0] == ARDUINO:
                    for s in seq[1:]:
                        send_command(s + '\n')
                        time.sleep(1)
                else:
                    handler = seq[1]
                    handler()

                
        else:
            print("Command not understood.")

#  ───────────────────────────── Main entry point ─────────────────────────
def main():
    if len(sys.argv) < 2 or sys.argv[1] not in {"voice", "manual"}:
        print("Usage: python3 walle.py [voice|manual]")
        return

    try:
        if sys.argv[1] == "voice":
            face_recognizer.start()        
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
        face_recognizer.stop()
        face_recognizer.join()
        rhino.delete()
        arduino.close()
        print("Bye!")

if __name__ == "__main__":
    main()

