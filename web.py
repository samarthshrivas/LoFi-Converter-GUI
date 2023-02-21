import os
import streamlit as st
import music
import yt_dlp
import uuid
uu = str(uuid.uuid4())



st.set_page_config(page_title="Lofi Converter", page_icon=":microphone:", layout="wide")
st.title("Lofi Converter")

# Define function to delete temporary audio files
def delete_temp_files():
    os.remove(audio_file)
    os.remove(output_file)
    os.remove(mp3_file)


st.write("Enter the YouTube link of the song to convert:")
youtube_link = st.text_input("YouTube link")
# Create sliders for delay, reverb, and slow factor
if youtube_link:
    with yt_dlp.YoutubeDL({'format': 'bestaudio/best', 'outtmpl': 'uploaded_files/'+uu +'.%(ext)s'}) as ydl:
        # Download audio from YouTube link and save as a WAV file
        info_dict = ydl.extract_info(youtube_link, download=True)
        audio_file = ydl.prepare_filename(info_dict)


    advanced_expander = st.expander("Advanced Settings")
    with advanced_expander:
        st.write("Adjust the parameters for the slowedreverb function:")
        delay = st.slider("Delay (ms)", min_value=0, max_value=20, value=2)
        room_size = st.slider("Reverb Room Size", min_value=0.1, max_value=1.0, value=0.75, step=0.1)
        damping = st.slider("Reverb Damping", min_value=0.1, max_value=1.0, value=0.5, step=0.1)
        wet_level = st.slider("Reverb Wet Level", min_value=0.0, max_value=1.0, value=0.08, step=0.01)
        dry_level = st.slider("Reverb Dry Level", min_value=0.0, max_value=1.0, value=0.2, step=0.01)
        slow_factor = st.slider("Slow Factor", min_value=0.0, max_value=1.0, value=0.08, step=0.01)



    # Process audio with slowedreverb function
    output_file = os.path.splitext(audio_file)[0] + "_lofi.wav"
    st.write("Original Audio")
    st.audio(audio_file, format="audio/wav")
    music.slowedreverb(audio_file, output_file, room_size, damping, wet_level, dry_level, delay, slow_factor)
    st.write("Lofi Converted Audio")
    st.audio(output_file, format="audio/wav")
    
    if st.button("Download MP3"):
        mp3_file = f"{os.path.splitext(output_file)[0]}.mp3"
        music.wav_to_mp3(output_file, mp3_file)
        st.download_button("Download", mp3_file, "lofi-audio.mp3")

