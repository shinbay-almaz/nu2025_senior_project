import simpleaudio as sa
import threading
import time

# path = '~/Desktop/micro_testing/usb_4_mic_array/assets'
path = './assets/tada.wav'

def play_audio(path: str):
    """Fire‑and‑forget WAV playback (runs inside its own thread)."""
    try:
        sa.WaveObject.from_wave_file(path).play().wait_done()
    except Exception as e:
        print(f"[AUDIO] {e}")


threading.Thread(target=play_audio,
				 args=(path,),
				 daemon=True).start()

time.sleep(5)
