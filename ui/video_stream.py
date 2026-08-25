import streamlit as st
import cv2
import time
from core.video.streamer import VideoStream

def render_camera_feed():
    st.header("👁️ Live Security Feed")
    # Создаем плейсхолдер для видео
    frame_placeholder = st.empty()
    
    # Инициализируем поток (если не был создан)
    if 'vs' not in st.session_state:
        st.session_state.vs = VideoStream(0).start() # 0 или rtsp адрес

    while True:
        frame = st.session_state.vs.read()
        if frame is not None:
            # Превращаем BGR (OpenCV) в RGB (Streamlit)
            frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            frame_placeholder.image(frame, channels="RGB", use_column_width=True)
            
            # Здесь же можем считать количество людей
            # faces = detector.detect(frame)
            # st.sidebar.metric("Людей в здании", len(faces))
            
        time.sleep(0.03) # ~30 FPS