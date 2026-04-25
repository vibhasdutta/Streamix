import sounddevice as sd
import numpy as np
import threading
import queue
import zlib
import asyncio
from shared.utils.logger import setup_logger

logger = setup_logger("voice_manager", "voice.log")

class VoiceManager:
    def __init__(self, loop, sample_rate=24000, chunk_duration=0.04, input_device=None, output_device=None):
        """Voice Manager for Watch Party voice chat.

        Args:
            sample_rate: Audio sample rate in Hz. 24kHz gives clear voice
                         while keeping bandwidth reasonable (~48KB/s raw).
            chunk_duration: Seconds per audio chunk. 40ms = good balance of
                           latency vs. overhead (25 packets/sec).
        """
        self.loop = loop
        self.sample_rate = sample_rate
        self.chunk_size = int(sample_rate * chunk_duration)
        
        self.input_device_index = input_device
        self.output_device_index = output_device
        
        self.mic_muted = True 
        self.speaker_muted = False
        self.voice_active = False # For UI detection
        self.current_volume = 0.0 # RMS value (0.0 to 1.0 approx)
        
        # Smaller queue = lower latency. At 40ms chunks, 10 items = 400ms max buffer
        self.playback_queue = queue.Queue(maxsize=10)
        self.on_voice_packet = None # Callback to send data: (binary_data)
        
        self.input_stream = None
        self.output_stream = None
        
        # Noise gate: RMS below this threshold is treated as silence
        self._noise_gate_threshold = 80
        # Simple smoothing for noise gate to avoid choppy cuts
        self._gate_open = False
        self._gate_hold_frames = 0
        self._gate_hold_max = 5  # Hold gate open for N chunks after speech stops

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
            logger.info(f"Recording started on device {self.input_device_index or 'default'} at {self.sample_rate}Hz, chunk={self.chunk_size}")
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
            data_float = indata.astype(np.float64)
            rms = np.sqrt(np.mean(data_float**2))
            
            # Sensitivity adjustment: scale for responsive meter
            self.current_volume = min(rms / 2000.0, 1.0)
        except:
            self.current_volume = 0.0

        if not self.mic_muted and self.on_voice_packet:
            try:
                # Noise gate with hold time to avoid choppy cuts
                data_float = indata.astype(np.float64)
                rms = np.sqrt(np.mean(data_float**2))
                
                if rms >= self._noise_gate_threshold:
                    self._gate_open = True
                    self._gate_hold_frames = self._gate_hold_max
                elif self._gate_hold_frames > 0:
                    self._gate_hold_frames -= 1
                else:
                    self._gate_open = False
                
                if not self._gate_open:
                    return  # Below noise gate, don't send
                
                # Compress with zlib level 6 (better ratio, still fast enough for real-time)
                compressed = zlib.compress(indata.tobytes(), level=6)
                self.loop.call_soon_threadsafe(self.on_voice_packet, compressed)
            except: pass

    def _speaker_callback(self, outdata, frames, time, status):
        try:
            if not self.speaker_muted and not self.playback_queue.empty():
                data = self.playback_queue.get_nowait()
                if len(data) == len(outdata):
                    outdata[:] = data.reshape(-1, 1)
                elif len(data) > len(outdata):
                    outdata[:] = data[:len(outdata)].reshape(-1, 1)
                else:
                    outdata[:len(data)] = data.reshape(-1, 1)
                    outdata[len(data):] = 0
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
        audio = (note * 32767).astype(np.int16)
        
        try:
            sd.play(audio, self.sample_rate, device=self.output_device_index)
            sd.wait()
        except Exception as e:
            print(f"[red]Test Sound Error: {e}[/red]")

    def loopback_test(self, duration=8.0, on_volume=None):
        """Run a mic loopback test: captures mic and plays back through speakers.

        Args:
            duration: How long to run the loopback (seconds).
            on_volume: Optional callback(rms_float) called every chunk with the
                       current mic RMS level (0.0 - 1.0).
        """
        loopback_queue = queue.Queue(maxsize=10)
        running = threading.Event()
        running.set()

        def _input_cb(indata, frames, time_info, status):
            if not running.is_set():
                return
            # Calculate volume for the UI meter
            try:
                data_float = indata.astype(np.float64)
                rms = np.sqrt(np.mean(data_float**2))
                vol = min(rms / 2000.0, 1.0)
                if on_volume:
                    on_volume(vol)
            except:
                pass
            # Push audio to playback
            if loopback_queue.full():
                try: loopback_queue.get_nowait()
                except: pass
            loopback_queue.put(indata.copy())

        def _output_cb(outdata, frames, time_info, status):
            try:
                if not loopback_queue.empty():
                    data = loopback_queue.get_nowait()
                    if len(data) == len(outdata):
                        outdata[:] = data
                    elif len(data) > len(outdata):
                        outdata[:] = data[:len(outdata)]
                    else:
                        outdata[:len(data)] = data
                        outdata[len(data):] = 0
                else:
                    outdata.fill(0)
            except:
                outdata.fill(0)

        in_stream = None
        out_stream = None
        try:
            in_stream = sd.InputStream(
                samplerate=self.sample_rate,
                channels=1,
                dtype='int16',
                callback=_input_cb,
                blocksize=self.chunk_size,
                device=self.input_device_index,
            )
            out_stream = sd.OutputStream(
                samplerate=self.sample_rate,
                channels=1,
                dtype='int16',
                callback=_output_cb,
                blocksize=self.chunk_size,
                device=self.output_device_index,
            )
            in_stream.start()
            out_stream.start()

            import time as _time
            _time.sleep(duration)

        finally:
            running.clear()
            if in_stream:
                in_stream.stop()
                in_stream.close()
            if out_stream:
                out_stream.stop()
                out_stream.close()

