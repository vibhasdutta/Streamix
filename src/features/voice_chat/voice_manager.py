import sounddevice as sd
import numpy as np
import threading
import queue
import zlib
import asyncio
from shared.utils.logger import setup_logger

logger = setup_logger("voice_manager", "voice.log")

class VoiceManager:
    def __init__(self, loop, sample_rate=16000, chunk_duration=0.1, input_device=None, output_device=None):
        self.loop = loop
        self.sample_rate = sample_rate
        self.chunk_size = int(sample_rate * chunk_duration)
        
        self.input_device_index = input_device
        self.output_device_index = output_device
        
        self.mic_muted = True 
        self.speaker_muted = False
        self.voice_active = False # For UI detection
        self.current_volume = 0.0 # RMS value (0.0 to 1.0 approx)
        
        self.playback_queue = queue.Queue(maxsize=20)
        self.on_voice_packet = None # Callback to send data: (binary_data)
        
        self.input_stream = None
        self.output_stream = None

    def start(self):
        # Microphone input
        try:
            self.input_stream = sd.InputStream(
                samplerate=self.sample_rate,
                channels=1,
                dtype='int16',
                callback=self._mic_callback,
                blocksize=self.chunk_size,
                device=self.input_device_index
            )
            self.input_stream.start()
            logger.info(f"Recording started on device {self.input_device_index or 'default'} at {self.sample_rate}Hz")
        except Exception as e:
            logger.error(f"VoiceManager Error (Input): {e}")

        # Speaker output
        try:
            self.output_stream = sd.OutputStream(
                samplerate=self.sample_rate,
                channels=1,
                dtype='int16',
                callback=self._speaker_callback,
                blocksize=self.chunk_size,
                device=self.output_device_index
            )
            self.output_stream.start()
            logger.info(f"Playback started on device {self.output_device_index or 'default'} at {self.sample_rate}Hz")
        except Exception as e:
            logger.error(f"VoiceManager Error (Output): {e}")

    def stop(self):
        if self.input_stream:
            self.input_stream.stop()
            self.input_stream.close()
        if self.output_stream:
            self.output_stream.stop()
            self.output_stream.close()

    def handle_incoming_audio(self, compressed_data):
        if not self.speaker_muted:
            try:
                data = zlib.decompress(compressed_data)
                audio_array = np.frombuffer(data, dtype='int16')
                if self.playback_queue.full():
                    try: self.playback_queue.get_nowait()
                    except: pass
                self.playback_queue.put(audio_array)
            except: pass

    def _mic_callback(self, indata, frames, time, status):
        # Update current volume (RMS)
        try:
            # Cast to float64 to prevent overflow during squaring
            data_float = indata.astype(np.float64)
            rms = np.sqrt(np.mean(data_float**2))
            
            # Sensitivity adjustment: most mics average 50-500 RMS for speech
            # We'll use 2000 as "max" for the bar graph to make it responsive
            self.current_volume = min(rms / 2000.0, 1.0)
        except:
            self.current_volume = 0.0

        if not self.mic_muted and self.on_voice_packet:
            try:
                # Basic noise gate: only send if volume > threshold
                # volume = np.linalg.norm(indata) / np.sqrt(len(indata))
                # if volume < 50: return 

                compressed = zlib.compress(indata.tobytes(), level=1)
                # Bridge to asyncio main loop
                self.loop.call_soon_threadsafe(self.on_voice_packet, compressed)
            except: pass

    def _speaker_callback(self, outdata, frames, time, status):
        try:
            if not self.speaker_muted and not self.playback_queue.empty():
                data = self.playback_queue.get_nowait()
                if len(data) == len(outdata):
                    outdata[:] = data.reshape(-1, 1)
                else:
                    outdata.fill(0)
            else:
                outdata.fill(0)
        except:
            outdata.fill(0)

    @staticmethod
    def get_devices(kind='input'):
        """Return a filtered list of audio devices."""
        devices = sd.query_devices()
        filtered = []
        for i, d in enumerate(devices):
            if kind == 'input' and d['max_input_channels'] > 0:
                filtered.append({'index': i, 'name': d['name'], 'hostapi': d['hostapi']})
            elif kind == 'output' and d['max_output_channels'] > 0:
                filtered.append({'index': i, 'name': d['name'], 'hostapi': d['hostapi']})
        return filtered

    def play_test_sound(self):
        """Play a short test tone on the selected output device."""
        duration = 0.5
        f = 440
        t = np.linspace(0, duration, int(self.sample_rate * duration), False)
        note = np.sin(f * t * 2 * np.pi)
        # Convert to 16-bit PCM
        audio = (note * 32767).astype(np.int16)
        
        try:
            sd.play(audio, self.sample_rate, device=self.output_device_index)
            sd.wait()
        except Exception as e:
            print(f"[red]Test Sound Error: {e}[/red]")
