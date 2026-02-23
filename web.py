import os
import streamlit as st
import music
import uuid
import requests
from pytubefix import YouTube
from streamlit.components.v1 import html

# Function to delete temporary audio files
def delete_temp_files(audio_file, output_file, mp3_file):
    if os.path.exists(audio_file):
        os.remove(audio_file)
    if os.path.exists(output_file):
        os.remove(output_file)
    if mp3_file and os.path.exists(mp3_file):
        os.remove(mp3_file)

def isDownlaodable(youtube_link):
    return True

# --- FALLBACK: Cobalt API Mirror (If pytubefix fails) ---
def download_via_cobalt(youtube_link, uu):
    # List of community instances (Updated frequently)
    instances = [
        "https://cobalt.kwiatekmiki.com/api/json",
        "https://api.ox.sys.sy/api/json",
        "https://cobalt.synced.ly/api/json"
    ]
    
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    
    data = {
        "url": youtube_link,
        "isAudioOnly": "true",
        "aFormat": "mp3"
    }
    
    for api_url in instances:
        try:
            print(f"Trying Fallback API: {api_url}")
            response = requests.post(api_url, headers=headers, json=data, timeout=15)
            response_data = response.json()
            
            if 'url' in response_data:
                download_link = response_data['url']
                # Download the file from the proxy
                audio_content = requests.get(download_link).content
                filename = f"uploaded_files/{uu}.mp3"
                
                with open(filename, 'wb') as f:
                    f.write(audio_content)
                return filename, "Unknown Title (via API)"
        except Exception as e:
            print(f"Fallback failed on {api_url}: {e}")
            continue
    return None, None

# --- PRIMARY: Pytubefix ---
@st.cache_data(ttl=3600)
def download_youtube_audio(youtube_link):
    uu = str(uuid.uuid4())
    
    if not os.path.exists("uploaded_files"):
        os.makedirs("uploaded_files")

    # METHOD 1: Try pytubefix (Runs locally, IP matches)
    try:
        print(f"Attempting pytubefix for: {youtube_link}")
        # use_po_token=True is CRITICAL to bypass bots
        yt = YouTube(youtube_link, use_po_token=True) 
        
        # Get audio stream
        ys = yt.streams.get_audio_only()
        song_name = yt.title
        
        # Download
        out_file = f"uploaded_files/{uu}.m4a"
        ys.download(filename=out_file)
        print("Pytubefix success.")
        
        # Convert m4a to compatible mp3 bytes for preview
        mp3_file_base = music.msc_to_mp3_inf(out_file)
        return (out_file, mp3_file_base, song_name)

    except Exception as e:
        print(f"Pytubefix failed ({e}). Switching to Fallback API...")
        
        # METHOD 2: Fallback to Cobalt Mirrors
        filename, song_title_fb = download_via_cobalt(youtube_link, uu)
        
        if filename:
            mp3_file_base = music.msc_to_mp3_inf(filename)
            return (filename, mp3_file_base, song_title_fb)
        else:
            return None

# Main function for the web app
def main():
    st.set_page_config(page_title="Lofi Converter", page_icon=":microphone:", layout="wide")
    st.title(":microphone: Lofi Converter")
    st.info("Tip: Use Headphones for best experience :headphones:")

    youtube_link = st.text_input("Enter YouTube link:", placeholder="https://www.youtube.com/watch?v=dQw4w9WgXcQ", disabled=True)
    
    uploaded_file = st.file_uploader("Upload audio file", type=["mp3", "wav", "m4a", "ogg", "flac"])
    
    # Session state to hold data across re-runs
    if 'downloaded_data' not in st.session_state:
        st.session_state.downloaded_data = None

    if youtube_link:
        # Only download if link changed or not downloaded yet
        if st.session_state.downloaded_data is None or st.session_state.downloaded_data[3] != youtube_link:
            with st.spinner("Downloading audio..."):
                d = download_youtube_audio(youtube_link)
                if d:
                    st.session_state.downloaded_data = d + (youtube_link,)
                else:
                    st.error("Failed to download. YouTube is blocking requests.")

    if uploaded_file:
        if st.session_state.downloaded_data is None or st.session_state.downloaded_data[3] != uploaded_file.name:
            with st.spinner("Processing uploaded audio..."):
                uu = str(uuid.uuid4())
                if not os.path.exists("uploaded_files"):
                    os.makedirs("uploaded_files")
                
                file_ext = os.path.splitext(uploaded_file.name)[1]
                audio_file = f"uploaded_files/{uu}{file_ext}"
                
                with open(audio_file, "wb") as f:
                    f.write(uploaded_file.getbuffer())
                
                mp3_file_base = music.msc_to_mp3_inf(audio_file)
                st.session_state.downloaded_data = (audio_file, mp3_file_base, uploaded_file.name, uploaded_file.name)

    if st.session_state.downloaded_data:
        audio_file, mp3_base_file, song_name, _ = st.session_state.downloaded_data

        st.write(f"**Original:** {song_name}")
        st.audio(mp3_base_file, format="audio/mp3")

        room_size, damping, wet_level, dry_level, delay, slow_factor = get_user_settings()

        if st.button("Convert to Lofi"):
            with st.spinner('Applying Lofi Effects...'):
                output_file = os.path.splitext(audio_file)[0] + "_lofi.wav"
                music.slowedreverb(audio_file, output_file, room_size, damping, wet_level, dry_level, delay, slow_factor)
                
                output_mp3_data = music.msc_to_mp3_inf(output_file)
                st.write("Lofi Converted Audio (Preview)")
                st.audio(output_mp3_data, format="audio/mp3")
                st.download_button("Download MP3", output_mp3_data, song_name+"_lofi.mp3")

    # Footer
    st.markdown("""<h10 style="text-align: center; position: fixed; bottom: 3rem;">Give a ⭐ on <a href="https://github.com/samarthshrivas/LoFi-Converter-GUI"> Github</a> </h10>""", unsafe_allow_html=True)

def get_user_settings():
    advanced_expander = st.expander("Advanced Settings")
    with advanced_expander:
        room_size = st.slider("Reverb Room Size", 0.1, 1.0, 0.75, 0.1)
        damping = st.slider("Reverb Damping", 0.1, 1.0, 0.5, 0.1)
        wet_level = st.slider("Reverb Wet Level", 0.0, 1.0, 0.08, 0.01)
        dry_level = st.slider("Reverb Dry Level", 0.0, 1.0, 0.2, 0.01)
        delay = st.slider("Delay (ms)", 0, 20, 2)
        slow_factor = st.slider("Slow Factor", 0.0, 0.2, 0.08, 0.01)
    return room_size, damping, wet_level, dry_level, delay, slow_factor

if __name__ == "__main__":
    main()