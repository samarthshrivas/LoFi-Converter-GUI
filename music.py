import subprocess as sp
import soundfile as sf
from pedalboard import Pedalboard, Reverb
from math import trunc
import numpy as np


def slowedreverb(audio, output, room_size = 0.75, damping = 0.5, wet_level = 0.08, dry_level = 0.2, delay = 2, slowfactor = 0.08):

    if '.wav' not in audio:
        # print('Audio needs to be .wav! Converting...')
        sp.call(f'ffmpeg -hide_banner -loglevel error -y -i "{audio}" tmp.wav', shell = True)
        audio = 'tmp.wav'
        
    audio, sample_rate = sf.read(audio)
    sample_rate -= trunc(sample_rate*slowfactor)

    # Add reverb
    board = Pedalboard([Reverb(
        room_size=room_size,
        damping=damping,
        wet_level=wet_level,
        dry_level=dry_level
        )])


    # Add surround sound effects
    effected = board(audio, sample_rate)
    channel1 = effected[:, 0]
    channel2 = effected[:, 1]
    shift_len = delay*1000
    shifted_channel1 = np.concatenate((np.zeros(shift_len), channel1[:-shift_len]))
    combined_signal = np.hstack((shifted_channel1.reshape(-1, 1), channel2.reshape(-1, 1)))


    #write outfile
    sf.write(output, combined_signal, sample_rate)
    # print(f"Converted.")


def wav_to_mp3(wav_file, mp3_file):
    command = f'ffmpeg -hide_banner -loglevel error -y -i "{wav_file}" "{mp3_file}"'

    sp.call(command, shell=True)

def msc_to_mp3_inf(wav_file):
    ffmpeg_command = ["ffmpeg", "-i", wav_file, "-f", "mp3", "pipe:1"]
    pipe = sp.run(ffmpeg_command,
                        stdout=sp.PIPE,
                        stderr=sp.PIPE,
                        bufsize=10**8)
    return pipe.stdout

# if "__main__" == __name__:
    # slowedreverb('kali.wav', 'test1.wav')



