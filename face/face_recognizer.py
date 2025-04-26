#!/usr/bin/env python3
"""
Face‑recognition demo (thread‑ready version)
-------------------------------------------
 * Everything works exactly as before (Edge‑TPU, LCD, rotation, FSM…)
 * Import FaceRecognitionThread, create it with a shared Queue, and .start().
 * Call .stop() when you want the thread to exit cleanly.
"""
# ────────────────────────── stdlib ────────────────────────────────────────────
import re, time, threading, queue
from collections import deque
from pathlib import Path

# ───────────────────────── 3rd‑party ─────────────────────────────────────────
from tflite_runtime.interpreter import Interpreter, load_delegate
from RPLCD.i2c import CharLCD
import numpy as np
from PIL import Image
import cv2
import pyttsx3

# ─────────────────────────── tunables ─────────────────────────────────────────
CAMERA_WIDTH   = 640
CAMERA_HEIGHT  = 480
THRESH_DET     = 0.70      # accept face detections ≥ 70 %
THRESH_EMB     = 0.50      # max squared‑distance to accept a match
LCD_MIN_INTVAL = 0.30      # seconds between two LCD updates
NOFACE_SLEEP   = 0.10      # pause when no face is found
MODEL_DIR      = Path(__file__).with_suffix('').parent / "models"

# ─────────────────────────── globals ───────────────────────────────────────────
people_embeddings: dict[str, list[np.ndarray]] = {}
lcd     = CharLCD('PCF8574', 0x27)
engine  = pyttsx3.init()

_last_lcd_lines   = ["", ""]
_last_lcd_time    = 0.0

# ───────────────────────── LCD helper ─────────────────────────────────────────
def lcd_show(line1: str = "", line2: str = "") -> None:
    global _last_lcd_lines, _last_lcd_time
    now = time.time()
    if (line1, line2) == tuple(_last_lcd_lines):
        return
    elapsed = now - _last_lcd_time
    if elapsed < LCD_MIN_INTVAL:
        time.sleep(LCD_MIN_INTVAL - elapsed)
    print(f"[LCD] {line1} | {line2}")
    lcd.clear()
    lcd.cursor_pos = (0, 0); lcd.write_string(line1[:16])
    lcd.cursor_pos = (1, 0); lcd.write_string(line2[:16])
    _last_lcd_lines[:] = [line1, line2]
    _last_lcd_time = time.time()

# ─────────────────────────── main thread class ───────────────────────────────
class FaceRecognitionThread(threading.Thread):
    """
    Run the full face‑recognition pipeline in a background thread.

        >>> q = queue.Queue()
        >>> fr = FaceRecognitionThread(shared_queue=q, use_edge_tpu=True)
        >>> fr.start()        # non‑blocking
        ...
        >>> fr.stop()         # shuts camera & windows
        >>> fr.join()
    """
    def __init__(self, shared_queue: "queue.Queue[dict]",
                 use_edge_tpu: bool = True):
        super().__init__(daemon=True)
        self.shared_queue = shared_queue
        self.use_edge_tpu = use_edge_tpu
        self._stop_event  = threading.Event()

    # ‑‑‑ public API ‑‑‑
    def stop(self) -> None:
        self._stop_event.set()

    def trigger(self) -> None:
        """Resume detection after the thread has paused."""
        print("Triggering...1")
        if hasattr(self, "_fsm") and self._fsm:
            print("Triggering...2")
            self._fsm.trigger()

    # ‑‑‑ internals ‑‑‑
    def run(self) -> None:
        # —── load labels —──
        labels = load_labels(MODEL_DIR / 'coco_labels.txt')

        # —── face detector —──
        intr = Interpreter(
            model_path=str(MODEL_DIR / (
                'ssd_mobilenet_v2_face_quant_postprocess_edgetpu.tflite'
                if self.use_edge_tpu else
                'ssd_mobilenet_v2_face_quant_postprocess.tflite')),
            experimental_delegates=([load_delegate('libedgetpu.so.1.0',
                                                   {'device':'usb:1'})]
                                     if self.use_edge_tpu else None)
        )
        intr.allocate_tensors()
        _, in_h, in_w, _ = intr.get_input_details()[0]['shape']

        # —── face‑embedding model —──
        intr_emb = Interpreter(
            model_path=str(MODEL_DIR / (
                'Mobilenet1_triplet1589223569_triplet_quant_edgetpu.tflite'
                if self.use_edge_tpu else
                'Mobilenet1_triplet1589223569_triplet_quant.tflite')),
            experimental_delegates=([load_delegate('libedgetpu.so.1.0',
                                                   {'device':'usb:1'})]
                                     if self.use_edge_tpu else None)
        )
        intr_emb.allocate_tensors()

        # —── camera —──
        cap = cv2.VideoCapture(0)
        cap.set(cv2.CAP_PROP_FRAME_WIDTH , CAMERA_WIDTH)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, CAMERA_HEIGHT)
        cap.set(cv2.CAP_PROP_FPS        , 30)
        if not cap.isOpened():
            print("[ERROR] Cannot open camera")
            return
        cv2.namedWindow("Frame", cv2.WINDOW_AUTOSIZE)

        try:
            self._fsm = FaceRecognizerFSM(        #  ← keep a handle
                cap, labels, in_h, in_w,
                intr, intr_emb,
                shared_queue=self.shared_queue,
                stop_event=self._stop_event)
            self._fsm.enqueue("start_detection")
            self._fsm.run()
        finally:
            cap.release()
            cv2.destroyAllWindows()

# ──────────────────────── finite‑state machine ────────────────────────────────
class FaceRecognizerFSM:
    def __init__(self, cap, labels, in_h, in_w, intr, intr_emb,
                 shared_queue: "queue.Queue[dict]",
                 stop_event: threading.Event):
        self.cap, self.labels = cap, labels
        self.in_h, self.in_w = in_h, in_w
        self.interpreter, self.interpreter_emb = intr, intr_emb
        self.state = "idle"
        self.queue = deque()
        self.shared_queue = shared_queue
        self.stop_event = stop_event
        self.paused = True   # wait for external trigger after each success
        self.transitions = {
            ("idle","start_detection")          : ("detecting", self.detect_face),
            ("detecting","face_detected")       : ("recognizing", self.recognize_face),
            ("detecting","face_not_detected")   : ("detecting" , self.detect_face),
            ("detecting","paused")              : ("detecting" , self.detect_face),
            ("recognizing","face_recognized")   : ("detecting" , self.detect_face),
            ("recognizing","face_not_recognized"): ("remembering", self.remember_face),
            ("checking","face_still_present")   : ("checking"  , self.check_presence),
            ("checking","face_lost")            : ("detecting" , self.detect_face),
            ("checking","face_not_similar")     : ("recognizing", self.recognize_face),
            ("remembering","successful")        : ("detecting" , self.detect_face),
            ("remembering","failure")           : ("detecting" , self.detect_face),
        }

    # ——— queue helpers ———
    def enqueue(self, action, *args):      self.queue.append((action, args))
    def trigger(self) -> None:
        """
        External resume‑switch.
        Call this when you want the FSM to leave its ‘paused’ state
        and start looking for faces again.
        """
        print("Triggering...3")
        self.paused = False

    # ——— main loop ———
    def run(self):
        while not self.stop_event.is_set():
            if not self.queue:             # nothing to do → small nap
                time.sleep(0.01)
                continue
            action, args = self.queue.popleft()
            key = (self.state, action)
            if key not in self.transitions:
                print(f"[FSM] invalid transition {key}")
                continue
            next_state, handler = self.transitions[key]
            print(f"[FSM] {self.state} --({action})--> {next_state}")
            self.state = next_state
            handler(*args)

    # ——— state handlers ———
    def detect_face(self):
        if self.stop_event.is_set(): return
        # lcd_show("Detecting your", "face…")
        emb = detect_face_on_camera(self.cap, self.labels,
                                    self.in_h, self.in_w,
                                    self.interpreter, self.interpreter_emb)
        if self.paused:
            time.sleep(NOFACE_SLEEP)
            self.enqueue("paused")
        elif emb is None:
            time.sleep(NOFACE_SLEEP)
            self.enqueue("face_not_detected")
        else:
            self.enqueue("face_detected", emb)

    def recognize_face(self, emb):
        if self.stop_event.is_set(): return
        lcd_show("Recognising…", "")
        person, distance = recognize_person_from_embedding(emb, return_dist=True)
        if person == "Unknown":
            self.shared_queue.put({"result": "not_recognized"})
            self.enqueue("face_not_recognized", emb)
        else:
            print(f"[INFO] recognised {person} ({distance:.3f})")
            engine.say(f"Hello {person}"); engine.runAndWait()
            self.shared_queue.put({"result": "recognized", "person": person})
            self.enqueue("face_recognized")
            self.paused = True                     # wait for next trigger

    def remember_face(self, emb):
        if self.stop_event.is_set(): return
        lcd_show("Who are you?", "")
        response = self.shared_queue.get()  # wait max 30 s
        print(response)
        name = response.get("name")
        if not name:
            self.shared_queue.task_done()
            self.enqueue("failure"); self.paused = True; return
        store_embedding(name, emb)
        print(f"[INFO] stored embedding for {name}")
        engine.say(f"Hello {name}"); engine.runAndWait()
        self.enqueue("successful")
        self.paused = True
        self.shared_queue.task_done()

    def check_presence(self, last_person, last_emb):
        if self.stop_event.is_set(): return
        lcd_show(last_person, "")
        curr_emb = detect_face_on_camera(self.cap, self.labels,
                                         self.in_h, self.in_w,
                                         self.interpreter, self.interpreter_emb)
        if curr_emb is None:
            self.enqueue("face_lost")
        elif calculate_similarity(last_emb, curr_emb) > THRESH_EMB:
            self.enqueue("face_not_similar", curr_emb)
        else:
            self.enqueue("face_still_present", last_person, curr_emb)

# ───────────────────── helper functions ───────────────────────────────────────
def calculate_similarity(e1, e2) -> float: return np.sum((e1 - e2) ** 2)

def store_embedding(name, emb): people_embeddings.setdefault(name, []).append(emb)

def recognize_person_from_embedding(emb, return_dist=False):
    best = float("inf"); who = "Unknown"
    for person, lst in people_embeddings.items():
        for e2 in lst:
            d = calculate_similarity(emb, e2)
            if d < best: best, who = d, person
    if return_dist:
        return (who if best <= THRESH_EMB else "Unknown"), best
    return who if best <= THRESH_EMB else "Unknown"

# ─────────────────── camera / tflite helpers ─────────────────────────────────
def detect_face_on_camera(cap, labels, in_h, in_w, intr, intr_emb):
    ok, frame = cap.read()
    if not ok: return None
    # frame = apply_rotation(frame)   # enable if your camera is tilted
    img_large = Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
    img = img_large.resize((in_w, in_h), Image.LANCZOS)
    results = detect_objects(intr, img, 0.5)
    annotate_objects_cv2(frame, results, labels)
    cv2.imshow("Frame", frame)
    if cv2.waitKey(1) & 0xFF == ord('q'):
        return None
    h, w = frame.shape[:2]
    y1,x1,y2,x2,score = get_best_box_param(results, w, h)
    if score >= THRESH_DET:
        face = np.array(img_large)[y1:y2, x1:x2]
        if face.size == 0: return None
        face = cv2.resize(face, (96,96)).astype("float32").reshape(1,96,96,3)/255.
        return img_to_emb(intr_emb, face)
    return None

# ──────────────────── tiny utility functions ─────────────────────────────────
def load_labels(path):
    with open(path, "r", encoding="utf-8") as f:
        lines = f.readlines()
    labels = {}
    for idx, line in enumerate(lines):
        pair = re.split(r'[:\s]+', line.strip(), maxsplit=1)
        if len(pair) == 2 and pair[0].isdigit():
            labels[int(pair[0])] = pair[1]
        else:
            labels[idx] = pair[0]
    return labels

def set_input_tensor(interpreter, image):
    idx = interpreter.get_input_details()[0]['index']
    interpreter.tensor(idx)()[0][:] = image

def get_output_tensor(interpreter, i):
    detail = interpreter.get_output_details()[i]
    return np.squeeze(interpreter.get_tensor(detail['index']))

def set_input_tensor_emb(interpreter, arr):
    det = interpreter.get_input_details()[0]
    scale, zero = det['quantization']
    interpreter.tensor(det['index'])()[0][:] = np.uint8(arr / scale + zero)

def img_to_emb(interpreter, arr):
    set_input_tensor_emb(interpreter, arr)
    interpreter.invoke()
    out = interpreter.get_output_details()[0]
    emb_q = interpreter.get_tensor(out['index'])
    scale, zero = out['quantization']
    return scale * (emb_q - zero)

def detect_objects(interpreter, image, thr=0.5):
    set_input_tensor(interpreter, image)
    interpreter.invoke()
    boxes   = get_output_tensor(interpreter, 0)
    classes = get_output_tensor(interpreter, 1)
    scores  = get_output_tensor(interpreter, 2)
    cnt     = int(get_output_tensor(interpreter, 3))
    res = []
    for i in range(cnt):
        if scores[i] >= thr:
            res.append({
                "bounding_box": boxes[i],
                "class_id"    : int(classes[i]),
                "score"       : scores[i]
            })
    return res

def get_best_box_param(results, w, h):
    best = 0; xmin=ymin=0; xmax, ymax = w, h
    for obj in results:
        if obj['score'] > best:
            best = obj['score']
            ymin_r,xmin_r,ymax_r,xmax_r = obj['bounding_box']
            xmin = int(xmin_r * w); xmax = int(xmax_r * w)
            ymin = int(ymin_r * h); ymax = int(ymax_r * h)
    return ymin,xmin,ymax,xmax,best

def annotate_objects_cv2(frame, results, labels):
    h, w = frame.shape[:2]
    for obj in results:
        ymin,xmin,ymax,xmax = obj['bounding_box']
        xmin,xmax = int(xmin*w), int(xmax*w)
        ymin,ymax = int(ymin*h), int(ymax*h)
        cv2.rectangle(frame, (xmin,ymin), (xmax,ymax), (0,255,0), 2)
        text = f"{labels.get(obj['class_id'],'Unknown')}: {obj['score']:.2f}"
        (tw,th), bl = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, 0.5, 1)
        cv2.rectangle(frame, (xmin, ymin-th-10),
                      (xmin+tw, ymin), (255,255,255), cv2.FILLED)
        cv2.putText(frame, text, (xmin, ymin-5),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0,0,0), 1)

# ──────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    """
    Minimal standalone test:
        $ python3 face_recognizer.py
        (Press Ctrl‑C or close the window to quit)
    """
    shared_q = queue.Queue()
    fr = FaceRecognitionThread(shared_queue=shared_q, use_edge_tpu=True)
    fr.start()
    try:
        while True:
            try:
                fr.trigger()
                print("[MAIN] message:", shared_q.get(timeout=1))
            except queue.Empty:
                pass
    except KeyboardInterrupt:
        print("\n[MAIN] Stopping…")
        fr.stop()
        fr.join()
