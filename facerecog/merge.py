import multiprocessing
import os

def run_face_recognition():
    os.system("python3 recognize_face_old.py")

def run_keyword_detection():
    os.system("python3 run_model.py")

if __name__ == "__main__":
    # Create processes
    face_recognition_process = multiprocessing.Process(target=run_face_recognition)
    keyword_detection_process = multiprocessing.Process(target=run_keyword_detection)
    
    # Start processes
    face_recognition_process.start()
    keyword_detection_process.start()
    
    # Join processes to wait for their completion
    face_recognition_process.join()
    keyword_detection_process.join()

